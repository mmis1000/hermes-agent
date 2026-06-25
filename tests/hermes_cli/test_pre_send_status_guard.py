from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import task_intents

    task_intents._DB_CACHE.clear()
    yield home
    task_intents._DB_CACHE.clear()


def _assistant_tool_call(seq: int, name: str, arguments: str):
    return {
        "id": seq,
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        ],
    }


def _tool_result(seq: int, name: str, content: str):
    return {"id": seq, "role": "tool", "tool_name": name, "content": content}


def test_recent_operation_synopsis_uses_target_30_and_keeps_rawish_tool_context():
    from hermes_cli.pre_send_status_guard import (
        PreSendStatusGuardConfig,
        build_recent_operation_synopsis,
    )

    messages = []
    for i in range(35):
        messages.append(_assistant_tool_call(i * 2, "terminal", f"echo step-{i}"))
        messages.append(_tool_result(i * 2 + 1, "terminal", f"result step-{i}"))

    ops = build_recent_operation_synopsis(
        messages,
        config=PreSendStatusGuardConfig(enabled=True, max_operations=30),
    )

    assert len(ops) == 30
    assert ops[0]["text"] == "echo step-20"
    assert ops[-1]["text"] == "result step-34"
    assert {op["kind"] for op in ops} == {"tool_call", "tool_result"}
    # The synopsis is deliberately neutral: no precomputed delivery/mutation class.
    assert all("classification" not in op for op in ops)
    assert all("delivery" not in op for op in ops)
    assert all("mutation" not in op for op in ops)


def test_status_guard_payload_includes_active_task_candidate_and_operations(hermes_home):
    from hermes_cli.pre_send_status_guard import (
        PreSendStatusGuardConfig,
        active_task_payload_from_task_intent,
        build_status_guard_payload,
    )
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-status-payload")
    state = mgr.record_direct_message("fix vibelive and run fresh review")
    messages = [
        _assistant_tool_call(1, "skill_view", '{"name":"verification-before-completion"}'),
        _tool_result(2, "delegate_task", "Verdict: REQUEST_CHANGES"),
        _assistant_tool_call(3, "patch", '{"path":"test.sh","old_string":"x","new_string":"y"}'),
    ]
    cfg = PreSendStatusGuardConfig(enabled=True, max_operations=30)

    payload = build_status_guard_payload(
        candidate_response="Done — fixed and uploaded.",
        active_task=active_task_payload_from_task_intent(state, config=cfg),
        messages=messages,
        config=cfg,
        platform="discord",
    )

    assert payload["active_task"]["raw_primary_text"] == "fix vibelive and run fresh review"
    assert payload["candidate_response"] == "Done — fixed and uploaded."
    assert [op["tool"] for op in payload["recent_operations"]] == ["skill_view", "delegate_task", "patch"]
    assert "REQUEST_CHANGES" in payload["recent_operations"][1]["text"]


def test_status_guard_judge_rejects_unsupported_status_claim():
    from hermes_cli.pre_send_status_guard import PreSendStatusGuard, PreSendStatusGuardConfig

    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return '{"decision":"reject_and_steer","unsupported_claims":["done","uploaded"],"reason":"review result was REQUEST_CHANGES and a later patch ran","steer_prompt":"Run fresh review or send partial status."}'

    guard = PreSendStatusGuard(
        config=PreSendStatusGuardConfig(enabled=True, max_operations=30),
        llm_call=fake_llm,
    )
    decision = guard.judge(
        candidate_response="Done — fixed and uploaded.",
        active_task={"id": "task-1", "kind": "direct_message", "raw_primary_text": "fix vibelive"},
        messages=[
            _tool_result(1, "delegate_task", "Verdict: REQUEST_CHANGES"),
            _assistant_tool_call(2, "patch", '{"path":"test.sh"}'),
        ],
        platform="discord",
    )

    assert decision is not None
    assert decision.decision == "reject_and_steer"
    assert decision.unsupported_claims == ["done", "uploaded"]
    assert calls[0]["max_tokens"] == 220
    assert calls[0]["timeout"] == 2.0


def test_status_guard_judge_allows_honest_partial_progress():
    from hermes_cli.pre_send_status_guard import PreSendStatusGuard, PreSendStatusGuardConfig

    guard = PreSendStatusGuard(
        config=PreSendStatusGuardConfig(enabled=True),
        llm_call=lambda **_: '{"decision":"allow","unsupported_claims":[],"reason":"honest partial status"}',
    )
    decision = guard.judge(
        candidate_response="Partial progress only — I changed files but did not verify yet.",
        active_task={"id": "task-1", "kind": "direct_message", "raw_primary_text": "resolve rebase conflicts"},
        messages=[_assistant_tool_call(1, "patch", '{"path":"gateway/run.py"}')],
    )

    assert decision is not None
    assert decision.allowed is True


def test_status_guard_fails_open_on_judge_error():
    from hermes_cli.pre_send_status_guard import PreSendStatusGuard, PreSendStatusGuardConfig

    def boom(**_):
        raise RuntimeError("provider unavailable")

    guard = PreSendStatusGuard(config=PreSendStatusGuardConfig(enabled=True), llm_call=boom)
    decision = guard.judge(
        candidate_response="Done.",
        active_task={"id": "task-1", "kind": "direct_message", "raw_primary_text": "do work"},
        messages=[],
    )

    assert decision is not None
    assert decision.allowed is True
    assert decision.fallback_reason == "status guard failed open"
