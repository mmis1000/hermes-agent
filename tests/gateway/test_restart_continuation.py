"""Explicit one-shot restart continuation (``gateway/restart_continuation.py``).

Contracts: a marker armed from a live gateway turn survives a clean restart into a FRESH OS
process, which runs a continuation turn through the real adapter admission path with no human
message, carrying the user's own task contract (+ supplements) and the arming request, and clears
the marker (UUID compare-and-set) only after that turn succeeds; a third process replays nothing.
An interrupted/failed continuation keeps the marker for the next process. It never fires in the
arming process, persistence precedes any restart request, stale acknowledgements cannot erase a
replacement, /stop /new /reset withdraw it, profiles keep separate markers.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import tools.restart_continuation_tool  # noqa: F401  # registers the schema
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from gateway import restart_continuation as rc
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource, SessionStore
from gateway.turn_context import TurnContext
from tests.gateway import restart_continuation_harness as harness

HARNESS = Path(harness.__file__).resolve()
REPO_ROOT = HARNESS.parents[2]

LONG_TASK = "Raise the live budget to 50 and keep every guard in place.\n" + "".join(
    f"step {i}: check guard {i} stays enabled\n" for i in range(400))
SUPPLEMENT = "Also keep the old budget as a fallback."
FOLLOWUP = "ok restart then verify"
JUDGE = {
    SUPPLEMENT: {"relationship": "supplement", "state_effect": "append_contract", "confidence": 0.9},
    FOLLOWUP: {"relationship": "same_task", "state_effect": "no_change", "confidence": 0.9},
}


@pytest.fixture
def home(monkeypatch) -> Path:
    """The per-test HERMES_HOME the suite's conftest isolates (never re-pointed mid-process), with
    the test user on the platform allowlist (inherited by child boots)."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "u1")
    return Path(os.environ["HERMES_HOME"])


def _child(scenario: str, args: dict) -> dict:
    """One gateway boot in a separate OS process on the same HERMES_HOME."""
    env = {**os.environ, "HERMES_DISABLE_LAZY_INSTALLS": "1", "PYTHONPATH": str(REPO_ROOT)}
    out = subprocess.run([sys.executable, str(HARNESS), scenario, json.dumps(args)], cwd=REPO_ROOT,
                         env=env, capture_output=True, text=True, timeout=240)
    lines = [line for line in out.stdout.splitlines() if line.startswith("RESULT ")]
    assert out.returncode == 0 and lines, f"child {scenario} failed:\n{out.stdout[-3000:]}\n{out.stderr[-6000:]}"
    return json.loads(lines[-1][len("RESULT "):])


def _arm_boot(*, interrupted_script=False) -> dict:
    """Boot 1: task, supplement, then the follow-up turn arms + requests the restart."""
    return _child("arm", {
        "messages": [LONG_TASK, SUPPLEMENT, FOLLOWUP], "judge": JUDGE,
        "script": [{"final": "Plan noted."}, {"final": "Fallback noted."},
                   {"tool": {"action": "arm_and_restart", "note": "verify /status shows 50"},
                    "final": "Restarting now."}],
    })


def _in_process_boot(monkeypatch, script):
    harness.install_fakes(monkeypatch.setattr)
    runner, adapter, restarts = harness.boot(monkeypatch.setattr)
    harness.ScriptedAgent.calls = []
    harness.ScriptedAgent.script = list(script)
    return runner, adapter


# ── fresh-process boundary through the real adapter path ─────────────────────────────────


