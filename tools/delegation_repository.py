"""Normalized durable authority for asynchronous delegation lifecycle state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from hermes_state import apply_wal_with_fallback, ensure_state_schema

_ACTIVE_STATES = {"starting", "running", "finalizing", "interrupt_requested"}
_TERMINAL_DELIVERY_STATES = {"delivered", "consumed", "suppressed"}
_DELIVERY_STATES = _TERMINAL_DELIVERY_STATES | {"pending", "held_by_wait", "delivering"}
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def _json(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _attempt_state(value: Any) -> str:
    state = str(value or "").lower()
    if state in _ACTIVE_STATES:
        return state
    if state in {"completed", "success"}:
        return "completed"
    if state in {"error", "failed", "budget_exhausted"}:
        return "error"
    if state in {"interrupted", "cancelled"}:
        return "interrupted"
    if state == "timeout":
        return "timeout"
    return "unknown"


def _new_id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex}"


class DelegationRepository:
    """Own connections, transactions, migration, CAS, leases, and projection."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(20):
            conn = sqlite3.connect(
                str(self.db_path), timeout=10, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                apply_wal_with_fallback(conn, db_label=str(self.db_path))
                conn.execute("PRAGMA foreign_keys=ON")
                key = str(self.db_path.resolve())
                if key not in _SCHEMA_READY:
                    with _SCHEMA_LOCK:
                        if key not in _SCHEMA_READY:
                            ensure_state_schema(conn)
                            _SCHEMA_READY.add(key)
                return conn
            except sqlite3.OperationalError as exc:
                conn.close()
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                time.sleep(0.02 * (attempt + 1))
        raise AssertionError("unreachable")

    @contextmanager
    def write_txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _materialize(self, conn: sqlite3.Connection, delegation_id: str) -> str:
        row = conn.execute(
            "SELECT * FROM async_delegations WHERE delegation_id=?", (delegation_id,)
        ).fetchone()
        if row is None:
            return "not_found"
        if int(row["lifecycle_version"] or 1) == 2:
            return "already_materialized"

        run_id = _new_id("run")
        delivery = str(row["delivery_state"] or "pending")
        if delivery not in _DELIVERY_STATES:
            delivery = "pending"
        if delivery == "pending" and row["delivery_claim"] is not None:
            delivery = "delivering"
        conn.execute(
            """INSERT INTO delegation_runs
               (run_id, delegation_id, run_number, kind, created_at, completed_at,
                event_json, result_json, delivery_state, delivery_claim,
                delivery_claimed_at, delivery_attempts, delivered_at)
               VALUES (?,?,1,'initial',?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                delegation_id,
                float(row["dispatched_at"]),
                row["completed_at"],
                row["event_json"],
                row["result_json"],
                delivery,
                row["delivery_claim"],
                row["delivery_claimed_at"],
                int(row["delivery_attempts"] or 0),
                row["delivered_at"],
            ),
        )

        roots = [v for v in _json(row["root_subagent_ids_json"], []) if isinstance(v, str)]
        children = _json(row["children_json"], {})
        ordered = list(dict.fromkeys(roots + list(children)))
        pending = set(ordered)
        inserted: set[str] = set()
        while pending:
            progressed = False
            for logical_id in ordered:
                if logical_id not in pending:
                    continue
                child = children.get(logical_id) if isinstance(children.get(logical_id), dict) else {}
                parent = child.get("parent_id")
                if parent and parent in pending:
                    continue
                root_ordinal = roots.index(logical_id) if logical_id in roots else None
                conn.execute(
                    """INSERT INTO delegation_logical_subagents
                       (logical_id, delegation_id, parent_logical_id, root_ordinal,
                        spec_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
                    (
                        logical_id,
                        delegation_id,
                        parent if parent in inserted else None,
                        root_ordinal,
                        _dump(child),
                        float(row["dispatched_at"]),
                        float(row["updated_at"]),
                    ),
                )
                state = _attempt_state(child.get("status"))
                owner_pid = row["owner_pid"] if state in _ACTIVE_STATES else None
                owner_started = row["owner_started_at"] if state in _ACTIVE_STATES else None
                completed_at = None if state in _ACTIVE_STATES else row["completed_at"] or row["updated_at"]
                conn.execute(
                    """INSERT INTO delegation_attempts
                       (attempt_id, logical_id, run_id, attempt_number,
                        physical_worker_id, state, owner_pid, owner_started_at,
                        created_at, started_at, completed_at, updated_at,
                        metadata_json, interrupt_reason, interrupt_requested_at)
                       VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _new_id("attempt"),
                        logical_id,
                        run_id,
                        logical_id,
                        state,
                        owner_pid,
                        owner_started,
                        float(row["dispatched_at"]),
                        child.get("started_at"),
                        completed_at,
                        float(row["updated_at"]),
                        _dump(child),
                        child.get("interrupt_reason"),
                        row["updated_at"] if state == "interrupt_requested" else None,
                    ),
                )
                inserted.add(logical_id)
                pending.remove(logical_id)
                progressed = True
            if not progressed:  # malformed parent cycle: preserve members, break the cycle
                logical_id = next(iter(pending))
                child = children.get(logical_id) if isinstance(children.get(logical_id), dict) else {}
                ordered.remove(logical_id)
                ordered.append(logical_id)
                child = dict(child)
                child["parent_id"] = None
                children[logical_id] = child

        conn.execute(
            "UPDATE async_delegations SET lifecycle_version=2 WHERE delegation_id=? AND lifecycle_version=1",
            (delegation_id,),
        )
        return "materialized"

    def _ensure_materialized(self, delegation_id: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT lifecycle_version FROM async_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return "not_found"
        if int(row[0] or 1) == 2:
            return "already_materialized"
        with self.write_txn() as conn:
            return self._materialize(conn, delegation_id)

    def register_initial_dispatch(
        self,
        record: Dict[str, Any],
        *,
        owner_pid: Optional[int] = None,
        owner_started_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        run_id = _new_id("run")
        roots = [v for v in record.get("root_subagent_ids", []) if isinstance(v, str) and v]
        attempt_ids = [_new_id("attempt") for _ in roots]
        now = time.time()
        dispatched = float(record.get("dispatched_at") or now)
        task = {
            key: record.get(key)
            for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch")
            if key in record
        }
        with self.write_txn() as conn:
            try:
                conn.execute(
                    """INSERT INTO async_delegations
                       (delegation_id, origin_session, origin_ui_session_id,
                        parent_session_id, state, dispatched_at, updated_at,
                        delivery_state, delivery_attempts, owner_pid,
                        owner_started_at, task_json, lifecycle_version)
                       VALUES (?,?,?,?,'running',?,?,'pending',0,?,?,?,2)""",
                    (
                        record["delegation_id"],
                        record.get("session_key", ""),
                        record.get("origin_ui_session_id", ""),
                        record.get("parent_session_id"),
                        dispatched,
                        now,
                        owner_pid,
                        owner_started_at,
                        _dump(task),
                    ),
                )
            except sqlite3.IntegrityError:
                return {"status": "already_exists"}
            conn.execute(
                """INSERT INTO delegation_runs
                   (run_id, delegation_id, run_number, kind, created_at)
                   VALUES (?,?,1,'initial',?)""",
                (run_id, record["delegation_id"], dispatched),
            )
            attempts = []
            raw_goals = task.get("goals")
            goals: List[Any] = raw_goals if isinstance(raw_goals, list) else []
            for ordinal, (logical_id, attempt_id) in enumerate(zip(roots, attempt_ids)):
                spec = dict(task)
                if ordinal < len(goals):
                    spec["goal"] = goals[ordinal]
                conn.execute(
                    """INSERT INTO delegation_logical_subagents
                       (logical_id, delegation_id, root_ordinal, spec_json,
                        created_at, updated_at) VALUES (?,?,?,?,?,?)""",
                    (logical_id, record["delegation_id"], ordinal, _dump(spec), dispatched, now),
                )
                conn.execute(
                    """INSERT INTO delegation_attempts
                       (attempt_id, logical_id, run_id, attempt_number,
                        physical_worker_id, state, owner_pid, owner_started_at,
                        created_at, updated_at, metadata_json)
                       VALUES (?,?,?,1,?,'starting',?,?,?,?,?)""",
                    (
                        attempt_id,
                        logical_id,
                        run_id,
                        logical_id,
                        owner_pid,
                        owner_started_at,
                        dispatched,
                        now,
                        _dump({"subagent_id": logical_id, "status": "starting", **spec}),
                    ),
                )
                attempts.append(
                    {"attempt_id": attempt_id, "logical_id": logical_id, "attempt_number": 1}
                )
        return {"status": "registered", "run_id": run_id, "attempts": attempts}

    def reserve_resumed_attempt(
        self,
        logical_id: str,
        *,
        physical_worker_id: Optional[str] = None,
        owner_pid: Optional[int] = None,
        owner_started_at: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        attempt_id, run_id, now = _new_id("attempt"), _new_id("run"), time.time()
        with self.write_txn() as conn:
            logical = conn.execute(
                "SELECT delegation_id FROM delegation_logical_subagents WHERE logical_id=?",
                (logical_id,),
            ).fetchone()
            if logical is None:
                return {"status": "not_found"}
            active = conn.execute(
                "SELECT attempt_id FROM delegation_attempts WHERE logical_id=? "
                "AND state IN ('starting','running','finalizing','interrupt_requested')",
                (logical_id,),
            ).fetchone()
            if active is not None:
                return {"status": "already_running", "attempt_id": active[0]}
            delegation_id = logical[0]
            run_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(run_number),0)+1 FROM delegation_runs WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone()[0]
            )
            attempt_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt_number),0)+1 FROM delegation_attempts WHERE logical_id=?",
                    (logical_id,),
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO delegation_runs (run_id,delegation_id,run_number,kind,created_at) "
                "VALUES (?,?,?,'resumed',?)",
                (run_id, delegation_id, run_number, now),
            )
            conn.execute(
                """INSERT INTO delegation_attempts
                   (attempt_id,logical_id,run_id,attempt_number,physical_worker_id,
                    state,owner_pid,owner_started_at,created_at,updated_at,metadata_json)
                   VALUES (?,?,?,?,?,'starting',?,?,?,?,?)""",
                (
                    attempt_id,
                    logical_id,
                    run_id,
                    attempt_number,
                    physical_worker_id or logical_id,
                    owner_pid,
                    owner_started_at,
                    now,
                    now,
                    _dump(metadata or {}),
                ),
            )
        return {
            "status": "reserved",
            "delegation_id": delegation_id,
            "run_id": run_id,
            "run_number": run_number,
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
        }

    def transition_attempt(
        self,
        attempt_id: str,
        expected_states: Iterable[str],
        new_state: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        expected = set(expected_states)
        now = time.time()
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT state,metadata_json FROM delegation_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return {"status": "not_found"}
            if row[0] not in expected or row[0] not in _ACTIVE_STATES:
                return {"status": "stale", "state": row[0]}
            merged = _json(row[1], {})
            if metadata:
                merged.update(metadata)
            terminal_at = completed_at
            if new_state not in _ACTIVE_STATES and terminal_at is None:
                terminal_at = now
            cur = conn.execute(
                """UPDATE delegation_attempts SET state=?,metadata_json=?,
                   started_at=COALESCE(?,started_at), completed_at=?,updated_at=?
                   WHERE attempt_id=? AND state=?""",
                (new_state, _dump(merged), started_at, terminal_at, now, attempt_id, row[0]),
            )
            return {"status": "updated" if cur.rowcount == 1 else "stale"}

    def request_interrupt(self, attempt_id: str, reason: str = "") -> Dict[str, Any]:
        now = time.time()
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT state,interrupt_requested_at FROM delegation_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return {"status": "not_found"}
            if row[0] == "interrupt_requested" or row[1] is not None:
                return {"status": "already_requested"}
            if row[0] not in _ACTIVE_STATES:
                return {"status": "already_terminal"}
            cur = conn.execute(
                """UPDATE delegation_attempts SET state='interrupt_requested',
                   interrupt_reason=?,interrupt_requested_at=?,updated_at=?
                   WHERE attempt_id=? AND state=? AND interrupt_requested_at IS NULL""",
                (reason or None, now, now, attempt_id, row[0]),
            )
            return {"status": "interrupt_requested" if cur.rowcount == 1 else "stale"}

    def take_interrupt(self, attempt_id: str) -> Dict[str, Any]:
        now = time.time()
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT interrupt_reason FROM delegation_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return {"status": "not_found"}
            cur = conn.execute(
                """UPDATE delegation_attempts SET interrupt_taken_at=?,updated_at=?
                   WHERE attempt_id=? AND state='interrupt_requested'
                     AND interrupt_requested_at IS NOT NULL AND interrupt_taken_at IS NULL""",
                (now, now, attempt_id),
            )
            if cur.rowcount != 1:
                return {"status": "not_pending"}
            return {"status": "taken", "reason": str(row[0] or "")}

    def complete_run(
        self, run_id: str, event: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        completed_at = float(event.get("completed_at") or time.time())
        with self.write_txn() as conn:
            run = conn.execute(
                "SELECT delegation_id,completed_at FROM delegation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                return {"status": "not_found"}
            if run[1] is not None:
                return {"status": "stale"}
            cur = conn.execute(
                """UPDATE delegation_runs SET completed_at=?,event_json=?,result_json=?
                   WHERE run_id=? AND completed_at IS NULL""",
                (completed_at, _dump(event), _dump(result), run_id),
            )
            if cur.rowcount != 1:
                return {"status": "stale"}
            attempts = conn.execute(
                """SELECT a.attempt_id,a.state,a.metadata_json,l.root_ordinal
                   FROM delegation_attempts a JOIN delegation_logical_subagents l
                     ON l.logical_id=a.logical_id WHERE a.run_id=?
                   ORDER BY l.root_ordinal IS NULL,l.root_ordinal,a.attempt_number""",
                (run_id,),
            ).fetchall()
            raw_results = result.get("results")
            child_results: List[Any] = raw_results if isinstance(raw_results, list) else []
            for index, attempt in enumerate(attempts):
                child = child_results[index] if index < len(child_results) and isinstance(child_results[index], dict) else result
                state = _attempt_state(child.get("status") or event.get("status"))
                metadata = _json(attempt[2], {})
                metadata.update(child)
                conn.execute(
                    """UPDATE delegation_attempts SET state=?,completed_at=?,updated_at=?,metadata_json=?
                       WHERE attempt_id=? AND state IN ('starting','running','finalizing','interrupt_requested')""",
                    (state, completed_at, completed_at, _dump(metadata), attempt[0]),
                )
        return {"status": "completed", "delegation_id": run[0], "run_id": run_id}

    def _resolve_run(
        self, conn: sqlite3.Connection, delegation_id: str, run_id: Optional[str]
    ) -> Dict[str, Any]:
        if run_id:
            row = conn.execute(
                "SELECT * FROM delegation_runs WHERE run_id=? AND delegation_id=?",
                (run_id, delegation_id),
            ).fetchone()
            return {"status": "found", "row": row} if row else {"status": "not_found"}
        rows = conn.execute(
            "SELECT * FROM delegation_runs WHERE delegation_id=?", (delegation_id,)
        ).fetchall()
        if not rows:
            return {"status": "not_found"}
        if len(rows) != 1:
            return {"status": "ambiguous_run"}
        return {"status": "found", "row": rows[0]}

    def claim_run_delivery(
        self,
        delegation_id: str,
        run_id: Optional[str],
        token: str,
        *,
        now: Optional[float] = None,
        delivering_stale_seconds: float = 300,
        wait_stale_seconds: float = 360,
    ) -> Dict[str, Any]:
        claimed_at = float(now or time.time())
        self._ensure_materialized(delegation_id)
        with self.write_txn() as conn:
            resolved = self._resolve_run(conn, delegation_id, run_id)
            if resolved["status"] != "found":
                return {"status": resolved["status"]}
            row = resolved["row"]
            if row["completed_at"] is None or row["event_json"] is None:
                return {"status": "not_ready"}
            disposition = row["delivery_state"]
            claimable = disposition == "pending"
            claimable |= disposition == "delivering" and (
                row["delivery_claimed_at"] is None
                or row["delivery_claimed_at"] < claimed_at - delivering_stale_seconds
            )
            claimable |= disposition == "held_by_wait" and (
                row["delivery_claimed_at"] is None
                or row["delivery_claimed_at"] < claimed_at - wait_stale_seconds
            )
            if not claimable:
                return {"status": "held" if disposition in {"delivering", "held_by_wait"} else "stale"}
            cur = conn.execute(
                """UPDATE delegation_runs SET delivery_state='delivering',delivery_claim=?,
                   delivery_claimed_at=?,delivery_attempts=delivery_attempts+1
                   WHERE run_id=? AND delivery_state=?""",
                (token, claimed_at, row["run_id"], disposition),
            )
            return (
                {"status": "claimed", "token": token, "run_id": row["run_id"]}
                if cur.rowcount == 1
                else {"status": "held"}
            )

    def release_run_delivery(self, run_id: str, token: str) -> Dict[str, Any]:
        with self.write_txn() as conn:
            cur = conn.execute(
                """UPDATE delegation_runs SET delivery_state='pending',delivery_claim=NULL,
                   delivery_claimed_at=NULL WHERE run_id=? AND delivery_state='delivering'
                   AND delivery_claim=?""",
                (run_id, token),
            )
        return {"status": "released" if cur.rowcount == 1 else "stale"}

    def commit_run_delivery(
        self, run_id: str, token: str, *, disposition: str = "delivered"
    ) -> Dict[str, Any]:
        if disposition not in _TERMINAL_DELIVERY_STATES:
            raise ValueError("terminal delivery disposition required")
        now = time.time()
        with self.write_txn() as conn:
            cur = conn.execute(
                """UPDATE delegation_runs SET delivery_state=?,delivered_at=?,
                   delivery_claim=NULL,delivery_claimed_at=NULL
                   WHERE run_id=? AND delivery_state IN ('delivering','held_by_wait')
                     AND delivery_claim=?""",
                (disposition, now, run_id, token),
            )
        return {"status": disposition if cur.rowcount == 1 else "stale"}

    def recover_orphaned_attempts(
        self, owner_alive: Callable[[int, Optional[int]], bool]
    ) -> Dict[str, Any]:
        recovered: List[str] = []
        now = time.time()
        with self.write_txn() as conn:
            rows = conn.execute(
                """SELECT a.* FROM delegation_attempts a
                   WHERE a.state IN ('starting','running','finalizing','interrupt_requested')
                     AND a.attempt_number=(SELECT MAX(x.attempt_number)
                         FROM delegation_attempts x WHERE x.logical_id=a.logical_id)"""
            ).fetchall()
            for row in rows:
                pid = row["owner_pid"]
                if pid and owner_alive(int(pid), row["owner_started_at"]):
                    continue
                cur = conn.execute(
                    """UPDATE delegation_attempts SET state='unknown',completed_at=?,updated_at=?
                       WHERE attempt_id=? AND state=? AND owner_pid IS ? AND owner_started_at IS ?""",
                    (now, now, row["attempt_id"], row["state"], pid, row["owner_started_at"]),
                )
                if cur.rowcount == 1:
                    recovered.append(row["attempt_id"])
        return {"status": "recovered", "attempt_ids": recovered}

    def _materialize_all(self) -> None:
        conn = self._connect()
        try:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT delegation_id FROM async_delegations WHERE lifecycle_version=1"
                )
            ]
        finally:
            conn.close()
        if ids:
            with self.write_txn() as conn:
                for delegation_id in ids:
                    self._materialize(conn, delegation_id)

    def snapshot(
        self, delegation_id: str, *, session_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if self._ensure_materialized(delegation_id) == "not_found":
            return None
        conn = self._connect()
        try:
            params: List[Any] = [delegation_id]
            owner_clause = ""
            if session_key is not None:
                owner_clause = " AND origin_session=?"
                params.append(session_key)
            container = conn.execute(
                "SELECT * FROM async_delegations WHERE delegation_id=?" + owner_clause,
                params,
            ).fetchone()
            if container is None:
                return None
            run = conn.execute(
                "SELECT * FROM delegation_runs WHERE delegation_id=? "
                "ORDER BY run_number DESC LIMIT 1",
                (delegation_id,),
            ).fetchone()
            attempts = conn.execute(
                """SELECT a.*,l.parent_logical_id,l.root_ordinal,l.spec_json
                   FROM delegation_attempts a JOIN delegation_logical_subagents l
                     ON l.logical_id=a.logical_id
                   WHERE l.delegation_id=? AND a.attempt_number=(
                     SELECT MAX(x.attempt_number) FROM delegation_attempts x
                     WHERE x.logical_id=a.logical_id)
                   ORDER BY l.root_ordinal IS NULL,l.root_ordinal,l.created_at,l.logical_id""",
                (delegation_id,),
            ).fetchall()
        finally:
            conn.close()

        task = _json(container["task_json"], {})
        children: Dict[str, Dict[str, Any]] = {}
        roots: List[str] = []
        active_states: List[str] = []
        owner_pid = owner_started_at = None
        interrupt_requests: Dict[str, str] = {}
        for attempt in attempts:
            metadata = _json(attempt["metadata_json"], {})
            spec = _json(attempt["spec_json"], {})
            child = {**spec, **metadata}
            child.update(
                {
                    "subagent_id": attempt["logical_id"],
                    "parent_id": attempt["parent_logical_id"],
                    "status": attempt["state"],
                    "attempt_id": attempt["attempt_id"],
                    "attempt_number": attempt["attempt_number"],
                    "run_id": attempt["run_id"],
                }
            )
            children[attempt["logical_id"]] = child
            if attempt["root_ordinal"] is not None:
                roots.append(attempt["logical_id"])
            if attempt["state"] in _ACTIVE_STATES:
                active_states.append(attempt["state"])
                if owner_pid is None:
                    owner_pid, owner_started_at = attempt["owner_pid"], attempt["owner_started_at"]
            if attempt["interrupt_requested_at"] is not None and attempt["interrupt_taken_at"] is None:
                interrupt_requests[attempt["logical_id"]] = str(attempt["interrupt_reason"] or "")

        if active_states:
            state = next(
                candidate
                for candidate in ("interrupt_requested", "finalizing", "running", "starting")
                if candidate in active_states
            )
        elif run and run["completed_at"] is not None:
            event_value = _json(run["event_json"], {})
            state = _attempt_state(event_value.get("status"))
        elif attempts:
            state = attempts[0]["state"]
        else:
            state = "running" if run and run["completed_at"] is None else "unknown"

        event = _json(run["event_json"], {}) if run and run["event_json"] else None
        result = _json(run["result_json"], {}) if run and run["result_json"] else None
        delivery = str(run["delivery_state"] if run else "pending")
        return {
            **task,
            "delegation_id": delegation_id,
            "origin_session": container["origin_session"],
            "session_key": container["origin_session"],
            "origin_ui_session_id": container["origin_ui_session_id"],
            "parent_session_id": container["parent_session_id"],
            "lifecycle_version": 2,
            "state": state,
            "worker_status": state,
            "status": state,
            "dispatched_at": container["dispatched_at"],
            "completed_at": run["completed_at"] if run else None,
            "updated_at": max(
                [float(container["updated_at"])]
                + [float(a["updated_at"]) for a in attempts]
                + ([float(run["completed_at"] or run["created_at"])] if run else [])
            ),
            "event": event,
            "result": result,
            "delivery_state": delivery,
            "delivery_disposition": delivery,
            "delivery_attempts": int(run["delivery_attempts"] if run else 0),
            "delivered_at": run["delivered_at"] if run else None,
            "delivery_claim": run["delivery_claim"] if run else None,
            "delivery_claimed_at": run["delivery_claimed_at"] if run else None,
            "run_id": run["run_id"] if run else None,
            "root_subagent_ids": roots,
            "children": children,
            "interrupt_requests": interrupt_requests,
            "interrupt_reason": container["interrupt_reason"],
            "abandon_reason": container["abandon_reason"],
            "owner_pid": owner_pid,
            "owner_started_at": owner_started_at,
        }

    def list_snapshots(
        self, *, session_keys: Optional[List[str]] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        self._materialize_all()
        conn = self._connect()
        try:
            params: List[Any] = []
            where = ""
            if session_keys is not None:
                if not session_keys:
                    return []
                where = f"WHERE origin_session IN ({','.join('?' for _ in session_keys)})"
                params.extend(session_keys)
            params.append(max(0, min(int(limit), 100)))
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT delegation_id FROM async_delegations {where} "
                    "ORDER BY dispatched_at DESC,delegation_id LIMIT ?",
                    params,
                )
            ]
        finally:
            conn.close()
        return [snap for delegation_id in ids if (snap := self.snapshot(delegation_id)) is not None]

    def prune(self, *, cutoff: float, max_terminal: int) -> Dict[str, Any]:
        self._materialize_all()
        with self.write_txn() as conn:
            eligible = [
                row[0]
                for row in conn.execute(
                    """SELECT d.delegation_id FROM async_delegations d
                       WHERE d.updated_at < ?
                         AND NOT EXISTS (SELECT 1 FROM delegation_attempts a
                           JOIN delegation_logical_subagents l ON l.logical_id=a.logical_id
                           WHERE l.delegation_id=d.delegation_id
                             AND a.state IN ('starting','running','finalizing','interrupt_requested'))
                         AND NOT EXISTS (SELECT 1 FROM delegation_runs r
                           WHERE r.delegation_id=d.delegation_id
                             AND (r.completed_at IS NULL OR r.delivery_state NOT IN
                               ('delivered','consumed','suppressed')))
                       ORDER BY d.updated_at DESC""",
                    (cutoff,),
                )
            ]
            keep = max(0, int(max_terminal))
            delete_ids = eligible[keep:]
            if delete_ids:
                conn.executemany(
                    "DELETE FROM async_delegations WHERE delegation_id=?",
                    [(delegation_id,) for delegation_id in delete_ids],
                )
        return {"status": "pruned", "deleted": len(delete_ids)}

    def delete(self, delegation_id: str) -> bool:
        with self.write_txn() as conn:
            changed = conn.execute(
                "DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,)
            ).rowcount == 1
        return changed

    def resolve_run_id(
        self, delegation_id: str, run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        if self._ensure_materialized(delegation_id) == "not_found":
            return {"status": "not_found"}
        conn = self._connect()
        try:
            resolved = self._resolve_run(conn, delegation_id, run_id)
            if resolved["status"] != "found":
                return {"status": resolved["status"]}
            return {"status": "found", "run_id": resolved["row"]["run_id"]}
        finally:
            conn.close()

    def inspect_delivery(self, delegation_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self.resolve_run_id(delegation_id, run_id)
        if resolved["status"] != "found":
            return resolved
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM delegation_runs WHERE run_id=?", (resolved["run_id"],)
            ).fetchone()
            return {"status": "found", **dict(row)}
        finally:
            conn.close()

    def set_delivery(
        self,
        delegation_id: str,
        run_id: Optional[str],
        *,
        expected: Iterable[str],
        disposition: str,
        token: Optional[str] = None,
        require_token: Optional[str] = None,
        delivered: bool = False,
        increment_attempts: bool = False,
    ) -> Dict[str, Any]:
        resolved = self.resolve_run_id(delegation_id, run_id)
        if resolved["status"] != "found":
            return resolved
        expected_values = tuple(expected)
        placeholders = ",".join("?" for _ in expected_values)
        now = time.time()
        token_clause = " AND delivery_claim=?" if require_token is not None else ""
        params: List[Any] = [
            disposition,
            now if delivered else None,
            token,
            now if token else None,
            1 if increment_attempts else 0,
            resolved["run_id"],
            *expected_values,
        ]
        if require_token is not None:
            params.append(require_token)
        with self.write_txn() as conn:
            cur = conn.execute(
                f"""UPDATE delegation_runs SET delivery_state=?,
                    delivered_at=COALESCE(?,delivered_at),delivery_claim=?,
                    delivery_claimed_at=?,delivery_attempts=delivery_attempts+?
                    WHERE run_id=? AND delivery_state IN ({placeholders}){token_clause}""",
                params,
            )
        return {"status": "updated" if cur.rowcount == 1 else "stale", "run_id": resolved["run_id"]}

    def recover_stale_wait_holds(
        self, *, cutoff: float, delegation_id: Optional[str] = None
    ) -> int:
        self._materialize_all()
        where = " AND r.delegation_id=?" if delegation_id else ""
        params: List[Any] = [cutoff]
        if delegation_id:
            params.append(delegation_id)
        with self.write_txn() as conn:
            cur = conn.execute(
                """UPDATE delegation_runs AS r SET delivery_state='pending',
                   delivery_claim=NULL,delivery_claimed_at=NULL
                   WHERE delivery_state='held_by_wait'
                     AND (delivery_claimed_at IS NULL OR delivery_claimed_at < ?)"""
                + where,
                params,
            )
        return cur.rowcount

    def pending_events(self, *, delivery_state: str = "pending") -> List[Dict[str, Any]]:
        self._materialize_all()
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT r.run_id,r.event_json FROM delegation_runs r
                   WHERE r.completed_at IS NOT NULL AND r.delivery_state=?
                     AND r.event_json IS NOT NULL ORDER BY r.completed_at,r.run_id""",
                (delivery_state,),
            ).fetchall()
            return [
                {"run_id": row["run_id"], "event": _json(row["event_json"], {})}
                for row in rows
            ]
        finally:
            conn.close()

    def register_subagent(self, record: Dict[str, Any]) -> Optional[Dict[str, str]]:
        worker_id = record.get("subagent_id")
        if not isinstance(worker_id, str) or not worker_id:
            return None
        parent_id = record.get("parent_id")
        now = time.time()
        with self.write_txn() as conn:
            attempt = conn.execute(
                """SELECT a.attempt_id,a.logical_id,l.delegation_id FROM delegation_attempts a
                   JOIN delegation_logical_subagents l ON l.logical_id=a.logical_id
                   WHERE a.physical_worker_id=? OR a.logical_id=?
                   ORDER BY a.attempt_number DESC LIMIT 1""",
                (worker_id, worker_id),
            ).fetchone()
            if attempt is None and isinstance(parent_id, str) and parent_id:
                parent = conn.execute(
                    """SELECT a.run_id,a.owner_pid,a.owner_started_at,l.delegation_id,l.logical_id
                       FROM delegation_attempts a JOIN delegation_logical_subagents l
                         ON l.logical_id=a.logical_id
                       WHERE (a.physical_worker_id=? OR a.logical_id=?)
                       ORDER BY a.attempt_number DESC LIMIT 1""",
                    (parent_id, parent_id),
                ).fetchone()
                if parent is None:
                    return None
                conn.execute(
                    """INSERT INTO delegation_logical_subagents
                       (logical_id,delegation_id,parent_logical_id,spec_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (worker_id, parent[3], parent[4], _dump(record), now, now),
                )
                attempt_id = _new_id("attempt")
                conn.execute(
                    """INSERT INTO delegation_attempts
                       (attempt_id,logical_id,run_id,attempt_number,physical_worker_id,state,
                        owner_pid,owner_started_at,created_at,updated_at,metadata_json)
                       VALUES (?,?,?,1,?,'starting',?,?,?,?,?)""",
                    (attempt_id, worker_id, parent[0], worker_id, parent[1], parent[2], now, now, _dump(record)),
                )
                attempt = (attempt_id, worker_id, parent[3])
            if attempt is None:
                return None
            current = conn.execute(
                "SELECT state,metadata_json FROM delegation_attempts WHERE attempt_id=?",
                (attempt[0],),
            ).fetchone()
            metadata = _json(current[1], {})
            metadata.update({key: value for key, value in record.items() if key != "agent"})
            requested = conn.execute(
                "SELECT interrupt_requested_at FROM delegation_attempts WHERE attempt_id=?",
                (attempt[0],),
            ).fetchone()[0]
            state = "interrupt_requested" if requested is not None else str(record.get("status") or current[0])
            if current[0] in _ACTIVE_STATES:
                conn.execute(
                    "UPDATE delegation_attempts SET state=?,metadata_json=?,updated_at=? WHERE attempt_id=?",
                    (state, _dump(metadata), now, attempt[0]),
                )
            return {"delegation_id": str(attempt[2]), "attempt_id": str(attempt[0])}

    def find_attempt(
        self, worker_id: str, *, delegation_id: Optional[str] = None, session_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        self._materialize_all()
        conn = self._connect()
        try:
            clauses, params = ["(a.physical_worker_id=? OR a.logical_id=?)"], [worker_id, worker_id]
            if delegation_id is not None:
                clauses.append("l.delegation_id=?")
                params.append(delegation_id)
            if session_key is not None:
                clauses.append("d.origin_session=?")
                params.append(session_key)
            row = conn.execute(
                """SELECT a.*,l.delegation_id FROM delegation_attempts a
                   JOIN delegation_logical_subagents l ON l.logical_id=a.logical_id
                   JOIN async_delegations d ON d.delegation_id=l.delegation_id WHERE """
                + " AND ".join(clauses)
                + " ORDER BY a.attempt_number DESC LIMIT 1",
                params,
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_attempt_metadata(self, attempt_id: str, metadata: Dict[str, Any]) -> bool:
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM delegation_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                return False
            merged = _json(row[0], {})
            merged.update(metadata)
            return conn.execute(
                "UPDATE delegation_attempts SET metadata_json=?,updated_at=? WHERE attempt_id=?",
                (_dump(merged), time.time(), attempt_id),
            ).rowcount == 1
