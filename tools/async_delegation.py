#!/usr/bin/env python3
"""Async (background) delegation registry behind ``delegate_task(background=true)``.

The parent dispatches a subagent on a module-level daemon executor and returns a handle
immediately. On completion a ``type="async_delegation"`` event (self-contained task-source
block) is pushed onto the SHARED ``process_registry.completion_queue`` the CLI/gateway drain
while idle, so results surface as a NEW turn (never mid-turn) and inherit its de-dup and
crash-recovery wiring. Only the async lifecycle lives here; the child run is an injected ``runner``."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional

from hermes_constants import get_hermes_home, hermes_home_key
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# ── Module-level state ──────────────────────────────────────────────────────
# Persistent daemon executor (never a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat async); daemon workers can't hang a hard exit.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict; kept for the run plus a short completed tail.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# Completed records retained (in memory and in the ledger) for status queries.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# Cap retried deliveries so an unroutable row converges to terminal 'dropped'.
_MAX_DELIVERY_ATTEMPTS = 8
# Pending completions older than this are dropped on restart replay instead of
# re-run as a full-context turn; 48h keeps weekend results deliverable.
_MAX_COMPLETION_REPLAY_AGE_S = 48 * 3600.0
# A delivery claim older than this is abandoned and may be re-claimed.
_CLAIM_LEASE_S = 300.0
_MAX_DURABLE_LIST = 100
_DB_LOCK = threading.Lock()
_STATE_CONDITION = threading.Condition()
_ACTIVE_STATES = {"starting", "running", "finalizing", "interrupt_requested", "stalling"}
_WAIT_POLL_SECONDS = 0.05
# Public lifecycle waits are capped at 300 seconds. Keep the durable hold lease
# longer than that bound so a live waiter cannot be pre-empted, while a process
# that dies mid-wait cannot strand the result forever.
_WAIT_HOLD_STALE_SECONDS = 360.0

# ── Orphaned-completion sweep ────────────────────────────────────────────────
# Startup replay runs once per process, so a completion whose owner died while THIS process was
# already running (a desktop reload) would wait for the next restart (#97202). Delivery loops (gateway
# watcher, TUI poller) sweep each home they serve at most once per interval.
ORPHAN_SWEEP_INTERVAL_S = 30.0
# Idle time before a dead owner's pending row is re-offered; keeps the sweep off a row just touched.
_ORPHAN_STALE_S = 60.0
_orphan_lock = threading.Lock()
# (home key, delegation_id) put on this process's queue by replay or sweep and not re-offered while
# that copy is alive. A consumer that discards its copy with the row still pending hands it back
# (``return_completion_offer``); the delivery claim stays the only thing that settles the row.
_offered: set = set()
_last_orphan_sweep: Dict[str, float] = {}

# ── Stale-delegation detection (progress-based, on by default) ──────────────
# A runner wedged before returning never reaches its finalizer, so it would show
# "dispatched" forever. No wall-clock timeout (heavy work must never be killed for
# taking long): one monitor thread samples per-dispatch PROGRESS via an injected
# ``progress_fn``; a frozen child is interrupted, given a grace window to unwind via
# the normal finalize path, and only force-finalized (terminal ``stalled`` event) if
# it never returns. Thresholds mirror delegate_tool's sync heartbeat monitor.
_STALE_CHECK_INTERVAL = 30.0
_STALE_IDLE_SECONDS = 450.0
_STALE_IN_TOOL_SECONDS = 1200.0
_STALL_GRACE_SECONDS = 120.0

_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()

_LIVE_STATES = {"running", "stalling", "finalizing"}
_ACTIVE_STATES = {"starting", "running", "finalizing", "interrupt_requested", "stalling"}
# Routing origin persisted at dispatch so a restart-recovered completion can
# reconstruct a full SessionSource (scope_id drives relay tenant egress).
_ROUTING_KEYS = ("scope_id", "user_id", "user_name")
# Structured stall metadata — additive, present only on stall finalizations.
_STALL_META_KEYS = ("stalled_after_quiet_seconds", "stall_threshold_seconds", "stall_phase", "stall_grace_seconds")
# Private stall bookkeeping on the record -> public field in list_async_delegations().
_STALL_FIELD_MAP = (("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
                    ("_stall_threshold_seconds", "stall_threshold_seconds"), ("_stall_in_tool", "stall_in_tool"))


# ── Durable ledger (state.db / async_delegations) ───────────────────────────


def _connect() -> sqlite3.Connection:
    from hermes_cli.sqlite_util import open_db
    # Same state.db as hermes_state.SessionDB -- reuse its owner-only (0600)
    # hardening so this writer doesn't create/leave the file (and its WAL
    # sidecars) at the process umask. See hermes_state._secure_state_db_files.
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import _secure_state_db_files

    path = _db_path()
    # A late replay or writer must not resurrect a removed named profile (#123265).
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    # wal=False: SessionDB owns state.db's journal mode (_initialize_schema applies the barriers).
    conn = open_db(path, db_label="state.db (async_delegation)", busy_timeout_ms=10_000,
                   wal=False, row_factory=None, initialize=_initialize_schema)
    _secure_state_db_files(path)
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state_repair import apply_durability_barriers
    from hermes_state_schema import reconcile_state_schema
    # Preserve the journal mode SessionDB configured on state.db: forcing WAL from
    # every short-lived connection collides with live transcript/FTS writers.
    apply_durability_barriers(conn)
    # Single durable-shape authority: the canonical SCHEMA_SQL drives both
    # table creation and column backfill (reconcile_state_schema replays the
    # canonical DDL and reuses SessionDB's declarative reconciliation). This
    # module previously carried its own CREATE TABLE + ALTER column list,
    # which drifted from SCHEMA_SQL — same-name columns with different
    # nullability/defaults depending on which authority touched the database
    # first (#94691).
    reconcile_state_schema(conn)


def _transaction():
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect())


def _capture_routing_origin() -> Dict[str, Any]:
    """Snapshot scope_id/user_id/user_name on the PARENT thread (the daemon worker
    has no contextvars) so a restart-replayed completion can rebuild a SessionSource.
    Best-effort: empty values are omitted."""
    try:
        from gateway.session_context import get_session_env
        return {k: v for k in _ROUTING_KEYS if (v := get_session_env(f"HERMES_SESSION_{k.upper()}", ""))}
    except Exception:  # noqa: BLE001 - routing origin is additive, never fatal
        return {}








def record_unit_child(delegation_id: str, entry: Dict[str, Any]) -> None:
    """Durably record ONE finished child of a still-running multi-child unit on the unit's own row, so a crash before
    the unit joins loses only the children that had not finished. Stored in ``result_json`` (overwritten by the real
    result at finalize); ``recover_abandoned_delegations`` replays it. Best-effort: a failed write costs recovery
    fidelity, never the live result."""
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND state='running'",
                               (delegation_id,)).fetchone()
            if row is None:
                return
            partial = json.loads(row[0] or "{}") or {}
            results = [r for r in partial.get("results") or [] if r.get("task_index") != entry.get("task_index")]
            results.append(entry)
            conn.execute("UPDATE async_delegations SET result_json=?, updated_at=? WHERE delegation_id=? AND state='running'",
                         (json.dumps({"results": results, "partial": True}), time.time(), delegation_id))
    except Exception:  # noqa: BLE001 — recovery bookkeeping must never fail a live child
        logger.warning("Async delegation %s: could not record finished child %s", delegation_id, entry.get("task_index"), exc_info=True)


def _recovered_results(task: Dict[str, Any], result_json: Optional[str], error: str) -> Optional[List[Dict[str, Any]]]:
    """Per-task results for an abandoned unit: recorded children as they finished, the rest ``unknown``."""
    partial = json.loads(result_json or "{}") or {}
    if not (task.get("is_batch") and partial.get("partial") and partial.get("results")):
        return None
    recorded = {r["task_index"]: r for r in partial["results"] if isinstance(r.get("task_index"), int)}
    indexes = task.get("task_indexes") or list(range(len(task.get("goals") or [])))
    return [recorded.get(i) or {"task_index": i, "status": "unknown", "summary": None, "error": error} for i in indexes]


def _owner_liveness() -> Optional[Callable[[Any, Any], bool]]:
    """``alive(owner_pid, owner_started_at)`` over the shared drift-tolerant start-time comparator,
    or None when the liveness probes cannot be imported."""
    try:
        from gateway.status import _pid_exists, get_process_start_time, start_time_fingerprints_match
    except Exception:
        return None

    def alive(pid, started) -> bool:
        return bool(pid) and _pid_exists(int(pid)) and (
            started is None or start_time_fingerprints_match(started, get_process_start_time(int(pid)) or 0))
    return alive






def _replay_runs(rows, target_queue, now: float) -> int:
    """Put each pending run's event on ``target_queue`` stamped ``restored``, or terminally drop its delivery
    past ``_MAX_COMPLETION_REPLAY_AGE_S``. Records the offer so the orphan sweep skips the delegation until
    the copy is handed back (``return_completion_offer``)."""
    home, restored = hermes_home_key(get_hermes_home()), 0
    for row in rows:
        delegation_id = row["delegation_id"]
        age_basis = row["completed_at"] or row["dispatched_at"]
        if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
            _repository().drop_undelivered(row["run_id"])
            logger.warning("Async delegation %s: pending completion is %.1fh old "
                           "(cap %.1fh); terminally dropping the replay (result remains queryable).",
                           delegation_id, (now - age_basis) / 3600.0, _MAX_COMPLETION_REPLAY_AGE_S / 3600.0)
            continue
        evt = dict(row["event"])
        evt.update(restored=True, delivery_managed=True, run_id=row["run_id"])
        target_queue.put(evt)
        with _orphan_lock:
            _offered.add((home, delegation_id))
        restored += 1
    return restored


def sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Offer this home's completions whose owner died after THIS process started (#97202).

    Startup replay (``restore_undelivered_completions``) covers owners that died before the process
    started; this covers the rest while it runs. Abandoned in-flight attempts are first classified by
    ``recover_abandoned_delegations``. A completed run qualifies when its delivery is pending (or its
    claim's lease expired), it is idle past ``_ORPHAN_STALE_S``, and its owning process fails the shared
    liveness check. A delegation is offered once per live in-memory copy: a consumer that discards the
    copy with the run still pending hands it back for the next sweep. The consumer's delivery claim stays
    the atomic cross-process gate, so two processes offering one run never both deliver it. Runs past
    the delivery budget or the replay age converge to ``dropped``. Reads the current profile's ledger:
    callers bind the owning profile first."""
    alive = _owner_liveness()
    if alive is None or not _db_path().exists():
        return 0  # never create a ledger just to sweep it
    recover_abandoned_delegations()
    now = time.time() if now is None else now
    home = hermes_home_key(get_hermes_home())
    with _orphan_lock:
        offered = {delegation_id for key, delegation_id in _offered if key == home}
    orphans = []
    for row in _repository().pending_completions():
        claim_live = row["delivery_claim"] is not None and (row["delivery_claimed_at"] or 0) >= now - _CLAIM_LEASE_S
        if claim_live or row["completed_at"] >= now - _ORPHAN_STALE_S:
            continue
        if row["delegation_id"] in offered or alive(row["owner_pid"], row["owner_started_at"]):
            continue
        if (row["delivery_attempts"] or 0) >= _MAX_DELIVERY_ATTEMPTS:
            # Its last claimant died holding the final attempt; converge like a released final claim.
            _repository().drop_undelivered(row["run_id"])
            logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                           "marking terminally dropped (result remains queryable).",
                           row["delegation_id"], _MAX_DELIVERY_ATTEMPTS)
            continue
        orphans.append(row)
    return _replay_runs(orphans, target_queue, now)