@pytest.mark.asyncio
async def test_clean_handoff_continues_after_restart_without_human_then_never_again(home, monkeypatch):
    boot1 = _arm_boot()
    assert len(boot1["restarts"]) == 1, boot1
    arm_call = boot1["calls"][2]["tool_result"]
    assert arm_call["success"] and arm_call["restart"] == "requested"
    marker = boot1["marker"]
    assert marker["task"]["primary"]["truncated"] is True  # stored as an honest excerpt
    assert [s["text"] for s in marker["task"]["supplements"]] == [SUPPLEMENT]
    assert marker["request"]["text"] == FOLLOWUP and isinstance(marker["request"]["row_id"], int)

    # Boot 2 (this process): the startup pass alone produces the continuation turn.
    runner, adapter = _in_process_boot(monkeypatch, [{"final": "Verified: budget is 50."}])
    assert await harness.startup_restore(runner, adapter) == 1
    [turn] = harness.ScriptedAgent.calls
    note = turn["message"]
    assert LONG_TASK in note  # the full user wording, hydrated from the task-intent record
    assert SUPPLEMENT in note and FOLLOWUP in note and "verify /status shows 50" in note
    assert "EXCERPT" not in note
    assert "ask what they would like to do next" not in note
    assert "Verified: budget is 50." in adapter.sent
    after = harness.session_snapshot(runner)
    assert after["session_id"] == boot1["session_id"]  # same conversation, not a healed/new one
    assert after["marker"] is None

    # The arming row the marker pointed at is the persisted follow-up.
    db = runner.session_store._db_for_key(after["key"])
    assert db.gateway_input_row  # API used for the anchor
    rows = db.get_messages(boot1["session_id"])
    assert any(r.get("id") == marker["request"]["row_id"] and r.get("content") == FOLLOWUP for r in rows)

    # Boot 3: nothing left to replay.
    boot3 = _child("boot", {"script": [{"final": "should not run"}]})
    assert boot3["scheduled"] == 0 and boot3["calls"] == []
    assert boot3["session_id"] == boot1["session_id"] and boot3["marker"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"interrupted": True}, {"failed": True, "error": "provider down"}],
                         ids=["interrupted", "failed"])
async def test_interrupted_or_failed_continuation_retries_in_next_process_only(home, monkeypatch, result):
    boot1 = _arm_boot()
    runner, adapter = _in_process_boot(monkeypatch, [{"final": "", "result": result}])
    assert await harness.startup_restore(runner, adapter) == 1
    kept = harness.session_snapshot(runner)
    assert kept["session_id"] == boot1["session_id"]
    assert kept["marker"]["id"] == boot1["marker"]["id"]
    # Same process: a reconnect pass does not loop on it.
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0

    boot3 = _child("boot", {"script": [{"final": "Verified."}]})
    assert boot3["scheduled"] == 1 and len(boot3["calls"]) == 1
    assert boot3["marker"] is None and boot3["session_id"] == boot1["session_id"]


@pytest.mark.asyncio
async def test_user_message_first_after_restart_leads_the_designated_turn(home, monkeypatch):
    boot1 = _arm_boot()
    runner, adapter = _in_process_boot(monkeypatch, [{"final": "Answered, then verified."}])
    await harness.deliver(runner, adapter, "what's the current budget?", "after-restart-1")
    [turn] = harness.ScriptedAgent.calls
    assert turn["message"].endswith("what's the current budget?")
    assert "Address it FIRST" in turn["message"] and SUPPLEMENT in turn["message"]
    assert harness.session_snapshot(runner)["marker"] is None
    assert runner._schedule_resume_pending_sessions() == 0  # nothing else replays it here
    assert boot1["marker"]["id"]


@pytest.mark.asyncio
async def test_marker_never_fires_in_the_arming_process(home, monkeypatch):
    harness.install_fakes(monkeypatch.setattr, JUDGE)
    runner, adapter, restarts = harness.boot(monkeypatch.setattr)
    harness.ScriptedAgent.calls = []
    harness.ScriptedAgent.script = [{"tool": {"action": "arm"}, "final": "Armed."}]
    await harness.deliver(runner, adapter, "finish the migration", "m-1")
    assert harness.ScriptedAgent.calls[0]["tool_result"]["restart"] == "not_requested"
    assert restarts == []
    marker = harness.session_snapshot(runner)["marker"]
    assert marker and marker["boot_id"] == rc.PROCESS_BOOT_ID
    assert runner._schedule_resume_pending_sessions() == 0
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0  # reconnect pass
    assert harness.session_snapshot(runner)["marker"]["id"] == marker["id"]


