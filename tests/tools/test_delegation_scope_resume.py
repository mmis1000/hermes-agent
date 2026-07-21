from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import PurePosixPath
from threading import Event
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from agent.delegation_policy import (
    AccessMode,
    BackingObjectRef,
    DelegationSessionPolicy,
    ExecutionProfile,
    VisibleObjectGrant,
)
from tools.delegation_scope import (
    AttemptScopeRegistry,
    BackingObjectRecord,
    BackingObjectRegistry,
    RevealRequest,
    ResolvedInvocationScope,
    admit_trusted_run_execution,
    deserialize_delegation_authority,
    execution_profile_hash,
    resolve_invocation_scope,
    serialize_delegation_authority,
)


def _authority_fixture():
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/image@sha256:deadbeef",
        default_workdir="/workspace",
        allowed_toolsets=frozenset({"terminal", "delegation"}),
        qualified_mcp_servers=frozenset({"safe-mcp"}),
        network="bridge",
        cpu=2.0,
        memory_mb=4096,
        shm_mb=1024,
        pids_limit=256,
    )
    backing = BackingObjectRef(
        object_id="object-1",
        kind="host_path",
        identity="device:inode",
        revision="revision-7",
    )
    grant = VisibleObjectGrant(
        visible_path="/workspace/input",
        mode=AccessMode.RO,
        backing=backing,
        object_type="directory",
    )
    scope = ResolvedInvocationScope(
        profile_name="isolated",
        profile_hash=execution_profile_hash(profile),
        profile=profile,
        workdir=PurePosixPath("/workspace"),
        reveal=(RevealRequest(PurePosixPath("/workspace/input"), "ro"),),
        visible_objects=(grant,),
    )
    registry = BackingObjectRegistry(
        {"object-1": BackingObjectRecord(backing=backing, object_type="directory")}
    )
    return scope, registry


def test_authority_round_trip_is_versioned_canonical_and_credential_free():
    scope, registry = _authority_fixture()

    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("delegation", "terminal"),
        disabled_toolsets=("browser",),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
        parent_attempt_id="attempt-parent",
    )
    restored = deserialize_delegation_authority(authority, backing_registry=registry)

    assert authority["version"] == 1
    assert authority["profile"]["hash"] == scope.profile_hash
    assert authority["profile"]["snapshot"]["image"] == scope.profile.image
    assert authority["reveal"] == [{"path": "/workspace/input", "mode": "ro"}]
    assert authority["visible_objects"] == [
        {
            "path": "/workspace/input",
            "mode": "ro",
            "object_type": "directory",
            "backing": {
                "object_id": "object-1",
                "kind": "host_path",
                "identity": "device:inode",
                "revision": "revision-7",
            },
        }
    ]
    assert authority["tools"] == {
        "enabled_toolsets": ["delegation", "terminal"],
        "disabled_toolsets": ["browser"],
    }
    assert authority["lineage"] == {
        "scope_id": "scope-initial",
        "attempt_id": "attempt-initial",
        "parent_attempt_id": "attempt-parent",
    }
    assert authority["state"] == {"revoked": False, "cleaned": False}
    assert "api_key" not in repr(authority).lower()
    assert restored == scope


def test_expanded_carveout_authority_round_trip_preserves_effective_tree(tmp_path):
    root = tmp_path / "root"
    selected = root / "a" / "b"
    carveout = selected / "c"
    carveout.mkdir(parents=True)
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/image@sha256:deadbeef",
        default_workdir="/workspace",
        allowed_toolsets=frozenset({"terminal", "delegation"}),
        network="none",
    )
    base_policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={profile.name},
        profile_snapshots={profile.name: profile},
        visible_objects=(),
        protected_prefixes=(),
    )
    admitted = admit_trusted_run_execution(
        base_policy,
        {
            "profile": profile.name,
            "workdir": str(root),
            "reveal": [
                {"path": str(root), "mode": "rw"},
                {"path": str(carveout), "mode": "ro"},
            ],
        },
        inherited_network=False,
    )
    child_scope = resolve_invocation_scope(
        admitted.policy,
        profile.name,
        str(selected),
        [{"path": str(selected), "mode": "rw"}],
        backing_registry=admitted.backing_registry,
    )
    assert child_scope is not None
    authority = serialize_delegation_authority(
        child_scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-tree",
        attempt_id="attempt-tree",
    )

    restored = deserialize_delegation_authority(
        authority,
        backing_registry=admitted.backing_registry,
    )

    assert restored == child_scope
    assert authority["reveal"] == [
        {"path": str(selected), "mode": "rw"},
        {"path": str(carveout), "mode": "ro"},
    ]
