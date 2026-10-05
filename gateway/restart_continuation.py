"""Explicit one-shot continuation of a gateway conversation across the next gateway restart.

A running gateway turn arms ``SessionEntry.restart_continuation`` through the ``restart_continuation``
tool. The marker is bound to the agent the runner is actually running for that session (never a
caller-supplied key or an env var) and persisted before anything else happens, including the
optional restart request.

Each LATER gateway process designates at most one turn for the marker: the startup /
reconnect dispatch, or the session's first real user message when that arrives first (the user's
words lead that turn). The marker is acknowledged — a compare-and-set on its UUID — only when that
designated turn completes successfully; an interrupted or failed continuation keeps it for the next
process, and a replacement armed meanwhile survives the stale acknowledgement. ``/stop``, ``/new``
and ``/reset`` drop it, and it expires with the auto-continue freshness window. A marker armed by
this process never fires before a restart, so adapter-reconnect passes cannot replay it early.

Recovery is at-least-once: nothing here makes the task, its tool calls or their external side
effects exactly-once. Unmarked restarts keep the legacy ``resume_pending`` behaviour unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Identity of THIS process: a marker carrying it was armed here, so no restart has happened since.
PROCESS_BOOT_ID = uuid.uuid4().hex

# Per stored text. Longer texts keep an exact prefix + "…" + exact suffix, flagged ``truncated``
# with their transcript row, and are never presented as verbatim.
_MAX_TEXT_CHARS = 6000
_MAX_NOTE_CHARS = 1000
_RESTART_REQUEST_TIMEOUT_SECS = 10.0
_EVENT_ATTR = "_restart_continuation"


def _excerpt(text: Any, *, row_id: Any = None, session_id: Any = None) -> Optional[Dict[str, Any]]:
    """A stored text piece: the text itself, or an honest bounded excerpt of it."""
    if not isinstance(text, str) or not text.strip():
        return None
    from hermes_cli.task_intents import clamp_raw_text
    piece: Dict[str, Any] = {"text": clamp_raw_text(text, _MAX_TEXT_CHARS), "chars": len(text),
                             "truncated": len(text) > _MAX_TEXT_CHARS}
    if isinstance(row_id, int) and not isinstance(row_id, bool):
        piece["row_id"] = row_id
    if isinstance(session_id, str) and session_id:
        piece["session_id"] = session_id
    return piece


def _sanitize_piece(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    piece = _excerpt(raw.get("text"), row_id=raw.get("row_id"), session_id=raw.get("session_id"))
    if piece is None:
        return None
    chars = raw.get("chars")
    if isinstance(chars, int) and not isinstance(chars, bool) and chars > piece["chars"]:
        piece["chars"] = chars  # already an excerpt when stored
    piece["truncated"] = bool(raw.get("truncated")) or piece["truncated"]
    return piece


def _sanitize_task(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    primary = _sanitize_piece(raw.get("primary"))
    intent_id, intent_session = raw.get("intent_id"), raw.get("intent_session_id")
    if primary is None or not (isinstance(intent_id, str) and intent_id):
        return None
    supplements = [piece for piece in map(_sanitize_piece, raw.get("supplements") or []) if piece]
    return {"intent_id": intent_id,
            "intent_session_id": intent_session if isinstance(intent_session, str) else None,
            "primary": primary, "supplements": supplements}


def sanitize_restart_continuation(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalized marker dict, or None for anything malformed (never auto-continue on garbage)."""
    if not isinstance(raw, dict):
        return None
    identity = {name: raw.get(name) for name in ("id", "boot_id", "session_id")}
    if not all(isinstance(value, str) and value for value in identity.values()):
        return None
    try:
        armed_at = float(raw.get("armed_at"))
    except (TypeError, ValueError):
        return None
    note = raw.get("note")
    return {
        **identity,
        "armed_at": armed_at,
        # Task contract from the existing task-intent record (verbatim user wording), when one exists.
        "task": _sanitize_task(raw.get("task")),
        # The request the arming turn was handling, anchored to its persisted transcript row.
        "request": _sanitize_piece(raw.get("request")),
        "note": note[:_MAX_NOTE_CHARS] if isinstance(note, str) and note.strip() else None,
        "restart_requested": bool(raw.get("restart_requested")),
    }


