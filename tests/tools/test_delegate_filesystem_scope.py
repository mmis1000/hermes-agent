import json
import threading
from types import SimpleNamespace
from typing import Any, cast
from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

from agent.delegation_policy import (
    AccessMode,
    BackingObjectRef,
    DelegationSessionPolicy,
    ExecutionProfile,
    VisibleObjectGrant,
)
from tools.delegate_tool import _run_single_child, delegate_task
from tools.delegation_scope import (
    AttemptScopeRegistry,
    ResolvedInvocationScope,
    execution_profile_hash,
)
from tools import terminal_tool


def test_protected_workers_receive_fresh_physical_attempt_and_scope_ids():
    registry = AttemptScopeRegistry()
    invocation_scope = cast(Any, SimpleNamespace(profile_name="isolated"))

    first = registry.reserve(invocation_scope, "logical-a")
    second = registry.reserve(invocation_scope, "logical-b")

    assert first.attempt_id != second.attempt_id
    assert first.scope_id != second.scope_id
    assert first.logical_child_id == "logical-a"
    assert second.logical_child_id == "logical-b"
    assert first.state == second.state == "starting"


def test_attempt_ledger_rolls_back_partial_resources_idempotently():
    registry = AttemptScopeRegistry()
    invocation_scope = cast(Any, SimpleNamespace(profile_name="isolated"))
    first = registry.reserve(invocation_scope, "logical-a")
    second = registry.reserve(invocation_scope, "logical-b")
    cleaned: list[str] = []
    registry.add_resource(first.attempt_id, "environment", lambda: cleaned.append("a"))
    registry.add_resource(second.attempt_id, "environment", lambda: cleaned.append("b"))

    registry.rollback([first.attempt_id, second.attempt_id])
    registry.rollback([first.attempt_id, second.attempt_id])

    assert cleaned == ["b", "a"]
    assert registry.get(first.attempt_id).state == "cleaned"
    assert registry.get(second.attempt_id).state == "cleaned"


def test_protected_background_batch_binds_distinct_attempt_environment_keys():
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/isolated@sha256:deadbeef",
        default_workdir="/work",
        allowed_toolsets=frozenset({"terminal", "delegation"}),
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=(),
    )
    scope = ResolvedInvocationScope(
        profile_name="isolated",
        profile_hash=execution_profile_hash(profile),
        profile=profile,
        workdir=PurePosixPath("/work"),
        reveal=(),
        visible_objects=(),
    )
    parent = SimpleNamespace(
        delegation_policy=policy,
        delegation_backing_registry=None,
        _delegate_depth=0,
        session_id="parent-session",
    )
    children = [
        SimpleNamespace(_subagent_id="logical-a"),
        SimpleNamespace(_subagent_id="logical-b"),
    ]
    registry = AttemptScopeRegistry()
    prepare_idmaps = MagicMock(wraps=registry.prepare_idmapped_reveals)
    registry.prepare_idmapped_reveals = prepare_idmaps
    captured_mapping: dict[str, str] = {}
    captured_authority: dict[str, dict] = {}

    def dispatch(**kwargs):
        captured_mapping.update(kwargs["attempt_ids_by_logical_id"])
        captured_authority.update(kwargs["authority_by_logical_id"])
        kwargs["_bind_attempts"]("run-protected", captured_mapping)
        return {"status": "dispatched", "delegation_id": "deleg-protected"}

    with (
        patch("tools.delegation_scope.resolve_invocation_scope", return_value=scope),
        patch("tools.delegation_scope.attempt_scope_registry", registry),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value={
                "provider": "test",
                "model": "test-model",
                "base_url": "http://localhost/v1",
                "api_key": "test-key",
                "api_mode": "openai",
            },
        ),
        patch(
            "tools.delegation_live_log.create_live_transcripts",
            return_value=(None, [], []),
        ),
        patch("tools.delegate_tool._build_child_agent", side_effect=children),
        patch("gateway.session_context.async_delivery_supported", return_value=True),
        patch("tools.approval.get_current_session_key", return_value="parent-session"),
        patch("tools.async_delegation.dispatch_async_delegation_batch", side_effect=dispatch),
    ):
        result = delegate_task(
            tasks=[{"goal": "one"}, {"goal": "two"}],
            profile="isolated",
            background=True,
            parent_agent=parent,
        )

    try:
        assert '"status": "dispatched"' in result
        assert set(captured_mapping) == {"logical-a", "logical-b"}
        assert set(captured_authority) == set(captured_mapping)
        assert len(set(captured_mapping.values())) == 2
        assert [call.args[0] for call in prepare_idmaps.call_args_list] == list(
            captured_mapping.values()
        )
        for child in children:
            attempt_id = child._delegation_attempt_id
            assert attempt_id == captured_mapping[child._subagent_id]
            authority = captured_authority[child._subagent_id]
            assert authority["version"] == 1
            assert authority["lineage"]["attempt_id"] == attempt_id
            assert authority["lineage"]["scope_id"] == child._delegation_scope_id
            assert child._current_task_id == attempt_id
            assert terminal_tool._resolve_container_task_id(attempt_id) == attempt_id
            assert registry.get(attempt_id).state == "active"
    finally:
        registry.rollback(captured_mapping.values())