@pytest.mark.parametrize(
    ("inherited_network", "effective_network"),
    [(False, "none"), (True, "full")],
)
def test_inherited_network_authority_durable_round_trip_preserves_template_identity(
    inherited_network, effective_network
):
    template = ExecutionProfile(
        name="inherited",
        backend="docker",
        image="repo/image@sha256:deadbeef",
        default_workdir="/workspace",
        allowed_toolsets=frozenset({"terminal"}),
        network="inherit",
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"inherited"},
        profile_snapshots={"inherited": template},
        visible_objects=(),
        protected_prefixes=(),
    )
    scope = resolve_invocation_scope(
        policy,
        "inherited",
        None,
        None,
        inherited_network=inherited_network,
    )
    assert scope is not None
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-inherited",
        attempt_id="attempt-inherited",
    )
    durable_authority = json.loads(json.dumps(authority))

    restored = deserialize_delegation_authority(
        durable_authority,
        backing_registry=BackingObjectRegistry({}),
        expected_policy=policy,
    )

    assert durable_authority["profile"]["snapshot"]["network"] == effective_network
    assert restored.profile.network == effective_network
    drifted = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"inherited"},
        profile_snapshots={
            "inherited": ExecutionProfile(
                **{
                    **template.__dict__,
                    "image": "repo/image@sha256:changed",
                }
            )
        },
        visible_objects=(),
        protected_prefixes=(),
    )
    with pytest.raises(ValueError, match="unknown or disabled"):
        deserialize_delegation_authority(
            durable_authority,
            backing_registry=BackingObjectRegistry({}),
            expected_policy=drifted,
        )


def test_resumed_dispatch_rejects_revoked_authority_before_attempt_reservation(
    monkeypatch,
):
    from tools import async_delegation as ad

    scope, registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    authority = deepcopy(authority)
    authority["state"]["revoked"] = True
    monkeypatch.setattr(
        ad,
        "get_async_delegation",
        lambda *_args, **_kwargs: {
            "session_key": "owner",
            "children": {"logical": {"status": "completed", "goal": "work"}},
        },
    )
    monkeypatch.setattr(
        ad,
        "load_subagent_resume_bundle",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "protected": True,
            "authority": authority,
            "bundle": {},
        },
    )
    repository = MagicMock()
    monkeypatch.setattr(ad, "_repository", lambda: repository)

    result = ad.dispatch_resumed_subagent(
        "delegation",
        "logical",
        session_key="owner",
        message="continue",
        parent_agent=SimpleNamespace(delegation_backing_registry=registry),
    )

    assert result == {
        "status": "resume_unavailable",
        "reason": "protected authority is revoked or cleaned",
    }
    repository.reserve_resumed_attempt.assert_not_called()


def test_authority_tool_snapshot_mutation_fails_closed():
    scope, registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    authority["tools"]["enabled_toolsets"] = ["delegation"]

    with pytest.raises(ValueError, match="immutable snapshot changed"):
        deserialize_delegation_authority(authority, backing_registry=registry)


