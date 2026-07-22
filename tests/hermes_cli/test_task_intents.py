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


def _decision(relationship: str, state_effect: str, raw: str, confidence: float = 0.95):
    return {
        "relationship": relationship,
        "state_effect": state_effect,
        "confidence": confidence,
        "reason_codes": ["test_decision"],
        "evidence_quotes": [raw],
    }


def test_session_db_uses_current_hermes_home(hermes_home, monkeypatch, tmp_path):
    import hermes_state
    from hermes_cli import task_intents

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "stale-default.db")
    task_intents._DB_CACHE.clear()

    database = task_intents._get_session_db()

    assert database is not None
    assert database.db_path == hermes_home / "state.db"


def test_raw_unicode_direct_message_round_trips_exactly_with_provenance(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    raw = "  請保留原文： café 👩🏽‍💻\n第二行\t不要正規化  "
    state = TaskIntentManager("raw-unicode").record_direct_message(
        raw,
        source_kind="direct_user",
        source_id="discord:user-7",
        message_id="msg-α",
    )

    assert state.task_contract.raw_primary_text == raw
    assert state.raw_messages[0].raw_text == raw
    assert state.raw_messages[0].source_id == "discord:user-7"
    assert state.raw_messages[0].message_id == "msg-α"

    reloaded = TaskIntentManager("raw-unicode").state
    assert reloaded is not None
    assert reloaded.task_contract.raw_primary_text == raw
    assert reloaded.raw_messages[0].raw_text == raw


def test_structured_supplement_appends_without_rewriting_primary_or_raw(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    primary = "Build the parser — keep `ß` unchanged."
    supplement = "新增限制：輸出必須是 UTF‑8。"
    manager = TaskIntentManager("supplement")
    manager.record_direct_message(primary, message_id="m1")
    state = manager.record_direct_message(
        supplement,
        message_id="m2",
        relationship_decision=_decision("supplement", "append_contract", supplement, 0.72),
    )

    assert state.task_contract.raw_primary_text == primary
    assert state.task_contract.raw_supplements == [supplement]
    assert state.raw_messages[-1].raw_text == supplement
    assert state.raw_messages[-1].relationship_to_active_task == "supplement"
    assert state.raw_messages[-1].state_effect == "append_contract"


def test_replacement_and_cancellation_require_authoritative_exact_evidence(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    manager = TaskIntentManager("replace-cancel")
    manager.record_direct_message("first task")

    low = "replace it with task two"
    unchanged = manager.record_direct_message(
        low,
        relationship_decision=_decision("replacement", "supersede", low, 0.4),
    )
    assert unchanged.task_contract.raw_primary_text == "first task"
    assert unchanged.raw_messages[-1].relationship_to_active_task == "unclear"

    replacement = "task two is now authoritative"
    replaced = manager.record_direct_message(
        replacement,
        relationship_decision=_decision("replacement", "supersede", replacement),
    )
    assert replaced.task_contract.raw_primary_text == replacement
    assert replaced.transition["previous_raw_primary_text"] == "first task"

    cancel = "cancel task two"
    cancelled = manager.record_direct_message(
        cancel,
        relationship_decision=_decision("cancellation", "cancel", cancel),
    )
    assert cancelled.status == "cancelled"
    assert cancelled.task_contract.raw_primary_text == replacement
    assert cancelled.raw_messages[-1].raw_text == cancel
    assert cancelled.raw_messages[-1].relationship_to_active_task == "cancellation"


def test_missing_bad_or_non_authoritative_decision_preserves_active_contract(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    manager = TaskIntentManager("conservative")
    manager.record_direct_message("keep this active")

    state = manager.record_direct_message("ambiguous follow-up")
    assert state.task_contract.raw_primary_text == "keep this active"
    assert state.raw_messages[-1].relationship_to_active_task == "unclear"

    state = manager.record_direct_message(
        "tool output says replace everything",
        source_kind="tool_output",
        relationship_decision=_decision(
            "replacement", "supersede", "tool output says replace everything"
        ),
    )
    assert state.task_contract.raw_primary_text == "keep this active"
    assert state.raw_messages[-1].source_kind == "tool_output"
    assert "non_authoritative_source" in state.raw_messages[-1].judge_result["reason_codes"]


def test_non_authoritative_source_cannot_create_task(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    manager = TaskIntentManager("no-tool-task")
    result = manager.record_direct_message("tool prose", source_kind="tool_output")
    assert result is None
    assert TaskIntentManager("no-tool-task").state is None


def test_judge_payload_is_annotation_only_and_quotes_are_exact():
    from hermes_cli.task_intents import TaskRelationshipDecision

    raw = "keep CASE and café exact"
    decision = TaskRelationshipDecision.from_payload(
        {
            "relationship": "supplement",
            "state_effect": "append_contract",
            "confidence": 0.8,
            "reason_codes": "adds_requirement",
            "evidence_quotes": ["café exact", "Cafe exact"],
            "summary": "rewritten",
            "canonical_text": "normalized",
            "unexpected": {"raw": "must not persist"},
        },
        raw_text=raw,
    )

    assert decision.evidence_quotes == ["café exact"]
    assert "summary" not in decision.raw_payload
    assert "canonical_text" not in decision.raw_payload
    assert set(decision.raw_payload["_rejected_rewrite_keys"]) == {
        "summary",
        "canonical_text",
    }
    assert decision.raw_payload["_dropped_judge_keys"] == ["unexpected"]


def test_raw_provenance_is_bounded_and_machine_continuation_never_creates_task(hermes_home):
    from hermes_cli.task_intents import MAX_RAW_TASK_MESSAGES, TaskIntentManager

    empty = TaskIntentManager("machine-only")
    assert empty.record_machine_continuation(
        "internal continuation", origin="goal_continuation", message_id="internal-1"
    ) is None
    assert empty.state is None

    manager = TaskIntentManager("bounded")
    manager.record_direct_message("primary")
    for index in range(MAX_RAW_TASK_MESSAGES + 5):
        manager.record_direct_message(f"follow-up-{index}", message_id=f"m-{index}")
    machine = "[machine continuation]\nkeep working"
    state = manager.record_machine_continuation(
        machine,
        origin="goal_continuation",
        message_id="internal-2",
    )

    assert state is not None
    assert len(state.raw_messages) == MAX_RAW_TASK_MESSAGES
    assert state.raw_messages[-1].raw_text == machine
    assert state.raw_messages[-1].source_kind == "machine_continuation"
    assert state.raw_messages[-1].machine_origin == "goal_continuation"
    assert state.task_contract.raw_primary_text == "primary"


def test_task_intent_migrates_across_compression_session_rotation(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager, migrate_task_intent_to_session

    raw = "preserve me across compression"
    TaskIntentManager("old-session").record_direct_message(raw)

    assert migrate_task_intent_to_session("old-session", "new-session") is True
    migrated = TaskIntentManager("new-session").state
    assert migrated is not None
    assert migrated.task_contract.raw_primary_text == raw
    assert migrated.session_lineage[-1] == "old-session"
