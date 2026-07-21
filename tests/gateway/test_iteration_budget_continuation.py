from __future__ import annotations

import importlib
import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource


RAW_GOAL = "Fix café handling exactly — keep punctuation: [] {} and finish all checks."


class _Adapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.sent: list[dict] = []
        self.clarifies: list[dict] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id=str(len(self.sent)))

    async def send_clarify(
        self,
        chat_id,
        question,
        choices,
        clarify_id,
        session_key,
        metadata=None,
    ) -> SendResult:
        self.clarifies.append(
            {
                "chat_id": chat_id,
                "question": question,
                "choices": choices,
                "clarify_id": clarify_id,
                "session_key": session_key,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="clarify-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        user_name="tester",
        chat_type="dm",
    )


def _assistant_tool_call(name: str, arguments: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
    }


def test_iteration_judge_uses_small_auxiliary_call_and_exact_raw_goal():
    from gateway.iteration_continuation import judge_iteration_exhaustion

    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"decision":"auto_continue","reason":"forward progress"}'
                    )
                )
            ]
        )

    messages = [
        {"role": "user", "content": "older task"},
        _assistant_tool_call("terminal", json.dumps({"command": "pytest -q"})),
        {"role": "tool", "tool_name": "terminal", "content": "14 passed"},
        {
            "role": "user",
            "content": "synthetic control text",
            "_iteration_control_synthetic": True,
        },
    ]
    verdict = judge_iteration_exhaustion(
        result={
            "turn_exit_reason": "max_iterations_reached(90/90)",
            "api_calls": 90,
            "final_response": "I finished edits and still need one validation pass.",
            "messages": messages,
        },
        raw_goal=RAW_GOAL,
        llm_call=fake_call_llm,
        model="main-model",
        main_runtime={"provider": "openai"},
    )

    assert verdict.decision == "auto_continue"
    assert verdict.reason == "forward progress"
    assert len(calls) == 1
    call = calls[0]
    assert call["task"] == "task_intent"
    assert call["tools"] is None
    assert call["temperature"] == 0
    assert call["max_tokens"] <= 220
    assert call["timeout"] <= 8
    prompt = call["messages"][1]["content"]
    assert RAW_GOAL in prompt
    assert "synthetic control text" not in prompt
    assert len(prompt) < 12_000


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"decision":"continue","reason":"wrong enum"}',
        '{"decision":"auto_continue","reason":42}',
        "```json\n{\"decision\":\"auto_continue\",\"reason\":\"no fences\"}\n```",
    ],
)
def test_iteration_judge_malformed_verdict_fails_to_ask_user(raw):
    from gateway.iteration_continuation import judge_iteration_exhaustion

    verdict = judge_iteration_exhaustion(
        result={"messages": [], "turn_exit_reason": "max_iterations_reached(2/2)"},
        raw_goal=RAW_GOAL,
        llm_call=lambda **_: raw,
        model="m",
        main_runtime={},
    )

    assert verdict.decision == "ask_user"
    assert verdict.reason


def test_latest_real_user_goal_and_review_filter_ignore_synthetic_messages():
    from gateway.iteration_continuation import (
        build_recent_review_excerpt,
        latest_real_user_goal,
    )

    messages = [
        {"role": "user", "content": RAW_GOAL},
        {"role": "assistant", "content": "working"},
        {
            "role": "user",
            "content": "continue controller",
            "_iteration_control_synthetic": True,
        },
    ]

    assert latest_real_user_goal(messages) == RAW_GOAL
    excerpt = build_recent_review_excerpt(messages)
    assert RAW_GOAL in excerpt
    assert "continue controller" not in excerpt


def test_continuation_prompt_retains_raw_goal_and_has_source_marker():
    from gateway.iteration_continuation import (
        ITERATION_CONTROL_METADATA,
        build_iteration_continuation,
    )

    continuation = build_iteration_continuation(raw_goal=RAW_GOAL, user_approved=False)

    assert continuation.raw_goal == RAW_GOAL
    assert RAW_GOAL in continuation.prompt
    assert continuation.persist_metadata == ITERATION_CONTROL_METADATA
    assert continuation.persist_metadata["_iteration_control_synthetic"] is True


class _SequenceAgent:
    calls = 0
    seen: list[dict] = []
    always_exhaust = False
    enqueue_user_followup = False
    adapter: _Adapter | None = None
    session_key = "agent:main:telegram:dm:user-1"

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        type(self).calls += 1
        type(self).seen.append(
            {
                "message": message,
                "history": list(conversation_history or []),
                "kwargs": dict(kwargs),
            }
        )
        if type(self).calls == 1 and type(self).enqueue_user_followup:
            assert type(self).adapter is not None
            type(self).adapter._pending_messages[type(self).session_key] = MessageEvent(
                text="real queued follow-up",
                source=_source(),
                message_id="queued-1",
            )
        exhausted = type(self).always_exhaust or type(self).calls == 1
        user_msg = {"role": "user", "content": message}
        user_msg.update(kwargs.get("persist_user_metadata") or {})
        messages = list(conversation_history or []) + [user_msg]
        if exhausted:
            messages.append({"role": "assistant", "content": "forced unfinished summary"})
            return {
                "final_response": "forced unfinished summary",
                "messages": messages,
                "api_calls": 90,
                "completed": False,
                "interrupted": False,
                "turn_exit_reason": "max_iterations_reached(90/90)",
            }
        messages.append({"role": "assistant", "content": "finished after continuation"})
        return {
            "final_response": "finished after continuation",
            "messages": messages,
            "api_calls": 1,
            "completed": True,
            "interrupted": False,
            "turn_exit_reason": "text_response(finish_reason=stop)",
        }


