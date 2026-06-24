from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="thread",
        thread_id="t1",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter.pause_typing_for_chat = MagicMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._pending_approvals = {}
    runner._reasoning_config = None
    runner._service_tier = None
    runner._session_db = None
    runner._fallback_model = None
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=(
            "gpt-5.4",
            {
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openai",
                "api_mode": "chat_completions",
            },
        )
    )
    runner._resolve_turn_agent_config = MagicMock(
        return_value={
            "model": "gpt-5.4",
            "runtime": {
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openai",
                "api_mode": "chat_completions",
                "command": None,
                "args": [],
                "credential_pool": None,
            },
            "request_overrides": None,
        }
    )
    runner._session_key_for_source = lambda source: build_session_key(source)
    return runner, adapter


def test_judge_iteration_budget_exhaustion_auto_continue(monkeypatch):
    runner, _adapter = _make_runner()
    source = _make_source()

    captured = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs
            self.suppress_status_output = False

        def run_conversation(self, user_message):
            captured["user_message"] = user_message
            return {
                "final_response": '{"decision":"auto_continue","reason":"making concrete progress"}'
            }

        def shutdown_memory_provider(self):
            captured["shutdown"] = True

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("run_agent.AIAgent", FakeReviewAgent)

    result = runner._judge_iteration_budget_exhaustion(
        result={
            "turn_exit_reason": "max_iterations_reached(90/90)",
            "api_calls": 90,
            "final_response": "I investigated three modules and still need to finish e2e validation.",
            "messages": [
                {"role": "user", "content": "keep going until e2e is done"},
                {"role": "assistant", "tool_calls": [{"function": {"name": "terminal"}}]},
                {"role": "tool", "content": "tests passed through step 3"},
            ],
        },
        source=source,
        session_id="sess-1",
        session_key=build_session_key(source),
    )

    assert result == {
        "decision": "auto_continue",
        "reason": "making concrete progress",
    }
    runner._resolve_session_agent_runtime.assert_called_once()
    runner._resolve_turn_agent_config.assert_called_once()
    assert captured["init_kwargs"]["enabled_toolsets"] == []
    assert "keep going until e2e is done" in captured["user_message"]
    assert captured["closed"] is True


def test_judge_iteration_budget_exhaustion_skips_synthetic_summary_request(monkeypatch):
    runner, _adapter = _make_runner()
    source = _make_source()

    captured = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self.suppress_status_output = False

        def run_conversation(self, user_message):
            captured["user_message"] = user_message
            return {
                "final_response": '{"decision":"auto_continue","reason":"still progressing"}'
            }

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", FakeReviewAgent)

    runner._judge_iteration_budget_exhaustion(
        result={
            "turn_exit_reason": "max_iterations_reached(90/90)",
            "api_calls": 90,
            "final_response": "I finished the first chunk and need to continue the remaining work.",
            "messages": [
                {"role": "user", "content": "Do the full task, not just the first subtask. Finish A, B, and C."},
                {"role": "assistant", "tool_calls": [{"function": {"name": "terminal"}}]},
                {"role": "tool", "content": "completed A"},
                {
                    "role": "user",
                    "content": (
                        "You've reached the maximum number of tool-calling iterations allowed. "
                        "Please provide a final response summarizing what you've found and accomplished so far, "
                        "without calling any more tools."
                    ),
                },
            ],
        },
        source=source,
        session_id="sess-1",
        session_key=build_session_key(source),
    )

    assert "Do the full task, not just the first subtask. Finish A, B, and C." in captured["user_message"]
    assert "You've reached the maximum number of tool-calling iterations allowed" not in captured["user_message"]


def test_build_iteration_continue_prompt_preserves_real_user_goal_and_skips_synthetic():
    runner, _adapter = _make_runner()

    messages = [
        {"role": "user", "content": "Complete the full task end-to-end, including validation and final delivery."},
        {
            "role": "user",
            "content": (
                "You've reached the maximum number of tool-calling iterations allowed. "
                "Please provide a final response summarizing what you've found and accomplished so far, "
                "without calling any more tools."
            ),
        },
    ]

    real_goal = runner._extract_latest_real_user_goal(messages)
    prompt = runner._build_iteration_continue_prompt(user_approved=False, user_goal=real_goal)

    assert "Complete the full task end-to-end, including validation and final delivery." in prompt
    assert "You've reached the maximum number of tool-calling iterations allowed" not in prompt


def test_extract_latest_real_user_goal_uses_preserved_contract_and_later_supplement():
    runner, _adapter = _make_runner()

    messages = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.\n"
                "summary body\n\n"
                "[PRESERVED UNRESOLVED TASK CONTRACT]\n"
                "Primary task:\n"
                "Do the full job: build a multi-scene video with recurring characters and subtitle burn-in.\n\n"
                "Supplements / constraints:\n"
                "- Keep the same characters across scenes.\n"
                "[END PRESERVED UNRESOLVED TASK CONTRACT]"
            ),
        },
        {"role": "user", "content": "Low resolution is okay for the first pass."},
    ]

    real_goal = runner._extract_latest_real_user_goal(messages)

    assert "Do the full job: build a multi-scene video with recurring characters and subtitle burn-in." in real_goal
    assert "Keep the same characters across scenes." in real_goal
    assert "Low resolution is okay for the first pass." in real_goal


def test_extract_latest_real_user_goal_appends_raw_phrase_without_replacing_primary():
    runner, _adapter = _make_runner()

    messages = [
        {
            "role": "user",
            "content": (
                "[PRESERVED UNRESOLVED TASK CONTRACT]\n"
                "Primary task:\n"
                "add more styles\n"
                "[END PRESERVED UNRESOLVED TASK CONTRACT]"
            ),
        },
        {"role": "user", "content": "instead just add one style"},
    ]

    real_goal = runner._extract_latest_real_user_goal(messages)

    assert "Primary unresolved task:\nadd more styles" in real_goal
    assert "- instead just add one style" in real_goal


@pytest.mark.asyncio
async def test_request_iteration_continuation_approval_then_approve():
    runner, adapter = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)

    wait_task = asyncio.create_task(
        runner._request_iteration_continuation_approval(
            source=source,
            session_key=session_key,
            reason="large task is progressing, but review wants confirmation",
            metadata={"thread_id": "t1"},
        )
    )
    await asyncio.sleep(0)

    response = await runner._handle_approve_command(_make_event("/approve"))

    assert await wait_task is True
    assert "Continuation approved" in response
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "Continuation approval required" in sent_text
    adapter.pause_typing_for_chat.assert_called_once_with(source.chat_id)
    adapter.resume_typing_for_chat.assert_called_once_with(source.chat_id)
    assert session_key not in runner._pending_approvals


@pytest.mark.asyncio
async def test_request_iteration_continuation_approval_then_deny():
    runner, adapter = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)

    wait_task = asyncio.create_task(
        runner._request_iteration_continuation_approval(
            source=source,
            session_key=session_key,
            reason="possible loop detected near the budget limit",
            metadata={"thread_id": "t1"},
        )
    )
    await asyncio.sleep(0)

    response = await runner._handle_deny_command(_make_event("/deny"))

    assert await wait_task is False
    assert "Continuation denied" in response
    adapter.send.assert_awaited_once()
    adapter.resume_typing_for_chat.assert_called_once_with(source.chat_id)
    assert session_key not in runner._pending_approvals
