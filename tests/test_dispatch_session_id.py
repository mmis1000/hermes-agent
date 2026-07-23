"""Tests that handle_function_call forwards session_id into registry.dispatch."""

import json
from unittest.mock import MagicMock, patch


def _make_registry(captured: dict):
    """Return a mock registry whose dispatch records the kwargs it receives."""
    registry = MagicMock()

    def _dispatch(name, args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"result": "ok"})

    registry.dispatch.side_effect = _dispatch
    return registry


class TestSessionIdForwarding:

    def test_standard_path_forwards_session_id(self):
        """registry.dispatch receives session_id on the normal tool path."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                session_id="sess-abc",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("session_id") == "sess-abc"

    def test_execute_code_path_forwards_session_id(self):
        """registry.dispatch receives session_id on the execute_code path."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "execute_code",
                {"code": "print(1)"},
                task_id="t1",
                session_id="sess-xyz",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("session_id") == "sess-xyz"

    def test_session_id_default_is_none(self):
        """When session_id is omitted, dispatch receives None."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                skip_pre_tool_call_hook=True,
            )
        assert "session_id" in captured
        assert captured["session_id"] is None

    def test_parent_agent_forwarded_for_stateful_agent_tools(self):
        """Registry handlers such as delegation resume receive the live caller."""
        captured = {}
        parent = object()
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "delegation",
                {"action": "resume", "delegation_id": "d", "subagent_id": "sa", "message": "next"},
                task_id="task-parent",
                session_id="sess-parent",
                parent_agent=parent,
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("parent_agent") is parent

    def test_tool_search_bridge_preserves_parent_agent(self, monkeypatch):
        """Deferred stateful tools receive the live caller after bridge unwrap."""
        import model_tools

        parent = object()
        captured = {}
        original = model_tools.handle_function_call
        monkeypatch.setattr(
            model_tools,
            "get_tool_definitions",
            lambda **kwargs: [
                {
                    "type": "function",
                    "function": {
                        "name": "delegation",
                        "description": "stateful test tool",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
        monkeypatch.setattr(
            "tools.tool_search.resolve_underlying_call",
            lambda args: ("delegation", {"action": "list"}, None),
        )
        monkeypatch.setattr(
            "tools.tool_search.scoped_deferrable_names",
            lambda defs: {"delegation"},
        )

        def recording_dispatch(**kwargs):
            if kwargs.get("function_name") == "delegation":
                captured.update(kwargs)
                return '{"ok": true}'
            return original(**kwargs)

        monkeypatch.setattr(model_tools, "handle_function_call", recording_dispatch)
        result = original(
            function_name="tool_call",
            function_args={"name": "delegation", "arguments": {"action": "list"}},
            parent_agent=parent,
        )

        assert json.loads(result) == {"ok": True}
        assert captured["parent_agent"] is parent

    def test_task_id_still_forwarded(self):
        """Existing task_id forwarding is not broken by this change."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="task-999",
                session_id="sess-1",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("task_id") == "task-999"
