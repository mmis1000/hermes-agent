"""Focused admission tests for new delegate_task dispatches."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.delegate_tool import _delegation_route_for_admission, delegate_task


def _parent():
    return SimpleNamespace(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="account-token",
        provider="openai-codex",
        api_mode="codex_responses",
        model="gpt-5.6-luna",
        platform="cli",
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection=None,
        openrouter_min_coding_score=None,
        _session_db=None,
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _print_fn=None,
        tool_progress_callback=None,
        thinking_callback=None,
    )


def _codex_credentials(model="gpt-6-luna"):
    return {
        "model": model,
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "account-token",
        "api_mode": "codex_responses",
    }


def test_new_batch_rejects_unsupported_route_before_any_child_starts():
    """All children stay unbuilt when the selected account rejects the route."""
    parent = _parent()
    tasks = [{"goal": "Investigate route A"}, {"goal": "Investigate route B"}]

    with (
        patch(
            "tools.delegate_tool._load_config",
            return_value={"max_iterations": 50, "max_concurrent_children": 3},
        ),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_codex_credentials(),
        ),
        patch(
            "hermes_cli.models.cached_provider_model_ids",
            side_effect=[[], []],
        ) as catalog,
        patch("tools.delegate_tool._build_child_agent") as build_child,
        patch("tools.delegate_tool._run_single_child") as run_child,
        patch("tools.delegation_live_log.create_live_transcripts") as transcripts,
    ):
        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))

    assert "error" in result
    assert "admission rejected" in result["error"].lower()
    assert "gpt-6-luna" in result["error"]
    assert "openai-codex" in result["error"]
    assert catalog.call_count == 2
    assert catalog.call_args_list[0].kwargs == {
        "force_refresh": False,
        "cache_only": True,
        "require_verified": True,
        "api_key": "account-token",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }
    assert catalog.call_args_list[1].kwargs == {
        "force_refresh": True,
        "cache_only": False,
        "require_verified": True,
        "api_key": "account-token",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }
    build_child.assert_not_called()
    run_child.assert_not_called()
    transcripts.assert_not_called()


def test_known_catalog_model_is_rejected_when_codex_account_catalog_is_unavailable():
    """A static Codex picker entry cannot bypass account-scoped admission."""
    parent = _parent()
    credentials = _codex_credentials(model="gpt-5.6-luna")

    with (
        patch(
            "tools.delegate_tool._load_config",
            return_value={"max_iterations": 50},
        ),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=credentials,
        ),
        patch(
            "hermes_cli.models.cached_provider_model_ids",
            side_effect=[[], []],
        ),
        patch("tools.delegate_tool._build_child_agent") as build_child,
        patch("tools.delegate_tool._run_single_child") as run_child,
    ):
        result = json.loads(
            delegate_task(goal="Do not start this child", parent_agent=parent)
        )

    assert "error" in result
    assert "catalog" in result["error"].lower()
    assert "unavailable" in result["error"].lower()
    build_child.assert_not_called()
    run_child.assert_not_called()


def test_existing_task_validation_precedes_route_admission():
    """Malformed task input keeps its existing error and never probes a route."""
    parent = _parent()
    with (
        patch("tools.delegate_tool._resolve_delegation_credentials") as credentials,
        patch("hermes_cli.models.cached_provider_model_ids") as catalog,
    ):
        result = json.loads(
            delegate_task(
                tasks=[{"context": "missing the required goal"}],
                parent_agent=parent,
            )
        )

    assert "missing a 'goal'" in result["error"]
    credentials.assert_not_called()
    catalog.assert_not_called()


def test_callable_parent_credential_is_materialized_for_selected_admission_route():
    parent = _parent()
    calls = []

    def selected_token_provider():
        calls.append(True)
        return "selected-route-token"

    parent.api_key = selected_token_provider
    route = _delegation_route_for_admission(
        {
            "model": "gpt-5.6-luna",
            "provider": "openai-codex",
            "base_url": "https://selected-codex.invalid/backend-api/codex",
            "api_key": None,
            "api_mode": "codex_responses",
        },
        parent,
    )

    assert route["api_key"] == "selected-route-token"
    assert route["base_url"] == "https://selected-codex.invalid/backend-api/codex"
    assert calls == [True]


def test_failed_callable_credential_rejects_without_ambient_fallback():
    parent = _parent()
    parent.api_key = "ambient-token-must-not-be-used"

    def failed_token_provider():
        raise RuntimeError("selected token service unavailable")

    with pytest.raises(ValueError, match="ambient credential fallback is not permitted"):
        _delegation_route_for_admission(
            {
                "model": "gpt-5.6-luna",
                "provider": "openai-codex",
                "base_url": "https://selected-codex.invalid/backend-api/codex",
                "api_key": failed_token_provider,
                "api_mode": "codex_responses",
            },
            parent,
        )