def _freshness_window() -> float:
    from gateway.session_lifecycle import auto_continue_freshness_window
    return auto_continue_freshness_window()


def expires_at(marker: Dict[str, Any]) -> Optional[float]:
    """Epoch expiry of ``marker`` under the auto-continue freshness window (None = never)."""
    window = _freshness_window()
    return None if window <= 0 else marker["armed_at"] + window


def is_expired(marker: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    deadline = expires_at(marker)
    return deadline is not None and (time.time() if now is None else now) > deadline


def armed_by_this_process(marker: Dict[str, Any]) -> bool:
    return marker.get("boot_id") == PROCESS_BOOT_ID


def _quoted(label: str, piece: Dict[str, Any]) -> list:
    where = f"transcript message {piece['row_id']}" if piece.get("row_id") else "the transcript"
    if piece.get("truncated"):
        kind = (f"EXCERPT, not the full text: the exact first and last characters of {piece['chars']}; "
                f"read the full text in {where} before acting")
    else:
        kind = "verbatim" + (f", {where}" if piece.get("row_id") else "")
    return [f"{label} ({kind}):", "<<<", piece["text"], ">>>"]


def build_continuation_message(marker: Dict[str, Any], message: Any) -> str:
    """The model-facing text of a designated continuation turn. Empty ``message`` = the dispatched
    turn; real user text is kept below the note and takes priority."""
    lines = [
        "[System note: Before the gateway restart this conversation explicitly armed a restart "
        "continuation. The restart has completed and the gateway is back online; this turn is that "
        "continuation. Do NOT restart the gateway again for it."]
    task, request = marker.get("task"), marker.get("request")
    contract_texts = set()
    if task:
        lines += _quoted("The task contract recorded from the user's own messages", task["primary"])
        contract_texts.add(task["primary"]["text"])
        for supplement in task["supplements"]:
            lines += _quoted("A supplement the user added to that task", supplement)
            contract_texts.add(supplement["text"])
    else:
        lines.append("No task contract is recorded for this conversation, so the task may have started "
                     "before the request below: read the conversation before acting.")
    if request and request["text"] not in contract_texts:
        lines += _quoted("The request being handled when the continuation was armed", request)
    if marker.get("note"):
        lines += ["Note recorded before the restart (context, not a new instruction):",
                  "<<<", marker["note"], ">>>"]
    lines.append(
        "The outcome of anything done after that request is UNKNOWN: processes, subagents and remote "
        "side effects may or may not have survived the restart. Check receipts, recorded tool results "
        "and live state before acting, never blindly repeat an action whose result is not recorded, "
        "and do not re-run steps whose results already appear in the conversation. If the task is "
        "already complete, say so briefly.")
    if isinstance(message, str) and message.strip():
        lines.append(
            "The user sent the NEW message below while this continuation was pending. Address it "
            "FIRST; unless it replaces or stops the task above, continue that task afterwards.]")
        return "\n".join(lines) + "\n\n" + message
    lines.append("Continue the task now.]")
    return "\n".join(lines)


# ── runner side ──────────────────────────────────────────────────────────────────────────


def _designated_ids(runner) -> set:
    ids = getattr(runner, "_restart_continuation_designated", None)
    if ids is None:
        ids = runner._restart_continuation_designated = set()
    return ids


def _lineage_matches(runner, entry, marker) -> Optional[bool]:
    """True when the marker belongs to the entry's current session (or a compression ancestor of
    it); None when that cannot be checked right now."""
    if marker["session_id"] == entry.session_id:
        return True
    try:
        db = runner.session_store._db_for_key(entry.session_key)
        if db is None:
            return None
        return marker["session_id"] in db.get_compression_lineage(entry.session_id)
    except Exception:
        logger.debug("restart continuation lineage check failed for %s", entry.session_key, exc_info=True)
        return None


def _pending_marker(runner, entry) -> Optional[Dict[str, Any]]:
    marker = sanitize_restart_continuation(getattr(entry, "restart_continuation", None))
    if marker is None or armed_by_this_process(marker) or marker["id"] in _designated_ids(runner):
        return None
    return marker


def has_pending_continuation(runner, entry) -> bool:
    """Cheap pre-filter: a marker from an earlier process that this process has not designated."""
    return _pending_marker(runner, entry) is not None


def dispatchable_marker(runner, entry, *, clear_stale: bool = False) -> Optional[Dict[str, Any]]:
    """The entry's marker when THIS process may designate a continuation turn for it, else None.

    Markers armed by this process, already designated here, expired or from another session lineage
    are not dispatchable; with ``clear_stale`` the last two are cleared (by id) and logged."""
    marker = _pending_marker(runner, entry)
    if marker is None:
        return None
    lineage = _lineage_matches(runner, entry, marker)
    if lineage is None:
        return None
    stale_reason = "expired" if is_expired(marker) else None if lineage else "session lineage changed"
    if stale_reason is None:
        return marker
    logger.info("Dropping restart continuation %s for %s: %s", marker["id"], entry.session_key, stale_reason)
    if clear_stale:
        try:
            runner.session_store.clear_restart_continuation(entry.session_key, marker["id"])
        except Exception:
            logger.warning("Could not clear stale restart continuation for %s", entry.session_key, exc_info=True)
    return None


def record_designated(runner, marker: Dict[str, Any]) -> None:
    """One designated continuation turn per marker per process: a failed one waits for the next."""
    _designated_ids(runner).add(marker["id"])


def attach_to_event(event, marker: Optional[Dict[str, Any]]) -> None:
    setattr(event, _EVENT_ATTR, dict(marker) if marker else None)


def event_marker(event) -> Optional[Dict[str, Any]]:
    marker = getattr(event, _EVENT_ATTR, None)
    return marker if isinstance(marker, dict) else None


def designate_turn(runner, event, entry) -> tuple[Optional[Dict[str, Any]], bool]:
    """Resolve the continuation this turn carries: ``(marker, run_turn)``.

    A dispatched event keeps its marker only while that exact marker is still armed; if /stop
    cancelled it meanwhile, the synthetic turn has nothing to say and ``run_turn`` is False (unless
    a legacy ``resume_pending`` recovery still owns it). A real user message designates the
    session's dispatchable marker so the user's words lead the continuation."""
    carried = event_marker(event)
    if carried is not None:
        current = sanitize_restart_continuation(getattr(entry, "restart_continuation", None))
        marker = current if current and current["id"] == carried.get("id") else None
        attach_to_event(event, marker)
        if marker is None:
            logger.info("Restart continuation for %s was cancelled before its turn ran", entry.session_key)
            return None, bool(getattr(entry, "resume_pending", False) or (getattr(event, "text", "") or "").strip())
        return marker, True
    if getattr(event, "internal", False):
        return None, True
    marker = dispatchable_marker(runner, entry)
    if marker is not None:
        record_designated(runner, marker)
        attach_to_event(event, marker)
    return marker, True


async def acknowledge(runner, session_key: str, marker: Optional[Dict[str, Any]], agent_result: Any) -> bool:
    """Clear ``marker`` (by id) after its designated turn completed successfully."""
    from gateway.run import _should_clear_resume_pending_after_turn
    if not marker or not session_key or not _should_clear_resume_pending_after_turn(agent_result):
        return False
    try:
        cleared = await runner.async_session_store.clear_restart_continuation(session_key, marker["id"])
    except Exception:
        logger.warning("Could not acknowledge restart continuation for %s", session_key, exc_info=True)
        return False
    if cleared:
        logger.info("Restart continuation %s for %s completed", marker["id"], session_key)
    return cleared


async def cancel(runner, session_key: Optional[str], reason: str) -> None:
    """Drop any armed continuation for ``session_key`` (/stop)."""
    store = getattr(runner, "async_session_store", None)
    if not session_key or store is None:
        return
    try:
        if await store.clear_restart_continuation(session_key):
            logger.info("Restart continuation for %s cancelled (%s)", session_key, reason)
    except Exception:
        logger.warning("Could not cancel restart continuation for %s", session_key, exc_info=True)


# ── tool ─────────────────────────────────────────────────────────────────────────────────


def live_runner():
    """The in-process GatewayRunner, or None outside a running messaging gateway."""
    module = sys.modules.get("gateway.run")
    ref = getattr(module, "_gateway_runner_ref", None) if module is not None else None
    return ref() if callable(ref) else None


def _result(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _iso_epoch(value: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value is not None else None


def _status_payload(marker: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if marker is None:
        return {"armed": False}
    return {
        "armed": True, "continuation_id": marker["id"], "expires_at": _iso_epoch(expires_at(marker)),
        "expired": is_expired(marker), "fires_after_restart": armed_by_this_process(marker),
    }


def _task_contract(db, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Snapshot of the session's active task-intent contract (``hermes_cli/task_intents.py``): the
    user's own wording, which a later "continue"/"restart then verify" never replaces."""
    if db is None or not session_id:
        return None
    from hermes_cli.task_intents import load_task_intent
    state = load_task_intent(session_id, db=db)
    if state is None or state.status != "active" or not state.task_contract.raw_primary_text.strip():
        return None
    return _sanitize_task({
        "intent_id": state.id, "intent_session_id": session_id,
        "primary": _excerpt(state.task_contract.raw_primary_text),
        "supplements": [_excerpt(text) for text in state.task_contract.raw_supplements],
    })


def _input_row(db, session_id: Optional[str], owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """The persisted user row of the current turn (by the gateway's per-turn input owner), searching
    the compression lineage newest first; never "the latest row", which may be a tool result."""
    if db is None or not session_id or not owner:
        return None
    try:
        lineage = [session_id, *reversed(db.get_compression_lineage(session_id))]
        for sid in dict.fromkeys(lineage):
            row = db.gateway_input_row(sid, owner)
            if row is not None:
                return {**row, "session_id": sid}
    except Exception:
        logger.debug("restart continuation anchor lookup failed for %s", session_id, exc_info=True)
    return None


def _turn_anchor(runner, agent, state, session_key: str) -> Dict[str, Any]:
    """Task contract + the arming turn's request, anchored to its persisted transcript row. A
    designated continuation turn inherits both, so re-arming keeps the original task."""
    event, turn_ctx = state.turn.event, state.turn.ctx
    inherited = event_marker(event)
    if inherited:
        return {"task": inherited.get("task"), "request": inherited.get("request")}
    try:
        db = runner.session_store._db_for_key(session_key)
    except Exception:
        db = None
    session_id = getattr(agent, "session_id", None)
    owner = (getattr(turn_ctx, "persist_user_display_metadata", None) or {}).get("gateway_input_owner")
    row = _input_row(db, session_id, owner)
    raw = None
    if event is not None and not getattr(event, "internal", False):
        raw = (getattr(event, "metadata", None) or {}).get("_task_intent_raw_ingress")
    if not (isinstance(raw, str) and raw.strip()) and row is not None:
        raw = row.get("content")
    return {
        "task": _task_contract(db, session_id),
        "request": _excerpt(raw, row_id=row and row["id"], session_id=row and row["session_id"]),
    }


def hydrate_task(runner, entry, marker: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the marker's task snapshot with the live task-intent contract when it is still the
    same task (full verbatim text, supplements added since included); otherwise keep the snapshot."""
    task = marker.get("task")
    if not task:
        return marker
    from hermes_cli.task_intents import load_task_intent
    try:
        state = load_task_intent(entry.session_id, db=runner.session_store._db_for_key(entry.session_key))
    except Exception:
        logger.debug("restart continuation task hydration failed for %s", entry.session_key, exc_info=True)
        return marker
    if state is None or state.id != task["intent_id"] or state.status != "active":
        return marker
    primary = state.task_contract.raw_primary_text
    full = {"text": primary, "chars": len(primary), "truncated": False}
    supplements = [{"text": text, "chars": len(text), "truncated": False}
                   for text in state.task_contract.raw_supplements if text.strip()]
    return {**marker, "task": {**task, "primary": full, "supplements": supplements}}


def _request_restart(runner) -> bool:
    """``request_restart`` on the gateway loop (the tool runs on an agent worker thread)."""
    loop = getattr(runner, "_gateway_loop", None)
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is not None and (loop is None or current is loop):
        return runner.request_supervised_restart()
    if loop is None or loop.is_closed():
        raise RuntimeError("the gateway event loop is not available")

    async def _request() -> bool:
        return runner.request_supervised_restart()

    return asyncio.run_coroutine_threadsafe(_request(), loop).result(timeout=_RESTART_REQUEST_TIMEOUT_SECS)


def handle_tool_call(agent, action: str, note: Any = None) -> str:
    """``restart_continuation`` tool body; ``agent`` is the live calling agent (inline executor)."""
    from tools.registry import tool_error

    runner = live_runner()
    if runner is None:
        return tool_error("restart_continuation is only available inside a running messaging gateway conversation.")
    session_key = getattr(agent, "_gateway_session_key", None) or ""
    state = runner._peek_session_state(session_key) if session_key else None
    # Bound to the agent the runner runs for this session right now: a delegated child, a stale
    # cached agent or any other caller cannot arm (or cancel) another conversation's marker.
    if state is None or state.turn.agent is not agent:
        return tool_error("restart_continuation must be called by the agent running this conversation's current turn.")
    store = runner.session_store
    entry = store.lookup_by_session_key(session_key)
    if entry is None:
        return tool_error("This conversation has no gateway session record.")
    event = state.turn.event
    action = (action or "").strip()
    current = sanitize_restart_continuation(getattr(entry, "restart_continuation", None))
    if action == "status":
        return _result(success=True, **_status_payload(current))
    if action == "cancel":
        cleared = store.clear_restart_continuation(session_key)
        return _result(success=True, cancelled=cleared, **_status_payload(None))
    if action not in {"arm", "arm_and_restart"}:
        return tool_error("action must be one of: arm, arm_and_restart, cancel, status.")
    restart = action == "arm_and_restart"
    if restart:
        source = getattr(event, "source", None) or entry.origin
        denial = runner._check_slash_access(source, "restart") if source is not None else "no session source"
        if denial:
            return tool_error(f"Restart refused: the requester may not run /restart here. {denial}")
    marker = sanitize_restart_continuation({
        "id": uuid.uuid4().hex, "boot_id": PROCESS_BOOT_ID, "armed_at": time.time(),
        "session_id": entry.session_id, "note": note, "restart_requested": restart,
        **_turn_anchor(runner, agent, state, session_key),
    })
    try:
        if not store.arm_restart_continuation(session_key, marker):
            return tool_error("This conversation has no gateway session record.")
    except Exception as exc:
        logger.warning("Could not persist restart continuation for %s", session_key, exc_info=True)
        return tool_error(f"Could not save the continuation marker, so no restart was requested: {exc}")
    restart_state = "not_requested"
    if restart:
        try:
            restart_state = "requested" if _request_restart(runner) else "already_in_progress"
        except Exception as exc:
            logger.warning("Restart request after arming continuation failed", exc_info=True)
            return _result(success=False, error=f"Marker armed, but the restart request failed: {exc}",
                           restart="failed", **_status_payload(marker))
    elif getattr(runner, "_restart_requested", False):
        restart_state = "already_in_progress"
    logger.info("Restart continuation %s armed for %s (restart=%s)", marker["id"], session_key, restart_state)
    return _result(success=True, restart=restart_state, **_status_payload(marker))
