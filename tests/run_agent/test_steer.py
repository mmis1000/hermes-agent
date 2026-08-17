"""Tests for AIAgent.steer() — mid-run user message injection.

/steer lets the user add a note to the agent's next tool result without
interrupting the current tool call. The agent sees the note inline with
tool output on its next iteration, preserving message-role alternation
and prompt-cache integrity.
"""
from __future__ import annotations

import json
import threading

import pytest

from agent.prompt_builder import STEER_MARKER_OPEN, format_steer_marker
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    """Build an AIAgent without running __init__, then install the steer
    state manually — matches the existing object.__new__ stub pattern
    used elsewhere in the test suite.
    """
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    return agent


class TestSteerAcceptance:
    def test_tool_wait_context_registers_only_supported_foreground_calls(self):
        from tools.foreground_wait import (
            ForegroundWaitRegistry,
            current_foreground_wait,
            track_foreground_wait,
        )

        class Agent:
            _foreground_waits = ForegroundWaitRegistry()

        agent = Agent()
        with track_foreground_wait(
            agent, "call-terminal", "terminal", {"background": False}
        ) as slot:
            assert slot is current_foreground_wait()
            assert [item.kind for item in agent._foreground_waits.snapshot()] == [
                "terminal"
            ]
        assert agent._foreground_waits.snapshot() == []

        with track_foreground_wait(
            agent, "call-background", "terminal", {"background": True}
        ) as slot:
            assert slot is None
        with track_foreground_wait(
            agent, "call-wait", "delegation", {"action": "wait"}
        ) as slot:
            assert slot is not None
            assert slot.kind == "delegation"

    def test_concurrent_terminal_worker_is_visible_to_ordinary_durable_steer(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from tests.run_agent.test_tool_call_guardrail_runtime import (
            _make_agent,
            _mock_tool_call,
        )
        from tools.foreground_wait import ForegroundWaitRegistry

        agent = _make_agent("terminal")
        agent._foreground_waits = ForegroundWaitRegistry()
        started = threading.Event()
        release = threading.Event()

        def blocking_invoke(*args, **kwargs):
            del args, kwargs
            started.set()
            assert release.wait(2)
            return json.dumps({"output": "done", "exit_code": 0})

        agent._invoke_tool = MagicMock(side_effect=blocking_invoke)
        assistant_message = SimpleNamespace(
            content="",
            tool_calls=[
                _mock_tool_call(
                    "terminal",
                    json.dumps({"command": "sleep 30", "background": False}),
                    "call-concurrent-terminal",
                )
            ],
        )
        errors = []

        def execute():
            try:
                agent._execute_tool_calls_concurrent(
                    assistant_message, [], "task-concurrent"
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        try:
            assert started.wait(2)
            outcomes = []
            result = agent.request_durable_steer(
                "ordinary guidance",
                mailbox_id="mail-concurrent",
                outcome_callback=outcomes.append,
            )

            assert result == {
                "status": "foreground_wait",
                "wait_kinds": ["terminal"],
            }
            assert outcomes == ["foreground_wait"]
            assert agent._drain_pending_steer_envelopes() == []
        finally:
            release.set()
            thread.join(3)
        assert not thread.is_alive()
        assert errors == []

    def test_durable_steer_refuses_active_foreground_wait_without_queueing(self):
        from tools.foreground_wait import ForegroundWaitRegistry

        agent = _bare_agent()
        agent._foreground_waits = ForegroundWaitRegistry()
        slot = agent._foreground_waits.register("call-terminal", "terminal")
        outcomes = []

        result = agent.request_durable_steer(
            "change direction",
            mailbox_id="mail-blocked",
            outcome_callback=outcomes.append,
            force=False,
        )

        assert result == {
            "status": "foreground_wait",
            "wait_kinds": ["terminal"],
        }
        assert outcomes == ["foreground_wait"]
        assert agent._pending_steer is None
        agent._foreground_waits.unregister(slot)

    def test_forced_durable_steer_waits_for_handoff_then_queues_exact_envelope(self):
        from tools.foreground_wait import ForegroundWaitRegistry

        agent = _bare_agent()
        agent._foreground_waits = ForegroundWaitRegistry()
        slot = agent._foreground_waits.register("call-terminal", "terminal")
        outcomes = []
        result = {}

        def request():
            result.update(
                agent.request_durable_steer(
                    "change direction",
                    mailbox_id="mail-forced",
                    outcome_callback=outcomes.append,
                    force=True,
                )
            )

        thread = threading.Thread(target=request)
        thread.start()
        assert slot.background_requested.wait(1)
        slot.complete_background(
            {
                "kind": "process",
                "session_id": "proc_original",
            }
        )
        thread.join(1)

        assert not thread.is_alive()
        assert result == {"status": "accepted", "wait_kinds": ["terminal"]}
        assert outcomes == []
        assert agent._pending_steer == "change direction"
        envelopes = agent._drain_pending_steer_envelopes()
        assert envelopes[0]["mailbox_id"] == "mail-forced"
        agent._foreground_waits.unregister(slot)

    def test_force_handoff_failure_retracts_envelope_and_reports_terminal_outcome(self):
        from tools.foreground_wait import ForegroundWaitRegistry

        agent = _bare_agent()
        agent._foreground_waits = ForegroundWaitRegistry()
        slot = agent._foreground_waits.register("call-fail", "terminal")
        outcomes = []

        def fail_handoff():
            assert slot.background_requested.wait(1)
            slot.fail_background("cannot adopt original process")

        thread = threading.Thread(target=fail_handoff)
        thread.start()
        result = agent.request_durable_steer(
            "do not inject me",
            mailbox_id="mail-fail",
            outcome_callback=outcomes.append,
            force=True,
        )
        thread.join(1)

        assert result["status"] == "force_background_failed"
        assert result["errors"] == ["cannot adopt original process"]
        assert outcomes == ["force_background_failed"]
        assert agent._drain_pending_steer_envelopes() == []
        agent._foreground_waits.unregister(slot)

    def test_force_completion_race_keeps_steer_without_inventing_handoff(self):
        from tools.foreground_wait import ForegroundWaitRegistry

        registry = ForegroundWaitRegistry()
        slot = registry.register("call-race", "delegation")
        registry.unregister(slot)

        result = registry.request_background([slot])

        assert result == {
            "status": "backgrounded",
            "wait_kinds": ["delegation"],
            "handoffs": [],
        }

    def test_accepts_non_empty_text(self):
        agent = _bare_agent()
        assert agent.steer("go ahead and check the logs") is True
        assert agent._pending_steer == "go ahead and check the logs"

    def test_rejects_empty_string(self):
        agent = _bare_agent()
        assert agent.steer("") is False
        assert agent._pending_steer is None

    def test_rejects_whitespace_only(self):
        agent = _bare_agent()
        assert agent.steer("   \n\t  ") is False
        assert agent._pending_steer is None

    def test_rejects_none(self):
        agent = _bare_agent()
        assert agent.steer(None) is False  # type: ignore[arg-type]
        assert agent._pending_steer is None

    def test_strips_surrounding_whitespace(self):
        agent = _bare_agent()
        assert agent.steer("  hello world  \n") is True
        assert agent._pending_steer == "hello world"

    def test_concatenates_multiple_steers_with_newlines(self):
        agent = _bare_agent()
        agent.steer("first note")
        agent.steer("second note")
        agent.steer("third note")
        assert agent._pending_steer == "first note\nsecond note\nthird note"

    def test_tracked_envelopes_preserve_identity_and_ack_injection(self):
        agent = _bare_agent()
        outcomes = []
        agent.steer(
            "first",
            mailbox_id="mail-1",
            outcome_callback=lambda outcome: outcomes.append(("mail-1", outcome)),
        )
        agent.steer(
            "second",
            mailbox_id="mail-2",
            outcome_callback=lambda outcome: outcomes.append(("mail-2", outcome)),
        )
        messages = [{"role": "tool", "content": "output", "tool_call_id": "1"}]

        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)

        assert messages[0]["content"].endswith(
            "first\nsecond\n[/OUT-OF-BAND USER MESSAGE]"
        )
        assert outcomes == [
            ("mail-1", "injected"),
            ("mail-2", "injected"),
        ]

    def test_handoff_tool_result_precedes_exact_parent_steer_marker(self):
        agent = _bare_agent()
        agent.steer("parent says continue elsewhere")
        content = (
            '{"status":"backgrounded","foreground_handoff":'
            '{"kind":"process","session_id":"proc_original",'
            '"continue":"process(action=\\"wait\\", session_id=\\"proc_original\\")"}}'
        )
        messages = [{"role": "tool", "content": content, "tool_call_id": "1"}]

        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)

        delivered = messages[0]["content"]
        assert delivered.index("proc_original") < delivered.index(
            "[OUT-OF-BAND USER MESSAGE"
        )
        assert "parent says continue elsewhere" in delivered

    def test_interrupt_acks_tracked_envelope_as_superseded(self):
        agent = _bare_agent()
        outcomes = []
        agent.steer(
            "do not deliver",
            mailbox_id="mail-1",
            outcome_callback=outcomes.append,
        )

        agent._clear_pending_steer("superseded_by_interrupt")

        assert outcomes == ["superseded_by_interrupt"]
        assert agent._pending_steer is None


