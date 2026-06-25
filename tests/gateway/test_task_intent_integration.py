from __future__ import annotations

from datetime import datetime, timedelta


from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionStore
from hermes_state import SessionDB


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
    store = SessionStore(tmp_path / "sessions", config)
    store._db = SessionDB(db_path=tmp_path / "state.db")
    GoalManager("sid-active-goal", db=store._db).set("Ship without losing context")
    entry = _expired_entry()

    assert store._route_reset_reason(entry) is None
    assert not entry.suspended


def test_goal_lookup_failure_blocks_idle_reset_and_expiry(tmp_path, monkeypatch):
    config = GatewayConfig()
    store = SessionStore(tmp_path / "sessions", config)
    entry = _expired_entry()

    class BrokenGoalManager:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("goal DB unavailable")

    monkeypatch.setattr("hermes_cli.goals.GoalManager", BrokenGoalManager)

    assert store._route_reset_reason(entry) is None
    assert not entry.suspended


def test_pre_send_guard_buffers_final_streaming_and_fails_open():
    from hermes_cli.pre_send_status_guard import final_streaming_allowed

    assert final_streaming_allowed({"task_intents": {"status_guard": {"enabled": True}}}) is False
    assert final_streaming_allowed({"task_intents": {"status_guard": {"enabled": False}}}) is True
    assert final_streaming_allowed({"task_intents": "malformed"}) is True
    assert final_streaming_allowed(None) is True
