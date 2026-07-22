#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000  # legacy compatibility; undelivered rows are never pruned
_MAX_DURABLE_LIST = 100
_DB_LOCK = threading.Lock()
_STATE_CONDITION = threading.Condition()
_ACTIVE_STATES = {"running", "finalizing", "interrupt_requested"}
_TERMINAL_DELIVERY_STATES = {"delivered", "consumed", "suppressed"}
_NONTERMINAL_DELIVERY_STATES = {"pending", "held_by_wait", "delivering"}
_WAIT_POLL_SECONDS = 0.05
# Public lifecycle waits are capped at 300 seconds. Keep the durable hold lease
# longer than that bound so a live waiter cannot be pre-empted, while a process
# that dies mid-wait cannot strand the result forever.
_WAIT_HOLD_STALE_SECONDS = 360.0


def _notify_state_change() -> None:
    """Wake local waiters; SQLite remains the lifecycle authority."""
    with _STATE_CONDITION:
        _STATE_CONDITION.notify_all()


def _json_object(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> List[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            root_subagent_ids_json TEXT NOT NULL DEFAULT '[]',
            children_json TEXT NOT NULL DEFAULT '{}',
            interrupt_requests_json TEXT NOT NULL DEFAULT '{}',
            interrupt_reason TEXT,
            abandon_reason TEXT
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        ("root_subagent_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("children_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("interrupt_requests_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("interrupt_reason", "TEXT"),
        ("abandon_reason", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    # Older durable claims used ``pending`` plus a non-null token. Promote
    # them in place so an upgraded database has one authoritative state.
    conn.execute(
        """UPDATE async_delegations SET delivery_state='delivering'
           WHERE delivery_state='pending' AND delivery_claim IS NOT NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_async_delegations_owner_updated
           ON async_delegations(origin_session, updated_at DESC)"""
    )
    return conn


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch")
        if key in record
    }
    root_ids = [
        value
        for value in (record.get("root_subagent_ids") or [])
        if isinstance(value, str) and value
    ]
    children = {
        child_id: {
            "subagent_id": child_id,
            "parent_id": None,
            "depth": 0,
            "status": "starting",
        }
        for child_id in root_ids
    }
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, root_subagent_ids_json,
                children_json, interrupt_requests_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?, ?, '{}')""",
            (
                record["delegation_id"],
                record.get("session_key", ""),
                record.get("origin_ui_session_id", ""),
                record.get("parent_session_id"),
                record["dispatched_at"],
                now,
                __import__("os").getpid(),
                owner_started_at,
                json.dumps(task_payload),
                json.dumps(root_ids),
                json.dumps(children),
            ),
        )
    _notify_state_change()
    _prune_durable_records()


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
    _notify_state_change()


def _prune_durable_records() -> None:
    """Bound safely terminal history; never prune an undelivered result."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """DELETE FROM async_delegations
               WHERE state NOT IN ('running','finalizing','interrupt_requested')
                 AND delivery_state IN ('delivered','consumed','suppressed')
                 AND updated_at < ?""",
            (cutoff,),
        )
        total_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations"
        ).fetchone()[0]
        excess = max(0, total_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing','interrupt_requested')
                       AND delivery_state IN ('delivered','consumed','suppressed')
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Persist worker completion without stealing an existing delivery hold."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=? WHERE delegation_id=?""",
            (
                event.get("status", "completed"),
                event.get("completed_at", now),
                now,
                json.dumps(event),
                json.dumps(result),
                event["delegation_id"],
            ),
        )
    _notify_state_change()


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json
               FROM async_delegations WHERE state IN ('running','finalizing','interrupt_requested')"""
        ).fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delivery_managed": True,
                "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    if recovered:
        _notify_state_change()
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).
    """
    recover_abandoned_delegations()
    recover_stale_wait_holds()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json FROM async_delegations
               WHERE state NOT IN ('running','finalizing','interrupt_requested')
                 AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for _delegation_id, payload in rows:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
                evt["delivery_managed"] = True
            target_queue.put(evt)
    return len(rows)


def restore_stale_wait_completions(target_queue) -> int:
    """Atomically requeue terminal results abandoned by a crashed waiter.

    Startup restoration cannot recover a wait hold that has not expired yet.
    Notification drains call this lightweight scan so the result is published
    once the lease expires, without re-enqueuing unrelated pending rows.
    """
    now = time.time()
    cutoff = now - _WAIT_HOLD_STALE_SECONDS
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """UPDATE async_delegations
                  SET delivery_state='pending', delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
                WHERE state NOT IN ('running','finalizing','interrupt_requested')
                  AND delivery_state='held_by_wait'
                  AND (delivery_claimed_at IS NULL OR delivery_claimed_at < ?)
                  AND event_json IS NOT NULL
            RETURNING delegation_id, event_json""",
            (now, cutoff),
        ).fetchall()
    for _delegation_id, payload in rows:
        evt = json.loads(payload)
        if isinstance(evt, dict):
            evt["restored"] = True
            evt["delivery_managed"] = True
        target_queue.put(evt)
    if rows:
        _notify_state_change()
    return len(rows)


_DURABLE_SELECT = """SELECT
    delegation_id, origin_session, origin_ui_session_id, parent_session_id,
    state, dispatched_at, completed_at, updated_at, event_json, result_json,
    delivery_state, delivery_attempts, delivered_at, delivery_claim,
    delivery_claimed_at, task_json, root_subagent_ids_json, children_json,
    interrupt_requests_json, interrupt_reason, abandon_reason, owner_pid,
    owner_started_at
FROM async_delegations"""


def _durable_snapshot(row: Any) -> Dict[str, Any]:
    task = _json_object(row[15])
    snapshot: Dict[str, Any] = {
        **task,
        "delegation_id": row[0],
        "origin_session": row[1],
        "session_key": row[1],
        "origin_ui_session_id": row[2],
        "parent_session_id": row[3],
        "state": row[4],
        "worker_status": row[4],
        "status": row[4],
        "dispatched_at": row[5],
        "completed_at": row[6],
        "updated_at": row[7],
        "event": _json_object(row[8]) if row[8] else None,
        "result": _json_object(row[9]) if row[9] else None,
        "delivery_state": row[10],
        "delivery_disposition": row[10],
        "delivery_attempts": row[11],
        "delivered_at": row[12],
        "delivery_claim": row[13],
        "delivery_claimed_at": row[14],
        "root_subagent_ids": [
            value for value in _json_list(row[16]) if isinstance(value, str)
        ],
        "children": _json_object(row[17]),
        "interrupt_requests": _json_object(row[18]),
        "interrupt_reason": row[19],
        "abandon_reason": row[20],
        "owner_pid": row[21],
        "owner_started_at": row[22],
    }
    return snapshot


def _terminal(snapshot: Dict[str, Any]) -> bool:
    return str(snapshot.get("state") or "") not in _ACTIVE_STATES


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Internal durable lookup. Model-facing callers must use the authorised view."""
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            f"{_DURABLE_SELECT} WHERE delegation_id=?", (delegation_id,)
        ).fetchone()
    return _durable_snapshot(row) if row is not None else None


def get_async_delegation(
    delegation_id: str, *, session_key: str
) -> Optional[Dict[str, Any]]:
    """Read one session-authorised lifecycle record without claiming delivery."""
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            f"{_DURABLE_SELECT} WHERE delegation_id=? AND origin_session=?",
            (delegation_id, session_key),
        ).fetchone()
    return _durable_snapshot(row) if row is not None else None


def list_durable_delegations(
    *, session_keys: Optional[List[str]] = None, limit: int = _MAX_DURABLE_LIST
) -> List[Dict[str, Any]]:
    """Read a bounded stable snapshot, optionally restricted to session owners."""
    bounded_limit = max(0, min(int(limit), _MAX_DURABLE_LIST))
    if bounded_limit == 0 or session_keys == []:
        return []
    params: List[Any] = []
    where = ""
    if session_keys is not None:
        owners = list(dict.fromkeys(str(value) for value in session_keys))
        if not owners:
            return []
        where = f" WHERE origin_session IN ({','.join('?' for _ in owners)})"
        params.extend(owners)
    params.append(bounded_limit)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f"{_DURABLE_SELECT}{where} ORDER BY dispatched_at DESC, delegation_id LIMIT ?",
            params,
        ).fetchall()
    return [_durable_snapshot(row) for row in rows]


def mark_completion_delivered(delegation_id: str) -> bool:
    """Legacy acknowledgement for an unclaimed pending completion."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'""",
            (now, now, delegation_id),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one terminal pending completion across consumers/processes."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivering',
                      delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND event_json IS NOT NULL
                 AND state NOT IN ('running','finalizing','interrupt_requested')
                 AND (
                      delivery_state='pending'
                      OR (delivery_state='delivering' AND delivery_claimed_at < ?)
                 )""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def recover_stale_wait_holds(delegation_id: Optional[str] = None) -> int:
    """Release wait holds whose owning process can no longer be trusted alive."""
    now = time.time()
    cutoff = now - _WAIT_HOLD_STALE_SECONDS
    where_id = " AND delegation_id=?" if delegation_id else ""
    params: tuple[Any, ...] = (
        (now, cutoff, delegation_id) if delegation_id else (now, cutoff)
    )
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='pending',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delivery_state='held_by_wait'
                 AND (delivery_claimed_at IS NULL OR delivery_claimed_at < ?)"""
            + where_id,
            params,
        )
        changed = cur.rowcount
    if changed:
        _notify_state_change()
    return changed


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    # ``ProcessRegistry.drain_notifications`` claims managed events before
    # formatting. Reuse that exact token instead of attempting a second claim.
    prepared_token = evt.get("_async_delivery_claim_token")
    if prepared_token:
        return str(prepared_token)
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def claim_async_delivery(delegation_id: str, *, managed: bool = False) -> Dict[str, Any]:
    """Atomically claim a queued terminal result for any automatic consumer.

    Unknown events are legacy pass-through unless the producer explicitly
    marked them managed. Durable dispositions are authoritative across threads
    and processes; a wait hold, consumption, suppression, or prior delivery
    can never be bypassed by an in-memory queue copy.
    """
    recover_stale_wait_holds(delegation_id)
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT state, delivery_state, event_json FROM async_delegations
               WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()
    if row is None:
        return {"status": "stale" if managed else "legacy"}
    state = str(row[0] or "")
    disposition = str(row[1] or "pending")
    if state in _ACTIVE_STATES or row[2] is None:
        return {"status": "not_ready"}
    if disposition == "held_by_wait":
        return {"status": "held"}
    if disposition in _TERMINAL_DELIVERY_STATES:
        return {"status": "stale"}

    token = f"auto:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    if claim_completion_delivery(delegation_id, token):
        return {"status": "claimed", "token": token}
    # Another owner won between SELECT and UPDATE. Report its current state so
    # callers defer rather than dropping a still-actionable queue event.
    current = inspect_async_delivery_claim(delegation_id, token)
    return {
        "status": "held"
        if current in {"held_by_wait", "delivering"}
        else "stale"
    }


def inspect_async_delivery_claim(delegation_id: str, token: str) -> str:
    """Inspect a token retained on a requeued delivery event."""
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT delivery_state, delivery_claim FROM async_delegations
               WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()
    if row is None:
        return "not_found"
    disposition, current_token = str(row[0] or "pending"), row[1]
    if disposition == "delivering" and current_token == token:
        return "current"
    return disposition


def finish_async_delivery(delegation_id: str, token: str, *, delivered: bool) -> bool:
    """Commit or release exactly the automatic claim identified by ``token``."""
    if delivered:
        return complete_completion_delivery(delegation_id, token)
    return release_completion_delivery(delegation_id, token)


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed automatic-delivery claim for retry."""
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='pending',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='delivering'
                 AND delivery_claim=?""",
            (time.time(), delegation_id, claim_id),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the automatic consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='delivering'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    if not claim_id or evt.get("type") != "async_delegation":
        return True
    if evt.get("_async_delivery_claim_token"):
        from tools.process_registry import commit_notification_delivery, process_registry

        return commit_notification_delivery(evt, process_registry.completion_queue)
    return complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if not claim_id or evt.get("type") != "async_delegation":
        return
    if evt.get("_async_delivery_claim_token"):
        from tools.process_registry import process_registry, requeue_notification_delivery

        requeue_notification_delivery(evt, process_registry.completion_queue)
        return
    release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def hold_completion_for_wait(
    delegation_id: str, claim_id: str, *, session_key: str
) -> bool:
    """Atomically reserve pending delivery for one authorised waiter."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='held_by_wait',
                      delivery_claim=?, delivery_claimed_at=?, updated_at=?
               WHERE delegation_id=? AND origin_session=?
                 AND delivery_state='pending'""",
            (claim_id, now, now, delegation_id, session_key),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def consume_waited_completion(
    delegation_id: str, claim_id: str, *, session_key: str
) -> bool:
    """Consume a terminal completion only for the waiter owning its hold."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='consumed',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND origin_session=?
                 AND state NOT IN ('running','finalizing','interrupt_requested')
                 AND event_json IS NOT NULL AND delivery_state='held_by_wait'
                 AND delivery_claim=?""",
            (now, now, delegation_id, session_key, claim_id),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def release_wait_hold(
    delegation_id: str, claim_id: str, *, session_key: str
) -> bool:
    """Release only this waiter's hold; timeout never consumes a result."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='pending',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND origin_session=?
                 AND delivery_state='held_by_wait' AND delivery_claim=?""",
            (now, delegation_id, session_key, claim_id),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
    return changed


def wait_for_delegation(
    delegation_id: str, *, session_key: str, timeout_seconds: float = 30.0
) -> Dict[str, Any]:
    """Boundedly wait, atomically consuming one otherwise-pending result."""
    timeout_seconds = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    claim_id = f"wait:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    owns_hold = False

    recover_stale_wait_holds(delegation_id)
    while True:
        snapshot = get_async_delegation(delegation_id, session_key=session_key)
        if snapshot is None:
            return {"status": "not_found", "delegation_id": delegation_id}

        if snapshot["delivery_state"] == "pending" and not owns_hold:
            owns_hold = hold_completion_for_wait(
                delegation_id, claim_id, session_key=session_key
            )
            if owns_hold:
                snapshot = get_async_delegation(
                    delegation_id, session_key=session_key
                ) or snapshot

        if _terminal(snapshot):
            claimed = False
            if owns_hold:
                claimed = consume_waited_completion(
                    delegation_id, claim_id, session_key=session_key
                )
            current = get_async_delegation(
                delegation_id, session_key=session_key
            ) or snapshot
            current["claimed_delivery"] = claimed
            return current

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Completion and timeout compete through the same SQLite authority.
            # Re-check terminal state immediately before releasing the hold.
            latest = get_async_delegation(
                delegation_id, session_key=session_key
            ) or snapshot
            if _terminal(latest):
                claimed = owns_hold and consume_waited_completion(
                    delegation_id, claim_id, session_key=session_key
                )
                current = get_async_delegation(
                    delegation_id, session_key=session_key
                ) or latest
                current["claimed_delivery"] = bool(claimed)
                return current
            if owns_hold:
                release_wait_hold(
                    delegation_id, claim_id, session_key=session_key
                )
                owns_hold = False
            current = get_async_delegation(
                delegation_id, session_key=session_key
            ) or latest
            current["status"] = "timeout"
            current["claimed_delivery"] = False
            return current

        with _STATE_CONDITION:
            _STATE_CONDITION.wait(timeout=min(remaining, _WAIT_POLL_SECONDS))


def suppress_completion_delivery(
    delegation_id: str, *, session_key: str, reason: str = ""
) -> str:
    """Atomically suppress pending/held delivery with an explicit race outcome."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT delivery_state FROM async_delegations
               WHERE delegation_id=? AND origin_session=?""",
            (delegation_id, session_key),
        ).fetchone()
        if row is None:
            return "not_found"
        disposition = str(row[0] or "pending")
        if disposition == "suppressed":
            if reason:
                conn.execute(
                    """UPDATE async_delegations SET abandon_reason=?, updated_at=?
                       WHERE delegation_id=? AND origin_session=?""",
                    (reason, now, delegation_id, session_key),
                )
            return "already_suppressed"
        if disposition not in {"pending", "held_by_wait"}:
            return "too_late"
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='suppressed',
                      delivery_claim=NULL, delivery_claimed_at=NULL,
                      abandon_reason=?, updated_at=?
               WHERE delegation_id=? AND origin_session=?
                 AND delivery_state IN ('pending','held_by_wait')""",
            (reason or None, now, delegation_id, session_key),
        )
        changed = cur.rowcount == 1
    if changed:
        _notify_state_change()
        return "applied"
    return "too_late"


