from pathlib import PurePosixPath
import subprocess
import threading
import time
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from agent.delegation_policy import (
    AccessMode,
    BackingObjectRef,
    ExecutionProfile,
    VisibleObjectGrant,
)
from tools import terminal_tool
from tools.environments import docker as docker_env
from tools.delegation_scope import (
    ResolvedInvocationScope,
    attempt_scope_registry,
    execution_profile_hash,
)


def _scope(runtime_identity=None, visible_objects=()):
    profile = ExecutionProfile(
        "protected", "docker", "repo/protected@sha256:deadbeef", "/workspace",
        frozenset({"terminal"}), runtime_identity=runtime_identity,
    )
    return ResolvedInvocationScope(
        profile.name, execution_profile_hash(profile), profile,
        PurePosixPath("/workspace"), (), visible_objects,
    )


def _directory_grant(source):
    return VisibleObjectGrant(
        "/workspace/shared",
        AccessMode.RW,
        BackingObjectRef("shared", "host_path", str(source), "rev-1"),
        "directory",
    )


def test_runtime_identity_prepares_before_environment_and_cleans_after_it(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    grant = _directory_grant(source)
    order = []

    def prepare(attempt_id, grants, identity, *, register_cleanup):
        assert attempt_id == "attempt-idmap"
        assert grants == (grant,)
        assert identity == (10001, 10002)
        register_cleanup(lambda: order.append("idmap"))
        return {"shared": "/private/mapped"}

    monkeypatch.setattr("tools.idmapped_mounts.prepare_idmapped_reveals", prepare)
    authority = attempt_scope_registry.reserve(
        _scope((10001, 10002), (grant,)),
        "logical-idmap",
        attempt_id="attempt-idmap",
    )

    attempt_scope_registry.prepare_idmapped_reveals(authority.attempt_id)
    attempt_scope_registry.add_resource(
        authority.attempt_id, "task-environment", lambda: order.append("environment")
    )

    assert authority.prepared_mount_sources == {"shared": "/private/mapped"}
    assert attempt_scope_registry.cleanup(authority.attempt_id) == ()
    assert order == ["environment", "idmap"]


def test_partial_setup_failure_retains_cleanup_for_attempt_retry(tmp_path, monkeypatch):
    source = tmp_path / "source-partial"
    source.mkdir()
    grant = _directory_grant(source)
    cleanup = MagicMock()

    def prepare(_attempt_id, _grants, _identity, *, register_cleanup):
        register_cleanup(cleanup)
        raise RuntimeError("second mount failed")

    monkeypatch.setattr("tools.idmapped_mounts.prepare_idmapped_reveals", prepare)
    authority = attempt_scope_registry.reserve(
        _scope((10001, 10001), (grant,)),
        "logical-partial",
        attempt_id="attempt-partial",
    )

    with pytest.raises(RuntimeError, match="second mount failed"):
        attempt_scope_registry.prepare_idmapped_reveals(authority.attempt_id)

    assert list(authority.resources.cleanup_callbacks) == ["idmapped-reveals"]
    assert attempt_scope_registry.cleanup(authority.attempt_id) == ()
    cleanup.assert_called_once_with()


def test_environment_failure_blocks_unmount_until_container_cleanup_retries(
    tmp_path, monkeypatch
):
    source = tmp_path / "source-environment-failure"
    source.mkdir()
    grant = _directory_grant(source)
    unmount = MagicMock()
    environment_calls = 0

    def prepare(_attempt_id, _grants, _identity, *, register_cleanup):
        register_cleanup(unmount)
        return {"shared": "/private/mapped"}

    def cleanup_environment():
        nonlocal environment_calls
        environment_calls += 1
        if environment_calls == 1:
            raise RuntimeError("docker rm failed")

    monkeypatch.setattr("tools.idmapped_mounts.prepare_idmapped_reveals", prepare)
    authority = attempt_scope_registry.reserve(
        _scope((10001, 10001), (grant,)),
        "logical-environment-failure",
        attempt_id="attempt-environment-failure",
    )
    attempt_scope_registry.prepare_idmapped_reveals(authority.attempt_id)
    attempt_scope_registry.add_resource(
        authority.attempt_id, "task-environment", cleanup_environment
    )

    first_errors = attempt_scope_registry.cleanup(authority.attempt_id)
    assert first_errors
    unmount.assert_not_called()

    assert attempt_scope_registry.cleanup(authority.attempt_id) == ()
    unmount.assert_called_once_with()


def test_omitted_runtime_identity_does_not_prepare_mounts(monkeypatch):
    prepare = MagicMock()
    monkeypatch.setattr("tools.idmapped_mounts.prepare_idmapped_reveals", prepare)
    authority = attempt_scope_registry.reserve(
        _scope(), "logical-ordinary", attempt_id="attempt-ordinary"
    )

    attempt_scope_registry.prepare_idmapped_reveals(authority.attempt_id)

    prepare.assert_not_called()
    assert authority.prepared_mount_sources == {}
    assert attempt_scope_registry.cleanup(authority.attempt_id) == ()


def test_cleanup_revokes_before_reverse_teardown_and_is_idempotent(
    monkeypatch, caplog
):
    authority = attempt_scope_registry.reserve(
        _scope(), "logical-clean", attempt_id="attempt-clean"
    )
    order = []

    def cleanup(name):
        def run():
            current = attempt_scope_registry.get(authority.attempt_id)
            assert current is not None and current.state == "revoked"
            order.append(name)
        return run

    attempt_scope_registry.add_resource(authority.attempt_id, "first", cleanup("first"))
    attempt_scope_registry.add_resource(authority.attempt_id, "second", cleanup("second"))

    with caplog.at_level("INFO", logger="tools.delegation_scope"):
        assert attempt_scope_registry.cleanup(authority.attempt_id) == ()
    assert attempt_scope_registry.cleanup(authority.attempt_id) == ()
    assert order == ["second", "first"]
    current = attempt_scope_registry.get(authority.attempt_id)
    assert current is not None and current.state == "cleaned"
    assert '"cleanup":"succeeded"' in caplog.text
    assert '"environment_owner":"attempt-clean"' in caplog.text

    monkeypatch.setattr(terminal_tool, "_create_environment", MagicMock())
    with pytest.raises(ValueError, match="cleaned"):
        terminal_tool.acquire_task_environment(authority.attempt_id)


def test_protected_force_remove_waits_for_docker_removal(monkeypatch):
    env = docker_env.DockerEnvironment.__new__(docker_env.DockerEnvironment)
    env._container_id = "protected-container"
    env._persist_across_processes = True
    env._persistent = False
    env._docker_exe = "docker"
    env._workspace_dir = None
    env._home_dir = None
    removal_finished = threading.Event()

    def run(cmd, **_kwargs):
        if cmd[1] == "rm":
            time.sleep(0.05)
            removal_finished.set()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", run)

    try:
        env.cleanup(force_remove=True)
        completed_before_return = removal_finished.is_set()
    finally:
        env.wait_for_cleanup(timeout=1.0)

    assert completed_before_return is True


def test_failed_protected_cleanup_is_reported_and_not_marked_cleaned(caplog):
    authority = attempt_scope_registry.reserve(
        _scope(), "logical-cleanup-failure", attempt_id="attempt-cleanup-failure"
    )

    def fail_cleanup():
        raise RuntimeError("container still exists")

    attempt_scope_registry.add_resource(
        authority.attempt_id, "task-environment", fail_cleanup
    )

    with caplog.at_level("INFO", logger="tools.delegation_scope"):
        errors = attempt_scope_registry.cleanup(authority.attempt_id)

    current = attempt_scope_registry.get(authority.attempt_id)
    assert len(errors) == 1
    assert current is not None and current.state == "revoked"
    assert '"cleanup":"failed"' in caplog.text
    assert '"environment_owner":"attempt-cleanup-failure"' in caplog.text


def test_protected_cleanup_propagates_removal_failure_and_retains_environment():
    task_id = "attempt-force-remove-failure"

    class FailingEnvironment:
        def cleanup(self, *, force_remove=False):
            assert force_remove is True
            raise RuntimeError("docker rm failed")

    env = FailingEnvironment()
    terminal_tool._active_environments[task_id] = env
    try:
        with pytest.raises(RuntimeError, match="docker rm failed"):
            terminal_tool.cleanup_vm(task_id, force_remove=True)
        assert terminal_tool._active_environments.get(task_id) is env
    finally:
        terminal_tool._active_environments.pop(task_id, None)


def test_child_result_reports_protected_teardown_failure_and_remains_retryable():
    from tools.delegate_tool import _run_single_child

    authority = attempt_scope_registry.reserve(
        _scope(), "logical-child-cleanup", attempt_id="attempt-child-cleanup"
    )
    calls = 0

    def cleanup():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("container still exists")

    attempt_scope_registry.add_resource(
        authority.attempt_id, "task-environment", cleanup
    )
    child = SimpleNamespace(
        _delegation_attempt_id=authority.attempt_id,
        _delegate_saved_tool_names=[],
        run_conversation=lambda **_kwargs: {
            "final_response": "work completed",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        },
        close=lambda: None,
    )

    result = _run_single_child(0, "finish work", child=child, parent_agent=None)

    assert result["status"] == "error"
    assert result["exit_reason"] == "cleanup_error"
    assert "container still exists" in result["error"]
    current = attempt_scope_registry.get(authority.attempt_id)
    assert current is not None and current.state == "revoked"
    assert attempt_scope_registry.cleanup(authority.attempt_id) == ()
    assert calls == 2
    assert attempt_scope_registry.get(authority.attempt_id).state == "cleaned"
