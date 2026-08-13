"""
Regression tests for the shared-container task_id mapping.

The top-level agent and all delegate_task subagents share a single
terminal sandbox keyed by ``"default"``.  ``_resolve_container_task_id``
is the sole gatekeeper for which tool-call task_ids go to the shared
container vs. get their own isolated sandbox.  RL / benchmark
environments opt in to isolation by calling
``register_task_env_overrides(task_id, {...})`` before the agent loop;
every other task_id collapses back to ``"default"``.

If you change the collapse logic, update both the helper and these
tests -- see `hermes-agent-dev` skill, "Why do subagents get their own
containers?" section, and the Container lifecycle paragraph under
Docker Backend in ``website/docs/user-guide/configuration.md``.
"""

import pytest

from tools import terminal_tool


@pytest.fixture(autouse=True)
def _clean_overrides():
    """Ensure no stray overrides from other tests leak in."""
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def test_none_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_empty_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id("") == "default"


def test_literal_default_stays_default():
    assert terminal_tool._resolve_container_task_id("default") == "default"


def test_subagent_task_id_collapses_to_default():
    # delegate_task constructs IDs like "subagent-<N>-<uuid_hex>"; these
    # should share the parent's container, not spin up their own.
    assert terminal_tool._resolve_container_task_id("subagent-0-deadbeef") == "default"
    assert terminal_tool._resolve_container_task_id("subagent-42-cafef00d") == "default"


def test_arbitrary_session_id_collapses_to_default():
    # Session UUIDs or anything else without an override still collapse.
    assert terminal_tool._resolve_container_task_id("sess-123e4567-e89b-12d3") == "default"


