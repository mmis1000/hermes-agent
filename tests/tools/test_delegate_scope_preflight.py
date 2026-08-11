from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from agent.delegation_policy import DelegationSessionPolicy, ExecutionProfile
from tools.delegate_tool import delegate_task
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.delegation_scope import ResolvedInvocationScope


def _policy() -> DelegationSessionPolicy:
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example@sha256:abc",
        default_workdir=PurePosixPath("/workspace"),
        allowed_toolsets=frozenset({"terminal"}),
    )
    return DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=frozenset({"isolated"}),
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=(),
    )


def _parent():
    return SimpleNamespace(
        delegation_policy=_policy(),
        delegation_backing_registry=None,
        _delegate_depth=0,
    )


def test_ordinary_omission_bypasses_scope_resolver_and_keeps_free_form_input():
    parent = SimpleNamespace(delegation_policy=None, _delegate_depth=0)
    with (
        patch("tools.delegation_scope.resolve_invocation_scope") as resolve,
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            side_effect=ValueError("legacy credential sentinel"),
        ) as credentials,
    ):
        result = delegate_task(
            goal="Use /host/source as ordinary prose, not authority",
            context="raw image and volume words remain generic free-form context",
            parent_agent=parent,
        )

    assert "legacy credential sentinel" in result
    resolve.assert_not_called()
    credentials.assert_called_once()


def test_invalid_batch_scope_preflight_starts_no_siblings_or_side_effects():
    parent = _parent()
    with (
        patch("tools.delegate_tool._resolve_delegation_credentials") as credentials,
        patch("tools.delegation_live_log.create_live_transcripts") as live_logs,
        patch("tools.delegate_tool._build_child_agent") as build_child,
        patch.object(DaemonThreadPoolExecutor, "submit") as submit,
        patch("tools.async_delegation.dispatch_async_delegation_batch") as dispatch,
    ):
        result = delegate_task(
            tasks=[
                {"goal": "first sibling", "context": "plain prose"},
                {"goal": "second sibling", "context": "plain prose"},
            ],
            profile="missing",
            parent_agent=parent,
        )

    assert "unknown execution profile" in result
    credentials.assert_not_called()
    live_logs.assert_not_called()
    build_child.assert_not_called()
    submit.assert_not_called()
    dispatch.assert_not_called()


def test_valid_batch_resolves_scope_once_and_passes_same_template_to_every_child():
    parent = _parent()
    parent.session_id = "parent-session"
    profile = parent.delegation_policy.profile_snapshots["isolated"]
    scope = ResolvedInvocationScope(
        profile_name="isolated",
        profile_hash="profile-hash",
        profile=profile,
        workdir=PurePosixPath("/workspace"),
        reveal=(),
        visible_objects=(),
    )
    children = [
        SimpleNamespace(_subagent_id="child-one"),
        SimpleNamespace(_subagent_id="child-two"),
    ]
    credentials = {
        "provider": "test-provider",
        "model": "test-model",
        "base_url": "http://localhost/v1",
        "api_key": "test-key",
        "api_mode": "openai",
    }
    with (
        patch(
            "tools.delegation_scope.resolve_invocation_scope", return_value=scope
        ) as resolve,
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=credentials,
        ),
        patch(
            "tools.delegation_live_log.create_live_transcripts",
            return_value=(None, [], []),
        ),
        patch("tools.delegate_tool._build_child_agent", side_effect=children) as build_child,
        patch("gateway.session_context.async_delivery_supported", return_value=True),
        patch("tools.approval.get_current_session_key", return_value="parent-session"),
        patch(
            "tools.async_delegation.dispatch_async_delegation_batch",
            return_value={"status": "dispatched", "delegation_id": "batch-id"},
        ),
    ):
        result = delegate_task(
            tasks=[{"goal": "one"}, {"goal": "two", "context": "plain prose"}],
            profile="isolated",
            background=True,
            parent_agent=parent,
        )

    assert '"status": "dispatched"' in result
    resolve.assert_called_once()
    assert build_child.call_count == 2
    for call in build_child.call_args_list:
        assert call.kwargs["resolved_scope"] is scope
    assert [call.kwargs["goal"] for call in build_child.call_args_list] == ["one", "two"]
    assert build_child.call_args_list[1].kwargs["context"] == "plain prose"
