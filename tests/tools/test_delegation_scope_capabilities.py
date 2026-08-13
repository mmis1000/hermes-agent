from types import SimpleNamespace

from tools import mcp_tool


def _definition(name):
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object"}},
    }


def test_protected_admission_excludes_tool_from_unqualified_mcp_server(monkeypatch):
    import model_tools
    from tools import delegate_tool

    monkeypatch.setattr(
        model_tools,
        "get_toolset_for_tool",
        lambda name: "mcp-shared" if name == "mcp_untrusted_read" else "terminal",
    )
    monkeypatch.setattr(
        mcp_tool,
        "get_mcp_tool_server_qualification",
        lambda name: "untrusted" if name == "mcp_untrusted_read" else None,
        raising=False,
    )

    admitted = delegate_tool._qualified_protected_tool_names(
        {"terminal", "mcp_untrusted_read"},
        {"terminal", "mcp-shared"},
        frozenset({"trusted"}),
    )

    assert admitted == {"terminal"}


def test_protected_admission_includes_tool_from_exact_qualified_mcp_server(monkeypatch):
    import model_tools
    from tools import delegate_tool

    monkeypatch.setattr(model_tools, "get_toolset_for_tool", lambda _name: "mcp-safe")
    monkeypatch.setattr(
        mcp_tool,
        "get_mcp_tool_server_qualification",
        lambda _name: "safe-server",
    )

    admitted = delegate_tool._qualified_protected_tool_names(
        {"mcp_safe_read"},
        {"mcp-safe"},
        frozenset({"safe-server"}),
    )

    assert admitted == {"mcp_safe_read"}


def test_protected_positive_tool_snapshot_excludes_late_unclassified_tool(monkeypatch):
    import model_tools
    from tools.registry import registry

    safe = _definition("terminal")
    unknown = _definition("new_host_global_tool")
    agent = SimpleNamespace(
        enabled_toolsets=["terminal"],
        disabled_toolsets=[],
        tools=[safe],
        valid_tool_names={"terminal"},
        _protected_tool_snapshot=frozenset({"terminal"}),
        _tool_snapshot_generation=-1,
        _context_engine_tool_names=set(),
        context_compressor=None,
    )
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: [safe, unknown],
    )
    monkeypatch.setattr(registry, "_generation", 100)

    added = mcp_tool.refresh_agent_mcp_tools(agent)

    assert added == set()
    assert agent.valid_tool_names == {"terminal"}
    assert [item["function"]["name"] for item in agent.tools] == ["terminal"]


def test_protected_refresh_excludes_snapshotted_name_from_unqualified_mcp_server(monkeypatch):
    import model_tools
    from tools.registry import registry

    safe = _definition("terminal")
    replaced = _definition("mcp_shared_read")
    agent = SimpleNamespace(
        enabled_toolsets=["terminal", "mcp-shared"],
        disabled_toolsets=[],
        tools=[safe, replaced],
        valid_tool_names={"terminal", "mcp_shared_read"},
        _protected_tool_snapshot=frozenset({"terminal", "mcp_shared_read"}),
        _protected_qualified_mcp_servers=frozenset({"trusted"}),
        _tool_snapshot_generation=-1,
        _context_engine_tool_names=set(),
        context_compressor=None,
    )
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: [safe, replaced],
    )
    monkeypatch.setattr(
        mcp_tool,
        "get_mcp_tool_server_qualification",
        lambda name: "untrusted" if name == "mcp_shared_read" else None,
    )
    monkeypatch.setattr(registry, "_generation", 101)

    mcp_tool.refresh_agent_mcp_tools(agent)

    assert agent.valid_tool_names == {"terminal"}
    assert [item["function"]["name"] for item in agent.tools] == ["terminal"]


def test_protected_web_extract_stores_overflow_in_attempt_storage(monkeypatch):
    from pathlib import PurePosixPath
    from agent.delegation_policy import ExecutionProfile
    from tools import web_tools
    from tools.delegation_scope import (
        ResolvedInvocationScope, attempt_scope_registry, execution_profile_hash,
    )

    profile = ExecutionProfile(
        "protected", "docker", "repo/protected@sha256:deadbeef", "/workspace",
        frozenset({"web", "file"}),
    )
    scope = ResolvedInvocationScope(
        profile.name, execution_profile_hash(profile), profile,
        PurePosixPath("/workspace"), (), (),
    )
    authority = attempt_scope_registry.reserve(
        scope, "logical-web", attempt_id="attempt-web"
    )
    writes = []

    class ScopedOps:
        def write_file(self, path, content):
            writes.append((path, content))
            return SimpleNamespace(error=None)

    monkeypatch.setattr("tools.file_tools._get_file_ops", lambda _task_id: ScopedOps())

    try:
        text, truncated = web_tools._truncate_with_footer(
            "line\n" * 100, "https://example.com/page", 40,
            task_id=authority.attempt_id,
        )
    finally:
        attempt_scope_registry.cleanup(authority.attempt_id)

    assert truncated is True
    assert writes and writes[0][0].startswith("/workspace/.hermes-web/")
    assert writes[0][0] in text
