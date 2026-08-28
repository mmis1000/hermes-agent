from __future__ import annotations

import json
import sys
import types
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest


def _stub_optional_http_modules() -> None:
    """Keep this focused unit file runnable without optional HTTP extras."""
    try:
        import requests  # noqa: F401
    except ImportError:
        requests = types.ModuleType("requests")
        requests.Session = object
        requests.request = lambda *args, **kwargs: None
        requests.exceptions = types.SimpleNamespace(RequestException=Exception)
        sys.modules["requests"] = requests
    try:
        import httpx  # noqa: F401
    except ImportError:
        httpx = types.ModuleType("httpx")
        httpx.Client = object
        httpx.AsyncClient = object
        httpx.Timeout = object
        httpx.TimeoutException = TimeoutError
        httpx.RequestError = Exception
        httpx.HTTPStatusError = Exception
        sys.modules["httpx"] = httpx

from agent.delegation_policy import ExecutionProfile


_DEFERRED_NAMES = {"present_artifacts", "x_search"}
_TOOLSETS = {
    "present_artifacts": "mcp-api-server",
    "x_search": "mcp-x-search",
    "terminal": "terminal",
}


def _definition(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


@pytest.fixture
def tool_surface(monkeypatch):
    import model_tools
    from tools import mcp_tool, tool_search

    definitions = [
        _definition("terminal", "Run a shell command directly."),
        _definition("present_artifacts", "Present UI archive artifacts."),
        _definition("x_search", "Search public X posts."),
    ]

    monkeypatch.setattr(
        model_tools,
        "get_toolset_for_tool",
        lambda name: _TOOLSETS.get(name),
    )
    monkeypatch.setattr(
        tool_search,
        "is_deferrable_tool_name",
        lambda name: name in _DEFERRED_NAMES,
    )
    monkeypatch.setattr(
        mcp_tool,
        "get_mcp_tool_server_qualification",
        lambda name: {
            "present_artifacts": "api-server",
            "x_search": "x-search",
        }.get(name),
    )
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: list(definitions),
    )
    monkeypatch.setattr(model_tools, "_resolve_active_context_length", lambda: 200_000)
    monkeypatch.setattr(
        tool_search,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw(
            {"enabled": "on", "listing": "on"}
        ),
    )
    return definitions


def _profile(*, allowed_toolsets=(), allowed_tools=()):
    return ExecutionProfile(
        name="protected",
        backend="docker",
        image="example/protected@sha256:abc",
        default_workdir=PurePosixPath("/workspace"),
        allowed_toolsets=frozenset(allowed_toolsets),
        allowed_tools=frozenset(allowed_tools),
        qualified_mcp_servers=frozenset({"api-server"}),
    )


def _agent(definitions):
    from tools.tool_search import ToolSearchConfig, assemble_tool_defs

    assembled = assemble_tool_defs(
        definitions,
        context_length=200_000,
        config=ToolSearchConfig.from_raw({"enabled": "on", "listing": "on"}),
    )
    return SimpleNamespace(
        enabled_toolsets=["mcp-api-server", "mcp-x-search", "terminal"],
        disabled_toolsets=[],
        tools=assembled.tool_defs,
        valid_tool_names={
            item["function"]["name"] for item in assembled.tool_defs
        },
    )


def _names(agent):
    return {item["function"]["name"] for item in agent.tools}