class TestSteerDrain:
    def test_drain_returns_and_clears(self):
        agent = _bare_agent()
        agent.steer("hello")
        assert agent._drain_pending_steer() == "hello"
        assert agent._pending_steer is None

    def test_drain_on_empty_returns_none(self):
        agent = _bare_agent()
        assert agent._drain_pending_steer() is None


class TestSteerInjection:
    def test_appends_to_last_tool_result(self):
        agent = _bare_agent()
        agent.steer("please also check auth.log")
        messages = [
            {"role": "user", "content": "what's in /var/log?"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "content": "ls output A", "tool_call_id": "a"},
            {"role": "tool", "content": "ls output B", "tool_call_id": "b"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
        # The LAST tool result is modified; earlier ones are untouched.
        assert messages[2]["content"] == "ls output A"
        assert "ls output B" in messages[3]["content"]
        assert STEER_MARKER_OPEN in messages[3]["content"]
        assert "please also check auth.log" in messages[3]["content"]
        # And pending_steer is consumed.
        assert agent._pending_steer is None

    def test_no_op_when_no_steer_pending(self):
        agent = _bare_agent()
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "content": "output", "tool_call_id": "a"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        assert messages[-1]["content"] == "output"  # unchanged

    def test_no_op_when_num_tool_msgs_zero(self):
        agent = _bare_agent()
        agent.steer("steer")
        messages = [{"role": "user", "content": "hi"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=0)
        # Steer should remain pending (nothing to drain into)
        assert agent._pending_steer == "steer"

    def test_marker_labels_text_as_out_of_band_user_message(self):
        """The injection marker must attribute the appended text to the user
        via the explicit out-of-band marker (which the system prompt tells the
        model to trust) — otherwise the model reads it as untrusted tool output
        and refuses it as suspected prompt injection.  Cache-safe: it only
        rewrites existing tool content, never the message-role sequence.
        """
        agent = _bare_agent()
        agent.steer("stop after next step")
        messages = [{"role": "tool", "content": "x", "tool_call_id": "1"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        content = messages[-1]["content"]
        assert STEER_MARKER_OPEN in content
        assert "stop after next step" in content

    def test_multimodal_content_list_preserved(self):
        """Anthropic-style list content should be preserved, with the steer
        appended as a text block."""
        agent = _bare_agent()
        agent.steer("extra note")
        original_blocks = [{"type": "text", "text": "existing output"}]
        messages = [
            {"role": "tool", "content": list(original_blocks), "tool_call_id": "1"}
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        new_content = messages[-1]["content"]
        assert isinstance(new_content, list)
        assert len(new_content) == 2
        assert new_content[0] == {"type": "text", "text": "existing output"}
        assert new_content[1]["type"] == "text"
        assert "extra note" in new_content[1]["text"]

    def test_restashed_when_no_tool_result_in_batch(self):
        """If the 'batch' contains no tool-role messages (e.g. all skipped
        after an interrupt), the steer should be put back into the pending
        slot so the caller's fallback path can deliver it."""
        agent = _bare_agent()
        agent.steer("ping")
        messages = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ]
        # Claim there were N tool msgs, but the tail has none — simulates
        # the interrupt-cancelled case.
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
        # Messages untouched
        assert messages[-1]["content"] == "y"
        # And the steer is back in pending so the fallback can grab it
        assert agent._pending_steer == "ping"


class TestSteerThreadSafety:
    def test_concurrent_steer_calls_preserve_all_text(self):
        agent = _bare_agent()
        N = 200

        def worker(idx: int) -> None:
            agent.steer(f"note-{idx}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        text = agent._drain_pending_steer()
        assert text is not None
        # Every single note must be preserved — none dropped by the lock.
        lines = text.split("\n")
        assert len(lines) == N
        assert set(lines) == {f"note-{i}" for i in range(N)}


class TestSteerClearedOnInterrupt:
    def test_clear_interrupt_drops_pending_steer(self):
        """A hard interrupt supersedes any pending steer — the agent's
        next tool iteration won't happen, so delivering the steer later
        would be surprising."""
        agent = _bare_agent()
        # Minimal surface needed by clear_interrupt()
        agent._interrupt_requested = True
        agent._interrupt_message = None
        agent._interrupt_thread_signal_pending = False
        agent._execution_thread_id = None
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None

        agent.steer("will be dropped")
        assert agent._pending_steer == "will be dropped"

        agent.clear_interrupt()
        assert agent._pending_steer is None


class TestPreApiCallSteerDrain:
    """Test that steers arriving during an API call are drained before the
    next API call — not deferred until the next tool batch.  This is the
    fix for the scenario where /steer sent during model thinking only lands
    after the agent is completely done."""

    def test_pre_api_drain_injects_into_last_tool_result(self):
        """If a steer is pending when the main loop starts building
        api_messages, it should be injected into the last tool result
        in the messages list."""
        agent = _bare_agent()
        # Simulate messages after a tool batch completed
        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "tc1", "function": {"name": "terminal", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "output here", "tool_call_id": "tc1"},
        ]
        # Steer arrives during API call (set after tool execution)
        agent.steer("focus on error handling")
        # Simulate what the pre-API-call drain does:
        _pre_api_steer = agent._drain_pending_steer()
        assert _pre_api_steer == "focus on error handling"
        # Inject into last tool msg (mirrors the new code in run_conversation)
        for _si in range(len(messages) - 1, -1, -1):
            if messages[_si].get("role") == "tool":
                messages[_si]["content"] += format_steer_marker(_pre_api_steer)
                break
        assert STEER_MARKER_OPEN in messages[-1]["content"]
        assert "focus on error handling" in messages[-1]["content"]
        assert agent._pending_steer is None

    def test_pre_api_drain_restashes_when_no_tool_message(self):
        """If there are no tool results yet (first iteration), the steer
        should be put back into _pending_steer for the post-tool drain."""
        agent = _bare_agent()
        messages = [
            {"role": "user", "content": "hello"},
        ]
        agent.steer("early steer")
        _pre_api_steer = agent._drain_pending_steer()
        assert _pre_api_steer == "early steer"
        # No tool message found — put it back
        found = False
        for _si in range(len(messages) - 1, -1, -1):
            if messages[_si].get("role") == "tool":
                found = True
                break
        assert not found
        # Restash
        agent._pending_steer = _pre_api_steer
        assert agent._pending_steer == "early steer"

    def test_pre_api_drain_finds_tool_msg_past_assistant(self):
        """The pre-API drain should scan backwards past a non-tool message
        (e.g., if an assistant message was somehow appended after tools)
        and still find the tool result."""
        agent = _bare_agent()
        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "let me check", "tool_calls": [
                {"id": "tc1", "function": {"name": "web_search", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "search results", "tool_call_id": "tc1"},
        ]
        agent.steer("change approach")
        _pre_api_steer = agent._drain_pending_steer()
        assert _pre_api_steer is not None
        for _si in range(len(messages) - 1, -1, -1):
            if messages[_si].get("role") == "tool":
                messages[_si]["content"] += format_steer_marker(_pre_api_steer)
                break
        assert "change approach" in messages[2]["content"]


class TestSteerMarkerContract:
    def test_system_prompt_note_describes_the_real_marker(self):
        """The system-prompt note tells the model which marker to trust; it
        must reference the exact open/close the injector emits, or the model
        trusts a marker that never appears (and vice-versa)."""
        from agent.prompt_builder import STEER_CHANNEL_NOTE, STEER_MARKER_CLOSE

        emitted = format_steer_marker("hi")
        assert STEER_MARKER_OPEN in emitted and STEER_MARKER_CLOSE in emitted
        assert STEER_MARKER_OPEN in STEER_CHANNEL_NOTE and STEER_MARKER_CLOSE in STEER_CHANNEL_NOTE

    def test_marker_no_longer_uses_the_distrusted_label(self):
        """Regression: the bare 'User guidance:' line read as tool content and
        got refused as injection — it must not come back."""
        assert "User guidance:" not in format_steer_marker("hi")


class TestSteerCommandRegistry:
    def test_steer_in_command_registry(self):
        """The /steer slash command must be registered so it reaches all
        platforms (CLI, gateway, TUI autocomplete, Telegram/Slack menus).
        """
        from hermes_cli.commands import resolve_command

        cmd = resolve_command("steer")
        assert cmd is not None
        assert cmd.name == "steer"
        assert cmd.category == "Session"
        assert cmd.args_hint == "<prompt>"

    def test_steer_in_bypass_set(self):
        """When the agent is running, /steer MUST bypass the Level-1
        base-adapter queue so it reaches the gateway runner's /steer
        handler. Otherwise it would be queued as user text and only
        delivered at turn end — defeating the whole point.
        """
        from hermes_cli.commands import ACTIVE_SESSION_BYPASS_COMMANDS, should_bypass_active_session

        assert "steer" in ACTIVE_SESSION_BYPASS_COMMANDS
        assert should_bypass_active_session("steer") is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
