from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import threading

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import SessionDB


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        chat_type="dm",
    )


def _runner(db: SessionDB):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_db = SimpleNamespace(_db=db)
    runner._task_intent_judge_cache = OrderedDict()
    runner._task_intent_judge_signature = ""
    runner._task_intent_judge_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    runner._fallback_model = None
    runner._provider_routing = {}
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=("test/main", {"provider": "openai", "api_key": "test"})
    )
    return runner


@pytest.mark.asyncio
async def test_raw_unicode_gateway_capture_precedes_all_message_decoration(tmp_path):
    from gateway.run import GatewayRunner
    from hermes_cli.task_intents import load_task_intent

    db = SessionDB(db_path=tmp_path / "state.db")
    runner = _runner(db)
    raw = "  請保留原文： café 👩🏽‍💻\n第二行\t不要正規化  "
    event = MessageEvent(text=raw, message_id="unicode-1", source=_source())

    GatewayRunner._capture_task_intent_ingress(event)
    event.text = "[Wed 2026-07-22 12:00 UTC]\n<AUTO-SKILL>\n" + raw
    await runner._record_task_intent_event(
        event=event,
        session_id="sid-unicode",
        session_key="discord:user-1:chat-1:thread-1",
    )

    state = load_task_intent("sid-unicode", db=db)
    assert state is not None
    assert state.task_contract.raw_primary_text == raw
    assert state.raw_messages[-1].raw_text == raw
    assert state.raw_messages[-1].source_kind == "direct_external_user"
    assert "AUTO-SKILL" not in state.task_contract.raw_primary_text


@pytest.mark.asyncio
async def test_synthetic_continuation_is_machine_provenance_only(tmp_path):
    from gateway.run import GatewayRunner
    from hermes_cli.task_intents import TaskIntentManager, load_task_intent

    db = SessionDB(db_path=tmp_path / "state.db")
    runner = _runner(db)
    TaskIntentManager("sid-machine", db=db).record_direct_message("Ship the release")
    runner._judge_direct_task_relationship = AsyncMock(
        side_effect=AssertionError("machine continuations must not reach the relationship judge")
    )
    event = MessageEvent(
        text="[Continuing toward your standing goal]\nGoal: Ship the release",
        message_id="machine-1",
        source=_source(),
        internal=True,
        metadata={"task_intent_machine_origin": "goal_continuation"},
    )

    GatewayRunner._capture_task_intent_ingress(event)
    await runner._record_task_intent_event(
        event=event,
        session_id="sid-machine",
        session_key="discord:user-1:chat-1:thread-1",
    )

    state = load_task_intent("sid-machine", db=db)
    assert state is not None
    assert state.task_contract.raw_primary_text == "Ship the release"
    assert state.task_contract.raw_supplements == []
    assert state.raw_messages[-1].source_kind == "machine_continuation"
    assert state.raw_messages[-1].machine_origin == "goal_continuation"
    assert state.raw_messages[-1].state_effect == "no_change"
    runner._judge_direct_task_relationship.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_applies_structured_supplement_replacement_and_cancellation(tmp_path):
    from gateway.run import GatewayRunner
    from hermes_cli.task_intents import TaskIntentManager, TaskRelationshipDecision, load_task_intent

    db = SessionDB(db_path=tmp_path / "state.db")
    runner = _runner(db)
    TaskIntentManager("sid-transitions", db=db).record_direct_message("Primary Ω task")
    decisions = [
        TaskRelationshipDecision.from_payload(
            {
                "relationship": "supplement",
                "state_effect": "append_contract",
                "confidence": 0.9,
                "evidence_quotes": ["補足"],
            },
            raw_text="補足：keep two variants",
        ),
        TaskRelationshipDecision.from_payload(
            {
                "relationship": "replacement",
                "state_effect": "supersede",
                "confidence": 0.95,
                "evidence_quotes": ["Replace"],
            },
            raw_text="Replace with β exactly",
        ),
        TaskRelationshipDecision.from_payload(
            {
                "relationship": "cancellation",
                "state_effect": "cancel",
                "confidence": 0.99,
                "evidence_quotes": ["Cancel"],
            },
            raw_text="Cancel β task",
        ),
    ]
    runner._judge_direct_task_relationship = AsyncMock(side_effect=decisions)

    for idx, raw in enumerate(
        ["補足：keep two variants", "Replace with β exactly", "Cancel β task"], start=1
    ):
        event = MessageEvent(text=raw, message_id=f"transition-{idx}", source=_source())
        GatewayRunner._capture_task_intent_ingress(event)
        event.text = f"<wrapper>{raw}</wrapper>"
        await runner._record_task_intent_event(
            event=event,
            session_id="sid-transitions",
            session_key="discord:user-1:chat-1:thread-1",
        )

    state = load_task_intent("sid-transitions", db=db)
    assert state is not None
    assert state.task_contract.raw_primary_text == "Replace with β exactly"
    assert state.task_contract.raw_supplements == []
    assert state.status == "cancelled"
    assert state.transition["cancellation_raw_text"] == "Cancel β task"
    assert [item.raw_text for item in state.raw_messages] == [
        "Replace with β exactly",
        "Cancel β task",
    ]
    assert all("<wrapper>" not in item.raw_text for item in state.raw_messages)


