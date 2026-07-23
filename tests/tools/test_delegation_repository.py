"""Behavior contracts for normalized delegation persistence."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from tools.delegation_repository import DelegationRepository


def _released_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE async_delegations (
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
    conn.commit()
    conn.close()


def _open_snapshot(path: str, delegation_id: str, start, output) -> None:
    start.wait()
    try:
        snap = DelegationRepository(Path(path)).snapshot(delegation_id)
        output.put(("ok", snap["lifecycle_version"] if snap else None))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        output.put(("error", f"{type(exc).__name__}: {exc}"))


@pytest.fixture
def repo(tmp_path: Path) -> DelegationRepository:
    return DelegationRepository(tmp_path / "state.db")


def _record(delegation_id="deleg-1", roots=None, dispatched_at=10.0):
    roots = ["sa-root"] if roots is None else roots
    return {
        "delegation_id": delegation_id,
        "session_key": "owner",
        "origin_ui_session_id": "ui-owner",
        "parent_session_id": "parent-owner",
        "dispatched_at": dispatched_at,
        "goal": "do work",
        "context": "context",
        "role": "leaf",
        "model": "test",
        "root_subagent_ids": roots,
    }


def test_initial_single_and_batch_shape_preserves_root_order(repo):
    single = repo.register_initial_dispatch(_record())
    batch = repo.register_initial_dispatch(
        _record("deleg-batch", ["sa-z", "sa-a"], dispatched_at=20.0)
    )

    assert single["status"] == "registered"
    assert single["run_id"]
    assert [(a["logical_id"], a["attempt_number"]) for a in single["attempts"]] == [
        ("sa-root", 1)
    ]
    assert [a["logical_id"] for a in batch["attempts"]] == ["sa-z", "sa-a"]
    snap = repo.snapshot("deleg-batch")
    assert snap["lifecycle_version"] == 2
    assert snap["root_subagent_ids"] == ["sa-z", "sa-a"]
    assert list(snap["children"]) == ["sa-z", "sa-a"]
    assert snap["state"] == "starting"


def test_batch_result_mapping_is_ordinal_not_uuid_order(repo):
    created = repo.register_initial_dispatch(_record(roots=["sa-z", "sa-a"]))
    result = {
        "results": [
            {"status": "completed", "summary": "first"},
            {"status": "error", "error": "second"},
        ]
    }
    event = {"status": "completed", "completed_at": 30.0, "results": result["results"]}

    assert repo.complete_run(created["run_id"], event, result)["status"] == "completed"
    children = repo.snapshot("deleg-1")["children"]
    assert children["sa-z"]["status"] == "completed"
    assert children["sa-z"]["summary"] == "first"
    assert children["sa-a"]["status"] == "error"
    assert children["sa-a"]["error"] == "second"


def test_concurrent_resume_has_one_winner(repo):
    initial = repo.register_initial_dispatch(_record())
    attempt_id = initial["attempts"][0]["attempt_id"]
    assert repo.transition_attempt(
        attempt_id, {"starting"}, "completed", completed_at=11.0
    )["status"] == "updated"
    barrier = threading.Barrier(9)
    outcomes = []

    def reserve(index):
        barrier.wait()
        outcomes.append(
            repo.reserve_resumed_attempt("sa-root", physical_worker_id=f"worker-{index}")
        )

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert [outcome["status"] for outcome in outcomes].count("reserved") == 1
    assert [outcome["status"] for outcome in outcomes].count("already_running") == 7


def test_stale_attempt_and_interrupt_cannot_poison_new_attempt(repo):
    initial = repo.register_initial_dispatch(_record())
    old_id = initial["attempts"][0]["attempt_id"]
    repo.transition_attempt(old_id, {"starting"}, "completed", completed_at=11.0)
    current = repo.reserve_resumed_attempt("sa-root", physical_worker_id="worker-new")

    assert repo.transition_attempt(old_id, {"completed"}, "error")["status"] == "stale"
    assert repo.request_interrupt(old_id, "too late")["status"] == "already_terminal"
    requested = repo.request_interrupt(current["attempt_id"], "stop")
    assert requested["status"] == "interrupt_requested"
    assert repo.request_interrupt(current["attempt_id"], "again")["status"] == "already_requested"
    assert repo.take_interrupt(current["attempt_id"]) == {"status": "taken", "reason": "stop"}
    assert repo.take_interrupt(current["attempt_id"])["status"] == "not_pending"
    assert repo.snapshot("deleg-1")["children"]["sa-root"]["status"] == "interrupt_requested"


def test_stale_run_completion_does_not_overwrite_or_publish(repo):
    initial = repo.register_initial_dispatch(_record())
    old_attempt = initial["attempts"][0]["attempt_id"]
    repo.transition_attempt(old_attempt, {"starting"}, "completed")
    resumed = repo.reserve_resumed_attempt("sa-root")
    fresh_event = {"status": "completed", "summary": "fresh", "completed_at": 20.0}
    assert repo.complete_run(resumed["run_id"], fresh_event, fresh_event)["status"] == "completed"

    stale_event = {"status": "error", "summary": "stale", "completed_at": 21.0}
    assert repo.complete_run(resumed["run_id"], stale_event, stale_event)["status"] == "stale"
    snap = repo.snapshot("deleg-1")
    assert snap["event"]["summary"] == "fresh"
    assert snap["result"]["summary"] == "fresh"


def test_owner_recovery_is_exact_and_allows_later_reserve(repo):
    initial = repo.register_initial_dispatch(
        _record(), owner_pid=999999, owner_started_at=1
    )
    attempt_id = initial["attempts"][0]["attempt_id"]

    recovered = repo.recover_orphaned_attempts(lambda _pid, _started: False)
    assert recovered["attempt_ids"] == [attempt_id]
    assert repo.snapshot("deleg-1")["children"]["sa-root"]["status"] == "unknown"
    resumed = repo.reserve_resumed_attempt("sa-root", owner_pid=123, owner_started_at=2)
    assert resumed["status"] == "reserved"
    assert resumed["attempt_number"] == 2


def test_legacy_migration_is_idempotent_and_preserves_disposition(tmp_path):
    path = tmp_path / "state.db"
    _released_schema(str(path))
    children = {
        "sa-z": {"subagent_id": "sa-z", "status": "success", "summary": "z"},
        "sa-a": {"subagent_id": "sa-a", "status": "failed", "error": "a"},
        "sa-child": {
            "subagent_id": "sa-child",
            "parent_id": "sa-z",
            "status": "running",
            "goal": "nested",
        },
    }
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, state, dispatched_at, completed_at,
            updated_at, event_json, result_json, delivery_state,
            delivery_attempts, delivery_claim, delivery_claimed_at,
            delivered_at, owner_pid, owner_started_at, task_json,
            root_subagent_ids_json, children_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "deleg-old", "owner", "completed", 1.0, 2.0, 2.0,
            json.dumps({"status": "completed", "summary": "event"}),
            json.dumps({"status": "completed", "summary": "result"}),
            "pending", 3, "claim-old", 1.5, None, 4321, 99,
            json.dumps({"goal": "legacy"}),
            json.dumps(["sa-z", "sa-a"]), json.dumps(children),
        ),
    )
    conn.commit()
    conn.close()

    repository = DelegationRepository(path)
    first = repository.snapshot("deleg-old")
    second = repository.snapshot("deleg-old")
    assert first == second
    assert first["lifecycle_version"] == 2
    assert first["root_subagent_ids"] == ["sa-z", "sa-a"]
    assert first["children"]["sa-child"]["parent_id"] == "sa-z"
    assert first["children"]["sa-child"]["status"] == "running"
    assert first["children"]["sa-z"]["status"] == "completed"
    assert first["children"]["sa-a"]["status"] == "error"
    assert first["delivery_state"] == "delivering"
    assert first["delivery_claim"] == "claim-old"
    assert first["delivery_attempts"] == 3
    assert first["owner_pid"] == 4321

    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT COUNT(*) FROM delegation_runs WHERE delegation_id='deleg-old'"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT lifecycle_version FROM async_delegations WHERE delegation_id='deleg-old'"
        ).fetchone()[0] == 2


def test_released_schema_migrates_under_concurrent_process_open(tmp_path):
    path = tmp_path / "state.db"
    _released_schema(str(path))
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, state, dispatched_at, updated_at,
            root_subagent_ids_json, children_json)
           VALUES ('deleg-mp','owner','running',1,1,'["sa-root"]',
                   '{"sa-root":{"status":"running"}}')"""
    )
    conn.commit()
    conn.close()

    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    output = ctx.Queue()
    workers = [ctx.Process(target=_open_snapshot, args=(str(path), "deleg-mp", start, output)) for _ in range(6)]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [output.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    assert outcomes == [("ok", 2)] * 6


def test_run_delivery_is_exact_and_omitted_run_fails_ambiguous(repo):
    initial = repo.register_initial_dispatch(_record())
    repo.complete_run(initial["run_id"], {"status": "completed"}, {"summary": "initial"})
    repo.transition_attempt(initial["attempts"][0]["attempt_id"], {"completed"}, "completed")
    resumed = repo.reserve_resumed_attempt("sa-root")
    repo.complete_run(resumed["run_id"], {"status": "completed"}, {"summary": "resumed"})

    first = repo.claim_run_delivery("deleg-1", initial["run_id"], "claim-1")
    second = repo.claim_run_delivery("deleg-1", resumed["run_id"], "claim-2")
    assert first["status"] == second["status"] == "claimed"
    assert repo.commit_run_delivery(initial["run_id"], "claim-1")["status"] == "delivered"
    assert repo.release_run_delivery(resumed["run_id"], "claim-2")["status"] == "released"
    assert repo.claim_run_delivery("deleg-1", None, "claim-3") == {
        "status": "ambiguous_run"
    }


def test_retention_protects_active_attempts_and_nonterminal_delivery(repo):
    active = repo.register_initial_dispatch(
        _record("deleg-active", ["sa-active"], dispatched_at=1.0)
    )
    pending = repo.register_initial_dispatch(
        _record("deleg-pending", ["sa-pending"], dispatched_at=2.0)
    )
    repo.complete_run(pending["run_id"], {"status": "completed"}, {"summary": "keep"})
    prune = repo.register_initial_dispatch(
        _record("deleg-prune", ["sa-prune"], dispatched_at=3.0)
    )
    repo.complete_run(prune["run_id"], {"status": "completed"}, {"summary": "drop"})
    claimed = repo.claim_run_delivery("deleg-prune", prune["run_id"], "claim")
    assert claimed["status"] == "claimed"
    repo.commit_run_delivery(prune["run_id"], "claim")

    outcome = repo.prune(cutoff=time.time() + 100.0, max_terminal=0)
    assert outcome["deleted"] == 1
    assert repo.snapshot("deleg-active") is not None
    assert repo.snapshot("deleg-pending") is not None
    assert repo.snapshot("deleg-prune") is None
    assert active["run_id"] and pending["run_id"]


def test_authorized_snapshot_derives_compatibility_fields(repo):
    created = repo.register_initial_dispatch(_record())
    attempt_id = created["attempts"][0]["attempt_id"]
    repo.transition_attempt(
        attempt_id,
        {"starting"},
        "running",
        metadata={"goal": "updated", "tool_count": 2, "last_tool": "terminal"},
    )
    own = repo.snapshot("deleg-1", session_key="owner")

    assert own["worker_status"] == own["state"] == "running"
    assert own["delivery_disposition"] == own["delivery_state"] == "pending"
    assert own["children"]["sa-root"]["goal"] == "updated"
    assert own["children"]["sa-root"]["tool_count"] == 2
    assert own["owner_pid"] is None
    assert repo.snapshot("deleg-1", session_key="foreign") is None