def test_protected_resume_reconstructs_scope_tools_and_fresh_private_attempt(
    monkeypatch,
):
    from tools import async_delegation as ad
    from tools import delegate_tool
    from tools import delegation_scope as scope_module
    from tools import terminal_tool

    scope, backing_registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=("browser",),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    monkeypatch.setattr(
        ad,
        "get_async_delegation",
        lambda *_args, **_kwargs: {
            "session_key": "owner",
            "origin_ui_session_id": "ui",
            "parent_session_id": "owner",
            "children": {"logical": {"status": "completed", "goal": "work"}},
        },
    )
    bundle = {
        "prior_child_session_id": "child-prior",
        "parent_session_id": "owner",
        "history": [{"role": "user", "content": "work"}],
        "reconstruction_metadata": {
            "parent_session_id": "owner",
            "model": "model",
            "provider": "provider",
            "enabled_toolsets": ["untrusted-old-tools"],
            "disabled_toolsets": [],
        },
    }
    monkeypatch.setattr(
        ad,
        "load_subagent_resume_bundle",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "protected": True,
            "authority": authority,
            "bundle": bundle,
        },
    )
    repository = MagicMock()
    repository.reserve_resumed_attempt.return_value = {
        "status": "reserved",
        "run_id": "run-new",
        "attempt_id": "attempt-new",
        "attempt_number": 2,
    }
    monkeypatch.setattr(ad, "_repository", lambda: repository)
    monkeypatch.setattr(
        delegate_tool,
        "prepare_resumed_child_session",
        lambda _bundle: {
            "session_id": "child-new",
            "parent_session_id": "child-prior",
            "delegate_from": "owner",
        },
    )
    child = SimpleNamespace(
        _delegation_session_ref={},
        _delegation_runtime_metadata={"child_session_id": "child-new"},
        close=MagicMock(),
    )
    build_calls = []
    monkeypatch.setattr(
        delegate_tool,
        "build_resumed_child_agent",
        lambda **kwargs: build_calls.append(kwargs) or child,
    )
    submitted = []
    monkeypatch.setattr(
        ad,
        "_get_executor",
        lambda _limit: SimpleNamespace(
            submit=lambda fn: submitted.append(fn) or SimpleNamespace()
        ),
    )
    attempt_registry = AttemptScopeRegistry()
    prepare_idmaps = MagicMock(wraps=attempt_registry.prepare_idmapped_reveals)
    attempt_registry.prepare_idmapped_reveals = prepare_idmaps
    monkeypatch.setattr(scope_module, "attempt_scope_registry", attempt_registry)
    parent = SimpleNamespace(
        delegation_backing_registry=backing_registry,
        delegation_policy=None,
        _delegation_attempt_id="attempt-parent",
    )

    result = ad.dispatch_resumed_subagent(
        "delegation",
        "logical",
        session_key="owner",
        message="continue",
        parent_agent=parent,
    )

    try:
        assert result["status"] == "dispatched"
        assert result["attempt_id"] == "attempt-new"
        assert len(submitted) == 1
        repository.reserve_resumed_attempt.assert_called_once_with(
            "logical",
            physical_worker_id=None,
            owner_pid=ANY,
            owner_started_at=ANY,
            metadata=ANY,
        )
        assert build_calls[0]["resolved_scope"] == scope
        assert build_calls[0]["authority_tools"] == authority["tools"]
        assert child._current_task_id == "attempt-new"
        assert child._delegation_scope_id != "scope-initial"
        assert attempt_registry.get("attempt-new").state == "active"
        assert terminal_tool._resolve_container_task_id("attempt-new") == "attempt-new"
        prepare_idmaps.assert_called_once_with("attempt-new")
    finally:
        attempt_registry.cleanup("attempt-new")


def test_resume_rejects_profile_disabled_by_current_parent_policy():
    scope, registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    disabled_policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=set(),
        profile_snapshots={scope.profile_name: scope.profile},
        visible_objects=scope.visible_objects,
        protected_prefixes=(),
    )

    with pytest.raises(ValueError, match="unknown or disabled"):
        deserialize_delegation_authority(
            authority,
            backing_registry=registry,
            expected_policy=disabled_policy,
        )


