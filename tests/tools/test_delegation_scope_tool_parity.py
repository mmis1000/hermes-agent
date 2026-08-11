from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.delegation_policy import ExecutionProfile
from tools import terminal_tool
from tools.delegation_scope import (
    ResolvedInvocationScope,
    attempt_scope_registry,
    execution_profile_hash,
)
from tools import process_registry as process_module


def _scope(network="inherit"):
    profile = ExecutionProfile(
        "protected", "docker", "repo/protected@sha256:deadbeef", "/workspace",
        frozenset({"terminal"}),
        network=network,
    )
    return ResolvedInvocationScope(
        profile.name, execution_profile_hash(profile), profile,
        PurePosixPath("/workspace"), (), (),
    )


def test_revoked_attempt_cannot_collapse_after_overrides_are_cleared(monkeypatch):
    authority = attempt_scope_registry.reserve(
        _scope(), "logical-late", attempt_id="attempt-late"
    )
    attempt_scope_registry.cleanup(authority.attempt_id)
    terminal_tool.clear_task_env_overrides(authority.attempt_id)
    create = MagicMock()
    monkeypatch.setattr(terminal_tool, "_create_environment", create)

    try:
        with pytest.raises(ValueError, match="revoked|cleaned"):
            terminal_tool.acquire_task_environment(authority.attempt_id)
    finally:
        attempt_scope_registry.cleanup(authority.attempt_id)

    create.assert_not_called()


def test_process_handler_denies_cross_attempt_session_even_when_id_is_known(monkeypatch):
    first = attempt_scope_registry.reserve(_scope(), "logical-a", attempt_id="attempt-owner-a")
    second = attempt_scope_registry.reserve(_scope(), "logical-b", attempt_id="attempt-owner-b")
    poll = MagicMock(return_value={"status": "running"})
    monkeypatch.setattr(
        process_module.process_registry,
        "get",
        lambda _session_id: SimpleNamespace(task_id=first.attempt_id),
    )
    monkeypatch.setattr(process_module.process_registry, "poll", poll)

    try:
        result = process_module._handle_process(
            {"action": "poll", "session_id": "proc_disclosed"},
            task_id=second.attempt_id,
        )
    finally:
        attempt_scope_registry.cleanup(first.attempt_id)
        attempt_scope_registry.cleanup(second.attempt_id)

    assert "owned by another protected attempt" in result
    poll.assert_not_called()


def test_attempt_environment_cleanup_kills_owned_background_processes(monkeypatch):
    kill_all = MagicMock(return_value=1)
    monkeypatch.setattr(process_module.process_registry, "kill_all", kill_all)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})

    terminal_tool.cleanup_vm("attempt-cleanup", force_remove=True)

    kill_all.assert_called_once_with(task_id="attempt-cleanup")


@pytest.mark.parametrize(
    ("frozen_network", "ordinary_network", "expected"),
    [("none", True, False), ("full", False, True)],
)
def test_protected_materialization_uses_only_frozen_network_authority(
    monkeypatch, frozen_network, ordinary_network, expected
):
    scope = _scope(frozen_network)
    authority = attempt_scope_registry.reserve(
        scope, "logical-network", attempt_id=f"attempt-network-{frozen_network}"
    )
    terminal_tool.register_task_env_overrides(
        authority.attempt_id,
        {
            "delegation_scope_id": authority.scope_id,
            "env_type": "docker",
            "docker_image": scope.profile.image,
            "cwd": str(scope.workdir),
        },
    )
    create = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr(terminal_tool, "_create_environment", create)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "docker",
            "docker_image": "ordinary:latest",
            "cwd": "/workspace",
            "timeout": 60,
            "docker_network": ordinary_network,
        },
    )

    try:
        terminal_tool.acquire_task_environment(authority.attempt_id)
    finally:
        terminal_tool.clear_task_env_overrides(authority.attempt_id)
        attempt_scope_registry.cleanup(authority.attempt_id)

    assert create.call_args.kwargs["container_config"]["docker_network"] is expected