def interrupt_async_delegation(
    delegation_id: str, *, session_key: str, reason: str = ""
) -> Dict[str, Any]:
    """Idempotently request cooperative interruption without suppressing delivery."""
    snapshot = get_async_delegation(delegation_id, session_key=session_key)
    if snapshot is None:
        return {"status": "not_found", "delegation_id": delegation_id}
    if _terminal(snapshot):
        return {
            "status": "already_terminal",
            "delegation_id": delegation_id,
            "worker_status": snapshot["state"],
        }
    if snapshot["state"] == "interrupt_requested":
        return {"status": "interrupt_requested", "delegation_id": delegation_id}

    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("session_key", "") != session_key:
            return {"status": "interrupt_unavailable", "delegation_id": delegation_id}
        fn = record.get("interrupt_fn")
        if not callable(fn):
            return {"status": "interrupt_unavailable", "delegation_id": delegation_id}
        with _DB_LOCK, _connect() as conn:
            cur = conn.execute(
                """UPDATE async_delegations SET state='interrupt_requested',
                          interrupt_reason=?, updated_at=?
                   WHERE delegation_id=? AND origin_session=?
                     AND state IN ('running','finalizing')""",
                (reason or None, time.time(), delegation_id, session_key),
            )
        if cur.rowcount != 1:
            current = get_async_delegation(delegation_id, session_key=session_key)
            if current is not None and current["state"] == "interrupt_requested":
                return {"status": "interrupt_requested", "delegation_id": delegation_id}
            return {
                "status": "already_terminal" if current and _terminal(current) else "interrupt_unavailable",
                "delegation_id": delegation_id,
            }
        record["status"] = "interrupt_requested"
    _notify_state_change()

    try:
        fn()
    except Exception as exc:
        with _records_lock:
            current_record = _records.get(delegation_id)
            if current_record is not None and current_record.get("status") == "interrupt_requested":
                current_record["status"] = "running"
        with _DB_LOCK, _connect() as conn:
            conn.execute(
                """UPDATE async_delegations SET state='running', updated_at=?
                   WHERE delegation_id=? AND origin_session=?
                     AND state='interrupt_requested'""",
                (time.time(), delegation_id, session_key),
            )
        _notify_state_change()
        return {
            "status": "interrupt_failed",
            "delegation_id": delegation_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"status": "interrupt_requested", "delegation_id": delegation_id}