@pytest.mark.parametrize(
    "mutation",
    ["malformed", "version", "profile", "backing", "reveal", "workdir"],
)
def test_resume_rejects_malformed_or_mutated_authority_snapshots(mutation):
    scope, registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    if mutation == "malformed":
        authority = {"version": 1}
    elif mutation == "version":
        authority["version"] = 999
    elif mutation == "profile":
        authority["profile"]["snapshot"]["image"] = "changed@sha256:bad"
    elif mutation == "backing":
        authority["visible_objects"][0]["backing"]["revision"] = "changed"
    elif mutation == "reveal":
        authority["reveal"][0]["path"] = "/other"
    elif mutation == "workdir":
        authority["workdir"] = "/other"

    with pytest.raises(ValueError):
        deserialize_delegation_authority(authority, backing_registry=registry)


def test_resume_revalidates_stale_backing_revision():
    scope, _registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    changed_backing = BackingObjectRef(
        object_id="object-1",
        kind="host_path",
        identity="dev:11:ino:22",
        revision="rev-2",
    )
    stale_registry = BackingObjectRegistry(
        {
            "object-1": BackingObjectRecord(
                backing=changed_backing,
                object_type="directory",
            )
        }
    )

    with pytest.raises(ValueError, match="missing or stale"):
        deserialize_delegation_authority(
            authority,
            backing_registry=stale_registry,
        )