def test_rl_task_with_override_keeps_its_own_id():
    # RL / benchmark pattern: register a per-task image, then the task_id
    # must survive ``_resolve_container_task_id`` so the rollout lands in
    # its own sandbox.
    terminal_tool.register_task_env_overrides(
        "tb2-task-fix-git", {"docker_image": "tb2:fix-git", "cwd": "/app"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("tb2-task-fix-git")
            == "tb2-task-fix-git"
        )
    finally:
        terminal_tool.clear_task_env_overrides("tb2-task-fix-git")


def test_cleared_override_collapses_again():
    terminal_tool.register_task_env_overrides("tb2-x", {"docker_image": "x:y"})
    assert terminal_tool._resolve_container_task_id("tb2-x") == "tb2-x"
    terminal_tool.clear_task_env_overrides("tb2-x")
    assert terminal_tool._resolve_container_task_id("tb2-x") == "default"


def test_get_active_env_reads_shared_container_from_subagent_id():
    """``get_active_env`` must see the shared ``"default"`` sandbox when
    called with a subagent's task_id, so the agent loop's turn-budget
    enforcement reads the real env (not None) during delegation."""
    sentinel = object()
    terminal_tool._active_environments["default"] = sentinel
    try:
        assert terminal_tool.get_active_env("subagent-7-cafe") is sentinel
        assert terminal_tool.get_active_env(None) is sentinel
        assert terminal_tool.get_active_env("default") is sentinel
    finally:
        terminal_tool._active_environments.pop("default", None)


def test_get_active_env_honours_rl_override():
    rl_env = object()
    default_env = object()
    terminal_tool._active_environments["default"] = default_env
    terminal_tool._active_environments["rl-42"] = rl_env
    terminal_tool.register_task_env_overrides("rl-42", {"docker_image": "x"})
    try:
        # With an override registered, lookup returns the task's own env,
        # not the shared "default" one.
        assert terminal_tool.get_active_env("rl-42") is rl_env
    finally:
        terminal_tool.clear_task_env_overrides("rl-42")
        terminal_tool._active_environments.pop("default", None)
        terminal_tool._active_environments.pop("rl-42", None)


def test_cwd_only_override_collapses_to_default():
    """CWD-only overrides (ACP adapter workspace tracking) must NOT trigger
    container isolation — they should collapse to the shared 'default'
    container so all surfaces (TUI, gateway, dashboard) share one sandbox.
    Regression for #37361."""
    terminal_tool.register_task_env_overrides(
        "acp-session-abc", {"cwd": "/home/user/project"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("acp-session-abc")
            == "default"
        )
    finally:
        terminal_tool.clear_task_env_overrides("acp-session-abc")


def test_delegation_scope_override_keeps_physical_attempt_isolated():
    terminal_tool.register_task_env_overrides(
        "attempt-protected",
        {"cwd": "/workspace", "delegation_scope_id": "scope-protected"},
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("attempt-protected")
            == "attempt-protected"
        )
    finally:
        terminal_tool.clear_task_env_overrides("attempt-protected")


def test_acquire_task_environment_creates_and_returns_effective_environment(
    monkeypatch,
):
    sentinel = object()
    created = []
    config = {
        "env_type": "local",
        "cwd": "/tmp",
        "timeout": 180,
        "host_cwd": None,
        "local_persistent": False,
    }
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool,
        "_create_environment",
        lambda **kwargs: created.append(kwargs) or sentinel,
    )

    env, backend, effective_key = terminal_tool.acquire_task_environment(
        "ordinary-child", timeout=45
    )

    assert (env, backend, effective_key) == (sentinel, "local", "default")
    assert created == [
        {
            "env_type": "local",
            "image": "",
            "cwd": "/tmp",
            "timeout": 45,
            "ssh_config": None,
            "container_config": None,
            "local_config": {"persistent": False},
            "task_id": "default",
            "host_cwd": None,
        }
    ]


def test_cwd_plus_docker_image_keeps_own_id():
    """When overrides include both cwd AND docker_image, isolation must
    still be honoured (RL/benchmark pattern with explicit cwd)."""
    terminal_tool.register_task_env_overrides(
        "rl-with-cwd", {"docker_image": "myimg:latest", "cwd": "/workspace"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("rl-with-cwd")
            == "rl-with-cwd"
        )
    finally:
        terminal_tool.clear_task_env_overrides("rl-with-cwd")


def test_env_type_override_keeps_own_id():
    """env_type is an isolation key — must trigger per-task container."""
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("bench-env")
            == "bench-env"
        )
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")


def test_protected_acquisition_materializes_only_resolved_profile_and_reveals(monkeypatch):
    from pathlib import PurePosixPath
    from unittest.mock import MagicMock
    from agent.delegation_policy import AccessMode, BackingObjectRef, ExecutionProfile, VisibleObjectGrant
    from tools.delegation_scope import (
        BackingObjectRecord,
        BackingObjectRegistry,
        ResolvedInvocationScope,
        attempt_scope_registry,
        execution_profile_hash,
    )

    profile = ExecutionProfile(
        name="protected", backend="docker", image="repo/protected@sha256:deadbeef",
        default_workdir=PurePosixPath("/workspace"), allowed_toolsets=frozenset({"terminal"}),
        network="none", cpu=2.0, memory_mb=1024, shm_mb=128, pids_limit=48,
    )
    scope = ResolvedInvocationScope(
        profile_name=profile.name, profile_hash=execution_profile_hash(profile), profile=profile,
        workdir=PurePosixPath("/workspace"), reveal=(),
        visible_objects=(VisibleObjectGrant(
            visible_path=PurePosixPath("/visible/data"), mode=AccessMode.RO,
            backing=BackingObjectRef(object_id="obj-1", kind="host_path", identity="/trusted/data", revision="rev-1"),
            object_type="directory",
        ),),
    )
    grant = scope.visible_objects[0]
    backing_registry = BackingObjectRegistry({
        grant.backing.object_id: BackingObjectRecord(grant.backing, grant.object_type)
    })
    authority = attempt_scope_registry.reserve(
        scope,
        "logical-1",
        attempt_id="attempt-protected-acquire",
        backing_registry=backing_registry,
    )
    create = MagicMock(return_value=object())
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "local", "cwd": "/ambient", "timeout": 60,
        "lifetime_seconds": 300, "docker_volumes": ["/ambient:/ambient"],
        "docker_extra_args": ["--privileged"],
    })
    monkeypatch.setattr(terminal_tool, "_create_environment", create)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})

    try:
        terminal_tool.register_task_env_overrides(authority.attempt_id, {
            "env_type": "docker", "docker_image": profile.image, "cwd": "/workspace",
            "delegation_scope_id": authority.scope_id,
        })
        terminal_tool.acquire_task_environment(authority.attempt_id)
    finally:
        terminal_tool.clear_task_env_overrides(authority.attempt_id)
        attempt_scope_registry.cleanup(authority.attempt_id)

    kwargs = create.call_args.kwargs
    assert (kwargs["env_type"], kwargs["image"], kwargs["cwd"]) == (
        "docker", profile.image, "/workspace"
    )
    container_config = kwargs["container_config"]
    backing_validator = container_config.pop("trusted_mounts_validator")
    assert callable(backing_validator)
    assert container_config == {
        "container_cpu": 2.0, "container_memory": 1024, "container_disk": 0,
        "container_persistent": False, "docker_network": False,
        "docker_persist_across_processes": False, "suppress_implicit_mounts": True,
        "trusted_mounts": [{"kind": "host_path", "source": "/trusted/data", "target": "/visible/data", "mode": "ro"}],
        "shm_mb": 128, "pids_limit": 48,
        "delegation_scope_id": authority.scope_id,
        "delegation_attempt_id": authority.attempt_id,
    }