def abandon_async_delegation(
    delegation_id: str, *, session_key: str, reason: str = ""
) -> Dict[str, Any]:
    """Suppress future delivery first, then best-effort interrupt the worker."""
    suppression = suppress_completion_delivery(
        delegation_id, session_key=session_key, reason=reason
    )
    if suppression == "not_found":
        return {
            "status": "not_found",
            "delegation_id": delegation_id,
            "suppression": "not_found",
            "worker": "not_found",
        }
    interrupted = interrupt_async_delegation(
        delegation_id, session_key=session_key, reason=reason
    )
    worker = str(interrupted.get("status") or "interrupt_unavailable")
    return {
        "status": "delivery_too_late" if suppression == "too_late" else "abandoned",
        "delegation_id": delegation_id,
        "suppression": suppression,
        "worker": worker,
    }


_TERMINAL_CHILD_STATES = {
    "completed",
    "success",
    "error",
    "failed",
    "interrupted",
    "cancelled",
    "timeout",
    "budget_exhausted",
}


def _delegation_for_subagent_locked(conn: Any, subagent_id: str) -> Optional[tuple]:
    """Find the durable delegation containing ``subagent_id``.

    Child lookup is a control-plane operation rather than a hot delivery path.
    SQLite's JSON extension is not guaranteed in every supported build, so use
    the bounded retained records and parse ``children_json`` in Python.
    """
    rows = conn.execute(
        """SELECT delegation_id, origin_session, state, children_json,
                  interrupt_requests_json
           FROM async_delegations ORDER BY updated_at DESC"""
    ).fetchall()
    for row in rows:
        children = _json_object(row[3])
        if subagent_id in children:
            return row
    return None