def _durable_cleanup_failure_harness(tmp_path, monkeypatch, *, phase):
    from tools import async_delegation as ad
    from tools import delegate_tool
    from tools import delegation_scope as scope_module
    from tools import terminal_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    scope, backing_registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    repository = ad._repository()
    initial = repository.register_initial_dispatch(
        {
            "delegation_id": f"deleg-{phase}",
            "session_key": "owner",
            "origin_ui_session_id": "ui",
            "parent_session_id": "owner",
            "goal": "work",
            "root_subagent_ids": ["logical"],
            "attempt_ids_by_logical_id": {"logical": "attempt-initial"},
            "authority_by_logical_id": {"logical": authority},
        }
    )
    repository.transition_attempt(
        initial["attempts"][0]["attempt_id"],
        {"starting"},
        "completed",
        metadata={"status": "completed", "child_session_id": "child-prior"},
    )
    bundle = {
        "prior_child_session_id": "child-prior",
        "parent_session_id": "owner",
        "history": [{"role": "user", "content": "work"}],
        "reconstruction_metadata": {
            "parent_session_id": "owner",
            "model": "model",
            "provider": "provider",
        },
    }
    monkeypatch.setattr(
        ad,
        "load_subagent_resume_bundle",
        lambda *_a, **_k: {
            "status": "ready",
            "protected": True,
            "authority": authority,
            "bundle": bundle,
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "prepare_resumed_child_session",
        lambda _bundle: {
            "session_id": "child-new",
            "parent_session_id": "child-prior",
            "delegate_from": "owner",
        },
    )
    executor = None
    worker_entered = None
    attempt_registry_holder = {}
    if phase == "construction":
        monkeypatch.setattr(
            delegate_tool,
            "build_resumed_child_agent",
            MagicMock(side_effect=RuntimeError("construction failed")),
        )
    elif phase == "materialization":
        monkeypatch.setattr(
            terminal_tool,
            "register_task_env_overrides",
            MagicMock(side_effect=RuntimeError("materialization failed")),
        )
    elif phase in {"activation", "executor", "submission"}:
        child = SimpleNamespace(
            _delegation_session_ref={},
            _delegation_runtime_metadata={"child_session_id": "child-new"},
            close=MagicMock(),
        )
        monkeypatch.setattr(
            delegate_tool, "build_resumed_child_agent", lambda **_kwargs: child
        )
        if phase == "activation":
            executor = ThreadPoolExecutor(max_workers=1)
            worker_entered = Event()

            def enter_worker(*_args, **_kwargs):
                worker_entered.set()
                return {
                    "status": "completed",
                    "summary": "unexpected worker entry",
                    "api_calls": 0,
                    "duration_seconds": 0.0,
                }

            monkeypatch.setattr(delegate_tool, "_run_single_child", enter_worker)
            monkeypatch.setattr(ad, "_get_executor", lambda _limit: executor)
        elif phase == "executor":
            monkeypatch.setattr(
                ad,
                "_get_executor",
                MagicMock(side_effect=RuntimeError("executor failed")),
            )
        else:

            def fail_submission(_worker):
                registry = attempt_registry_holder["registry"]
                registry.submission_observed_active = getattr(
                    registry, "activation_succeeded", False
                )
                raise RuntimeError("submission failed")

            monkeypatch.setattr(
                ad,
                "_get_executor",
                lambda _limit: SimpleNamespace(submit=fail_submission),
            )
    else:
        raise AssertionError(f"unsupported phase: {phase}")
    cleanup_vm = MagicMock(side_effect=RuntimeError("cleanup failed"))
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    if phase == "activation":
        assert worker_entered is not None

        class ActivationFailRegistry(AttemptScopeRegistry):
            worker_entered_before_activation_failure = False

            def activate(self, attempt_id, **metadata):
                self.worker_entered_before_activation_failure = worker_entered.wait(
                    timeout=1.0
                )
                raise RuntimeError("activation failed")

        attempt_registry = ActivationFailRegistry()
    elif phase == "submission":

        class RecordingActivationRegistry(AttemptScopeRegistry):
            activation_succeeded = False

            def activate(self, attempt_id, **metadata):
                authority = super().activate(attempt_id, **metadata)
                self.activation_succeeded = True
                return authority

        attempt_registry = RecordingActivationRegistry()
    else:
        attempt_registry = AttemptScopeRegistry()
    attempt_registry_holder["registry"] = attempt_registry
    monkeypatch.setattr(scope_module, "attempt_scope_registry", attempt_registry)
    parent = SimpleNamespace(
        delegation_backing_registry=backing_registry,
        delegation_policy=None,
        _delegation_attempt_id="attempt-parent",
    )

    try:
        result = ad.dispatch_resumed_subagent(
            f"deleg-{phase}",
            "logical",
            session_key="owner",
            message="continue",
            parent_agent=parent,
        )
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    return result, repository, attempt_registry, cleanup_vm


def test_protected_resume_construction_and_cleanup_failure_is_durable_and_retryable(
    tmp_path, monkeypatch
):
    from tools import terminal_tool

    result, repository, registry, cleanup_vm = _durable_cleanup_failure_harness(
        tmp_path, monkeypatch, phase="construction"
    )

    assert result["status"] == "dispatch_failed"
    assert result["exit_reason"] == "cleanup_error"
    assert "construction failed" in result["error"]
    assert "cleanup failed" in result["error"]
    snapshot = repository.snapshot("deleg-construction", session_key="owner")
    assert snapshot is not None
    child = snapshot["children"]["logical"]
    assert child["status"] == "error"
    assert child["exit_reason"] == "cleanup_error"
    assert child["authority_audit"]["state"] == {
        "revoked": True,
        "cleaned": False,
    }
    assert child["authority_audit"]["outcome"]["cleanup"] == "failed"
    attempt_id = result["attempt_id"]
    runtime = registry.get(attempt_id)
    assert runtime is not None
    assert runtime.state == "revoked"
    assert list(runtime.resources.cleanup_callbacks) == ["task-environment"]
    assert cleanup_vm.call_count == 1

    monkeypatch.setattr(terminal_tool, "cleanup_vm", MagicMock())
    assert registry.cleanup(attempt_id) == ()
    assert runtime.state == "cleaned"
    assert runtime.resources.cleanup_callbacks == {}


def test_protected_resume_materialization_and_cleanup_failure_is_durable_and_retryable(
    tmp_path, monkeypatch
):
    from tools import terminal_tool

    result, repository, registry, cleanup_vm = _durable_cleanup_failure_harness(
        tmp_path, monkeypatch, phase="materialization"
    )

    assert result["status"] == "dispatch_failed"
    assert result["exit_reason"] == "cleanup_error"
    assert "materialization failed" in result["error"]
    assert "cleanup failed" in result["error"]
    snapshot = repository.snapshot("deleg-materialization", session_key="owner")
    assert snapshot is not None
    child = snapshot["children"]["logical"]
    assert child["status"] == "error"
    assert child["exit_reason"] == "cleanup_error"
    assert child["authority_audit"]["state"] == {
        "revoked": True,
        "cleaned": False,
    }
    assert child["authority_audit"]["outcome"]["cleanup"] == "failed"
    attempt_id = result["attempt_id"]
    runtime = registry.get(attempt_id)
    assert runtime is not None
    assert runtime.state == "revoked"
    assert list(runtime.resources.cleanup_callbacks) == ["task-environment"]
    assert cleanup_vm.call_count == 1

    monkeypatch.setattr(terminal_tool, "cleanup_vm", MagicMock())
    assert registry.cleanup(attempt_id) == ()
    assert runtime.state == "cleaned"
    assert runtime.resources.cleanup_callbacks == {}


def test_protected_resume_executor_creation_and_cleanup_failure_is_terminal(
    tmp_path, monkeypatch
):
    result, repository, registry, _cleanup_vm = _durable_cleanup_failure_harness(
        tmp_path, monkeypatch, phase="executor"
    )

    assert result["status"] == "dispatch_failed"
    assert result["exit_reason"] == "cleanup_error"
    assert "executor failed" in result["error"]
    assert "cleanup failed" in result["error"]
    snapshot = repository.snapshot("deleg-executor", session_key="owner")
    assert snapshot is not None
    child = snapshot["children"]["logical"]
    assert child["status"] == "error"
    assert child["exit_reason"] == "cleanup_error"
    runtime = registry.get(result["attempt_id"])
    assert runtime is not None
    assert runtime.state == "revoked"
    assert list(runtime.resources.cleanup_callbacks) == ["task-environment"]


def test_protected_resume_activation_failure_prevents_worker_entry(
    tmp_path, monkeypatch
):
    result, repository, registry, _cleanup_vm = _durable_cleanup_failure_harness(
        tmp_path, monkeypatch, phase="activation"
    )

    assert result["status"] == "dispatch_failed"
    assert result["exit_reason"] == "cleanup_error"
    assert "activation failed" in result["error"]
    assert getattr(registry, "worker_entered_before_activation_failure") is False
    snapshot = repository.snapshot("deleg-activation", session_key="owner")
    assert snapshot is not None
    child = snapshot["children"]["logical"]
    assert child["status"] == "error"
    assert child["exit_reason"] == "cleanup_error"


def test_protected_resume_submission_and_cleanup_failure_is_durable_and_retryable(
    tmp_path, monkeypatch
):
    from tools import terminal_tool

    result, repository, registry, _cleanup_vm = _durable_cleanup_failure_harness(
        tmp_path, monkeypatch, phase="submission"
    )

    assert result["status"] == "dispatch_failed"
    assert result["exit_reason"] == "cleanup_error"
    assert "submission failed" in result["error"]
    assert "cleanup failed" in result["error"]
    snapshot = repository.snapshot("deleg-submission", session_key="owner")
    assert snapshot is not None
    child = snapshot["children"]["logical"]
    assert child["status"] == "error"
    assert child["exit_reason"] == "cleanup_error"
    assert child["authority_audit"]["state"] == {
        "revoked": True,
        "cleaned": False,
    }
    assert child["authority_audit"]["outcome"]["cleanup"] == "failed"
    attempt_id = result["attempt_id"]
    runtime = registry.get(attempt_id)
    assert runtime is not None
    assert getattr(registry, "submission_observed_active") is True
    assert runtime.state == "revoked"
    assert list(runtime.resources.cleanup_callbacks) == ["task-environment"]

    monkeypatch.setattr(terminal_tool, "cleanup_vm", MagicMock())
    assert registry.cleanup(attempt_id) == ()
    assert runtime.state == "cleaned"


def _dispatch_failure_harness(monkeypatch, *, phase):
    from tools import async_delegation as ad
    from tools import delegate_tool
    from tools import delegation_scope as scope_module

    scope, backing_registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-initial",
        attempt_id="attempt-initial",
    )
    monkeypatch.setattr(
        ad,
        "get_async_delegation",
        lambda *_a, **_k: {
            "session_key": "owner",
            "origin_ui_session_id": "ui",
            "parent_session_id": "owner",
            "children": {"logical": {"status": "completed", "goal": "work"}},
        },
    )
    bundle = {
        "prior_child_session_id": "child-prior",
        "parent_session_id": "owner",
        "history": [{"role": "user", "content": "work"}],
        "reconstruction_metadata": {
            "parent_session_id": "owner",
            "model": "model",
            "provider": "provider",
        },
    }
    monkeypatch.setattr(
        ad,
        "load_subagent_resume_bundle",
        lambda *_a, **_k: {
            "status": "ready",
            "protected": True,
            "authority": authority,
            "bundle": bundle,
        },
    )
    repository = MagicMock()
    repository.reserve_resumed_attempt.return_value = {
        "status": "reserved",
        "run_id": "run-new",
        "attempt_id": "attempt-new",
        "attempt_number": 2,
    }
    monkeypatch.setattr(ad, "_repository", lambda: repository)
    monkeypatch.setattr(
        delegate_tool,
        "prepare_resumed_child_session",
        lambda _bundle: {
            "session_id": "child-new",
            "parent_session_id": "child-prior",
            "delegate_from": "owner",
        },
    )
    child = SimpleNamespace(
        _delegation_session_ref={},
        _delegation_runtime_metadata={"child_session_id": "child-new"},
        close=MagicMock(),
    )
    if phase == "construction":
        monkeypatch.setattr(
            delegate_tool,
            "build_resumed_child_agent",
            MagicMock(side_effect=RuntimeError("construction failed")),
        )
    else:
        monkeypatch.setattr(
            delegate_tool, "build_resumed_child_agent", lambda **_kwargs: child
        )
    submit = (
        MagicMock(side_effect=RuntimeError("submission failed"))
        if phase == "submission"
        else MagicMock()
    )
    monkeypatch.setattr(
        ad, "_get_executor", lambda _limit: SimpleNamespace(submit=submit)
    )
    attempt_registry = AttemptScopeRegistry()
    monkeypatch.setattr(scope_module, "attempt_scope_registry", attempt_registry)
    parent = SimpleNamespace(
        delegation_backing_registry=backing_registry,
        delegation_policy=None,
        _delegation_attempt_id="attempt-parent",
    )
    result = ad.dispatch_resumed_subagent(
        "delegation",
        "logical",
        session_key="owner",
        message="continue",
        parent_agent=parent,
    )
    return result, attempt_registry, child