def maybe_sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """``sweep_orphaned_completions`` at most once per ``ORPHAN_SWEEP_INTERVAL_S`` per home (``now`` is
    monotonic), for delivery loops that tick far more often. Never raises into the loop."""
    home = hermes_home_key(get_hermes_home())
    now = time.monotonic() if now is None else now
    with _orphan_lock:
        last = _last_orphan_sweep.get(home)
        if last is not None and now - last < ORPHAN_SWEEP_INTERVAL_S:
            return 0
        _last_orphan_sweep[home] = now
    try:
        return sweep_orphaned_completions(target_queue)
    except Exception:
        logger.debug("Orphaned async delegation sweep failed", exc_info=True)
        return 0


def _update_delivery(sql: str, params: tuple) -> bool:
    """Run one UPDATE on the ledger; True iff exactly one row changed."""
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(sql, params).rowcount == 1






def is_interim_delegation_event(evt: Dict[str, Any]) -> bool:
    """An early per-task notice for a batch that is still running. It shares the batch's
    ``delegation_id`` but is NOT the durable completion: it must never claim, acknowledge or
    dedup against the final result's row (independent review reproduced exactly that loss)."""
    return evt.get("type") == "async_delegation" and bool(evt.get("task_failure_notice"))






def defer_completion_delivery(delegation_id: str, claim_id: str, *, run_id: Optional[str] = None) -> bool:
    """Return an exact unadmitted completion without spending its attempt budget."""
    repository = _repository()
    resolved = repository.resolve_run_id(delegation_id, run_id)
    if resolved.get("status") != "found":
        return False
    with repository.write_txn() as conn:
        return conn.execute(
            "UPDATE delegation_runs SET delivery_state='pending', delivery_claim=NULL, "
            "delivery_claimed_at=NULL, delivery_attempts=MAX(0,delivery_attempts-1) "
            "WHERE run_id=? AND delivery_state='delivering' AND delivery_claim=?",
            (resolved["run_id"], claim_id),
        ).rowcount == 1


def drop_completion_delivery(
    delegation_id: str, claim_id: str, *, run_id: Optional[str] = None
) -> bool:
    """Terminally suppress an exact claimed completion with no live target."""
    inspected = _repository().inspect_delivery(delegation_id, run_id)
    if inspected.get("status") != "found":
        return False
    outcome = _repository().commit_run_delivery(
        str(inspected["run_id"]), claim_id, disposition="suppressed"
    )
    return _changed(outcome, "suppressed")








def return_completion_offer(evt: Dict[str, Any]) -> None:
    """Hand an offered completion back to the orphan sweep after its in-memory copy was discarded while
    the durable row stays pending, e.g. a TUI session that cannot prove it owns the event drops it (every
    session poller drains one process-wide queue). The next sweep may offer the row again. Delegation ids
    are unique across profiles, so this clears the offer in every home."""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id or is_interim_delegation_event(evt):
        return
    with _orphan_lock:
        _offered.difference_update({key for key in _offered if key[1] == delegation_id})