def register_subagent_lifecycle(record: Dict[str, Any]) -> Optional[str]:
    """Associate a live child with its durable delegation and refresh metadata.

    Root IDs are written before executor submission. Descendants are associated
    through their already-associated parent, so no process-local authority is
    needed for model-facing authorization.
    """
    subagent_id = record.get("subagent_id")
    if not isinstance(subagent_id, str) or not subagent_id:
        return None
    parent_id = record.get("parent_id")
    now = time.time()
    delegation_id: Optional[str] = None
    with _DB_LOCK, _connect() as conn:
        row = _delegation_for_subagent_locked(conn, subagent_id)
        if row is None and isinstance(parent_id, str) and parent_id:
            row = _delegation_for_subagent_locked(conn, parent_id)
        if row is None:
            return None

        delegation_id = str(row[0])
        children = _json_object(row[3])
        interrupts = _json_object(row[4])
        child = dict(children.get(subagent_id) or {})
        for key in (
            "subagent_id",
            "parent_id",
            "depth",
            "goal",
            "model",
            "started_at",
            "status",
            "tool_count",
            "last_tool",
            "last_activity_at",
            "assistant_text_tail",
            "events",
            "interrupt_reason",
            "activity",
        ):
            if key in record:
                child[key] = record.get(key)
        child["subagent_id"] = subagent_id
        if subagent_id in interrupts:
            child["status"] = "interrupt_requested"
            child["interrupt_reason"] = str(interrupts.get(subagent_id) or "")
        children[subagent_id] = child
        conn.execute(
            """UPDATE async_delegations SET children_json=?, updated_at=?
               WHERE delegation_id=?""",
            (json.dumps(children), now, delegation_id),
        )
    _notify_state_change()
    return delegation_id