def test_protected_resume_construction_failure_rolls_back_attempt_resources(
    monkeypatch,
):
    from tools import terminal_tool

    result, registry, _child = _dispatch_failure_harness(
        monkeypatch, phase="construction"
    )

    assert result["status"] == "dispatch_failed"
    assert registry.get("attempt-new").state == "cleaned"
    assert "attempt-new" not in terminal_tool._task_env_overrides


def test_protected_resume_submission_failure_rolls_back_attempt_resources(
    monkeypatch,
):
    from tools import terminal_tool

    result, registry, child = _dispatch_failure_harness(
        monkeypatch, phase="submission"
    )

    assert result["status"] == "dispatch_failed"
    assert registry.get("attempt-new").state == "cleaned"
    assert "attempt-new" not in terminal_tool._task_env_overrides
    child.close.assert_called_once()


def test_protected_authority_reconstructs_after_local_attempt_registry_restart():
    scope, backing_registry = _authority_fixture()
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=("browser",),
        scope_id="scope-before-restart",
        attempt_id="attempt-before-restart",
    )
    old_registry = AttemptScopeRegistry()
    old_registry.reserve(scope, "logical", attempt_id="attempt-before-restart")
    old_registry.cleanup("attempt-before-restart")

    restarted_registry = AttemptScopeRegistry()
    restored_scope = deserialize_delegation_authority(
        authority,
        backing_registry=backing_registry,
    )
    fresh = restarted_registry.reserve(
        restored_scope, "logical", attempt_id="attempt-after-restart"
    )

    assert restored_scope == scope
    assert fresh.attempt_id == "attempt-after-restart"
    assert fresh.scope_id != authority["lineage"]["scope_id"]
    assert fresh.invocation_scope.visible_objects == scope.visible_objects