def test_legacy_relationship_judge_opt_in_survives_disabled_merged_default(monkeypatch):
    runner = _runner(MagicMock())
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "task_intents": {"relationship_judge": {"enabled": False}},
            "task_intent": {
                "relationship_judge": {"enabled": True, "max_output_tokens": 222}
            },
        },
    )

    config = runner._load_task_intent_micro_judge_config()

    assert config.enabled is True
    assert config.max_output_tokens == 222


def test_task_intent_metadata_survives_transcript_reload_and_marks_machine_user(tmp_path):
    from agent.conversation_compression import _is_real_user_message

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sid-metadata", source="discord")
    raw = "  raw café\nkeep spacing  "
    db.append_message(
        "sid-metadata",
        "user",
        "<decorated>not canonical</decorated>",
        task_intent_metadata={
            "raw_text": raw,
            "source_kind": "direct_external_user",
            "source_id": "discord:user-1",
            "message_id": "meta-1",
            "synthetic": False,
        },
    )
    db.append_message(
        "sid-metadata",
        "user",
        "[Continuing toward your standing goal]\nGoal: ship",
        task_intent_metadata={
            "raw_text": "[Continuing toward your standing goal]\nGoal: ship",
            "source_kind": "machine_continuation",
            "machine_origin": "goal_continuation",
            "synthetic": True,
        },
    )

    messages = db.get_messages_as_conversation("sid-metadata")

    assert messages[0]["_task_intent"]["raw_text"] == raw
    assert messages[0]["_task_intent"]["source_kind"] == "direct_external_user"
    assert _is_real_user_message(messages[0]) is True
    assert messages[1]["_task_intent"]["machine_origin"] == "goal_continuation"
    assert _is_real_user_message(messages[1]) is False


def test_historical_task_snapshot_uses_exact_raw_metadata_and_one_heading():
    from agent.context_compressor import ContextCompressor, HISTORICAL_TASK_HEADING

    raw = "  請保留  spacing\n第二行\tverbatim  "
    messages = [
        {
            "role": "user",
            "content": "<decorated>wrong</decorated>",
            "_task_intent": {
                "raw_text": raw,
                "source_kind": "direct_external_user",
                "synthetic": False,
            },
        }
    ]
    snapshot = ContextCompressor._latest_user_task_snapshot(messages)
    assert snapshot is not None
    assert raw in snapshot
    assert "<decorated>" not in snapshot

    duplicate = (
        f"{HISTORICAL_TASK_HEADING}\nstale one\n\n"
        f"{HISTORICAL_TASK_HEADING}\nstale two\n\n"
        "## Goal\nkeep this"
    )
    grounded = ContextCompressor._ground_historical_task_snapshot(duplicate, messages)
    assert grounded.count(HISTORICAL_TASK_HEADING) == 1
    assert raw in grounded
    assert "## Goal\nkeep this" in grounded


def _expired_entry() -> SessionEntry:
    return SessionEntry(
        session_key="discord:user-1:chat-1",
        session_id="sid-active-goal",
        platform=Platform.DISCORD,
        chat_type="dm",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=1),
    )


def test_active_goal_blocks_idle_reset_and_expiry(tmp_path):
    from hermes_cli.goals import GoalManager

    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="idle", idle_minutes=1)
    store = SessionStore(tmp_path / "sessions", config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    GoalManager("sid-active-goal", db=store._db).set("Ship without losing context")
    entry = _expired_entry()

    assert store._should_reset(entry, _source()) is None
    assert store._is_session_expired(entry) is False


def test_goal_lookup_failure_blocks_idle_reset_and_expiry(tmp_path, monkeypatch):
    config = GatewayConfig()
    config.default_reset_policy = SessionResetPolicy(mode="idle", idle_minutes=1)
    store = SessionStore(tmp_path / "sessions", config)
    entry = _expired_entry()

    class BrokenGoalManager:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("goal DB unavailable")

    monkeypatch.setattr("hermes_cli.goals.GoalManager", BrokenGoalManager)

    assert store._should_reset(entry, _source()) is None
    assert store._is_session_expired(entry) is False