def _make_runner(adapter: _Adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._draining = False
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
        multiplex_profiles=False,
    )
    runner._resolve_session_agent_runtime = lambda **_: (
        "main-model",
        {"provider": "openai", "api_key": "test", "base_url": "https://invalid"},
    )
    return runner


def _install_gateway_fakes(monkeypatch, tmp_path, *, chain_limit=8):
    _SequenceAgent.calls = 0
    _SequenceAgent.seen = []
    _SequenceAgent.always_exhaust = False
    _SequenceAgent.enqueue_user_followup = False
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _SequenceAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "agent": {"max_iteration_auto_continue_chain": chain_limit},
            "display": {"tool_progress": "off", "gateway_notify_interval": 0},
        },
    )
    return gateway_run


@pytest.mark.asyncio
async def test_gateway_auto_continues_at_current_recursive_seam_with_bookkeeping(monkeypatch, tmp_path):
    from gateway.iteration_continuation import IterationContinuationVerdict

    adapter = _Adapter()
    _SequenceAgent.adapter = adapter
    runner = _make_runner(adapter)
    _install_gateway_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner,
        "_judge_iteration_budget_exhaustion",
        lambda **_: IterationContinuationVerdict("auto_continue", "progressing"),
    )

    original = runner._run_agent
    recursive_kwargs: list[dict] = []

    async def spy(*args, **kwargs):
        recursive_kwargs.append(dict(kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(runner, "_run_agent", spy)
    result = await runner._run_agent(
        message=RAW_GOAL,
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-1",
        session_key=_SequenceAgent.session_key,
        _raw_task_goal=RAW_GOAL,
    )

    assert result["final_response"] == "finished after continuation"
    assert result["history_offset"] == 0
    assert len(_SequenceAgent.seen) == 2
    followup = _SequenceAgent.seen[1]
    assert RAW_GOAL in followup["message"]
    assert followup["kwargs"]["persist_user_metadata"]["_iteration_control_synthetic"] is True
    nested = recursive_kwargs[1]
    assert nested["_continuation_depth"] == 1
    assert nested["_raw_task_goal"] == RAW_GOAL
    assert isinstance(nested["_notify_started_at"], float)


@pytest.mark.asyncio
async def test_gateway_chain_bound_stops_recursive_auto_continue(monkeypatch, tmp_path):
    from gateway.iteration_continuation import IterationContinuationVerdict

    adapter = _Adapter()
    _SequenceAgent.adapter = adapter
    _SequenceAgent.always_exhaust = True
    runner = _make_runner(adapter)
    _install_gateway_fakes(monkeypatch, tmp_path, chain_limit=1)
    _SequenceAgent.always_exhaust = True
    judge_calls = []
    monkeypatch.setattr(
        runner,
        "_judge_iteration_budget_exhaustion",
        lambda **kwargs: judge_calls.append(kwargs) or IterationContinuationVerdict("auto_continue", "progressing"),
    )

    result = await runner._run_agent(
        message=RAW_GOAL,
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-bound",
        session_key=_SequenceAgent.session_key,
        _raw_task_goal=RAW_GOAL,
    )

    assert _SequenceAgent.calls == 2
    assert len(judge_calls) == 1
    assert result["final_response"] == "forced unfinished summary"


@pytest.mark.asyncio
async def test_gateway_pending_real_followup_takes_precedence_over_auto_continue(monkeypatch, tmp_path):
    adapter = _Adapter()
    _SequenceAgent.adapter = adapter
    runner = _make_runner(adapter)
    _install_gateway_fakes(monkeypatch, tmp_path)
    _SequenceAgent.enqueue_user_followup = True

    def should_not_run(**_):
        raise AssertionError("continuation judge must not run while a real follow-up is pending")

    monkeypatch.setattr(runner, "_judge_iteration_budget_exhaustion", should_not_run)

    result = await runner._run_agent(
        message=RAW_GOAL,
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-pending",
        session_key=_SequenceAgent.session_key,
        _raw_task_goal=RAW_GOAL,
    )

    assert result["final_response"] == "finished after continuation"
    assert _SequenceAgent.seen[1]["message"] == "real queued follow-up"
    assert "persist_user_metadata" not in _SequenceAgent.seen[1]["kwargs"]


@pytest.mark.asyncio
async def test_gateway_ask_user_uses_confirmation_then_marks_approved_continuation(monkeypatch, tmp_path):
    from gateway.iteration_continuation import IterationContinuationVerdict

    adapter = _Adapter()
    _SequenceAgent.adapter = adapter
    runner = _make_runner(adapter)
    _install_gateway_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner,
        "_judge_iteration_budget_exhaustion",
        lambda **_: IterationContinuationVerdict("ask_user", "needs confirmation"),
    )
    confirmation = []

    async def approve(**kwargs):
        confirmation.append(kwargs)
        return True

    monkeypatch.setattr(runner, "_request_iteration_continuation_confirmation", approve)

    result = await runner._run_agent(
        message=RAW_GOAL,
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-ask",
        session_key=_SequenceAgent.session_key,
        _raw_task_goal=RAW_GOAL,
    )

    assert result["final_response"] == "finished after continuation"
    assert confirmation[0]["reason"] == "needs confirmation"
    assert "user approved" in _SequenceAgent.seen[1]["message"].lower()
    assert _SequenceAgent.seen[1]["kwargs"]["persist_user_metadata"]["_iteration_control_synthetic"] is True
