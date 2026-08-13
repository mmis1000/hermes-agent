from pathlib import PurePosixPath
import subprocess
import threading
import time
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from agent.delegation_policy import ExecutionProfile
from tools import terminal_tool
from tools.environments import docker as docker_env
from tools.delegation_scope import (
    ResolvedInvocationScope,
    attempt_scope_registry,
    execution_profile_hash,
)


def _scope():
    profile = ExecutionProfile(
        "protected", "docker", "repo/protected@sha256:deadbeef", "/workspace",
        frozenset({"terminal"}),
    )
    return ResolvedInvocationScope(
        profile.name, execution_profile_hash(profile), profile,
        PurePosixPath("/workspace"), (), (),
    )


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