@pytest.mark.asyncio
async def test_stop_after_restart_cancels_before_the_continuation(home, monkeypatch):
    _arm_boot()
    runner, adapter = _in_process_boot(monkeypatch, [{"final": "should not run"}])
    await harness.deliver(runner, adapter, "/stop", "stop-1")
    assert harness.session_snapshot(runner)["marker"] is None
    assert await harness.startup_restore(runner, adapter) == 0
    assert harness.ScriptedAgent.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/reset"])
async def test_new_and_reset_drop_the_marker(home, monkeypatch, command):
    boot1 = _arm_boot()
    runner, adapter = _in_process_boot(monkeypatch, [])
    await harness.deliver(runner, adapter, command, "cmd-1")
    await harness.deliver(runner, adapter, "/approve", "cmd-2")  # the destructive-command confirmation
    after = harness.session_snapshot(runner)
    assert after["session_id"] != boot1["session_id"], adapter.sent
    assert after["marker"] is None
    assert await harness.startup_restore(runner, adapter) == 0
    assert harness.ScriptedAgent.calls == []


# ── unit contracts on the same production seams ─────────────────────────────────────────


def _source(chat_id: str = "c1") -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id="u1")


def _store(path: Path) -> SessionStore:
    return SessionStore(sessions_dir=path / "sessions", config=GatewayConfig())


def _marker(session_id: str, **extra) -> dict:
    return rc.sanitize_restart_continuation({
        "id": uuid.uuid4().hex, "boot_id": "earlier-process", "armed_at": datetime.now().timestamp(),
        "session_id": session_id, "request": {"text": "verify the new live budget"}, **extra,
    })


def _runner(monkeypatch, store: SessionStore):
    harness.install_fakes(monkeypatch.setattr)
    import gateway.run as gr
    runner = gr.GatewayRunner(GatewayConfig())
    runner.session_store = store
    runner._is_user_authorized_for_source = lambda _source: True
    runner._persist_active_agents = lambda: None
    adapter = MagicMock()
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock()
    adapter._session_tasks = {}
    runner._delivery_adapter_for = lambda _source: adapter
    return runner, adapter


async def _dispatched(runner, adapter) -> list:
    count = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0.05)
    events = [call.args[0] for call in adapter.handle_message.await_args_list]
    assert count == len(events)
    return events


def test_long_texts_are_excerpts_never_labelled_verbatim():
    marker = _marker("s", request={"text": "x" * 7000, "row_id": 42})
    assert marker["request"]["truncated"] and marker["request"]["chars"] == 7000
    note = rc.build_continuation_message(marker, "")
    assert "EXCERPT" in note and "transcript message 42" in note
    assert "verbatim" not in note.split("EXCERPT")[0].splitlines()[-1]
    short = rc.build_continuation_message(_marker("s", request={"text": "short ask", "row_id": 7}), "")
    assert "(verbatim, transcript message 7)" in short and "No task contract" in short


def test_followup_never_replaces_the_task_contract():
    marker = _marker("s", request={"text": "continue"}, task={
        "intent_id": "task-1", "intent_session_id": "s", "primary": {"text": "migrate the DB"},
        "supplements": [{"text": "keep backups"}]})
    note = rc.build_continuation_message(marker, "")
    assert note.index("migrate the DB") < note.index("keep backups") < note.index("continue")


@pytest.mark.asyncio
async def test_explicit_and_legacy_markers_yield_one_turn_with_explicit_wording(home, monkeypatch):
    store = _store(home)
    entry = store.get_or_create_session(_source())
    store.arm_restart_continuation(entry.session_key, _marker(entry.session_id))
    store.mark_resume_pending(entry.session_key, "restart_timeout")
    runner, adapter = _runner(monkeypatch, store)
    [event] = await _dispatched(runner, adapter)
    ctx = TurnContext(message="", history=[], session_key=entry.session_key, source=_source(),
                      restart_continuation=rc.event_marker(event))
    persist, _ = TurnRunner(runner, ctx)._prepare_turn_message([])
    assert "verify the new live budget" in ctx.message
    assert "ask what they would like to do next" not in ctx.message
    assert persist == ctx.message  # no blank user row


