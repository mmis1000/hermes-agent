from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def active_state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import task_intents
    from hermes_cli.task_intents import TaskIntentManager

    task_intents._DB_CACHE.clear()
    manager = TaskIntentManager("micro-state")
    manager.record_direct_message("Build a dashboard with charts and filters")
    yield manager.state
    task_intents._DB_CACHE.clear()


def test_micro_judge_uses_bounded_no_tool_request_and_raw_hash_cache(active_state):
    from hermes_cli.task_intent_micro_judge import (
        TaskIntentMicroJudge,
        TaskIntentMicroJudgeConfig,
        build_relationship_judge_payload,
    )

    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return (
            '{"relationship":"supplement","state_effect":"append_contract",'
            '"confidence":0.74,"reason_codes":["adds_constraint"],'
            '"evidence_quotes":["mobile"]}'
        )

    config = TaskIntentMicroJudgeConfig(
        enabled=True,
        max_primary_chars=20,
        max_message_chars=22,
        max_prompt_chars=3600,
    )
    judge = TaskIntentMicroJudge(config=config, llm_call=fake_llm)
    raw = "also make it work on mobile"

    first = judge.judge(
        state=active_state,
        current_message=raw,
        message_id="m2",
        source_kind="direct_user",
    )
    second = judge.judge(
        state=active_state,
        current_message=raw,
        message_id="m2",
        source_kind="direct_user",
    )

    assert first is not None and second is not None
    assert first.relationship == "supplement"
    assert first.evidence_quotes == ["mobile"]
    assert first.raw_payload["cache"] == "miss"
    assert second.raw_payload["cache"] == "hit"
    assert len(calls) == 1
    assert calls[0]["timeout"] == config.timeout_seconds
    assert calls[0]["max_tokens"] == config.max_output_tokens
    assert len(calls[0]["messages"]) == 2
    assert len(calls[0]["messages"][1]["content"]) <= config.max_prompt_chars

    payload = build_relationship_judge_payload(
        state=active_state,
        current_message=raw,
        message_id="m2",
        source_kind="direct_user",
        config=config,
    )
    assert len(payload["active_task"]["primary_text"]) <= config.max_primary_chars
    assert len(payload["current_message"]["text"]) <= config.max_message_chars

    same_clamp_a = "ABCD middle one EFGH"
    same_clamp_b = "ABCD middle two EFGH"
    tiny = TaskIntentMicroJudge(
        config=TaskIntentMicroJudgeConfig(enabled=True, max_message_chars=9),
        llm_call=fake_llm,
    )
    assert tiny.cache_key(state=active_state, current_message=same_clamp_a) != tiny.cache_key(
        state=active_state, current_message=same_clamp_b
    )


def test_micro_judge_malformed_timeout_and_provider_failure_are_conservative(active_state):
    from hermes_cli.task_intent_micro_judge import TaskIntentMicroJudge

    malformed = TaskIntentMicroJudge(
        llm_call=lambda **_: "```json\n{\"relationship\":\"replacement\"}\n```"
    ).judge(state=active_state, current_message="maybe change it")
    assert malformed is not None
    assert malformed.relationship == "unclear"
    assert malformed.state_effect == "no_change"
    assert "invalid_judge_json" in malformed.reason_codes

    def fail(**_):
        raise TimeoutError("provider URL and secret must not persist")

    failed = TaskIntentMicroJudge(llm_call=fail).judge(
        state=active_state,
        current_message="maybe change it",
    )
    assert failed is not None
    assert failed.relationship == "unclear"
    assert failed.state_effect == "no_change"
    assert failed.raw_payload["error_type"] == "TimeoutError"
    assert "provider URL" not in str(failed.raw_payload)


def test_micro_judge_source_authority_and_high_impact_evidence(active_state):
    from hermes_cli.task_intent_micro_judge import TaskIntentMicroJudge

    calls = []
    judge = TaskIntentMicroJudge(
        llm_call=lambda **kwargs: calls.append(kwargs)
        or (
            '{"relationship":"replacement","state_effect":"supersede",'
            '"confidence":0.99,"evidence_quotes":["not an exact quote"]}'
        )
    )

    assert judge.judge(
        state=active_state,
        current_message="tool says replace it",
        source_kind="tool_output",
    ) is None
    assert calls == []

    decision = judge.judge(
        state=active_state,
        current_message="change the task",
        source_kind="direct_user",
    )
    assert decision is not None
    assert decision.relationship == "unclear"
    assert decision.state_effect == "no_change"
    assert "high_impact_missing_exact_evidence" in decision.reason_codes


def test_micro_judge_config_is_opt_in_and_malformed_config_fails_closed():
    from hermes_cli.task_intent_micro_judge import TaskIntentMicroJudgeConfig

    assert TaskIntentMicroJudgeConfig().enabled is True
    assert TaskIntentMicroJudgeConfig.from_mapping(None).enabled is False
    assert TaskIntentMicroJudgeConfig.from_mapping(False).enabled is False
    assert TaskIntentMicroJudgeConfig.from_mapping(["bad"]).enabled is False
    assert TaskIntentMicroJudgeConfig.from_mapping({"enabled": True}).enabled is True