def delegation_contains_subagent(
    delegation_id: str, subagent_id: str, *, session_key: str
) -> bool:
    """Return membership only when both delegation and session are authorized."""
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT children_json FROM async_delegations
               WHERE delegation_id=? AND origin_session=?""",
            (delegation_id, session_key),
        ).fetchone()
    return bool(row is not None and subagent_id in _json_object(row[0]))


def request_pending_subagent_interrupt(
    delegation_id: str,
    subagent_id: str,
    *,
    session_key: str,
    reason: str = "",
) -> str:
    """Durably queue an interrupt for an authorized child still starting."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT state, children_json, interrupt_requests_json
               FROM async_delegations
               WHERE delegation_id=? AND origin_session=?""",
            (delegation_id, session_key),
        ).fetchone()
        if row is None:
            return "not_found"
        children = _json_object(row[1])
        child = dict(children.get(subagent_id) or {})
        if subagent_id not in children:
            return "not_found"
        child_state = str(child.get("status") or "").lower()
        if str(row[0] or "") not in _ACTIVE_STATES or child_state in _TERMINAL_CHILD_STATES:
            return "already_terminal"
        interrupts = _json_object(row[2])
        interrupts[subagent_id] = reason
        child["status"] = "interrupt_requested"
        if reason:
            child["interrupt_reason"] = reason
        children[subagent_id] = child
        conn.execute(
            """UPDATE async_delegations
               SET children_json=?, interrupt_requests_json=?, updated_at=?
               WHERE delegation_id=? AND origin_session=?""",
            (
                json.dumps(children),
                json.dumps(interrupts),
                now,
                delegation_id,
                session_key,
            ),
        )
    _notify_state_change()
    return "interrupt_requested"


def take_pending_subagent_interrupt(subagent_id: str) -> tuple[bool, str]:
    """Consume a queued startup interrupt immediately after live registration."""
    with _DB_LOCK, _connect() as conn:
        row = _delegation_for_subagent_locked(conn, subagent_id)
        if row is None:
            return False, ""
        interrupts = _json_object(row[4])
        if subagent_id not in interrupts:
            return False, ""
        reason = str(interrupts.pop(subagent_id) or "")
        conn.execute(
            """UPDATE async_delegations SET interrupt_requests_json=?, updated_at=?
               WHERE delegation_id=?""",
            (json.dumps(interrupts), time.time(), row[0]),
        )
    _notify_state_change()
    return True, reason


def pending_subagent_interrupt_ids(
    delegation_id: str, *, session_key: str
) -> set[str]:
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT interrupt_requests_json FROM async_delegations
               WHERE delegation_id=? AND origin_session=?""",
            (delegation_id, session_key),
        ).fetchone()
    return set(_json_object(row[0])) if row is not None else set()