def _event_delivery(fn, evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        fn(str(evt.get("delegation_id") or ""), claim_id)





def _json_object(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []




_DURABLE_SELECT = """SELECT
    delegation_id, origin_session, origin_ui_session_id, parent_session_id,
    state, dispatched_at, completed_at, updated_at, event_json, result_json,
    delivery_state, delivery_attempts, delivered_at, delivery_claim,
    delivery_claimed_at, task_json, root_subagent_ids_json, children_json,
    interrupt_requests_json, interrupt_reason, abandon_reason, owner_pid,
    owner_started_at, origin_session_id
FROM async_delegations"""


def _durable_snapshot(row):
    task = _json_object(row[15])
    return {
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
        "origin_session_id": row[23] or "",
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















_FAILED_TASK_STATES = frozenset({"error", "failed", "failure", "timeout", "stalled", "unknown", "interrupted"})
_FAILURE_SURFACE_WINDOW_S = 24 * 3600.0


def failed_delegations_for_session(
    origin_ui_session_id: str = "", parent_session_id: str = "", *, limit: int = 20, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Recently failed async delegation tasks owned by a session, newest first.

    The live roster forgets a child once it ends and does not survive a renderer reload, so a failed
    delegation had nowhere to show (#97202). This reads the durable row instead: one entry per failed
    task (a batch unit that "completed" can still carry failed tasks) with ``delegation_id``,
    ``task_index``, ``goal``, ``status``, ``error``, ``dispatched_at`` and ``completed_at``. Either selector claims a row:
    the UI session id at dispatch, or the spawner's durable session id (survives a reload re-mint)."""
    selectors = [(col, val) for col, val in (
        ("origin_ui_session_id", origin_ui_session_id), ("parent_session_id", parent_session_id)) if val]
    if not selectors:
        return []
    cutoff = (now if now is not None else time.time()) - _FAILURE_SURFACE_WINDOW_S
    owner_sql = " OR ".join(f"{col}=?" for col, _ in selectors)
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            f"""SELECT delegation_id, state, dispatched_at, completed_at, task_json, result_json FROM async_delegations
                WHERE ({owner_sql}) AND state NOT IN ('running','finalizing','interrupt_requested') AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT ?""",
            (*(val for _, val in selectors), cutoff, limit)).fetchall()
    failed: List[Dict[str, Any]] = []
    for delegation_id, state, dispatched_at, completed_at, task_json, result_json in rows:
        task, result = _json_object(task_json), _json_object(result_json)
        goals = task.get("goals") if isinstance(task.get("goals"), list) and task["goals"] else [task.get("goal") or ""]
        goal_for = dict(zip(task.get("task_indexes") or range(len(goals)), goals))
        tasks = result["results"] if isinstance(result.get("results"), list) else [] if task.get("is_batch") else [result]
        if not tasks and str(state).lower() in _FAILED_TASK_STATES:
            tasks = [{"task_index": 0, "error": result.get("error")}]
        for entry in tasks:
            status = str(entry.get("status") or state or "").lower()
            if status not in _FAILED_TASK_STATES:
                continue
            index = entry.get("task_index") if isinstance(entry.get("task_index"), int) else 0
            error = entry.get("error") or result.get("error")
            failed.append({
                "delegation_id": delegation_id, "task_index": index, "status": status,
                "goal": str(goal_for.get(index, goals[0]) or ""), "error": str(error) if error else None,
                "dispatched_at": dispatched_at, "completed_at": completed_at})
    return failed[:limit]


# ── In-memory registry queries ──────────────────────────────────────────────
def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow in place, never shrink) the shared daemon executor. Raising
    ``_max_workers`` is enough: the next ``submit`` spawns threads up to the new cap."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None:
            _executor = DaemonThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="async-delegate")
            _executor_max_workers = max_workers
        elif max_workers > _executor_max_workers:
            _executor._max_workers = max_workers
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of live async delegation UNITS (one per completion message: a task group or an ungrouped task)."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)


def active_task_count() -> int:
    """Number of running child subagents (a batch of N contributes N; a batch with
    no goal list counts 1) — the truthful observability figure, unlike slots."""
    with _records_lock:
        return sum(
            len(r.get("task_indexes") or r["goals"])
            if r.get("is_batch") and isinstance(r.get("goals"), (list, tuple)) and r["goals"] else 1
            for r in _records.values() if r.get("status") in _ACTIVE_STATES)


def _session_records(statuses, session_key: str, origin_ui_session_id: str, parent_session_id: str) -> list:
    """Records in ``statuses`` owned by a session: any non-empty selector claims the
    record — ``origin_ui_session_id`` (TUI tab), ``session_key`` (routing key at
    dispatch), or ``parent_session_id`` (spawner's durable id — the right one for
    gateway chats, whose session_key survives ``/new`` while the session id rotates)."""
    selectors = [(field, wanted) for field, wanted in (
        ("origin_ui_session_id", origin_ui_session_id), ("session_key", session_key),
        ("parent_session_id", parent_session_id)) if wanted]
    if not selectors:
        return []
    with _records_lock:
        return [r for r in _records.values() if r.get("status") in statuses
                and any(str(r.get(field) or "") == wanted for field, wanted in selectors)]


def has_live_for_session(session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "") -> bool:
    """Whether a session still owns any live (running/stalling/finalizing) delegation."""
    return bool(_session_records(_LIVE_STATES, session_key, origin_ui_session_id, parent_session_id))


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the cap. Caller holds ``_records_lock``.
    ``stalling``/``finalizing`` are still live: evicting one makes the late runner return hit
    ``_finalize``'s missing-record path and silently drop a real result."""
    completed = [(rid, r) for rid, r in _records.items() if r.get("status") not in _LIVE_STATES]
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: max(0, len(completed) - _MAX_RETAINED_COMPLETED)]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``. ``HERMES_SESSION_ID``
    is unsafe here: building the child agent calls ``set_current_session_id(child.session_id)``
    just before dispatch, so the wake would self-post into the subagent's own session. The
    request-scoped ``HERMES_SESSION_CHAT_ID`` (raw X-Hermes-Session-Id on api_server) survives
    child construction; on push platforms chat_id is a chat, not a session => ``""``."""
    try:
        from gateway.session_context import get_session_env
        is_api = get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server"
        return (get_session_env("HERMES_SESSION_CHAT_ID", "") or "") if is_api else ""
    except Exception:
        return ""


# ── Dispatch ────────────────────────────────────────────────────────────────
def _single_crash(error: str, duration: float) -> Dict[str, Any]:
    return {"status": "error", "summary": None, "error": error, "api_calls": 0, "duration_seconds": duration}


def _batch_crash(error: str, duration: float) -> Dict[str, Any]:
    return {"results": [], "error": error, "total_duration_seconds": duration}


def _batch_status(combined: Dict[str, Any]) -> str:
    """Batch status: completed unless every child errored/was interrupted."""
    child_results = combined.get("results") or []
    ok = ("completed", "success")
    return "error" if child_results and all(r.get("status") not in ok for r in child_results) else "completed"


def _dispatch(**kwargs) -> Dict[str, Any]:
    from hermes_cli.backend_retirement import retirement

    with retirement.work() as admitted:
        if not admitted:
            return {"status": "rejected", "error": "backend is retiring; reconnect to continue"}
        return _dispatch_admitted(**kwargs)


def _dispatch_admitted(
    *, delegation_id: str, goal: str, goals: Optional[List[str]], context: Optional[str],
    toolsets: Optional[List[str]], role: str, model: Optional[str], session_key: str,
    parent_session_id: Optional[str], runner: Callable[[], Dict[str, Any]], origin_ui_session_id: str,
    origin_session_id: str, interrupt_fn: Optional[Callable[[], None]], max_async_children: int,
    progress_fn: Optional[Callable[[], tuple]], capacity_error: str, slot_key: Optional[str] = None,
    task_indexes: Optional[List[int]] = None,
    task_transcripts: Optional[Dict[str, str]] = None,
    root_subagent_ids: Optional[List[str]] = None,
    attempt_ids_by_logical_id: Optional[Dict[str, str]] = None,
    authority_by_logical_id: Optional[Dict[str, Dict[str, Any]]] = None,
    _bind_attempts: Optional[Callable[[str, Dict[str, str]], None]] = None,
) -> Dict[str, Any]:
    """Shared dispatch core for single (``goals is None``) and batch units. Capacity check +
    record insert happen under ONE lock hold so concurrent dispatches can't both pass the check
    and exceed the cap. At capacity the dispatch is REJECTED (never queued) so a runaway model
    can't pile up unbounded background work. ``slot_key`` names the pool slot the unit occupies
    (default: its own id); the units of one delegate_task call share the first unit's id so
    splitting a call into per-group completions never consumes more capacity than the call did."""
    is_batch = goals is not None
    label = " batch" if is_batch else ""
    classify = _batch_status if is_batch else (lambda r: r.get("status") or "completed")
    crash_result = _batch_crash if is_batch else _single_crash
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id, "goal": goal, **({"goals": list(goals)} if is_batch else {}),
        "context": context, "toolsets": list(toolsets) if toolsets else None, "role": role, "model": model,
        "session_key": session_key, "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id, "parent_session_id": parent_session_id,
        **_capture_routing_origin(),
        "status": "running", "dispatched_at": dispatched_at, "completed_at": None,
        "interrupt_fn": interrupt_fn, **({"is_batch": True} if is_batch else {}), "progress_fn": progress_fn,
        "slot_key": slot_key or delegation_id,
        **({"task_transcripts": dict(task_transcripts)} if task_transcripts else {}),
        # Which of the call's ``goals`` this unit runs (None = all of them).
        **({"task_indexes": list(task_indexes)} if task_indexes is not None else {}),
        # The one stale-monitor thread serves every profile and starts with an empty Context;
        # a forced finalization runs under the dispatcher's so it settles the same state.db.
        "_context": contextvars.copy_context(),
        # Stale-monitor bookkeeping (see _stale_monitor_loop).
        "_progress_token": None, "_progress_ts": dispatched_at, "_interrupted_at": None}
    with _records_lock:
        active_slots = {r.get("slot_key") or r["delegation_id"] for r in _records.values() if r.get("status") in _ACTIVE_STATES}
        if record["slot_key"] not in active_slots and len(active_slots) >= max_async_children:
            return {"status": "rejected", "error": capacity_error}
        _records[delegation_id] = record
        live_units = sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)
    if root_subagent_ids is not None:
        record["root_subagent_ids"] = list(root_subagent_ids)
    if attempt_ids_by_logical_id is not None:
        record["attempt_ids_by_logical_id"] = dict(attempt_ids_by_logical_id)
    if authority_by_logical_id is not None:
        record["authority_by_logical_id"] = dict(authority_by_logical_id)
    try:
        _persist_dispatch(record)
        if _bind_attempts is not None:
            _bind_attempts(str(record["run_id"]), dict(zip(record["root_subagent_ids"], record["attempt_ids"])))
    except Exception as exc:
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(delegation_id)
        return {"status": "rejected", "reason": "dispatch_setup_failed", "error": str(exc)}
    # Units of one call share a slot, so live units can exceed slots: size the pool by units or a
    # unit queues behind a full pool and the stale monitor kills it before its child ever starts.
    executor = _get_executor(max(max_async_children, live_units))

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        with _records_lock:
            rec = _records.get(delegation_id)
            if rec is not None:
                # The stall clock starts when the runner starts; a unit queued behind a full pool is not stalled.
                rec.update(_started=True, _progress_ts=time.time())
        try:
            result = runner() or {}
            status = classify(result)
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception(f"Async delegation{label} %s crashed", delegation_id)
            result = crash_result(f"{type(exc).__name__}: {exc}", round(time.time() - dispatched_at, 2))
        finally:
            _finalize(delegation_id, result, status)

    from hermes_cli.backend_retirement import retirement

    # The outer dispatch reservation prevents a freeze during this handoff. Retain a worker
    # reservation too: the stall monitor may finalize its registry record before it really exits.
    retirement.acquire()
    try:
        future = executor.submit(propagate_context_to_thread(_worker))
        future.add_done_callback(lambda _: retirement.release())
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        retirement.release()
        with _records_lock:
            _records.pop(delegation_id, None)
        with _DB_LOCK, _transaction() as conn:
            conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
        return {"status": "rejected", "error": f"Failed to schedule async delegation{label}: {exc}"}
    if progress_fn is not None:
        _ensure_stale_monitor()
    return {"status": "dispatched", "delegation_id": delegation_id, **_dispatch_identity_payload(record)}


def dispatch_async_delegation(
    *, goal: str, context: Optional[str], toolsets: Optional[List[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, progress_fn: Optional[Callable[[], tuple]] = None,
    root_subagent_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.
    ``session_key``/``parent_session_id`` are captured on the parent thread (the worker carries
    no contextvars) and route the completion back to the spawning session.
    ``progress_fn() -> (token, in_tool)`` enables stale monitoring; omitted = unmonitored.
    Returns ``{"status": "dispatched", "delegation_id"}`` or ``{"status": "rejected", "error"}``."""
    delegation_id = _new_delegation_id()
    handle = _dispatch(
        delegation_id=delegation_id, goal=goal, goals=None, context=context, root_subagent_ids=root_subagent_ids,
        toolsets=toolsets, role=role, model=model, session_key=session_key,
        parent_session_id=parent_session_id, runner=runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        interrupt_fn=interrupt_fn, max_async_children=max_async_children, progress_fn=progress_fn,
        capacity_error=(
            f"Async delegation capacity reached ({max_async_children} running). Wait for one to finish "
            "(its result will re-enter the chat), or run this task synchronously (background=false). "
            "Raise delegation.max_concurrent_children in config.yaml to allow more concurrent background subagents."))
    if handle["status"] == "dispatched":
        logger.info("Dispatched async delegation %s (session_key=%s): %s",
                    delegation_id, session_key or "<cli>", (goal or "")[:80])
    return handle


def dispatch_async_delegation_batch(
    *, goals: List[str], context: Optional[str], toolsets: Optional[List[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, delegation_id: Optional[str] = None,
    progress_fn: Optional[Callable[[], tuple]] = None, slot_key: Optional[str] = None,
    task_indexes: Optional[List[int]] = None,
    task_transcripts: Optional[Dict[str, str]] = None,
    root_subagent_ids: Optional[List[str]] = None,
    attempt_ids_by_logical_id: Optional[Dict[str, str]] = None,
    authority_by_logical_id: Optional[Dict[str, Dict[str, Any]]] = None,
    _bind_attempts: Optional[Callable[[str, Dict[str, str]], None]] = None,
) -> Dict[str, Any]:
    """Dispatch a fan-out unit (a whole batch, or one ``group`` of a delegate_task call) as ONE
    background unit: ``runner`` runs its tasks and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict. The unit occupies ONE async slot — or joins the slot named
    by ``slot_key`` (in-unit parallelism is bounded separately) — and produces a SINGLE completion
    event carrying per-task ``results``."""
    delegation_id = delegation_id or _new_delegation_id()
    # ``goals`` is the whole call (result task_index indexes it); the unit's own goals label the record.
    unit_goals = [goals[i] for i in task_indexes] if task_indexes is not None else list(goals)
    n = len(unit_goals)
    combined_goal = unit_goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in unit_goals)
    handle = _dispatch(
        root_subagent_ids=root_subagent_ids, attempt_ids_by_logical_id=attempt_ids_by_logical_id,
        authority_by_logical_id=authority_by_logical_id, _bind_attempts=_bind_attempts,
        delegation_id=delegation_id, goal=combined_goal, goals=goals, context=context,
        toolsets=toolsets, role=role, model=model, session_key=session_key,
        parent_session_id=parent_session_id, runner=runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        interrupt_fn=interrupt_fn, max_async_children=max_async_children, progress_fn=progress_fn, slot_key=slot_key,
        task_indexes=task_indexes, task_transcripts=task_transcripts,
        capacity_error=(
            f"Async delegation capacity reached ({max_async_children} running). Wait for one to finish "
            "(its result will re-enter the chat), or raise delegation.max_concurrent_children in "
            "config.yaml to allow more concurrent background units."))
    if handle["status"] == "dispatched":
        logger.info("Dispatched async delegation batch %s (%d task(s), session_key=%s)",
                    delegation_id, n, session_key or "<cli>")
    return handle


# ── Finalization + completion events ────────────────────────────────────────
def _finalize(delegation_id: str, result: Any, status: str) -> None:
    """Atomically claim terminal delivery, push the completion event, then mark ``status``.
    ``result`` is a dict or a callable receiving the record snapshot (stall path). The record
    stays active ("finalizing") until durable persistence and queue publication finish; otherwise
    process shutdown can kill this daemon worker after status flips but before SQLite commits.
    A second call for the same id (late runner return after a forced stall) is a no-op."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in _ACTIVE_STATES:
            return
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["progress_fn"] = None  # stop stale-monitor sampling
        snapshot = dict(record)
    _push_completion_event(snapshot, result(snapshot) if callable(result) else result, status)
    with _records_lock:
        if delegation_id in _records:
            _records[delegation_id]["status"] = status
        _prune_completed_locked()


_FINISHED_CHILD_STATES = {"completed", "success"}


def _stalled_resume_targets(record: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """Logical children a force-finalized run left unfinished — the exact ``resume`` targets its notice names.
    Built before ``complete_run`` terminalizes the attempts, so it reads the dispatch identities, not the ledger.
    A record without real logical ids (cron's single dispatch) yields none rather than a placeholder."""
    if record.get("is_batch"):
        roots = record.get("root_subagent_ids") or []
    else:
        roots = [record["subagent_id"]] if record.get("subagent_id") else []
    finished = {r.get("subagent_id") for r in result.get("results") or []
                if isinstance(r, dict) and r.get("status") in _FINISHED_CHILD_STATES}
    return [sid for sid in roots if sid not in finished]


def _push_completion_event(record: Dict[str, Any], result: Dict[str, Any], status: str) -> None:
    """Push a type='async_delegation' event onto the shared completion queue. Batch records
    (``is_batch``) carry the per-task ``results`` list (plus live transcript paths, the
    full-fidelity record of each child's run) instead of a single summary. Best-effort: failure
    must not crash the worker, but it WOULD mean a silently-lost result, so we log loudly."""
    is_batch = bool(record.get("is_batch"))
    label = " batch" if is_batch else ""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(f"Async delegation{label} %s finished but process_registry import failed; "
                     "result lost: %s", record.get("delegation_id"), exc)
        return
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()
    if is_batch:
        payload = {
            "is_batch": True, "results": result.get("results") or [],
            "live_transcripts": result.get("live_transcripts"), "error": result.get("error"),
            "total_duration_seconds": result.get("total_duration_seconds"),
            **({"group": result["group"]} if result.get("group") is not None else {})}
    else:
        payload = {
            "summary": result.get("summary"), "error": result.get("error"), "api_calls": result.get("api_calls", 0),
            "duration_seconds": result.get("duration_seconds", round(completed_at - dispatched_at, 2))}
    evt = {
        "type": "async_delegation", "delivery_managed": True, "delegation_id": record.get("delegation_id"), "run_id": record.get("run_id"),
        # session_key routes back to the originating gateway session; "" => CLI.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""), **({"goals": record.get("goals")} if is_batch else {}),
        "context": record.get("context"), "toolsets": record.get("toolsets"), "role": record.get("role"),
        "model": record.get("model") if is_batch else (result.get("model") or record.get("model")),
        "status": status, **payload, "dispatched_at": dispatched_at, "completed_at": completed_at,
        **({} if is_batch else {"exit_reason": result.get("exit_reason")}),
        **{k: record[k] for k in _ROUTING_KEYS if record.get(k)},
        **{k: result[k] for k in _STALL_META_KEYS if k in result},
        **({"resume_subagent_ids": _stalled_resume_targets(record, result)} if status == "stalled" else {})}
    try:
        if not _persist_completion(evt, result):
            return
    except Exception as exc:  # noqa: BLE001 — a lost durable row is recoverable; a lost result + leaked slot is not
        logger.error(f"Async delegation{label} %s: durable completion write failed; delivering in-memory "
                     "only (a restart may report this unit as unknown): %s", record.get("delegation_id"), exc)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(f"Async delegation{label} %s: failed to enqueue completion event; "
                     "result lost: %s", record.get("delegation_id"), exc)


def push_task_failure_notice(delegation_id: str, entry: Dict[str, Any], *, n_tasks: int) -> None:
    """Surface ONE failed child of a still-running detached batch to the parent now, instead of
    when the slowest sibling finishes. In a 1,393-agent run every wave-1 child died in a 401 storm
    at 08:29 and the parent learned of it at 09:36, when the batch's "unknown outcome" block finally
    arrived: 66 minutes of a dead wave with nothing running. The notice rides the same
    ``type="async_delegation"`` event shape as the batch result (so every drain/route/format path
    treats it identically) with ``task_failure_notice=True`` and a single-entry ``results`` list; the
    batch record is NOT finalized and its consolidated result still arrives as before."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in _ACTIVE_STATES:
            return
        snapshot = dict(record)
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error("Async delegation batch %s: task failure notice dropped (process_registry import): %s", delegation_id, exc)
        return
    evt = {
        "type": "async_delegation", "task_failure_notice": True, "is_batch": True, "n_tasks": n_tasks,
        "delegation_id": delegation_id, "results": [entry],
        "session_key": snapshot.get("session_key", ""),
        "origin_ui_session_id": snapshot.get("origin_ui_session_id", ""),
        "origin_session_id": snapshot.get("origin_session_id", ""),
        "parent_session_id": snapshot.get("parent_session_id"),
        "goal": snapshot.get("goal", ""), "goals": snapshot.get("goals"), "context": snapshot.get("context"),
        "toolsets": snapshot.get("toolsets"), "role": snapshot.get("role"), "model": snapshot.get("model"),
        "status": "running", "dispatched_at": snapshot.get("dispatched_at") or time.time(), "completed_at": time.time(),
        **{k: snapshot[k] for k in _ROUTING_KEYS if snapshot.get(k)}}
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error("Async delegation batch %s: failed to enqueue task failure notice: %s", delegation_id, exc)


# ── Stale monitor ───────────────────────────────────────────────────────────
def _ensure_stale_monitor() -> None:
    """Start (once) the stale-delegation monitor thread. One daemon thread serves
    every dispatch; it exits when no monitorable records remain and is restarted
    by the next dispatch with a ``progress_fn``."""
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop, name="async-delegate-stale-monitor", daemon=True)
        _monitor_thread.start()


def _sweep_stale_locked(now: float):
    """One monitor pass over ``_records``; caller holds ``_records_lock``. Returns
    ``(stalled, expired, any_monitorable)``: newly-stalling ``(delegation_id, quiet_for, in_tool)``
    tuples, stalling ids past the grace window, and whether anything is left to monitor."""
    stalled, expired, any_monitorable = [], [], False  # (delegation_id, quiet_for, in_tool) / ids past grace
    for record in _records.values():
        status = record.get("status")
        if status == "stalling":
            any_monitorable = True
            if now - (record.get("_interrupted_at") or now) >= _STALL_GRACE_SECONDS:
                expired.append(record["delegation_id"])
            continue
        progress_fn = record.get("progress_fn")
        if status != "running" or progress_fn is None:
            continue
        any_monitorable = True
        if not record.get("_started"):
            continue  # queued behind a full pool: not stalled, but keep the monitor alive for when it starts
        try:
            token, in_tool = progress_fn()
            token = _liveness_view(token)
        except Exception:
            # An unreadable child must not look permanently healthy —
            # keep the last timestamp running instead of refreshing it.
            token, in_tool = record.get("_progress_token"), False
        if token != record.get("_progress_token"):
            record.update(_progress_token=token, _progress_ts=now)
            continue
        quiet_for = now - (record.get("_progress_ts") or now)
        limit = _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
        if quiet_for >= limit:
            # Stall context feeds the terminal event and status listings.
            record.update(
                status="stalling", _interrupted_at=now, _stall_quiet_seconds=round(quiet_for, 2),
                _stall_threshold_seconds=limit, _stall_in_tool=bool(in_tool))
            stalled.append((record["delegation_id"], quiet_for, in_tool))
    return stalled, expired, any_monitorable


def _call_interrupt(fn, msg: str, *args) -> bool:
    """Invoke an ``interrupt_fn``; True on success, else debug-log ``msg`` (+ exc)."""
    if not callable(fn):
        return False
    try:
        fn()
        return True
    except Exception as exc:
        logger.debug(msg, *args, exc)
        return False


def _stale_monitor_loop() -> None:
    """Sweep running delegations for stalled progress. A changed progress token refreshes the
    record's timestamp; a frozen token past the idle/in-tool threshold marks the record
    ``stalling`` and calls ``interrupt_fn``; a ``stalling`` record still unreturned after the
    grace window is force-finalized with a terminal ``stalled`` event."""
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        now = time.time()
        with _records_lock:
            stalled, expired, any_monitorable = _sweep_stale_locked(now)
        for delegation_id, quiet_for, in_tool in stalled:
            logger.warning("Async delegation %s made no progress for %.0fs "
                           "(in_tool=%s) — interrupting; grace window %.0fs",
                           delegation_id, quiet_for, in_tool, _STALL_GRACE_SECONDS)
            with _records_lock:
                fn = (_records.get(delegation_id) or {}).get("interrupt_fn")
            _call_interrupt(fn, "Async delegation %s stall interrupt failed: %s", delegation_id)
        for delegation_id in expired:
            with _records_lock:
                ctx = (_records.get(delegation_id) or {}).get("_context") or contextvars.copy_context()
            ctx.run(_finalize, delegation_id, lambda rec, d=delegation_id: _stalled_result(d, rec), "stalled")
        if not any_monitorable:
            return


def _stalled_error_text(event_record: Dict[str, Any]) -> str:
    """Human wording for a force-finalized stall. This string reaches the user (CLI timeline, Desktop
    async-result card), so it names the task, how long it was silent, and what to do — no issue
    numbers or worker internals (those stay in the log line and the stall_* metadata)."""
    goal = " ".join(str(event_record.get("goal") or "").split())
    label = f'Background task "{goal[:120]}"' if goal else "The background task"
    quiet = float(event_record.get("_stall_quiet_seconds") or 0)
    silence = f" after {round(quiet / 60)} min of no progress" if quiet >= 60 else ""
    return (f"{label} stopped responding{silence} and was cancelled. Nothing else was affected; "
            "ask me to run it again if you still need it.")


def _stalled_result(delegation_id: str, event_record: Dict[str, Any]) -> Dict[str, Any]:
    """Synthetic terminal result for a stalling delegation whose runner never returned."""
    completed_at = event_record.get("completed_at") or time.time()
    duration = round(completed_at - (event_record.get("dispatched_at") or completed_at), 2)
    error = _stalled_error_text(event_record)
    logger.error("Async delegation %s force-finalized as stalled after %.0fs", delegation_id, duration)
    # Structured stall metadata lets parents/UIs distinguish a stall-monitor
    # kill from other failures without parsing the error string.
    stall_in_tool = event_record.get("_stall_in_tool")
    stall_meta = {
        "stalled_after_quiet_seconds": event_record.get("_stall_quiet_seconds"),
        "stall_threshold_seconds": event_record.get("_stall_threshold_seconds"),
        "stall_phase": "in_tool" if stall_in_tool else "idle" if stall_in_tool is not None else None,
        "stall_grace_seconds": _STALL_GRACE_SECONDS}
    if event_record.get("is_batch"):
        return {**_batch_crash(error, duration), **stall_meta}
    return {**_single_crash(error, duration), "status": "stalled", "exit_reason": "stalled", **stall_meta}


# ── Observability + control ─────────────────────────────────────────────────
def _liveness_view(token: Any) -> Any:
    """The part of a progress token that decides frozen vs moving: each child's (api_call_count, current_tool,
    last_activity_ts). Extra observer fields (iteration budget, which also moves on refunds) never count as progress."""
    if not isinstance(token, (list, tuple)):
        return token
    return tuple(tuple(part[:3]) if isinstance(part, (list, tuple)) else part for part in token)


def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Parse a progress token into per-child activity dicts (best-effort): delegate_tool
    emits one ``(api_call_count, current_tool, last_activity_ts)`` tuple per child;
    foreign token shapes degrade to ``None`` entries."""
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if not (isinstance(part, (list, tuple)) and len(part) >= 2):
            out.append(None)
            continue
        entry: Dict[str, Any] = {"api_calls": part[0], "current_tool": part[1]}
        if len(part) >= 3 and isinstance(part[2], (int, float)):
            entry["seconds_since_activity"] = round(max(0.0, now - float(part[2])), 1)
        if len(part) >= 5 and isinstance(part[3], int) and isinstance(part[4], int):
            entry["iterations_used"], entry["iterations_max"] = part[3], part[4]
        if len(part) >= 6 and part[1] and isinstance(part[5], (int, float)):
            entry["seconds_in_tool"] = round(max(0.0, now - float(part[5])), 1)
        out.append(entry)
    return out


def subagent_activity(subagent_ids) -> Dict[str, Dict[str, Any]]:
    """Live activity per logical subagent id, for observers that must tell a working child from a stuck one.

    A unit's progress token lists its children in ``root_subagent_ids`` order. Only units holding a requested id are
    sampled, outside the registry lock. An id this process holds no live record for (finished, a resumed attempt,
    another process) maps to ``{"known": False}``, and a live child whose sample failed has no activity fields —
    absence means unknown, never healthy. Stall fields appear once the stale monitor has flagged the unit.
    ``seconds_in_tool`` (time in the current tool) is the signal for a long or hung tool: activity is heartbeated
    throughout a tool, so ``seconds_since_activity`` stays near zero inside one.
    """
    wanted = {str(sid) for sid in subagent_ids or () if sid}
    out: Dict[str, Dict[str, Any]] = {sid: {"known": False} for sid in wanted}
    now = time.time()
    units = []
    with _records_lock:
        for r in _records.values():
            state = r.get("status")
            roots = r.get("root_subagent_ids") if r.get("is_batch") else [r.get("subagent_id")]
            if state in _ACTIVE_STATES and wanted.intersection(roots or ()):
                stall = {dst: r[src] for src, dst in _STALL_FIELD_MAP if r.get(src) is not None}
                units.append((dict(delegation_id=r.get("delegation_id"), run_id=r.get("run_id"), unit_state=state,
                                   stall_suspected=state == "stalling", **stall), list(roots), r.get("progress_fn")))
    for base, roots, sampler in units:
        activity: List = []
        if callable(sampler):
            try:
                activity = _children_activity_from_token(sampler()[0], now) or []
            except Exception:
                activity = []
        for index, sid in enumerate(roots):
            if sid in wanted:
                entry = {"known": True, **base}
                if index < len(activity) and isinstance(activity[index], dict):
                    entry.update(activity[index])
                out[sid] = entry
    return out


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed) without callables or private
    monitor bookkeeping; adds computed live fields for UIs (``seconds_since_progress``,
    ``children_activity``/``in_tool`` sampled from ``progress_fn``) and stall context once tripped.

    Safe to call from any thread. See #51690.
    """
    now = time.time()
    samplers: Dict[str, Callable] = {}
    with _records_lock:
        items = []
        for r in _records.values():
            item = {k: v for k, v in r.items() if k not in {"interrupt_fn", "progress_fn"} and not k.startswith("_")}
            status = r.get("status")
            if status in _ACTIVE_STATES:
                if r.get("_progress_ts"):
                    item["seconds_since_progress"] = round(now - r["_progress_ts"], 1)
                if callable(r.get("progress_fn")):
                    samplers[r["delegation_id"]] = r["progress_fn"]
            if status in ("stalling", "stalled"):
                for src, dst in _STALL_FIELD_MAP:
                    if r.get(src) is not None:
                        item[dst] = r.get(src)
            items.append(item)
    # Sample OUTSIDE the lock — progress_fn reads child-agent attributes and a
    # slow/broken sampler must not block every dispatch/finalize.
    for item in items:
        fn = samplers.get(item.get("delegation_id"))
        if fn is None:
            continue
        try:
            token, in_tool = fn()
        except Exception:
            continue
        activity = _children_activity_from_token(token, now)
        if activity is not None:
            item["children_activity"] = activity
        item["in_tool"] = bool(in_tool)
    return items


def _interrupt_records(targets: List[Dict[str, Any]], caller: str, reason: str, msg: str) -> int:
    """Call ``interrupt_fn`` on each record; log ``msg`` once; returns how many succeeded."""
    count = sum(
        _call_interrupt(r.get("interrupt_fn"), "%s: %s interrupt failed: %s", caller, r.get("delegation_id"))
        for r in targets)
    if count:
        logger.info(msg, count, reason)
    return count


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop (``/stop``, shutdown). Returns how
    many. The child still emits a completion event (status='interrupted') via the
    normal finalize path."""
    with _records_lock:
        targets = [r for r in _records.values() if r.get("status") in _ACTIVE_STATES]
    return _interrupt_records(targets, "interrupt_all", reason, "Interrupted %d async delegation(s) (%s)")


def interrupt_for_session(
    session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "", reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE ending session to stop (any
    selector matches, see ``_session_records``). Returns how many."""
    targets = _session_records(_ACTIVE_STATES, session_key, origin_ui_session_id, parent_session_id)
    return _interrupt_records(
        targets, "interrupt_for_session", reason, "Interrupted %d async delegation(s) for ending session (%s)")


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor + monitor."""
    global _executor, _executor_max_workers, _monitor_thread
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    _monitor_stop.set()
    with _monitor_lock:
        thread, _monitor_thread = _monitor_thread, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with _records_lock:
        _records.clear()
    with _orphan_lock:
        _offered.clear()
        _last_orphan_sweep.clear()

from tools.delegation_repository import DelegationRepository, _TERMINAL_DELIVERY_STATES, _attempt_state

def _notify_state_change() -> None:
    """Wake local waiters; SQLite remains the lifecycle authority."""
    with _STATE_CONDITION:
        _STATE_CONDITION.notify_all()

def _db_path():
    return get_hermes_home() / "state.db"

def _repository() -> DelegationRepository:
    return DelegationRepository(_db_path())

def _changed(outcome: Dict[str, Any], *success: str) -> bool:
    changed = outcome.get("status") in success
    if changed:
        _notify_state_change()
    return changed

def _persist_dispatch(record: Dict[str, Any]) -> None:
    try:
        from gateway.status import get_process_start_time

        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    if not record.get("root_subagent_ids"):
        record["root_subagent_ids"] = [f"sa-{record['delegation_id']}-{i}" for i in range(len(record.get("goals") or [None]))]
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch", "task_indexes", "task_transcripts", *_ROUTING_KEYS)
        if key in record}
    try:  # where the children's terminals started; lets recovery add a git-state hint
        task_payload["owner_cwd"] = os.getcwd()
    except OSError:
        pass
    outcome = _repository().register_initial_dispatch(
        record, owner_pid=os.getpid(), owner_started_at=owner_started_at, task=task_payload
    )
    if outcome.get("status") != "registered":
        raise RuntimeError(f"durable delegation registration failed: {outcome}")
    record["run_id"] = outcome["run_id"]
    record["attempt_ids"] = [item["attempt_id"] for item in outcome["attempts"]]
    _notify_state_change()
    _prune_durable_records()

def _delete_durable_delegation(delegation_id: str) -> None:
    if _repository().delete(delegation_id):
        _notify_state_change()

def _prune_durable_records() -> None:
    """Bound safely terminal history; never prune an undelivered result."""
    _repository().prune(
        cutoff=time.time() - _DURABLE_RETENTION_SECONDS,
        max_terminal=_MAX_RETAINED_COMPLETED,
    )

def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> bool:
    """Persist worker completion without stealing an existing delivery hold. False only for a duplicate or
    stale completion: a delegation whose dispatch was never persisted is still delivered in memory (a lost
    durable row is recoverable; a lost result is not)."""
    run_id = event.get("run_id")
    if not run_id:
        resolved = _repository().resolve_run_id(event["delegation_id"])
        if resolved.get("status") == "not_found":
            return True
        if resolved.get("status") != "found":
            return False
        run_id = resolved["run_id"]
    return _repository().complete_run(str(run_id), event, result).get("status") in {"completed", "not_found"}

# Most advanced first: a unit with any child finalizing was last seen finalizing.
_LAST_KNOWN_ORDER = ("finalizing", "interrupt_requested", "running", "starting")


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0

    def owner_alive(pid: int, started: Optional[int]) -> bool:
        if not _pid_exists(pid):
            return False
        return started is None or get_process_start_time(pid) == int(started)

    repository = _repository()
    recovered = repository.recover_orphaned_attempts(owner_alive)["attempts"]
    if not recovered:
        return 0
    affected = 0
    last_states: Dict[tuple, List[str]] = {}
    for item in recovered:
        last_states.setdefault((item["delegation_id"], item["run_id"]), []).append(item["state"])
    for (delegation_id, run_id), states in last_states.items():
        current = repository.snapshot(delegation_id, run_id=run_id)
        if (
            not current or current["completed_at"] is not None
            or any(child["status"] in _ACTIVE_STATES for child in current["children"].values())
        ):
            continue
        now = time.time()
        error = "Delegation owner exited before recording a terminal result; outcome unknown."
        # What the parent needs to continue or re-dispatch from the event alone: the last persisted
        # status, each task's transcript locator and verbatim tail, and the owner's git state.
        diagnostics = {
            "last_known_status": next((s for s in _LAST_KNOWN_ORDER if s in states), states[0]),
            "task_transcripts": current.get("task_transcripts") or {},
        }
        from tools.async_delegation_recovery_hints import git_state_hint, transcript_tails
        if tails := transcript_tails(diagnostics["task_transcripts"]):
            diagnostics["transcript_tails"] = tails
        if hint := git_state_hint(current.get("owner_cwd")):
            diagnostics["git_state_hint"] = hint
        event = {
            "type": "async_delegation",
            "delivery_managed": True,
            "delegation_id": delegation_id,
            "run_id": run_id,
            "session_key": current["session_key"],
            "origin_ui_session_id": current["origin_ui_session_id"],
            "origin_session_id": current.get("origin_session_id", ""),
            "parent_session_id": current["parent_session_id"],
            **{k: current[k] for k in _ROUTING_KEYS if current.get(k)},
            "goal": current.get("goal", ""),
            "goals": current.get("goals"),
            "context": current.get("context"),
            "toolsets": current.get("toolsets"),
            "role": current.get("role"),
            "model": current.get("model"),
            "is_batch": bool(current.get("is_batch")),
            "status": "unknown",
            "summary": None,
            "error": error,
            **diagnostics,
            "dispatched_at": current["dispatched_at"],
            "completed_at": now,
            "resume_subagent_ids": [
                sid for sid, child in current["children"].items()
                if child.get("run_id") == run_id and not child.get("parent_id")
                and child.get("resume_available") and child.get("status") not in _FINISHED_CHILD_STATES
            ],
        }
        if current.get("is_batch"):
            results = []
            for i, child in enumerate(current["children"].values()):
                finished = child.get("finished_result")
                results.append(finished if isinstance(finished, dict) else {"task_index": i, "status": "unknown", "summary": None, "error": error})
            event["results"] = results
            recorded = sum(isinstance(c.get("finished_result"), dict) for c in current["children"].values())
            event["error"] = error + f" {recorded}/{len(results)} child results were recorded."
        result = {"status": "unknown", "summary": None, "error": event["error"], **diagnostics}
        if repository.complete_run(run_id, event, result).get("status") == "completed":
            affected += 1
    if affected:
        _notify_state_change()
    return affected

def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484). Runs older than
    ``_MAX_COMPLETION_REPLAY_AGE_S`` are terminally dropped instead of
    replaying a turn nobody is waiting on.
    """
    if not _db_path().exists():
        return 0  # nothing to replay; a replay must not create (or migrate) the ledger (#123265)
    recover_abandoned_delegations()
    rows = [row for row in _repository().pending_completions() if row["delivery_state"] == "pending"]
    return _replay_runs(rows, target_queue, time.time())

def restore_stale_wait_completions(
    target_queue,
    *,
    session_key: str = "",
    owns_event: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> int:
    """Requeue expired wait holds only for a consumer that can own them.

    Foreign consumers leave the durable row untouched. An authorised consumer
    atomically claims the expired hold before enqueueing it, so another process
    cannot publish the same result and a busy local queue cannot grow duplicate
    restored copies.
    """
    restored = 0
    cutoff = time.time() - _WAIT_HOLD_STALE_SECONDS
    for row in _repository().pending_events(delivery_state="held_by_wait"):
        event = dict(row["event"])
        inspected = _repository().inspect_delivery(
            str(event.get("delegation_id") or ""), row["run_id"]
        )
        claimed_at = inspected.get("delivery_claimed_at")
        if claimed_at is not None and claimed_at >= cutoff:
            continue
        try:
            owned = bool(owns_event(event)) if owns_event else bool(
                session_key and str(event.get("session_key") or "") == session_key
            )
        except Exception:
            owned = False
        if not owned:
            continue
        token = f"stale-restore:{os.getpid()}:{uuid.uuid4().hex}"
        outcome = _repository().claim_run_delivery(
            str(event.get("delegation_id") or ""),
            row["run_id"],
            token,
            wait_stale_seconds=_WAIT_HOLD_STALE_SECONDS,
        )
        if outcome.get("status") != "claimed":
            continue
        event.update(
            restored=True,
            delivery_managed=True,
            run_id=row["run_id"],
            _async_delivery_claim_token=token,
        )
        try:
            target_queue.put(event)
        except Exception:
            _repository().release_run_delivery(row["run_id"], token)
            raise
        restored += 1
    return restored

def _terminal(snapshot: Dict[str, Any]) -> bool:
    """A run is terminal only after its durable producer finalization lands."""
    return bool(
        snapshot.get("completed_at") is not None
        and snapshot.get("event") is not None
    )

def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Internal durable lookup. Model-facing callers must use the authorised view."""
    return _repository().trusted_snapshot(delegation_id)