@pytest.mark.asyncio
async def test_continuation_waits_for_redelivery_of_previous_answer(home, monkeypatch):
    store = _store(home)
    entry = store.get_or_create_session(_source())
    store.arm_restart_continuation(entry.session_key, _marker(entry.session_id))
    runner, adapter = _runner(monkeypatch, store)
    redelivered = asyncio.Event()
    order: list = []

    async def boot_sends():
        await redelivered.wait()
        order.append("redelivered")

    runner._startup_redelivery = (asyncio.create_task(boot_sends()), frozenset({entry.session_key}))
    adapter.handle_message.side_effect = lambda _event: order.append("continuation")
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0.05)
    assert order == []
    redelivered.set()
    await asyncio.sleep(0.05)
    assert order == ["redelivered", "continuation"]


def test_redelivery_claim_and_legacy_clears_leave_marker(home, monkeypatch):
    store = _store(home)
    entry = store.get_or_create_session(_source())
    marker = _marker(entry.session_id)
    store.arm_restart_continuation(entry.session_key, marker)
    store.mark_resume_pending(entry.session_key, "restart_timeout")
    store.clear_resume_pending(entry.session_key)
    store.recover_interrupted_turns()
    store.discard_active_turn_markers()
    runner, _ = _runner(monkeypatch, store)
    asyncio.run(runner._clear_resume_pending_for_claimed_obligations([{"session_key": entry.session_key}]))
    assert _store(home).lookup_by_session_key(entry.session_key).restart_continuation["id"] == marker["id"]


@pytest.mark.asyncio
async def test_expired_marker_is_dropped_not_dispatched(home, monkeypatch):
    monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "60")
    store = _store(home)
    entry = store.get_or_create_session(_source())
    store.arm_restart_continuation(entry.session_key, _marker(entry.session_id, armed_at=0.0))
    runner, adapter = _runner(monkeypatch, store)
    assert await _dispatched(runner, adapter) == []
    assert store.lookup_by_session_key(entry.session_key).restart_continuation is None


def test_stale_acknowledgement_cannot_erase_replacement(home):
    store = _store(home)
    entry = store.get_or_create_session(_source())
    first, replacement = _marker(entry.session_id), _marker(entry.session_id)
    store.arm_restart_continuation(entry.session_key, first)
    store.arm_restart_continuation(entry.session_key, replacement)
    assert not store.clear_restart_continuation(entry.session_key, first["id"])
    assert _store(home).lookup_by_session_key(entry.session_key).restart_continuation["id"] == replacement["id"]
    assert store.clear_restart_continuation(entry.session_key, replacement["id"])


def test_failed_save_leaves_marker_unpublished(home, monkeypatch):
    store = _store(home)
    entry = store.get_or_create_session(_source())
    monkeypatch.setattr(store, "_save_entry", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        store.arm_restart_continuation(entry.session_key, _marker(entry.session_id))
    assert store.lookup_by_session_key(entry.session_key).restart_continuation is None


def test_profiles_keep_separate_markers_a_b_a(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("a", "b")}
    keys = {}
    for name, path in homes.items():
        path.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(path))
        store = _store(path)
        entry = store.get_or_create_session(_source(chat_id="same-chat"))
        keys[name] = entry.session_key
        if name == "a":
            store.arm_restart_continuation(entry.session_key, _marker(entry.session_id))
    monkeypatch.setenv("HERMES_HOME", str(homes["b"]))
    assert _store(homes["b"]).lookup_by_session_key(keys["b"]).restart_continuation is None
    monkeypatch.setenv("HERMES_HOME", str(homes["a"]))
    assert _store(homes["a"]).lookup_by_session_key(keys["a"]).restart_continuation is not None


# ── tool: caller binding, permission, ordering ───────────────────────────────────────────


class _Agent:
    def __init__(self, session_key: str, session_id: str):
        self._gateway_session_key = session_key
        self.session_id = session_id