def archive_subagent_tail(subagent_id: str, tail: Dict[str, Any]) -> None:
    """Persist a bounded, already-redacted child tail before live removal."""
    archived = {
        key: tail.get(key)
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
            "events",
            "assistant_text_tail",
            "last_activity_at",
            "activity",
        )
        if key in tail
    }
    archived["subagent_id"] = subagent_id
    with _DB_LOCK, _connect() as conn:
        row = _delegation_for_subagent_locked(conn, subagent_id)
        if row is None:
            return
        children = _json_object(row[3])
        child = dict(children.get(subagent_id) or {})
        child.update(archived)
        children[subagent_id] = child
        interrupts = _json_object(row[4])
        interrupts.pop(subagent_id, None)
        conn.execute(
            """UPDATE async_delegations
               SET children_json=?, interrupt_requests_json=?, updated_at=?
               WHERE delegation_id=?""",
            (json.dumps(children), json.dumps(interrupts), time.time(), row[0]),
        )
    _notify_state_change()


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegations currently running."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") in _ACTIVE_STATES)


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") not in _ACTIVE_STATES
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    root_subagent_ids: Optional[List[str]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "root_subagent_ids": list(root_subagent_ids or []),
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") in _ACTIVE_STATES
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        event_record = dict(record)

    _push_completion_event(event_record, result, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return

    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delivery_managed": True,
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    _persist_completion(evt, result)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    root_subagent_ids: Optional[List[str]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "root_subagent_ids": list(root_subagent_ids or []),
        "is_batch": True,
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") in _ACTIVE_STATES
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        event_record = dict(record)

    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            delegation_id, exc,
        )
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delivery_managed": True,
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    _persist_completion(evt, combined)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            delegation_id, exc,
        )
    finally:
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record["status"] = status
            _prune_completed_locked()


def list_async_delegations(
    session_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Snapshot async delegations without changing delivery ownership.

    ``session_key=None`` preserves the process-wide internal/debug view.
    Authorised callers receive the durable view so records survive restarts.
    """
    if session_key is not None:
        return list_durable_delegations(session_keys=[session_key])
    with _records_lock:
        return [
            {k: v for k, v in r.items() if k != "interrupt_fn"}
            for r in _records.values()
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") == "running"
            and (
                (origin_ui_session_id and str(r.get("origin_ui_session_id") or "") == origin_ui_session_id)
                or (session_key and str(r.get("session_key") or "") == session_key)
                or (parent_session_id and str(r.get("parent_session_id") or "") == parent_session_id)
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