def get_async_delegation(
    delegation_id: str, *, session_key: str, run_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Read one session-authorised lifecycle record without claiming delivery."""
    return _repository().snapshot(
        delegation_id, session_key=session_key, run_id=run_id
    )

def get_async_delegation_attempt(
    delegation_id: str, attempt_id: str, *, session_key: str
) -> Optional[Dict[str, Any]]:
    return _repository().snapshot_for_attempt(
        delegation_id, attempt_id, session_key=session_key
    )

def list_durable_delegations(
    *, session_keys: Optional[List[str]] = None, limit: int = _MAX_DURABLE_LIST
) -> List[Dict[str, Any]]:
    """Read a bounded stable snapshot, optionally restricted to session owners."""
    return _repository().list_snapshots(session_keys=session_keys, limit=limit)

def load_subagent_resume_bundle(
    delegation_id: str,
    logical_id: str,
    *,
    session_key: str,
) -> Dict[str, Any]:
    """Load one authorized child's validated provider-facing replay bundle."""
    # Resume reconstruction is the sole consumer of canonical authority.  It
    # remains owner-authorized but bypasses the ordinary audit projection so
    # backing identity/revision can be revalidated before use.
    snapshot = _repository().trusted_snapshot(
        delegation_id, session_key=session_key
    )
    if snapshot is None:
        return {"status": "not_found"}
    child = (snapshot.get("children") or {}).get(logical_id)
    if not isinstance(child, dict):
        return {"status": "not_found"}
    protected = bool(child.get("protected_execution"))
    authority = child.get("authority")
    if protected and not isinstance(authority, dict):
        return {
            "status": "resume_unavailable",
            "reason": "protected authority is missing",
        }
    if authority is not None and not isinstance(authority, dict):
        return {
            "status": "resume_unavailable",
            "reason": "protected authority is malformed",
        }
    metadata = {
        key: child.get(key)
        for key in _RESUME_METADATA_FIELDS
        if key in child
    }
    candidates = [metadata]
    latest_attempt_id = child.get("attempt_id")
    for prior in _repository().resume_metadata_candidates(delegation_id, logical_id):
        if prior.get("attempt_id") == latest_attempt_id:
            continue
        raw = prior.get("metadata")
        if not isinstance(raw, dict):
            continue
        candidate = {
            key: raw.get(key)
            for key in _RESUME_METADATA_FIELDS
            if key in raw
        }
        if candidate:
            candidates.append(candidate)

    from hermes_state import SessionDB

    bundle = None
    last_missing_error = "missing subagent transcript"
    for candidate in candidates:
        child_session_id = str(candidate.get("child_session_id") or "")
        try:
            bundle = SessionDB().get_subagent_resume_bundle(
                child_session_id, candidate
            )
            break
        except ValueError as exc:
            # A completed attempt could advance its durable anchor before
            # the continuation row was actually persisted. Recover from the
            # newest older valid segment, but never bypass ownership/lineage
            # failures by trying a different attempt.
            if str(exc) != "missing subagent transcript":
                return {"status": "resume_unavailable", "reason": str(exc)}
            last_missing_error = str(exc)
        except (OSError, RuntimeError, TypeError) as exc:
            return {"status": "resume_unavailable", "reason": str(exc)}
    if bundle is None:
        return {"status": "resume_unavailable", "reason": last_missing_error}
    return {
        "status": "ready",
        "delegation_id": delegation_id,
        "subagent_id": logical_id,
        "attempt_id": child.get("attempt_id"),
        "attempt_number": child.get("attempt_number"),
        "run_id": child.get("run_id"),
        "authority": dict(authority) if isinstance(authority, dict) else None,
        "protected": protected,
        "bundle": bundle,
    }

def dispatch_resumed_subagent(
    delegation_id: str,
    logical_id: str,
    *,
    session_key: str,
    message: str,
    parent_agent,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Reserve, reconstruct, and dispatch one persisted logical child."""
    snapshot = get_async_delegation(delegation_id, session_key=session_key)
    if snapshot is None:
        return {"status": "not_found"}
    child_snapshot = ((snapshot or {}).get("children") or {}).get(logical_id) or {}
    if not child_snapshot:
        return {"status": "not_found"}
    if child_snapshot.get("status") in _ACTIVE_STATES:
        return {
            "status": "already_running",
            "attempt_id": child_snapshot.get("attempt_id"),
            "run_id": child_snapshot.get("run_id"),
        }
    loaded = load_subagent_resume_bundle(
        delegation_id, logical_id, session_key=session_key
    )
    if loaded.get("status") != "ready":
        return loaded

    restored_scope = None
    expected_policy = None
    if loaded.get("protected"):
        from agent.delegation_policy import DelegationSessionPolicy
        from tools.delegation_scope import deserialize_delegation_authority

        authority = loaded.get("authority")
        if not isinstance(authority, Mapping):
            return {
                "status": "resume_unavailable",
                "reason": "protected authority is malformed",
            }
        parent_policy = getattr(parent_agent, "delegation_policy", None)
        expected_policy = (
            parent_policy if isinstance(parent_policy, DelegationSessionPolicy) else None
        )
        try:
            restored_scope = deserialize_delegation_authority(
                authority,
                backing_registry=getattr(
                    parent_agent, "delegation_backing_registry", None
                ),
                expected_policy=expected_policy,
            )
        except (TypeError, ValueError) as exc:
            return {"status": "resume_unavailable", "reason": str(exc)}

    bundle = loaded["bundle"]
    from tools import delegate_tool as _delegate

    continuation = _delegate.prepare_resumed_child_session(bundle)
    # Keep the last persisted segment as the durable anchor while this attempt
    # is starting/running. The loader follows marked child continuations, so it
    # still discovers a partially persisted new segment after process loss,
    # without ever pointing durable state at a session that may not yet exist.
    metadata = {
        **dict(bundle["reconstruction_metadata"]),
        "child_session_id": bundle["prior_child_session_id"],
    }
    authority_tools = None
    if loaded.get("protected"):
        authority_tools = dict(loaded["authority"]["tools"])
        metadata["enabled_toolsets"] = list(authority_tools["enabled_toolsets"])
        metadata["disabled_toolsets"] = list(authority_tools["disabled_toolsets"])
    try:
        from gateway.status import get_process_start_time

        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    reserved = _repository().reserve_resumed_attempt(
        logical_id,
        physical_worker_id=None if loaded.get("protected") else logical_id,
        owner_pid=os.getpid(),
        owner_started_at=owner_started_at,
        metadata=metadata,
    )
    if reserved.get("status") != "reserved":
        return reserved
    _notify_state_change()

    run_id = str(reserved["run_id"])
    attempt_id = str(reserved["attempt_id"])
    dispatched_at = time.time()
    protected_attempt_registry = None
    protected_attempt_authority = None
    event_record = {
        "delegation_id": delegation_id,
        "run_id": run_id,
        "session_key": snapshot.get("session_key", ""),
        "origin_ui_session_id": snapshot.get("origin_ui_session_id", ""),
        "parent_session_id": snapshot.get("parent_session_id"),
        "goal": child_snapshot.get("goal") or snapshot.get("goal", ""),
        "context": snapshot.get("context"),
        "toolsets": metadata.get("enabled_toolsets"),
        "role": metadata.get("role"),
        "model": metadata.get("model"),
        "dispatched_at": dispatched_at,
        "subagent_id": logical_id,
        "attempt_id": attempt_id,
        "attempt_number": reserved["attempt_number"],
    }

    def finish(result: Dict[str, Any], status: str) -> None:
        completed = {**event_record, "completed_at": time.time()}
        result.setdefault("subagent_id", logical_id)
        result.setdefault("attempt_id", attempt_id)
        result.setdefault("run_id", run_id)
        _push_completion_event(completed, result, status)

    def fail_before_execution(exc: Exception) -> Dict[str, Any]:
        cleanup_errors: tuple[Exception, ...] = ()
        if protected_attempt_registry is not None:
            try:
                cleanup_errors = tuple(
                    protected_attempt_registry.cleanup(attempt_id)
                )
            except Exception as cleanup_exc:
                cleanup_errors = (cleanup_exc,)
        error = f"{type(exc).__name__}: {exc}"
        exit_reason = "dispatch_failed"
        if cleanup_errors:
            cleanup_detail = "; ".join(
                f"{type(item).__name__}: {item}" for item in cleanup_errors
            )
            error = f"{error}; cleanup failed: {cleanup_detail}"
            exit_reason = "cleanup_error"
        result = {
            "status": "error",
            "summary": None,
            "error": error,
            "exit_reason": exit_reason,
            # Failure occurred before the continuation session ran; keep the
            # last persisted child segment as the retry anchor.
            "child_session_id": bundle["prior_child_session_id"],
            "api_calls": 0,
            "duration_seconds": round(time.time() - dispatched_at, 2),
        }
        finish(result, "error")
        return {
            "status": "dispatch_failed",
            "delegation_id": delegation_id,
            "subagent_id": logical_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "attempt_number": reserved["attempt_number"],
            "error": error,
            "exit_reason": exit_reason,
        }

    if loaded.get("protected"):
        from tools import delegation_scope as _delegation_scope
        from tools import terminal_tool as _terminal_tool

        if restored_scope is None:
            return fail_before_execution(
                RuntimeError("protected authority scope is unavailable")
            )
        protected_attempt_registry = _delegation_scope.attempt_scope_registry
        try:
            protected_attempt_authority = protected_attempt_registry.reserve(
                restored_scope,
                logical_id,
                attempt_id=attempt_id,
                backing_registry=getattr(
                    parent_agent, "delegation_backing_registry", None
                ),
            )
            protected_attempt_registry.prepare_idmapped_reveals(attempt_id)

            def _cleanup_resumed_environment() -> None:
                try:
                    _terminal_tool.cleanup_vm(attempt_id, force_remove=True)
                finally:
                    _terminal_tool.clear_task_env_overrides(attempt_id)

            protected_attempt_registry.add_resource(
                attempt_id, "task-environment", _cleanup_resumed_environment
            )
            _terminal_tool.register_task_env_overrides(
                attempt_id,
                {
                    "env_type": restored_scope.profile.backend,
                    "docker_image": restored_scope.profile.image,
                    "cwd": str(restored_scope.workdir),
                    "delegation_scope_id": protected_attempt_authority.scope_id,
                },
            )
        except Exception as exc:
            return fail_before_execution(exc)

    child = None
    lifecycle_closed = threading.Event()

    def close_lifecycle(result: Dict[str, Any]) -> None:
        """Run the spawned-child host lifecycle (summary budget, memory, subagent_stop, cost rollup) exactly once on
        every path after the child was built — building fired subagent_start. A failure here is logged and never
        replaces the run's own result."""
        if lifecycle_closed.is_set():
            return
        lifecycle_closed.set()
        result.setdefault("task_index", 0)
        task = {"goal": str(event_record["goal"] or "resumed subagent")}
        try:
            from tools.delegate_tool_results import _finalize_child_results
            _finalize_child_results([result], [task], [(0, task, child)], parent_agent)
        except Exception:
            logger.warning("Resumed delegation %s/%s: host lifecycle finalization failed",
                           delegation_id, logical_id, exc_info=True)

    def fail_after_build(exc: Exception) -> Dict[str, Any]:
        close_lifecycle({"status": "error", "summary": None, "error": f"{type(exc).__name__}: {exc}"})
        try:
            child.close()
        except Exception:
            pass
        return fail_before_execution(exc)

    try:
        if loaded.get("protected"):
            # Re-resolve trusted backing identity/revision immediately before
            # constructing a child that can consume the restored scope.
            from tools.delegation_scope import deserialize_delegation_authority

            restored_scope = deserialize_delegation_authority(
                loaded["authority"],
                backing_registry=getattr(
                    parent_agent, "delegation_backing_registry", None
                ),
                expected_policy=expected_policy,
            )
        child = _delegate.build_resumed_child_agent(
            bundle=bundle,
            logical_id=logical_id,
            goal=str(event_record["goal"] or "resumed subagent"),
            parent_agent=parent_agent,
            continuation=continuation,
            resolved_scope=restored_scope,
            authority_tools=authority_tools,
        )
        child._delegation_run_id = run_id
        child._delegation_attempt_id = attempt_id
        if protected_attempt_authority is not None:
            child._delegation_scope_id = protected_attempt_authority.scope_id
            child._current_task_id = attempt_id
            child.resolved_attempt_authority = protected_attempt_authority
        child._delegation_session_ref.update(
            {"run_id": run_id, "attempt_id": attempt_id}
        )
        child_metadata = dict(child._delegation_runtime_metadata)
        # Keep the prior segment only in durable in-flight metadata.  The
        # reconstructed child must retain its newly allocated session ID so a
        # successful completion can advance the replay anchor.
        metadata = {
            **child_metadata,
            "child_session_id": bundle["prior_child_session_id"],
        }
    except Exception as exc:
        return fail_after_build(exc) if child is not None else fail_before_execution(exc)

    try:
        executor = _get_executor(max_async_children)
    except Exception as exc:
        return fail_after_build(exc)

    def worker() -> None:
        result: Dict[str, Any]
        status = "error"
        try:
            if loaded.get("protected"):
                from tools.delegation_scope import deserialize_delegation_authority

                deserialize_delegation_authority(
                    loaded["authority"],
                    backing_registry=getattr(
                        parent_agent, "delegation_backing_registry", None
                    ),
                    expected_policy=expected_policy,
                )
            _repository().transition_attempt(
                attempt_id, {"starting"}, "running", metadata=metadata
            )
            result = _delegate._run_single_child(
                0,
                str(event_record["goal"] or "resumed subagent"),
                child=child,
                parent_agent=parent_agent,
                conversation_history=bundle["history"],
                resume_message=message,
                resume_workdir=metadata.get("workdir"),
            )
            status = str(result.get("status") or "error")
            if status == "completed":
                # The standard conversation runner has now persisted the new
                # child segment.  Completing the exact attempt with this field
                # atomically advances the anchor used by the next resume.
                result["child_session_id"] = continuation["session_id"]
        except Exception as exc:
            logger.exception("Resumed delegation %s/%s crashed", delegation_id, logical_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        close_lifecycle(result)
        finish(result, status)

    try:
        if protected_attempt_registry is not None:
            protected_attempt_registry.activate(
                attempt_id, delegation_id=delegation_id, run_id=run_id
            )
        executor.submit(propagate_context_to_thread(worker))
    except Exception as exc:
        return fail_after_build(exc)

    return {
        "status": "dispatched",
        "delegation_id": delegation_id,
        "subagent_id": logical_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "attempt_number": reserved["attempt_number"],
        "child_session_id": continuation["session_id"],
    }

def mark_completion_delivered(delegation_id: str) -> bool:
    """Acknowledge an unclaimed pending completion."""
    return _changed(_repository().acknowledge_pending(delegation_id), "delivered")

def claim_completion_delivery(
    delegation_id: str, claim_id: str, *, run_id: Optional[str] = None
) -> bool:
    """Claim one terminal pending or expired wait-held completion."""
    inspected = _repository().inspect_delivery(delegation_id, run_id)
    if inspected.get("status") == "not_found":
        return True
    if inspected.get("status") != "found":
        return False
    return _changed(
        _repository().claim_run_delivery(
            delegation_id,
            run_id,
            claim_id,
            wait_stale_seconds=_WAIT_HOLD_STALE_SECONDS,
        ),
        "claimed",
    )

def recover_stale_wait_holds(delegation_id: Optional[str] = None) -> int:
    """Release wait holds whose owning process can no longer be trusted alive."""
    changed = _repository().recover_stale_wait_holds(
        cutoff=time.time() - _WAIT_HOLD_STALE_SECONDS,
        delegation_id=delegation_id,
    )
    if changed:
        _notify_state_change()
    return changed

def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events (and interim notices) need no token."""
    if is_interim_delegation_event(evt):
        return ""
    if (
        evt.get("type") != "async_delegation"
        or evt.get("event_kind") == "fallback"
    ):
        return ""
    if evt.get("_async_delivery_claim_token"):
        return str(evt["_async_delivery_claim_token"])
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    run_id = evt.get("run_id")
    return claim_id if claim_completion_delivery(
        delegation_id,
        claim_id,
        run_id=str(run_id) if run_id else None,
    ) else None

def claim_async_delivery(
    delegation_id: str,
    *,
    managed: bool = False,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically claim a queued terminal result for any automatic consumer.

    Unknown events are legacy pass-through unless the producer explicitly
    marked them managed. Durable dispositions are authoritative across threads
    and processes; a wait hold, consumption, suppression, or prior delivery
    can never be bypassed by an in-memory queue copy.
    """
    recover_stale_wait_holds(delegation_id)
    inspected = _repository().inspect_delivery(delegation_id, run_id)
    status = inspected.get("status")
    if status == "not_found":
        return {"status": "stale" if managed else "legacy"}
    if status == "ambiguous_run":
        return {"status": "stale", "reason": "ambiguous_run"}
    if inspected.get("completed_at") is None or inspected.get("event_json") is None:
        return {"status": "not_ready"}
    disposition = inspected.get("delivery_state")
    if disposition == "held_by_wait":
        return {"status": "held"}
    if disposition in _TERMINAL_DELIVERY_STATES:
        return {"status": "stale"}
    token = f"auto:{os.getpid()}:{uuid.uuid4().hex}"
    outcome = _repository().claim_run_delivery(delegation_id, run_id, token)
    if outcome.get("status") == "claimed":
        _notify_state_change()
        return {"status": "claimed", "token": token}
    return {"status": "held" if outcome.get("status") == "held" else "stale"}

def inspect_async_delivery_claim(
    delegation_id: str, token: str, *, run_id: Optional[str] = None
) -> str:
    """Inspect a token retained on a requeued delivery event."""
    inspected = _repository().inspect_delivery(delegation_id, run_id)
    if inspected.get("status") != "found":
        return "not_found" if inspected.get("status") == "not_found" else "stale"
    if inspected.get("delivery_state") == "delivering" and inspected.get("delivery_claim") == token:
        return "current"
    return str(inspected.get("delivery_state") or "pending")

def _delivery_claim_action(
    delegation_id: str,
    claim_id: str,
    *,
    delivered: bool,
    run_id: Optional[str] = None,
) -> bool:
    inspected = _repository().inspect_delivery(delegation_id, run_id)
    if inspected.get("status") != "found":
        return False
    run_id = str(inspected["run_id"])
    outcome = (
        _repository().commit_run_delivery(run_id, claim_id)
        if delivered
        else _repository().release_run_delivery(run_id, claim_id)
    )
    return _changed(outcome, "delivered" if delivered else "released")

def release_completion_delivery(
    delegation_id: str, claim_id: str, *, run_id: Optional[str] = None
) -> bool:
    """Release a failed automatic-delivery claim for retry."""
    return _delivery_claim_action(
        delegation_id, claim_id, delivered=False, run_id=run_id
    )

def complete_completion_delivery(
    delegation_id: str, claim_id: str, *, run_id: Optional[str] = None
) -> bool:
    """Acknowledge acceptance for the automatic consumer holding this claim."""
    return _delivery_claim_action(
        delegation_id, claim_id, delivered=True, run_id=run_id
    )

def finish_async_delivery(
    delegation_id: str,
    token: str,
    *,
    delivered: bool,
    run_id: Optional[str] = None,
) -> bool:
    """Commit or release exactly the automatic claim identified by ``token``."""
    return _delivery_claim_action(
        delegation_id, token, delivered=delivered, run_id=run_id
    )

def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    if not claim_id or evt.get("type") != "async_delegation":
        return True
    if evt.get("_async_delivery_claim_token"):
        from tools.process_registry import commit_notification_delivery, process_registry

        return commit_notification_delivery(evt, process_registry.completion_queue)
    run_id = evt.get("run_id")
    return _delivery_claim_action(
        str(evt.get("delegation_id") or ""),
        claim_id,
        delivered=True,
        run_id=str(run_id) if run_id else None,
    )

def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    """Release a failed claim for a consumer that discards its copy (the TUI poller): the run is
    pending again, so it must stay eligible for the orphan sweep."""
    if not claim_id or evt.get("type") != "async_delegation":
        return
    if evt.get("_async_delivery_claim_token"):
        from tools.process_registry import process_registry, requeue_notification_delivery

        requeue_notification_delivery(evt, process_registry.completion_queue)
        return
    run_id = evt.get("run_id")
    _delivery_claim_action(
        str(evt.get("delegation_id") or ""),
        claim_id,
        delivered=False,
        run_id=str(run_id) if run_id else None,
    )
    return_completion_offer(evt)

def hold_completion_for_wait(
    delegation_id: str, claim_id: str, *, session_key: str,
    run_id: Optional[str] = None,
) -> bool:
    """Atomically reserve pending delivery for one authorised waiter."""
    return _changed(
        _repository().hold_for_wait(
            delegation_id, session_key, claim_id, run_id=run_id
        ),
        "held",
    )

def consume_waited_completion(
    delegation_id: str, claim_id: str, *, session_key: str,
    run_id: Optional[str] = None,
) -> bool:
    """Consume a terminal completion only for the waiter owning its hold."""
    return _changed(
        _repository().consume_wait_hold(
            delegation_id, session_key, claim_id, run_id=run_id
        ),
        "consumed",
    )

def release_wait_hold(
    delegation_id: str, claim_id: str, *, session_key: str,
    run_id: Optional[str] = None,
) -> bool:
    """Release only this waiter's hold; timeout never consumes a result."""
    return _changed(
        _repository().release_wait_hold(
            delegation_id, session_key, claim_id, run_id=run_id
        ),
        "released",
    )

def wait_for_delegation(
    delegation_id: str, *, session_key: str, timeout_seconds: float = 30.0,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Wait on the exact run selected at call start and consume it at most once."""
    timeout_seconds = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    claim_id = f"wait:{os.getpid()}:{uuid.uuid4().hex}"

    recover_stale_wait_holds(delegation_id)
    binding = _repository().hold_for_wait(
        delegation_id, session_key, claim_id, run_id=run_id
    )
    if binding.get("status") == "not_found":
        return {"status": "not_found", "delegation_id": delegation_id}
    bound_run_id = binding.get("run_id")
    if not isinstance(bound_run_id, str) or not bound_run_id:
        return {"status": "not_found", "delegation_id": delegation_id}
    owns_hold = binding.get("status") == "held"

    def _wait_bound() -> Dict[str, Any]:
        while True:
            snapshot = get_async_delegation(
                delegation_id, session_key=session_key, run_id=bound_run_id
            )
            if snapshot is None:
                if owns_hold:
                    release_wait_hold(
                        delegation_id, claim_id, session_key=session_key,
                        run_id=bound_run_id,
                    )
                return {"status": "not_found", "delegation_id": delegation_id}
            if _terminal(snapshot):
                claimed = owns_hold and consume_waited_completion(
                    delegation_id, claim_id, session_key=session_key,
                    run_id=bound_run_id,
                )
                current = get_async_delegation(
                    delegation_id, session_key=session_key, run_id=bound_run_id
                ) or snapshot
                current["claimed_delivery"] = bool(claimed)
                return current

            from tools.foreground_wait import current_foreground_wait

            wait_slot = current_foreground_wait()
            if (
                wait_slot is not None
                and wait_slot.kind == "delegation"
                and wait_slot.background_requested.is_set()
            ):
                if owns_hold:
                    release_wait_hold(
                        delegation_id,
                        claim_id,
                        session_key=session_key,
                        run_id=bound_run_id,
                    )
                current = get_async_delegation(
                    delegation_id, session_key=session_key, run_id=bound_run_id
                ) or snapshot
                handoff = {
                    "kind": "delegation",
                    "delegation_id": delegation_id,
                    "run_id": bound_run_id,
                    "continue": (
                        'delegate_task(action="wait", delegation_id='
                        f'"{delegation_id}", run_id="{bound_run_id}")'
                    ),
                    "inspect": (
                        'delegate_task(action="status", delegation_id='
                        f'"{delegation_id}")'
                    ),
                    "stop": (
                        'delegate_task(action="interrupt", delegation_id='
                        f'"{delegation_id}", cascade=true)'
                    ),
                }
                current["status"] = "backgrounded"
                current["claimed_delivery"] = False
                current["foreground_handoff"] = handoff
                wait_slot.complete_background(handoff)
                return current

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                latest = get_async_delegation(
                    delegation_id, session_key=session_key, run_id=bound_run_id
                ) or snapshot
                if _terminal(latest):
                    claimed = owns_hold and consume_waited_completion(
                        delegation_id, claim_id, session_key=session_key,
                        run_id=bound_run_id,
                    )
                    current = get_async_delegation(
                        delegation_id, session_key=session_key, run_id=bound_run_id
                    ) or latest
                    current["claimed_delivery"] = bool(claimed)
                    return current
                if owns_hold:
                    release_wait_hold(
                        delegation_id, claim_id, session_key=session_key,
                        run_id=bound_run_id,
                    )
                current = get_async_delegation(
                    delegation_id, session_key=session_key, run_id=bound_run_id
                ) or latest
                current["status"] = "timeout"
                current["claimed_delivery"] = False
                return current
            with _STATE_CONDITION:
                _STATE_CONDITION.wait(timeout=min(remaining, _WAIT_POLL_SECONDS))

    try:
        return _annotate_wait_snapshot(_wait_bound())
    except BaseException:
        # A transient DB/read failure after acquisition must never strand a
        # held_by_wait run. Preserve the original exception if cleanup also
        # encounters the same transient lock.
        if owns_hold:
            for retry_index in range(3):
                try:
                    release_wait_hold(
                        delegation_id, claim_id, session_key=session_key,
                        run_id=bound_run_id,
                    )
                    break
                except Exception:
                    if retry_index < 2:
                        time.sleep(0.05)
        raise

def suppress_completion_delivery(
    delegation_id: str, *, session_key: str, reason: str = ""
) -> str:
    """Atomically suppress pending/held delivery with an explicit race outcome."""
    status = _repository().suppress_delivery(
        delegation_id, session_key, reason
    ).get("status")
    if status == "suppressed":
        _notify_state_change()
        return "applied"
    if status in {"not_found", "already_suppressed", "too_late"}:
        return str(status)
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
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("session_key", "") != session_key:
            record = None
        interrupt_lock = (
            record.setdefault("_interrupt_lock", threading.Lock())
            if record is not None
            else threading.Lock()
        )

    # Durable request ownership and the live callback form one per-delegation
    # transaction. Never wait for this lock while holding _records_lock.
    with interrupt_lock:
        snapshot = get_async_delegation(delegation_id, session_key=session_key)
        if snapshot is None:
            return {"status": "not_found", "delegation_id": delegation_id}
        if _terminal(snapshot):
            return {
                "status": "already_terminal",
                "delegation_id": delegation_id,
                "worker_status": snapshot["state"],
            }
        fn = None
        if record is not None:
            with _records_lock:
                current = _records.get(delegation_id)
                if current is record and current.get("session_key", "") == session_key:
                    fn = current.get("interrupt_fn")

        requested_attempt_ids = []
        for child in snapshot["children"].values():
            if child["status"] not in _ACTIVE_STATES:
                continue
            attempt_id = child.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                continue
            outcome = _repository().request_interrupt(attempt_id, reason)
            if outcome.get("status") == "interrupt_requested":
                requested_attempt_ids.append(attempt_id)

        # A caller that owns no transition is idempotent. It observes the
        # prior owner's completed transaction and never invokes or rolls back.
        if not requested_attempt_ids:
            current_snapshot = get_async_delegation(
                delegation_id, session_key=session_key
            )
            if current_snapshot is None:
                return {"status": "not_found", "delegation_id": delegation_id}
            if _terminal(current_snapshot):
                return {
                    "status": "already_terminal",
                    "delegation_id": delegation_id,
                    "worker_status": current_snapshot["state"],
                }
            status = (
                "interrupt_unavailable"
                if record is None
                else (
                    "interrupt_requested"
                    if current_snapshot["state"] == "interrupt_requested"
                    else "interrupt_unavailable"
                )
            )
            return {"status": status, "delegation_id": delegation_id}

        if not callable(fn):
            # Resumed runs are not represented by the legacy per-delegation
            # closure. Their durable exact-attempt requests are authoritative;
            # best-effort the live child registry when construction has finished.
            from tools import delegate_tool as _delegate_tool

            for child in snapshot["children"].values():
                child_id = child.get("subagent_id")
                if isinstance(child_id, str) and child_id:
                    _delegate_tool.interrupt_subagent_status(child_id, reason=reason)
            _notify_state_change()
            return {
                "status": "interrupt_requested",
                "delegation_id": delegation_id,
                "attempt_ids": requested_attempt_ids,
            }

        with _records_lock:
            current = _records.get(delegation_id)
            if record is not None and current is not None and current is record:
                current["status"] = "interrupt_requested"
        _notify_state_change()
        try:
            fn()
        except Exception as exc:
            for attempt_id in requested_attempt_ids:
                _repository().rollback_interrupt_request(attempt_id)
            current_snapshot = get_async_delegation(
                delegation_id, session_key=session_key
            )
            with _records_lock:
                current = _records.get(delegation_id)
                if (
                    record is not None
                    and current is not None
                    and current is record
                    and current.get("status") == "interrupt_requested"
                ):
                    if current_snapshot and current_snapshot["state"] in _ACTIVE_STATES:
                        current["status"] = current_snapshot["state"]
            _notify_state_change()
            return {
                "status": "interrupt_failed",
                "delegation_id": delegation_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "status": "interrupt_requested",
            "delegation_id": delegation_id,
            "attempt_ids": requested_attempt_ids,
            "run_id": snapshot.get("active_run_id") or snapshot.get("run_id"),
        }

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
    # Physical interruption happens first so durable revocation cannot prevent
    # the best-effort worker stop. The logical tombstone is retained in
    # spec_json and closes every later resume/steer/interrupt path.
    _repository().tombstone_delegation_authorities(
        delegation_id, revoked=True, cleaned=True
    )
    worker = str(interrupted.get("status") or "interrupt_unavailable")
    return {
        "status": "delivery_too_late" if suppression == "too_late" else "abandoned",
        "delegation_id": delegation_id,
        "suppression": suppression,
        "worker": worker,
    }

def register_subagent_lifecycle(record: Dict[str, Any]) -> Optional[str]:
    """Associate a live child with its durable delegation and refresh metadata.

    Root IDs are written before executor submission. Descendants are associated
    through their already-associated parent, so no process-local authority is
    needed for model-facing authorization.
    """
    outcome = _repository().register_subagent(record)
    if not outcome:
        return None
    record["delegation_attempt_id"] = outcome["attempt_id"]
    _notify_state_change()
    return outcome["delegation_id"]

def delegation_contains_subagent(
    delegation_id: str, subagent_id: str, *, session_key: str
) -> bool:
    """Return membership only when both delegation and session are authorized."""
    return _repository().find_attempt(
        subagent_id, delegation_id=delegation_id, session_key=session_key
    ) is not None

def enqueue_subagent_steer(
    delegation_id: str,
    subagent_id: str,
    *,
    session_key: str,
    message: str,
    force: bool = False,
) -> Dict[str, Any]:
    outcome = _repository().enqueue_steer(
        delegation_id, subagent_id, session_key, message, force=force
    )
    if outcome.get("status") == "accepted":
        _notify_state_change()
    return outcome

def inspect_subagent_steer(mailbox_id: str) -> Dict[str, Any]:
    return _repository().inspect_steer(mailbox_id)

def request_pending_subagent_interrupt(
    delegation_id: str,
    subagent_id: str,
    *,
    session_key: str,
    reason: str = "",
) -> str:
    """Durably queue an interrupt for an authorized child still starting."""
    attempt = _repository().find_attempt(
        subagent_id, delegation_id=delegation_id, session_key=session_key
    )
    if attempt is None:
        return "not_found"
    status = str(
        _repository().request_interrupt(attempt["attempt_id"], reason).get("status")
    )
    if status == "already_requested":
        status = "interrupt_requested"
    if status == "interrupt_requested":
        _notify_state_change()
    return status

def take_pending_subagent_interrupt(subagent_id: str) -> tuple[bool, str]:
    """Consume a queued startup interrupt immediately after live registration."""
    attempt = _repository().find_attempt(subagent_id)
    if attempt is None:
        return False, ""
    outcome = _repository().take_interrupt(attempt["attempt_id"])
    if outcome.get("status") != "taken":
        return False, ""
    _notify_state_change()
    return True, str(outcome.get("reason") or "")

def pending_subagent_interrupt_ids(
    delegation_id: str, *, session_key: str
) -> set[str]:
    snapshot = get_async_delegation(delegation_id, session_key=session_key)
    return set(snapshot.get("interrupt_requests", {})) if snapshot else set()

def archive_subagent_tail(subagent_id: str, tail: Dict[str, Any]) -> None:
    """Persist a bounded, already-redacted child tail before live removal."""
    supplied_attempt = tail.get("delegation_attempt_id")
    if "delegation_attempt_id" in tail and (
        not isinstance(supplied_attempt, str) or not supplied_attempt
    ):
        return
    supplied_run = tail.get("delegation_run_id")
    if supplied_run is not None and (
        not isinstance(supplied_run, str) or not supplied_run
    ):
        return
    attempt = _repository().find_attempt(
        subagent_id,
        attempt_id=supplied_attempt,
        run_id=supplied_run,
    )
    if attempt is None or attempt["state"] not in _ACTIVE_STATES:
        return
    state = _attempt_state(tail.get("status"))
    outcome = _repository().transition_attempt(
        attempt["attempt_id"],
        {attempt["state"]},
        state,
        metadata=tail,
        completed_at=None if state in _ACTIVE_STATES else time.time(),
    )
    if outcome.get("status") == "updated":
        _notify_state_change()

_MAX_DURABLE_LIST = 100
_WAIT_POLL_SECONDS = 0.05
_STATE_CONDITION = threading.Condition()


def _dispatch_identity_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return the stable identities allocated before a worker is submitted.

    ``subagent_id`` is the logical child identity and ``run_id`` identifies
    this execution of the delegation.  Neither value is a provider/session or
    container identity: those are deliberately allocated by the worker only
    after the caller has received the dispatch receipt.  Keeping this payload
    here also makes the single, batch, and cron dispatch paths agree on the
    public receipt shape.
    """
    roots = [
        str(value)
        for value in (record.get("root_subagent_ids") or [])
        if isinstance(value, str) and value
    ]
    run_id = record.get("run_id")
    subagents = [
        {
            "subagent_id": logical_id,
            "run_id": run_id,
            "status": "starting",
        }
        for logical_id in roots
    ]
    return {
        "run_id": run_id,
        "subagent_ids": roots,
        "subagents": subagents,
    }


def _annotate_wait_snapshot(
    snapshot: Dict[str, Any], *, claimed_delivery: Optional[bool] = None
) -> Dict[str, Any]:
    """Expose one stable wait result/readiness/delivery projection."""
    ready = snapshot.get("completed_at") is not None and snapshot.get("result") is not None
    snapshot["result_ready"] = bool(ready)
    snapshot["result_available"] = bool(ready)
    if claimed_delivery is not None:
        snapshot["claimed_delivery"] = bool(claimed_delivery)
    else:
        snapshot["claimed_delivery"] = bool(snapshot.get("claimed_delivery", False))
    snapshot["delivery_consumed"] = snapshot.get("delivery_state") == "consumed"
    return snapshot

_RESUME_METADATA_FIELDS = frozenset(
    {
        "child_session_id",
        "parent_session_id",
        "parent_logical_id",
        "depth",
        "role",
        "model",
        "provider",
        "api_mode",
        "reasoning_config",
        "enabled_toolsets",
        "disabled_toolsets",
        "workdir",
        "max_iterations",
        "max_tokens",
        "fallback_routes",
        "provider_preferences",
    }
)


def _finalize_batch(delegation_id, combined, status):
    return _finalize(delegation_id, combined, status)