def test_exact_tool_profile_keeps_bridge_as_authorized_carrier_only(tool_surface):
    _stub_optional_http_modules()
    import model_tools
    from tools import delegate_tool
    from tools.registry import registry
    from tools.tool_search import BRIDGE_TOOL_NAMES

    agent = _agent(tool_surface)
    delegate_tool.configure_protected_agent_tools(
        agent,
        _profile(allowed_tools={"present_artifacts"}),
    )

    assert BRIDGE_TOOL_NAMES <= _names(agent)
    assert "terminal" not in _names(agent)
    assert "x_search" not in _names(agent)
    search_schema = next(
        item["function"] for item in agent.tools
        if item["function"]["name"] == "tool_search"
    )
    assert "present_artifacts" in search_schema["description"]
    assert "x_search" not in search_schema["description"]
    assert agent._protected_deferred_tool_snapshot == frozenset({"present_artifacts"})

    searched = json.loads(
        model_tools.handle_function_call(
            "tool_search",
            {"query": "archive artifacts"},
            enabled_toolsets=agent.enabled_toolsets,
            disabled_toolsets=agent.disabled_toolsets,
            parent_agent=agent,
        )
    )
    assert searched["total_available"] == 1
    assert [hit["name"] for hit in searched["matches"]] == ["present_artifacts"]

    described = json.loads(
        model_tools.handle_function_call(
            "tool_describe",
            {"name": "present_artifacts"},
            enabled_toolsets=agent.enabled_toolsets,
            parent_agent=agent,
        )
    )
    assert described["name"] == "present_artifacts"

    denied_description = json.loads(
        model_tools.handle_function_call(
            "tool_describe",
            {"name": "x_search"},
            enabled_toolsets=agent.enabled_toolsets,
            parent_agent=agent,
        )
    )
    assert "error" in denied_description

    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        registry,
        "dispatch",
        lambda name, args, **_kwargs: calls.append((name, args)) or json.dumps({"ok": True}),
    )
    try:
        invoked = json.loads(
            model_tools.handle_function_call(
                "tool_call",
                {
                    "name": "present_artifacts",
                    "arguments": {"query": "archive"},
                },
                enabled_toolsets=agent.enabled_toolsets,
                parent_agent=agent,
            )
        )
        denied = json.loads(
            model_tools.handle_function_call(
                "tool_call",
                {"name": "x_search", "arguments": {"query": "posts"}},
                enabled_toolsets=agent.enabled_toolsets,
                parent_agent=agent,
            )
        )
    finally:
        monkeypatch.undo()

    assert invoked == {"ok": True}
    assert "error" in denied
    assert calls == [("present_artifacts", {"query": "archive"})]


def test_whole_toolset_profile_filters_bridge_catalog_to_that_toolset(tool_surface):
    _stub_optional_http_modules()
    from tools import delegate_tool
    from tools.tool_search import BRIDGE_TOOL_NAMES

    agent = _agent(tool_surface)
    delegate_tool.configure_protected_agent_tools(
        agent,
        _profile(allowed_toolsets={"mcp-api-server"}),
    )

    assert BRIDGE_TOOL_NAMES <= _names(agent)
    assert "x_search" not in _names(agent)
    assert agent._protected_deferred_tool_snapshot == frozenset({"present_artifacts"})


def test_profile_without_authorized_deferred_tool_drops_empty_bridge(tool_surface):
    _stub_optional_http_modules()
    from tools import delegate_tool
    from tools.tool_search import BRIDGE_TOOL_NAMES

    agent = _agent(tool_surface)
    delegate_tool.configure_protected_agent_tools(
        agent,
        _profile(allowed_toolsets={"terminal"}),
    )

    assert _names(agent) == {"terminal"}
    assert not (_names(agent) & BRIDGE_TOOL_NAMES)
    assert agent._protected_deferred_tool_snapshot == frozenset()


def test_unprotected_tool_search_still_catalogs_both_deferred_tools(tool_surface):
    from tools.tool_search import ToolSearchConfig, assemble_tool_defs

    result = assemble_tool_defs(
        tool_surface,
        context_length=200_000,
        config=ToolSearchConfig.from_raw({"enabled": "on", "listing": "on"}),
    )
    names = {item["function"]["name"] for item in result.tool_defs}

    assert {"terminal", "tool_search", "tool_describe", "tool_call"} <= names
    search_schema = next(
        item["function"] for item in result.tool_defs
        if item["function"]["name"] == "tool_search"
    )
    assert "present_artifacts" in search_schema["description"]
    assert "x_search" in search_schema["description"]
    assert result.deferred_count == 2


def test_executor_scope_helper_honors_protected_deferred_snapshot(tool_surface):
    _stub_optional_http_modules()
    from agent.tool_executor import _tool_search_scoped_names
    from tools import delegate_tool

    agent = _agent(tool_surface)
    delegate_tool.configure_protected_agent_tools(
        agent,
        _profile(allowed_tools={"present_artifacts"}),
    )

    assert _tool_search_scoped_names(agent) == frozenset({"present_artifacts"})
