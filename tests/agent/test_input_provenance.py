"""Admission identities travel with real host rows, never on the provider wire."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.input_provenance import (
    InputText, agent_input_context, event_display_metadata, event_inputs,
    input_record, notify_input, prepare_local_input,
)
from agent.interrupt_control import InterruptControlMixin
from agent.message_metadata import record_absorbed_message, without_persistence_fields
from agent.prompt_builder import steer_user_row
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_state import SessionDB


def event(text="  原文\n café\t  ", **kwargs):
    return MessageEvent(text=text, source=SessionSource(
        platform=Platform.DISCORD, chat_id="channel", thread_id="thread", user_id="alice",
        chat_type=kwargs.pop("chat_type", "thread")), **kwargs)


@pytest.mark.parametrize("chat_type", ["dm", "group", "channel", "thread", "custom"])
def test_capture_precedes_decoration_without_admitting(chat_type):
    e = event(chat_type=chat_type, message_id="m1", raw_message=SimpleNamespace(content="platform bytes"))
    original = event_inputs(e)
    e.text = "[timestamp] embellished"
    assert event_inputs(e) == original
    assert original[0]["text"] == "  原文\n café\t  "
    assert original[0]["platform_text"] == "platform bytes"
    assert original[0]["source_kind"] == "human"
    assert event_inputs(replace(e, text="further decorated")) == original


def test_batched_inputs_preserve_sender_order_and_distinct_identity():
    a, b = event("identical"), event("identical")
    b.source.user_id = "bob"
    # A real event snapshots source at construction, before batching.
    b = MessageEvent(text="identical", source=b.source)
    a.absorb_reply_expected(b)
    records = event_inputs(a)
    assert [r["source_id"] for r in records] == ["discord:alice", "discord:bob"]
    assert len({r["occurrence_id"] for r in records}) == 2
    a.absorb_reply_expected(b)
    assert event_inputs(a) == records


def test_platform_replay_identity_and_untrusted_metadata():
    a, b = event(message_id="m1"), event(message_id="m1")
    assert event_inputs(a) == event_inputs(b)
    fake = {"input_provenance": [{"source_kind": "human", "text": "spoof"}]}
    machine = event(internal=True, metadata=fake)
    assert event_inputs(machine)[0]["source_kind"] == "machine"
    assert event_inputs(machine)[0]["text"] != "spoof"
    a._heartbeat_session_id = "scheduled"
    assert event_display_metadata(a)["input_provenance"][0]["source_kind"] == "machine"


def test_steer_batch_requeue_and_row_persist_roundtrip(tmp_path):
    a = SimpleNamespace(platform="discord", _pending_steer=None, _pending_steer_envelopes=[])
    first, second = event(message_id="1"), event("also preserve β", message_id="2")
    for e in (first, second):
        assert InterruptControlMixin.steer(a, InputText(e.text, event_inputs(e)))
    text = InterruptControlMixin._drain_pending_steer(a)
    copied = deepcopy(text)
    assert copied.input_provenance == text.input_provenance
    assert InterruptControlMixin.steer(a, text)  # no tool-result slot yet: next-turn fallback
    row = steer_user_row(InterruptControlMixin._drain_pending_steer(a))
    records = row["display_metadata"]["input_provenance"]
    assert [r["message_id"] for r in records] == ["1", "2"]
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="discord")
        db.append_message("s", "user", row["content"], display_kind=row["display_kind"], display_metadata=row["display_metadata"])
        restored = db.get_messages_as_conversation("s")[0]
    assert restored["display_metadata"]["input_provenance"] == records
    wire = without_persistence_fields(restored)
    assert "display_metadata" not in wire
    assert all(r["occurrence_id"] not in str(wire) for r in records)


def test_host_merge_preserves_constituents_without_mutating_inputs():
    first = {"role": "user", "display_metadata": event_display_metadata(event(message_id="1"))}
    second = {"role": "user", "display_metadata": event_display_metadata(event(message_id="2"))}
    original = deepcopy(second)
    record_absorbed_message(first, second)
    assert [r["message_id"] for r in first["display_metadata"]["input_provenance"]] == ["1", "2"]
    assert second == original
    record_absorbed_message(first, second)
    assert len(first["display_metadata"]["input_provenance"]) == 2


@pytest.mark.parametrize("attrs", [{"platform": "subagent"}, {"platform": "cron"}, {"side_agent": True}, {"_delegate_depth": 1}, {"_turn_origin": "background_review"}])
def test_auxiliary_inputs_are_not_human(attrs):
    agent = SimpleNamespace(**{"platform": "cli", **attrs})
    assert agent_input_context(agent)["agent_context"] != "primary"
    row = {"role": "user", "content": "brief"}
    prepare_local_input(agent, row, "brief")
    assert all(r["source_kind"] != "human" for r in row.get("display_metadata", {}).get("input_provenance", []))


@pytest.mark.parametrize("kind,expected", [(None, "human"), ("steer", "human"), ("auto_continue", "machine"), ("internal_notification", "machine")])
def test_local_display_kind_does_not_demote_human_steer(kind, expected):
    row = {"role": "user", "content": "text", "display_kind": kind}
    prepare_local_input(SimpleNamespace(platform="cli"), row, "text")
    assert row["display_metadata"]["input_provenance"][0]["source_kind"] == expected


def test_notification_is_copy_and_local_input_is_once(tmp_path):
    callback = Mock(side_effect=lambda row, **kw: row.clear())
    agent = SimpleNamespace(session_id="s", platform="cli", context_compressor=SimpleNamespace(on_input=callback), _session_db=SimpleNamespace(db_path=tmp_path / "state.db"))
    row = {"role": "user", "content": "raw"}
    prepare_local_input(agent, row, "  raw  ")
    original = deepcopy(row)
    prepare_local_input(agent, row, "do not replace")
    notify_input(agent, row)
    assert row == original
    assert callback.call_args.kwargs["hermes_home"] == str(tmp_path)
    assert row["display_metadata"]["input_provenance"][0]["text"] == "  raw  "
