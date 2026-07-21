"""Durable delegation delivery/control invariants.

These tests intentionally exercise SQLite state rather than installing a second
in-memory lifecycle registry.
"""

import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _dispatch(runner, *, session_key="owner", interrupt_fn=None, roots=None):
    return ad.dispatch_async_delegation(
        goal="durable control",
        context=None,
        toolsets=None,
        role="leaf",
        model="test",
        session_key=session_key,
        parent_session_id="parent-owner",
        origin_ui_session_id="ui-owner",
        runner=runner,
        interrupt_fn=interrupt_fn,
        root_subagent_ids=roots,
        max_async_children=8,
    )


def _wait_terminal(delegation_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = ad.get_durable_delegation(delegation_id)
        if row and row["state"] not in {"running", "finalizing", "interrupt_requested"}:
            return row
        time.sleep(0.005)
    raise AssertionError("delegation did not become terminal")


def test_terminal_wait_consumes_once_and_auto_delivery_drops_it():
    dispatched = _dispatch(
        lambda: {"status": "completed", "summary": "one result"}
    )
    _wait_terminal(dispatched["delegation_id"])

    first = ad.wait_for_delegation(
        dispatched["delegation_id"], session_key="owner", timeout_seconds=1
    )
    second = ad.wait_for_delegation(
        dispatched["delegation_id"], session_key="owner", timeout_seconds=0
    )

    assert first["claimed_delivery"] is True
    assert first["result"]["summary"] == "one result"
    assert first["delivery_disposition"] == "consumed"
    assert second["claimed_delivery"] is False
    assert second["delivery_disposition"] == "consumed"
    assert not ad.claim_completion_delivery(dispatched["delegation_id"], "auto")


def test_wait_timeout_releases_hold_without_consuming_late_completion():
    release = threading.Event()
    dispatched = _dispatch(
        lambda: release.wait(2) or {"status": "completed", "summary": "late"}
    )

    timed_out = ad.wait_for_delegation(
        dispatched["delegation_id"], session_key="owner", timeout_seconds=0.02
    )
    assert timed_out["status"] == "timeout"
    assert timed_out["claimed_delivery"] is False
    row = ad.get_durable_delegation(dispatched["delegation_id"])
    assert row["delivery_state"] == "pending"
    assert row["delivery_claim"] is None

    release.set()
    _wait_terminal(dispatched["delegation_id"])
    assert ad.claim_completion_delivery(dispatched["delegation_id"], "auto")
    assert ad.release_completion_delivery(dispatched["delegation_id"], "auto")


def test_completion_while_wait_hold_is_registered_is_consumed_by_waiter():
    release = threading.Event()
    dispatched = _dispatch(
        lambda: (release.wait(2), {"status": "completed", "summary": "at edge"})[1]
    )
    outcome = {}

    def wait():
        outcome.update(
            ad.wait_for_delegation(
                dispatched["delegation_id"],
                session_key="owner",
                timeout_seconds=1,
            )
        )

    thread = threading.Thread(target=wait)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        row = ad.get_durable_delegation(dispatched["delegation_id"])
        if row and row["delivery_state"] == "held_by_wait":
            break
        time.sleep(0.002)
    else:
        raise AssertionError("wait never registered its durable hold")

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert outcome["claimed_delivery"] is True
    assert outcome["delivery_disposition"] == "consumed"
    assert outcome["result"]["summary"] == "at edge"


def test_multiple_waiters_have_exactly_one_result_owner():
    release = threading.Event()
    start = threading.Barrier(3)
    dispatched = _dispatch(
        lambda: release.wait(2) or {"status": "completed", "summary": "single owner"}
    )
    outcomes = []

    def wait():
        start.wait()
        outcomes.append(
            ad.wait_for_delegation(
                dispatched["delegation_id"],
                session_key="owner",
                timeout_seconds=1,
            )
        )

    threads = [threading.Thread(target=wait) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if ad.get_durable_delegation(dispatched["delegation_id"])["delivery_state"] == "held_by_wait":
            break
        time.sleep(0.002)
    release.set()
    for thread in threads:
        thread.join(2)

    assert len(outcomes) == 2
    assert sum(result["claimed_delivery"] is True for result in outcomes) == 1
    assert ad.get_durable_delegation(dispatched["delegation_id"])["delivery_state"] == "consumed"


def test_abandon_suppresses_before_delivery_and_is_too_late_after_claim():
    release = threading.Event()
    interrupted = threading.Event()
    first = _dispatch(
        lambda: release.wait(2) or {"status": "interrupted", "summary": "stopped"},
        interrupt_fn=interrupted.set,
    )

    abandoned = ad.abandon_async_delegation(
        first["delegation_id"], session_key="owner", reason="obsolete"
    )
    assert abandoned["suppression"] == "applied"
    assert interrupted.wait(1)
    assert ad.get_durable_delegation(first["delegation_id"])["delivery_state"] == "suppressed"
    release.set()
    _wait_terminal(first["delegation_id"])
    assert not ad.claim_completion_delivery(first["delegation_id"], "auto")

    second = _dispatch(lambda: {"status": "completed", "summary": "accepted"})
    _wait_terminal(second["delegation_id"])
    assert ad.claim_completion_delivery(second["delegation_id"], "gateway")
    too_late = ad.abandon_async_delegation(
        second["delegation_id"], session_key="owner", reason="too late"
    )
    assert too_late["suppression"] == "too_late"
    assert ad.get_durable_delegation(second["delegation_id"])["delivery_state"] == "delivering"


def test_foreign_session_is_indistinguishable_from_unknown():
    release = threading.Event()
    dispatched = _dispatch(
        lambda: release.wait(2) or {"status": "completed", "summary": "private"}
    )
    try:
        foreign = ad.get_async_delegation(
            dispatched["delegation_id"], session_key="foreign"
        )
        unknown = ad.get_async_delegation("deleg_unknown", session_key="foreign")
        assert foreign is None
        assert unknown is None
        assert ad.list_durable_delegations(session_keys=["foreign"]) == []
    finally:
        release.set()


def test_pruning_keeps_all_undelivered_and_held_terminal_results(monkeypatch):
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 1)
    states = ["pending", "held_by_wait", "delivering", "delivered", "consumed", "suppressed"]
    for index, delivery_state in enumerate(states):
        delegation_id = f"deleg_retention_{index}"
        ad._persist_dispatch(
            {
                "delegation_id": delegation_id,
                "session_key": "owner",
                "origin_ui_session_id": "ui-owner",
                "parent_session_id": "parent-owner",
                "dispatched_at": float(index + 1),
                "root_subagent_ids": [],
            }
        )
        ad._persist_completion(
            {
                "delegation_id": delegation_id,
                "status": "completed",
                "completed_at": float(index + 1),
            },
            {"status": "completed", "summary": delivery_state},
        )
        with ad._DB_LOCK, ad._connect() as conn:
            conn.execute(
                "UPDATE async_delegations SET delivery_state=?, delivery_claim=? WHERE delegation_id=?",
                (
                    delivery_state,
                    "claim" if delivery_state in {"held_by_wait", "delivering"} else None,
                    delegation_id,
                ),
            )

    ad._prune_durable_records()

    for index, delivery_state in enumerate(states[:3]):
        row = ad.get_durable_delegation(f"deleg_retention_{index}")
        assert row is not None, delivery_state
        assert row["delivery_state"] == delivery_state


def test_restore_only_enqueues_pending_terminal_results():
    for index, state in enumerate(("pending", "consumed", "suppressed")):
        delegation_id = f"deleg_restore_{index}"
        ad._persist_dispatch(
            {
                "delegation_id": delegation_id,
                "session_key": "owner",
                "origin_ui_session_id": "",
                "parent_session_id": None,
                "dispatched_at": float(index + 1),
                "root_subagent_ids": [],
            }
        )
        ad._persist_completion(
            {"delegation_id": delegation_id, "status": "completed", "completed_at": float(index + 1)},
            {"status": "completed", "summary": state},
        )
        with ad._DB_LOCK, ad._connect() as conn:
            conn.execute(
                "UPDATE async_delegations SET delivery_state=? WHERE delegation_id=?",
                (state, delegation_id),
            )

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["delegation_id"] == "deleg_restore_0"