def test_child_completion_cleans_resources_by_physical_attempt_identity():
    registry = AttemptScopeRegistry()
    scope = cast(Any, SimpleNamespace(profile_name="isolated"))
    authority = registry.reserve(scope, "logical-child", attempt_id="attempt-physical")
    cleaned: list[str] = []
    registry.add_resource(
        authority.attempt_id, "sentinel", lambda: cleaned.append("attempt-physical")
    )
    registry.activate(authority.attempt_id)
    parent = SimpleNamespace(_current_task_id=None)
    child = MagicMock()
    child._subagent_id = "logical-child"
    child._delegation_attempt_id = authority.attempt_id
    child._delegation_run_id = None
    child._delegate_depth = 1
    child._delegate_role = "leaf"
    child._parent_subagent_id = None
    child._delegation_runtime_metadata = {}
    child._credential_pool = None
    child.tool_progress_callback = None
    child.model = "test-model"
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "api_calls": 1,
    }

    with (
        patch("tools.delegation_scope.attempt_scope_registry", registry),
        patch("tools.delegate_tool._register_subagent"),
        patch("tools.delegate_tool._unregister_subagent"),
        patch("tools.terminal_tool.get_session_cwd", return_value="/tmp"),
        patch("tools.terminal_tool.record_session_cwd"),
    ):
        result = _run_single_child(0, "goal", child=child, parent_agent=parent)

    assert result["status"] == "completed"
    assert cleaned == ["attempt-physical"]
    assert registry.get(authority.attempt_id).state == "cleaned"


def test_partial_protected_child_construction_rolls_back_before_transcripts_or_start():
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/isolated@sha256:deadbeef",
        default_workdir="/work",
        allowed_toolsets=frozenset({"terminal"}),
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=(),
    )
    scope = ResolvedInvocationScope(
        profile_name="isolated",
        profile_hash=execution_profile_hash(profile),
        profile=profile,
        workdir=PurePosixPath("/work"),
        reveal=(),
        visible_objects=(),
    )
    parent = SimpleNamespace(
        delegation_policy=policy,
        delegation_backing_registry=None,
        _delegate_depth=0,
    )
    first_child = MagicMock()
    first_child._subagent_id = "logical-a"

    with (
        patch("tools.delegation_scope.resolve_invocation_scope", return_value=scope),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value={
                "provider": "test",
                "model": "test-model",
                "base_url": "http://localhost/v1",
                "api_key": "test-key",
                "api_mode": "openai",
            },
        ),
        patch(
            "tools.delegate_tool._build_child_agent",
            side_effect=[first_child, RuntimeError("second child failed")],
        ),
        patch("tools.delegation_live_log.create_live_transcripts") as live_logs,
        patch("tools.async_delegation.dispatch_async_delegation_batch") as dispatch,
    ):
        result = delegate_task(
            tasks=[{"goal": "one"}, {"goal": "two"}],
            profile="isolated",
            parent_agent=parent,
        )

    assert "protected child construction failed" in result
    first_child.close.assert_called_once()
    live_logs.assert_not_called()
    dispatch.assert_not_called()