def _tool_setup(home, monkeypatch):
    store = _store(home)
    source = _source()
    entry = store.get_or_create_session(source)
    runner, _ = _runner(monkeypatch, store)
    agent = _Agent(entry.session_key, entry.session_id)
    state = runner._session_state(entry.session_key)
    state.turn.agent = agent
    state.turn.event = MessageEvent(text="deploy the budget change", source=source,
                                    metadata={"_task_intent_raw_ingress": "deploy the budget change"})
    restarts: list = []

    def fake_request_restart(**kw):
        # Durable BEFORE the restart is requested: a fresh reader already sees it.
        restarts.append(_store(home).lookup_by_session_key(entry.session_key).restart_continuation)
        return True

    runner.request_restart = fake_request_restart
    return runner, store, entry, agent, restarts


def _call(agent, **args):
    return json.loads(INLINE_TOOL_EXECUTORS["restart_continuation"](
        agent, args, InlineToolContext(effective_task_id="t")))


@pytest.mark.asyncio
async def test_failed_persist_requests_no_restart(home, monkeypatch):
    runner, store, entry, agent, restarts = _tool_setup(home, monkeypatch)
    monkeypatch.setattr(store, "_save_entry", MagicMock(side_effect=OSError("disk full")))
    assert "error" in _call(agent, action="arm_and_restart") and restarts == []
    assert store.lookup_by_session_key(entry.session_key).restart_continuation is None


@pytest.mark.asyncio
async def test_persist_precedes_restart_request_across_the_loop_hop(home, monkeypatch):
    runner, store, entry, agent, restarts = _tool_setup(home, monkeypatch)
    runner._gateway_loop = asyncio.get_running_loop()
    result = await asyncio.to_thread(_call, agent, action="arm_and_restart")
    assert result["success"] and result["restart"] == "requested"
    assert [m["id"] for m in restarts] == [result["continuation_id"]]


@pytest.mark.asyncio
async def test_only_the_running_agent_can_arm(home, monkeypatch):
    runner, store, entry, agent, restarts = _tool_setup(home, monkeypatch)
    impostor = _Agent(entry.session_key, entry.session_id)  # same key, not the running agent
    monkeypatch.setenv("HERMES_SESSION_KEY", entry.session_key)
    assert "error" in _call(impostor, action="arm")
    from tools.registry import registry
    assert "error" in json.loads(registry.dispatch("restart_continuation", {"action": "arm"}))
    assert store.lookup_by_session_key(entry.session_key).restart_continuation is None


@pytest.mark.asyncio
async def test_restart_needs_slash_restart_permission(home, monkeypatch):
    runner, store, entry, agent, restarts = _tool_setup(home, monkeypatch)
    runner._check_slash_access = lambda _source, cmd: "admin only" if cmd == "restart" else None
    assert "error" in _call(agent, action="arm_and_restart")
    assert restarts == [] and store.lookup_by_session_key(entry.session_key).restart_continuation is None
    assert _call(agent, action="arm")["success"]  # arming alone requests nothing


@pytest.mark.asyncio
async def test_rearm_in_continuation_turn_keeps_original_task_and_request(home, monkeypatch):
    runner, store, entry, agent, restarts = _tool_setup(home, monkeypatch)
    original = _marker(entry.session_id, request={"text": "the original ask", "row_id": 5}, task={
        "intent_id": "task-1", "intent_session_id": entry.session_id, "primary": {"text": "migrate"}})
    event = runner._session_state(entry.session_key).turn.event
    event.internal = True
    rc.attach_to_event(event, original)
    assert _call(agent, action="arm")["success"]
    marker = store.lookup_by_session_key(entry.session_key).restart_continuation
    assert marker["id"] != original["id"]
    assert (marker["task"], marker["request"]) == (original["task"], original["request"])


def test_tool_exposed_on_messaging_platforms_only():
    from toolsets import resolve_toolset

    assert "restart_continuation" in resolve_toolset("hermes-discord")
    assert "restart_continuation" in resolve_toolset("hermes-telegram")
    for name in ("hermes-cli", "hermes-cron", "hermes-api-server", "hermes-webhook", "hermes-acp"):
        assert "restart_continuation" not in resolve_toolset(name)
