#!/usr/bin/env python3
"""Model-facing lifecycle controls for background subagent delegations."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

from tools import async_delegation as _async
from tools import delegate_tool as _delegate
from tools.approval import get_current_session_key
from tools.registry import registry

_ACTIONS = {"list", "status", "tail", "wait", "steer", "resume", "interrupt", "abandon"}
_TOOL_FIELDS = {
    "action",
    "delegation_id",
    "subagent_id",
    "timeout_seconds",
    "limit",
    "cascade",
    "reason",
    "message",
}
_ACTION_FIELDS = {
    "list": set(),
    "status": {"delegation_id", "subagent_id"},
    "tail": {"delegation_id", "subagent_id", "limit"},
    "wait": {"delegation_id", "timeout_seconds"},
    "steer": {"delegation_id", "subagent_id", "message"},
    "resume": {"delegation_id", "subagent_id", "message"},
    "interrupt": {"delegation_id", "subagent_id", "cascade", "reason"},
    "abandon": {"delegation_id", "cascade", "reason"},
}
_MAX_WAIT_SECONDS = 300.0
_MAX_TAIL_EVENTS = 64
_MAX_REASON_CHARS = 1000
_MAX_STEER_CHARS = 12000
_MAX_ID_CHARS = 200


def check_delegation_control_requirements() -> bool:
    return True


def _invalid(action: str, error: str) -> str:
    return json.dumps(
        {"action": action or "delegation", "status": "invalid_arguments", "error": error},
        ensure_ascii=False,
    )


def _resolve_session_key(session_key: Optional[str]) -> str:
    if session_key is not None:
        return session_key
    return get_current_session_key(default="")


def _control_session_candidates(session_key: str) -> List[str]:
    """Exact controller plus proven compression ancestors, fail-closed."""
    if not session_key:
        return [session_key]
    try:
        from hermes_state import SessionDB

        candidates = SessionDB().control_session_candidates(session_key)
    except Exception:
        candidates = []
    return candidates or [session_key]


def _redact_reason(reason: Optional[str]) -> str:
    text = str(reason or "")
    return _delegate.redact_observable_text(text) if text else ""


def _validate(
    *,
    action: str,
    delegation_id: Optional[str],
    subagent_id: Optional[str],
    timeout_seconds: Optional[float],
    limit: Optional[int],
    cascade: Optional[bool],
    reason: Optional[str],
    message: Optional[str],
) -> Optional[str]:
    if action not in _ACTIONS:
        return f"Unknown action {action!r}; expected one of {sorted(_ACTIONS)}."
    supplied = {
        key
        for key, value in {
            "delegation_id": delegation_id,
            "subagent_id": subagent_id,
            "timeout_seconds": timeout_seconds,
            "limit": limit,
            "cascade": cascade,
            "reason": reason,
            "message": message,
        }.items()
        if value is not None
    }
    disallowed = sorted(supplied - _ACTION_FIELDS[action])
    if disallowed:
        return f"Action {action!r} does not accept: {', '.join(disallowed)}."
    for name, value in (("delegation_id", delegation_id), ("subagent_id", subagent_id)):
        if value is not None and not isinstance(value, str):
            return f"{name} must be a string."
        if isinstance(value, str) and len(value) > _MAX_ID_CHARS:
            return f"{name} must not exceed {_MAX_ID_CHARS} characters."
    if action != "list" and not str(delegation_id or "").strip():
        return f"Action {action!r} requires delegation_id."
    if subagent_id is not None and not subagent_id.strip():
        return "subagent_id must not be empty."
    if action in {"steer", "resume"} and not str(subagent_id or "").strip():
        return f"Action {action!r} requires subagent_id."
    if message is not None and not isinstance(message, str):
        return "message must be a string."
    if action in {"steer", "resume"} and not str(message or "").strip():
        return f"Action {action!r} requires a non-empty message."
    if isinstance(message, str) and len(message) > _MAX_STEER_CHARS:
        return f"message must not exceed {_MAX_STEER_CHARS} characters."
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            return "timeout_seconds must be a number."
        timeout_value = float(timeout_seconds)
        if not math.isfinite(timeout_value):
            return "timeout_seconds must be finite."
        if timeout_value < 0:
            return "timeout_seconds must be non-negative."
        if timeout_value > _MAX_WAIT_SECONDS:
            return f"timeout_seconds must not exceed {int(_MAX_WAIT_SECONDS)}."
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            return "limit must be an integer."
        if limit < 1:
            return "limit must be at least 1."
        if limit > _MAX_TAIL_EVENTS:
            return f"limit must not exceed {_MAX_TAIL_EVENTS}."
    if cascade is not None and not isinstance(cascade, bool):
        return "cascade must be a boolean."
    if reason is not None and not isinstance(reason, str):
        return "reason must be a string."
    if reason is not None and len(reason) > _MAX_REASON_CHARS:
        return f"reason must not exceed {_MAX_REASON_CHARS} characters."
    return None


def _active_children(delegation_id: str, *, session_key: str) -> List[Dict[str, Any]]:
    children = []
    for child in _delegate.list_active_subagents():
        child_id = str(child.get("subagent_id") or "")
        if child_id and _async.delegation_contains_subagent(
            delegation_id, child_id, session_key=session_key
        ):
            children.append(child)
    return children


def _safe_activity(value: Any) -> Optional[Dict[str, Any]]:
    """Expose operational counters only; never provider reasoning payloads."""
    if not isinstance(value, dict):
        return None
    allowed = {
        "last_activity_ts",
        "last_activity_desc",
        "seconds_since_activity",
        "current_tool",
        "api_call_count",
        "max_iterations",
        "budget_used",
        "budget_max",
    }
    return {key: value.get(key) for key in allowed if value.get(key) is not None}


def _children_for_record(
    record: Dict[str, Any],
    *,
    session_key: str,
    subagent_id: Optional[str] = None,
    include_tail: bool = False,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    delegation_id = str(record.get("delegation_id") or "")
    pending_interrupt_ids = _async.pending_subagent_interrupt_ids(
        delegation_id, session_key=session_key
    )
    by_id: Dict[str, Dict[str, Any]] = {}

    for child_id, archived in (record.get("children") or {}).items():
        if not isinstance(child_id, str):
            continue
        item = dict(archived) if isinstance(archived, dict) else {}
        item["subagent_id"] = child_id
        item["live"] = False
        by_id[child_id] = item

    for active in _active_children(delegation_id, session_key=session_key):
        child_id = str(active.get("subagent_id") or "")
        active = dict(active)
        active["live"] = True
        by_id[child_id] = {**by_id.get(child_id, {}), **active}

    roots = list(record.get("root_subagent_ids") or [])
    for child_id in roots:
        by_id.setdefault(
            child_id,
            {
                "subagent_id": child_id,
                "status": (
                    "interrupt_requested"
                    if child_id in pending_interrupt_ids
                    else (
                        "starting"
                        if record.get("worker_status") in _async._ACTIVE_STATES
                        else record.get("worker_status")
                    )
                ),
                "live": False,
            },
        )

    if subagent_id:
        branch_ids = {subagent_id}
        changed = True
        while changed:
            changed = False
            for child_id, child in by_id.items():
                if child_id not in branch_ids and child.get("parent_id") in branch_ids:
                    branch_ids.add(child_id)
                    changed = True
        by_id = {child_id: child for child_id, child in by_id.items() if child_id in branch_ids}

    root_order = {child_id: index for index, child_id in enumerate(roots)}
    children = sorted(
        by_id.values(),
        key=lambda item: (
            int(item.get("depth") or 0),
            root_order.get(item.get("subagent_id"), len(root_order)),
            float(item.get("started_at") or item.get("last_activity_at") or 0),
            str(item.get("subagent_id") or ""),
        ),
    )

    output = []
    for child in children:
        item = {
            key: child.get(key)
            for key in (
                "subagent_id",
                "parent_id",
                "depth",
                "goal",
                "model",
                "started_at",
                "status",
                "interrupt_reason",
                "tool_count",
                "last_tool",
                "last_activity_at",
                "live",
            )
            if child.get(key) is not None
        }
        activity = _safe_activity(child.get("activity"))
        if activity:
            item["activity"] = activity
        steers = child.get("steers")
        if isinstance(steers, list) and steers:
            item["steers"] = [dict(steer) for steer in steers[-20:] if isinstance(steer, dict)]
        if include_tail:
            item["assistant_text_tail"] = _delegate.redact_observable_text(
                child.get("assistant_text_tail") or ""
            )
            raw_events = child.get("events")
            events: List[Any] = raw_events if isinstance(raw_events, list) else []
            # The producer only stores tool.started/tool.completed. Re-filter the
            # archive so legacy reasoning records can never cross this boundary.
            item["events"] = [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("type") in {"tool.started", "tool.completed"}
            ][-limit:]
        output.append(item)
    return output


def _not_found(action: str, delegation_id: str) -> str:
    return json.dumps(
        {"action": action, "status": "not_found", "delegation_id": delegation_id},
        ensure_ascii=False,
    )


def delegation_control(
    *,
    action: str,
    delegation_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    limit: Optional[int] = None,
    cascade: Optional[bool] = None,
    reason: Optional[str] = None,
    message: Optional[str] = None,
    session_key: Optional[str] = None,
    parent_agent=None,
) -> str:
    """Execute one session-scoped delegation lifecycle action."""
    if not isinstance(action, str):
        return _invalid("delegation", "action must be a string.")
    action = action.strip().lower()
    error = _validate(
        action=action,
        delegation_id=delegation_id,
        subagent_id=subagent_id,
        timeout_seconds=timeout_seconds,
        limit=limit,
        cascade=cascade,
        reason=reason,
        message=message,
    )
    if error:
        return _invalid(action, error)

    caller_session = _resolve_session_key(session_key)
    owner_candidates = _control_session_candidates(caller_session)
    origin = caller_session
    audit_reason = _redact_reason(reason)

    if action == "list":
        delegations = []
        for record in _async.list_durable_delegations(session_keys=owner_candidates):
            record_origin = str(record.get("session_key") or "")
            delegations.append(
                {
                    "delegation_id": record.get("delegation_id"),
                    "goal": record.get("goal"),
                    "count": len(record.get("goals") or [record.get("goal")]),
                    "worker_status": record.get("worker_status"),
                    "delivery_disposition": record.get("delivery_disposition"),
                    "dispatched_at": record.get("dispatched_at"),
                    "completed_at": record.get("completed_at"),
                    "subagents": _children_for_record(
                        record, session_key=record_origin, include_tail=False
                    ),
                }
            )
        return json.dumps(
            {"action": action, "status": "ok", "delegations": delegations},
            ensure_ascii=False,
        )

    target = str(delegation_id or "").strip()
    child_target = subagent_id.strip() if isinstance(subagent_id, str) else None
    record = None
    for owner_candidate in owner_candidates:
        record = _async.get_async_delegation(target, session_key=owner_candidate)
        if record is not None:
            origin = owner_candidate
            break
    if record is None:
        return _not_found(action, target)
    if child_target and not _async.delegation_contains_subagent(
        target, child_target, session_key=origin
    ):
        return _not_found(action, target)

    if action in {"status", "tail"}:
        include_tail = action == "tail"
        event_limit = min(_MAX_TAIL_EVENTS, int(limit or 20))
        payload = {
            "action": action,
            "status": record.get("worker_status"),
            "delegation_id": target,
            "worker_status": record.get("worker_status"),
            "delivery_disposition": record.get("delivery_disposition"),
            "goal": record.get("goal"),
            "dispatched_at": record.get("dispatched_at"),
            "completed_at": record.get("completed_at"),
            "result_available": record.get("result") is not None,
            "interrupt_reason": record.get("interrupt_reason"),
            "abandon_reason": record.get("abandon_reason"),
            "subagents": _children_for_record(
                record,
                session_key=origin,
                subagent_id=child_target,
                include_tail=include_tail,
                limit=event_limit,
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    if action == "wait":
        waited = _async.wait_for_delegation(
            target,
            session_key=origin,
            timeout_seconds=30.0 if timeout_seconds is None else float(timeout_seconds),
        )
        if waited.get("status") == "not_found":
            return _not_found(action, target)
        payload = {
            "action": action,
            "status": waited.get("status"),
            "delegation_id": target,
            "worker_status": waited.get("worker_status"),
            "delivery_disposition": waited.get("delivery_disposition"),
            "claimed_delivery": bool(waited.get("claimed_delivery", False)),
            "result": waited.get("result"),
            "subagents": _children_for_record(
                waited, session_key=origin, include_tail=True, limit=20
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    if action == "steer":
        queued = _async.enqueue_subagent_steer(
            target,
            str(child_target),
            session_key=origin,
            message=str(message).strip(),
        )
        if queued.get("status") != "accepted":
            payload = {
                "action": action,
                "status": queued.get("status"),
                "delegation_id": target,
                "subagent_id": child_target,
            }
            if queued.get("terminal_status"):
                payload["terminal_status"] = queued.get("terminal_status")
            return json.dumps(payload, ensure_ascii=False)

        _delegate.forward_pending_subagent_steers(
            str(child_target), str(queued["attempt_id"])
        )
        mailbox = _async.inspect_subagent_steer(str(queued["mailbox_id"]))
        mailbox_status = str(mailbox.get("status") or "pending")
        honest_status = (
            mailbox_status
            if mailbox_status in {
                "superseded_by_interrupt",
                "too_late_after_completion",
            }
            else "accepted"
        )
        return json.dumps(
            {
                "action": action,
                "status": honest_status,
                "delegation_id": target,
                "subagent_id": child_target,
                "attempt_id": queued.get("attempt_id"),
                "mailbox_id": queued.get("mailbox_id"),
                "steer_status": mailbox_status,
            },
            ensure_ascii=False,
        )

    if action == "resume":
        resumed = _async.dispatch_resumed_subagent(
            target,
            str(child_target),
            session_key=origin,
            message=str(message).strip(),
            parent_agent=parent_agent,
        )
        payload = {
            "action": action,
            **{
                key: value
                for key, value in resumed.items()
                if key not in {"bundle", "error"}
            },
        }
        if resumed.get("error"):
            payload["error"] = _redact_reason(str(resumed["error"]))
        return json.dumps(payload, ensure_ascii=False)

    use_cascade = True if cascade is None else cascade
    if not use_cascade:
        return json.dumps(
            {
                "action": action,
                "status": "unsupported_mode",
                "delegation_id": target,
                "error": (
                    "cascade=false is unsupported: AIAgent.interrupt() is "
                    "cooperative and necessarily propagates through the targeted branch."
                ),
            },
            ensure_ascii=False,
        )

    if action == "interrupt":
        if child_target:
            # Persist the exact-attempt interrupt first so it wins races with
            # pending/forwarding steer mailboxes and survives process loss.
            durable_outcome = _async.request_pending_subagent_interrupt(
                target,
                child_target,
                session_key=origin,
                reason=audit_reason,
            )
            if durable_outcome == "not_found":
                return _not_found(action, target)
            live_outcome = _delegate.interrupt_subagent_status(
                child_target, reason=audit_reason
            )
            outcome = (
                live_outcome
                if live_outcome not in {"not_live", "already_terminal"}
                else durable_outcome
            )
            current = _async.get_async_delegation(target, session_key=origin) or record
            payload = {
                "action": action,
                "status": outcome,
                "delegation_id": target,
                "subagent_id": child_target,
                "cascade": True,
                "delivery_disposition": current.get("delivery_disposition"),
                "reason": audit_reason,
            }
        else:
            payload = _async.interrupt_async_delegation(
                target, session_key=origin, reason=audit_reason
            )
            payload["action"] = action
            payload["cascade"] = True
            payload["reason"] = audit_reason
            current = _async.get_async_delegation(target, session_key=origin)
            if current is not None:
                payload["delivery_disposition"] = current.get("delivery_disposition")
        return json.dumps(payload, ensure_ascii=False)

    payload = _async.abandon_async_delegation(
        target, session_key=origin, reason=audit_reason
    )
    payload["action"] = action
    payload["cascade"] = True
    payload["reason"] = audit_reason
    return json.dumps(payload, ensure_ascii=False)


def _handle_delegation_args(args: Dict[str, Any], *, parent_agent=None) -> str:
    if not isinstance(args, dict):
        return _invalid("delegation", "arguments must be an object.")
    unknown = sorted(set(args) - _TOOL_FIELDS)
    if unknown:
        raw_action = args.get("action")
        action = raw_action.strip().lower() if isinstance(raw_action, str) else "delegation"
        return _invalid(action, f"Unknown argument(s): {', '.join(unknown)}.")
    return delegation_control(
        action=args.get("action", ""),
        delegation_id=args.get("delegation_id"),
        subagent_id=args.get("subagent_id"),
        timeout_seconds=args.get("timeout_seconds"),
        limit=args.get("limit"),
        cascade=args.get("cascade"),
        reason=args.get("reason"),
        message=args.get("message"),
        parent_agent=parent_agent,
    )


DELEGATION_CONTROL_SCHEMA = {
    "name": "delegation",
    "description": (
        "Observe and control background work previously spawned by delegate_task. "
        "Actions are session-scoped: list/status inspect lifecycle state, tail "
        "returns bounded assistant text and tool previews (never hidden reasoning), "
        "wait blocks for a bounded time and consumes an unclaimed completed result, "
        "steer durably queues guidance for one exact live child attempt, resume "
        "starts a new attempt from a terminal child's persisted transcript, "
        "interrupt requests a cooperative stop while preserving delivery, and "
        "abandon suppresses stale delivery before requesting interruption."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Lifecycle action to perform.",
            },
            "delegation_id": {
                "type": "string",
                "maxLength": _MAX_ID_CHARS,
                "description": "Handle returned by delegate_task; omitted only for list.",
            },
            "subagent_id": {
                "type": "string",
                "maxLength": _MAX_ID_CHARS,
                "description": "Child filter/branch target; required for steer/resume.",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": _MAX_WAIT_SECONDS,
                "description": "Bounded wait duration; default 30 seconds. Zero checks immediately.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_TAIL_EVENTS,
                "description": "Recent events returned by tail; default 20.",
            },
            "cascade": {
                "type": "boolean",
                "description": (
                    "Interrupt descendants of the delegation or selected child branch. "
                    "Defaults true; false is rejected because cooperative agent interrupts propagate."
                ),
            },
            "reason": {
                "type": "string",
                "maxLength": _MAX_REASON_CHARS,
                "description": "Optional audit reason for interrupt or abandon.",
            },
            "message": {
                "type": "string",
                "maxLength": _MAX_STEER_CHARS,
                "description": "Guidance for steer, or the next user instruction for resume.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="delegation",
    toolset="delegation",
    schema=DELEGATION_CONTROL_SCHEMA,
    handler=lambda args, **kw: _handle_delegation_args(
        args, parent_agent=kw.get("parent_agent")
    ),
    check_fn=check_delegation_control_requirements,
    emoji="🎛️",
    max_result_size_chars=24000,
)