def test_protected_acquisition_revalidates_backing_revision_before_creation(monkeypatch):
    from pathlib import PurePosixPath
    from unittest.mock import MagicMock
    from agent.delegation_policy import AccessMode, BackingObjectRef, ExecutionProfile, VisibleObjectGrant
    from tools.delegation_scope import (
        BackingObjectRecord, ResolvedInvocationScope, attempt_scope_registry,
        execution_profile_hash,
    )

    backing = BackingObjectRef("obj-race", "host_path", "/trusted/race", "rev-1")
    grant = VisibleObjectGrant(PurePosixPath("/visible/race"), AccessMode.RO, backing, "directory")
    profile = ExecutionProfile(
        "protected", "docker", "repo/protected@sha256:deadbeef", "/workspace",
        frozenset({"terminal"}),
    )
    scope = ResolvedInvocationScope(
        profile.name, execution_profile_hash(profile), profile,
        PurePosixPath("/workspace"), (), (grant,),
    )

    class MutableRegistry:
        current = BackingObjectRecord(backing, "directory")

        def get(self, _object_id):
            return self.current

    registry = MutableRegistry()
    authority = attempt_scope_registry.reserve(
        scope, "logical-race", attempt_id="attempt-race", backing_registry=registry
    )
    create = MagicMock(return_value=object())
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "docker", "docker_image": profile.image, "cwd": "/workspace",
        "timeout": 60, "lifetime_seconds": 300,
    })
    monkeypatch.setattr(terminal_tool, "_create_environment", create)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    terminal_tool.register_task_env_overrides(authority.attempt_id, {
        "env_type": "docker", "docker_image": profile.image, "cwd": "/workspace",
        "delegation_scope_id": authority.scope_id,
    })
    registry.current = BackingObjectRecord(
        BackingObjectRef("obj-race", "host_path", "/trusted/race", "rev-2"),
        "directory",
    )

    try:
        with pytest.raises(ValueError, match="backing.*changed"):
            terminal_tool.acquire_task_environment(authority.attempt_id)
    finally:
        terminal_tool.clear_task_env_overrides(authority.attempt_id)
        attempt_scope_registry.cleanup(authority.attempt_id)

    create.assert_not_called()