def test_protected_persistence_failure_revokes_attempts_and_runs_no_child():
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/isolated@sha256:deadbeef",
        default_workdir="/work",
        allowed_toolsets=frozenset({"terminal"}),
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=(),
    )
    scope = ResolvedInvocationScope(
        profile_name="isolated",
        profile_hash=execution_profile_hash(profile),
        profile=profile,
        workdir=PurePosixPath("/work"),
        reveal=(),
        visible_objects=(),
    )
    parent = SimpleNamespace(
        delegation_policy=policy,
        delegation_backing_registry=None,
        _delegate_depth=0,
        session_id="parent-session",
    )
    child = MagicMock()
    child._subagent_id = "logical-a"
    registry = AttemptScopeRegistry()
    submitted = MagicMock()

    with (
        patch("tools.delegation_scope.resolve_invocation_scope", return_value=scope),
        patch("tools.delegation_scope.attempt_scope_registry", registry),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value={
                "provider": "test",
                "model": "test-model",
                "base_url": "http://localhost/v1",
                "api_key": "test-key",
                "api_mode": "openai",
            },
        ),
        patch(
            "tools.delegation_live_log.create_live_transcripts",
            return_value=(None, [], []),
        ),
        patch("tools.delegate_tool._build_child_agent", return_value=child),
        patch("gateway.session_context.async_delivery_supported", return_value=True),
        patch("tools.approval.get_current_session_key", return_value="parent-session"),
        patch(
            "tools.async_delegation.dispatch_async_delegation_batch",
            return_value={
                "status": "rejected",
                "reason": "dispatch_setup_failed",
                "error": "persistence failed",
            },
        ),
        patch("tools.delegate_tool._run_single_child", submitted),
    ):
        result = delegate_task(
            goal="never run",
            profile="isolated",
            background=True,
            parent_agent=parent,
        )

    assert "persistence failed" in result
    submitted.assert_not_called()
    child.close.assert_called_once()
    authority = registry.get(child._delegation_attempt_id)
    assert authority is not None
    assert authority.state == "cleaned"


def test_protected_orchestrator_policy_and_toolsets_derive_only_from_effective_scope():
    selected = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/isolated@sha256:deadbeef",
        default_workdir="/work",
        allowed_toolsets=frozenset({"terminal", "delegation"}),
    )
    omitted_profile = ExecutionProfile(
        name="other",
        backend="docker",
        image="repo/other@sha256:cafebabe",
        default_workdir="/other",
        allowed_toolsets=frozenset({"terminal", "web", "delegation"}),
    )
    grant_a = VisibleObjectGrant(
        visible_path="/project/a",
        backing=BackingObjectRef(
            object_id="object-a", kind="host_path", identity="id-a", revision="1"
        ),
        mode=AccessMode.RW,
        object_type="directory",
    )
    grant_b = VisibleObjectGrant(
        visible_path="/project/b",
        backing=BackingObjectRef(
            object_id="object-b", kind="host_path", identity="id-b", revision="1"
        ),
        mode=AccessMode.RW,
        object_type="directory",
    )
    parent_policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated", "other"},
        profile_snapshots={"isolated": selected, "other": omitted_profile},
        visible_objects=(grant_a, grant_b),
        protected_prefixes=(),
    )
    effective_grant = VisibleObjectGrant(
        visible_path="/project/a",
        backing=grant_a.backing,
        mode=AccessMode.RO,
        object_type="directory",
    )
    scope = ResolvedInvocationScope(
        profile_name="isolated",
        profile_hash="profile-hash",
        profile=selected,
        workdir=PurePosixPath("/work"),
        reveal=(),
        visible_objects=(effective_grant,),
    )
    parent = MagicMock()
    parent.delegation_policy = parent_policy
    parent.delegation_backing_registry = object()
    parent.enabled_toolsets = ["terminal", "web", "delegation"]
    parent.disabled_toolsets = []
    parent._delegate_depth = 0
    parent._session_db = None
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = ""
    parent.request_overrides = {}

    with patch("tools.delegate_tool._get_max_spawn_depth", return_value=3), patch(
        "run_agent.AIAgent"
    ) as agent_cls:
        child = MagicMock()
        agent_cls.return_value = child
        from tools.delegate_tool import _build_child_agent

        _build_child_agent(
            task_index=0,
            goal="orchestrate safely",
            context=None,
            toolsets=None,
            model="test-model",
            max_iterations=3,
            task_count=1,
            parent_agent=parent,
            role="orchestrator",
            resolved_scope=scope,
        )

    kwargs = agent_cls.call_args.kwargs
    child_policy = kwargs["delegation_policy"]
    assert child_policy.allowed_profiles == frozenset({"isolated"})
    assert set(child_policy.profile_snapshots) == {"isolated"}
    assert child_policy.visible_objects == (effective_grant,)
    assert set(kwargs["enabled_toolsets"]) == {"terminal", "delegation"}
    assert child.delegation_backing_registry is parent.delegation_backing_registry


def test_synchronous_nested_child_uses_fresh_physical_identity_for_run_and_cleanup():
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="repo/isolated@sha256:deadbeef",
        default_workdir="/work",
        allowed_toolsets=frozenset({"terminal", "delegation"}),
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=(),
    )
    parent = SimpleNamespace(
        delegation_policy=policy,
        delegation_backing_registry=None,
        _delegate_depth=1,
        _delegation_attempt_id="attempt-parent",
        _delegation_scope_id="scope-parent",
        session_id="parent-session",
    )
    grandchild = MagicMock()
    grandchild._subagent_id = "sa-grandchild"
    grandchild._active_children = []
    grandchild._active_children_lock = threading.Lock()
    run_task_ids = []

    def run_conversation(*_args, **kwargs):
        run_task_ids.append(kwargs["task_id"])
        return {"final_response": "nested complete"}

    grandchild.run_conversation.side_effect = run_conversation
    registry = AttemptScopeRegistry()
    terminal_tool._task_env_overrides.clear()

    with patch("tools.delegation_scope.attempt_scope_registry", registry), patch(
        "tools.delegate_tool._build_child_agent", return_value=grandchild
    ), patch(
        "tools.delegate_tool._load_config", return_value={}
    ), patch(
        "tools.delegation_live_log.create_live_transcripts",
        return_value=(None, [None], []),
    ), patch(
        "tools.delegation_live_log.update_manifest_statuses"
    ), patch(
        "tools.delegate_tool._get_max_spawn_depth", return_value=4
    ):
        result = json.loads(
            delegate_task(
                goal="nested protected work",
                parent_agent=parent,
                background=False,
                role="leaf",
                profile="isolated",
            )
        )

    assert result["results"][0]["status"] == "completed"
    assert len(run_task_ids) == 1
    attempt_id = grandchild._delegation_attempt_id
    assert attempt_id == run_task_ids[0]
    assert attempt_id != parent._delegation_attempt_id
    assert grandchild._current_task_id == attempt_id
    authority = registry.get(attempt_id)
    assert authority is not None
    assert authority.state == "cleaned"
    assert attempt_id not in terminal_tool._task_env_overrides
