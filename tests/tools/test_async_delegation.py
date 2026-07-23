"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import (
    finish_notification_delivery,
    format_process_notification,
    prepare_notification_delivery,
    process_registry,
)


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    # Give just-released workers a beat to finalize BEFORE draining, so their
    # completion events land now instead of leaking into the next test's
    # queue (worker threads push events asynchronously; a drain that races an
    # in-flight _finalize misses it).
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _drain_for(delegation_id, timeout=5.0):
    """Drain until the event for *delegation_id* appears (discarding others).

    Completion events are pushed asynchronously by worker threads, so a
    straggler from a PREVIOUS test can land after that test's teardown drain
    and leak into the current test's queue. Matching on delegation_id makes
    the assertion immune to that cross-test leak.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    return None


def test_active_for_session_counts_every_live_delegation_state():
    with ad._records_lock:
        ad._records.update(
            {
                "running": {
                    "status": "running",
                    "origin_ui_session_id": "desktop-sid",
                },
                "stalling": {
                    "status": "stalling",
                    "origin_ui_session_id": "desktop-sid",
                },
                "finalizing": {
                    "status": "finalizing",
                    "origin_ui_session_id": "desktop-sid",
                },
                "completed": {
                    "status": "completed",
                    "origin_ui_session_id": "desktop-sid",
                },
                "other-session": {
                    "status": "running",
                    "origin_ui_session_id": "other-sid",
                },
            }
        )

    assert ad.active_for_session("desktop-sid") == 3
    assert ad.active_for_session("other-sid") == 1
    assert ad.active_for_session("") == 0
def test_resume_reserves_exact_attempt_and_runs_with_hydrated_history(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import uuid
    from hermes_state import SessionDB
    from tools import delegate_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    suffix = uuid.uuid4().hex
    delegation_id = f"deleg-resume-e2e-{suffix}"
    logical_id = f"sa-resume-e2e-{suffix}"
    parent_session = f"parent-resume-e2e-{suffix}"
    child_session = f"child-resume-e2e-{suffix}"
    db = SessionDB()
    db.create_session(parent_session, source="discord")
    db.create_session(
        child_session,
        source="subagent",
        parent_session_id=parent_session,
        model_config={"_delegate_from": parent_session},
    )
    db.append_message(child_session, "user", "original child goal")
    db.append_message(child_session, "assistant", "partial answer")

    record = {
        "delegation_id": delegation_id,
        "goal": "finish the original task",
        "goals": ["finish the original task"],
        "context": None,
        "toolsets": ["file"],
        "role": "leaf",
        "model": "test-model",
        "session_key": parent_session,
        "origin_ui_session_id": "ui-parent",
        "parent_session_id": parent_session,
        "status": "running",
        "dispatched_at": time.time(),
        "root_subagent_ids": [logical_id],
        "is_batch": False,
    }
    ad._persist_dispatch(record)
    initial_run = record["run_id"]
    initial_attempt = record["attempt_ids"][0]
    metadata = {
        "child_session_id": child_session,
        "parent_session_id": parent_session,
        "model": "test-model",
        "provider": "test-provider",
        "role": "leaf",
        "depth": 1,
        "enabled_toolsets": ["file"],
        "disabled_toolsets": ["delegation"],
        "max_iterations": 7,
    }
    assert ad._repository().transition_attempt(
        initial_attempt, {"starting"}, "completed", metadata=metadata
    )["status"] == "updated"
    assert ad._repository().complete_run(
        initial_run,
        {
            "delegation_id": delegation_id,
            "run_id": initial_run,
            "status": "completed",
            "completed_at": time.time(),
        },
        {"status": "completed", "summary": "partial answer"},
    )["status"] == "completed"

    submitted = []

    class CapturingExecutor:
        def submit(self, fn):
            submitted.append(fn)
            return SimpleNamespace()

    monkeypatch.setattr(ad, "_get_executor", lambda _limit: CapturingExecutor())
    continuation = {
        "session_id": f"child-resume-e2e-next-{suffix}",
        "parent_session_id": child_session,
        "delegate_from": parent_session,
    }
    monkeypatch.setattr(
        delegate_tool, "prepare_resumed_child_session", lambda _bundle: continuation
    )
    resumed_metadata = {
        **metadata,
        "child_session_id": continuation["session_id"],
    }
    built = SimpleNamespace(
        _delegation_session_ref={},
        _delegation_runtime_metadata=resumed_metadata,
        close=lambda: None,
    )
    build_calls = []

    def fake_build(**kwargs):
        build_calls.append(kwargs)
        return built

    monkeypatch.setattr(delegate_tool, "build_resumed_child_agent", fake_build)
    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        db.create_session(
            continuation["session_id"],
            source="subagent",
            parent_session_id=child_session,
            model_config={"_delegate_from": parent_session},
        )
        db.append_message(
            continuation["session_id"], "user", kwargs["resume_message"]
        )
        db.append_message(
            continuation["session_id"], "assistant", "new resumed work"
        )
        return {
            "status": "completed",
            "summary": "resumed result",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "test-model",
        }

    monkeypatch.setattr(delegate_tool, "_run_single_child", fake_run)

    dispatched = ad.dispatch_resumed_subagent(
        delegation_id,
        logical_id,
        session_key=parent_session,
        message="continue from the partial answer",
        parent_agent=None,
    )
    assert dispatched["status"] == "dispatched"
    assert dispatched["attempt_number"] == 2
    assert dispatched["run_id"] != initial_run
    assert dispatched["attempt_id"] != initial_attempt
    assert len(submitted) == 1
    assert build_calls[0]["logical_id"] == logical_id

    duplicate = ad.dispatch_resumed_subagent(
        delegation_id,
        logical_id,
        session_key=parent_session,
        message="duplicate resume",
        parent_agent=object(),
    )
    assert duplicate == {
        "status": "already_running",
        "attempt_id": dispatched["attempt_id"],
        "run_id": dispatched["run_id"],
    }

    submitted[0]()
    assert run_calls[0][1]["resume_message"] == "continue from the partial answer"
    assert [item["content"] for item in run_calls[0][1]["conversation_history"]] == [
        "original child goal",
        "partial answer",
    ]
    event = _drain_for(delegation_id)
    assert event["run_id"] == dispatched["run_id"]
    assert event["status"] == "completed"
    assert event["summary"] == "resumed result"
    resumed_snapshot = ad._repository().snapshot(
        delegation_id, run_id=dispatched["run_id"]
    )
    assert resumed_snapshot["children"][logical_id]["status"] == "completed"
    assert (
        resumed_snapshot["children"][logical_id]["child_session_id"]
        == continuation["session_id"]
    )
    next_resume = ad.load_subagent_resume_bundle(
        delegation_id, logical_id, session_key=parent_session
    )
    assert next_resume["status"] == "ready"
    assert next_resume["bundle"]["prior_child_session_id"] == continuation["session_id"]
    assert next_resume["bundle"]["child_lineage"] == [
        child_session,
        continuation["session_id"],
    ]
    assert [item["content"] for item in next_resume["bundle"]["history"]] == [
        "original child goal",
        "partial answer",
        "continue from the partial answer",
        "new resumed work",
    ]


def test_resume_construction_failure_is_terminal_and_retryable(monkeypatch):
    from types import SimpleNamespace
    from tools import delegate_tool

    delegation_id = "deleg-resume-build-failure"
    logical_id = "sa-resume-build-failure"
    record = {
        "delegation_id": delegation_id,
        "goal": "retryable task",
        "session_key": "owner",
        "status": "running",
        "dispatched_at": time.time(),
        "root_subagent_ids": [logical_id],
    }
    ad._persist_dispatch(record)
    initial_attempt = record["attempt_ids"][0]
    metadata = {
        "child_session_id": "child-last-good",
        "parent_session_id": "owner",
        "provider": "provider",
        "model": "model",
        "role": "leaf",
        "depth": 1,
    }
    ad._repository().transition_attempt(
        initial_attempt, {"starting"}, "completed", metadata=metadata
    )
    bundle = {
        "prior_child_session_id": "child-last-good",
        "history": [{"role": "user", "content": "goal"}],
        "reconstruction_metadata": dict(metadata),
    }
    monkeypatch.setattr(
        ad,
        "load_subagent_resume_bundle",
        lambda *a, **kw: {
            "status": "ready",
            "delegation_id": delegation_id,
            "subagent_id": logical_id,
            "bundle": bundle,
        },
    )
    counter = iter(("child-failed-segment", "child-retry-segment"))
    monkeypatch.setattr(
        delegate_tool,
        "prepare_resumed_child_session",
        lambda _bundle: {
            "session_id": next(counter),
            "parent_session_id": "child-last-good",
            "delegate_from": "owner",
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "build_resumed_child_agent",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("constructor failed")),
    )

    failed = ad.dispatch_resumed_subagent(
        delegation_id,
        logical_id,
        session_key="owner",
        message="try again",
        parent_agent=object(),
    )
    assert failed["status"] == "dispatch_failed"
    assert failed["attempt_number"] == 2
    failed_event = _drain_for(delegation_id)
    assert failed_event["run_id"] == failed["run_id"]
    assert failed_event["status"] == "error"
    failed_snapshot = ad._repository().snapshot(
        delegation_id, run_id=failed["run_id"]
    )
    failed_child = failed_snapshot["children"][logical_id]
    assert failed_child["status"] == "error"
    assert failed_child["child_session_id"] == "child-last-good"

    submitted = []

    class CapturingExecutor:
        def submit(self, fn):
            submitted.append(fn)
            return SimpleNamespace()

    monkeypatch.setattr(ad, "_get_executor", lambda _limit: CapturingExecutor())
    monkeypatch.setattr(
        delegate_tool,
        "build_resumed_child_agent",
        lambda **kwargs: SimpleNamespace(
            _delegation_session_ref={},
            _delegation_runtime_metadata=dict(metadata),
            close=lambda: None,
        ),
    )
    retried = ad.dispatch_resumed_subagent(
        delegation_id,
        logical_id,
        session_key="owner",
        message="retry after repaired config",
        parent_agent=object(),
    )
    assert retried["status"] == "dispatched"
    assert retried["attempt_number"] == 3
    assert retried["child_session_id"] == "child-retry-segment"
    assert len(submitted) == 1


def test_wait_releases_exact_hold_when_snapshot_read_fails(monkeypatch):
    import sqlite3

    released = []

    class FakeRepository:
        def recover_stale_wait_holds(self, **_kwargs):
            return 0

        def hold_for_wait(self, delegation_id, session_key, token, **_kwargs):
            return {"status": "held", "run_id": "run-bound"}

        def release_wait_hold(self, delegation_id, session_key, token, **kwargs):
            released.append((delegation_id, session_key, token, kwargs.get("run_id")))
            return {"status": "released"}

    fake_repository = FakeRepository()
    monkeypatch.setattr(ad, "_repository", lambda: fake_repository)
    monkeypatch.setattr(
        ad,
        "get_async_delegation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        ad.wait_for_delegation(
            "deleg-wait-read-error",
            session_key="owner",
            timeout_seconds=0,
            run_id="run-bound",
        )
    assert len(released) == 1
    assert released[0][0:2] == ("deleg-wait-read-error", "owner")
    assert released[0][3] == "run-bound"


def test_dispatch_returns_immediately_without_blocking():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m"}

    t0 = time.monotonic()
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    elapsed = time.monotonic() - t0

    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: dispatch returned while the runner is still
    # gated (active), so it cannot have waited on the gate. The active_count
    # check is the environment-independent proof; the generous wall-clock
    # bound is a loose sanity backstop, not the primary assertion (a loaded
    # CI runner can be slow but never anywhere near the runner's 5s gate).
    assert ad.active_count() == 1
    assert elapsed < 4.0, f"dispatch blocked {elapsed:.2f}s (gate is 5s)"
    gate.set()


def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "ASYNC DELEGATION COMPLETE",
        "Compute the meaning of life",
        "User is a philosopher",
        "Toolsets: web",
        "The answer is 42.",
        "Status: completed",
        "API calls: 7",
    ]:
        assert needle in text, f"missing {needle!r}"


def test_dispatch_rejected_at_capacity():
    ev = threading.Event()

    def blocker():
        ev.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    for i in range(2):
        r = ad.dispatch_async_delegation(
            goal=f"task{i}", context=None, toolsets=None, role="leaf",
            model="m", session_key="", runner=blocker, max_async_children=2,
        )
        assert r["status"] == "dispatched"

    r3 = ad.dispatch_async_delegation(
        goal="task3", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, max_async_children=2,
    )
    assert r3["status"] == "rejected"
    assert "capacity reached" in r3["error"]
    ev.set()


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}
    # No short internal timeout: the blocker holds until interrupt_fn fires.
    # The old ev.wait(timeout=5) made this test a change-detector for CI
    # worker load — on a CPU-starved runner the 5s expired before
    # interrupt_all() ran, the record finalized, and interrupt_all() found
    # nothing running (n == 0). The pytest-level timeout is the real
    # runaway guard.

    def blocker():
        ev.wait(timeout=60)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    r = ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt. Match on THIS
    # delegation's id — straggler 'completed' events from a previous test's
    # workers can finalize after that test's teardown drain and leak into
    # this queue (observed on loaded CI workers).
    evt = _drain_for(r["delegation_id"])
    assert evt is not None
    assert evt["status"] == "interrupted"


def _fast_stale_monitor(monkeypatch, *, idle=0.15, in_tool=0.3, grace=0.15):
    """Shrink the stale-monitor cadence so tests run in milliseconds."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", idle)
    monkeypatch.setattr(ad, "_STALE_IN_TOOL_SECONDS", in_tool)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", grace)


def test_stalled_runner_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation(
        goal="stuck child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        # Frozen progress token: the child never advances an API call.
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["delegation_id"] == res["delegation_id"]
        assert evt["api_calls"] == 0
        assert "stalled" in evt["error"]
        # Interrupt was requested BEFORE force-finalization (grace window).
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    # If the ignored runner eventually returns, it must not enqueue a second
    # completion for a delegation the monitor already finalized.
    assert _drain_one(timeout=0.5) is None


def test_progressing_runner_is_never_stalled(monkeypatch):
    """A child that keeps advancing is left alone no matter how long it runs."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    ticks = {"n": 0}

    def slow_but_alive_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "done", "api_calls": 7}

    def progress_fn():
        # Token advances on every sample — simulates a child making steady
        # API-call progress.
        ticks["n"] += 1
        return (ticks["n"], None), False

    res = ad.dispatch_async_delegation(
        goal="slow child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=slow_but_alive_runner,
        max_async_children=1, progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    # Run well past the (shrunk) idle threshold — several monitor sweeps.
    time.sleep(0.6)
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"
    assert evt["summary"] == "done"


def test_stalling_runner_that_honors_interrupt_keeps_its_result(monkeypatch):
    """Interrupt-responsive children finalize through the NORMAL path.

    The monitor's interrupt gives a wedged-looking child a grace window; if
    the runner returns during it, the real result (partial work, api_calls)
    is delivered instead of a synthetic stalled event.
    """
    _fast_stale_monitor(monkeypatch, grace=5.0)
    interrupted = threading.Event()

    def runner():
        # "Wedged" until interrupted, then unwinds and reports partial work.
        interrupted.wait(timeout=10)
        return {
            "status": "interrupted",
            "summary": "partial work saved",
            "api_calls": 3,
        }

    res = ad.dispatch_async_delegation(
        goal="responsive child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1,
        progress_fn=lambda: ((3, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "interrupted"
    assert evt["summary"] == "partial work saved"
    assert evt["api_calls"] == 3
    assert ad.active_count() == 0


def test_streaming_child_counts_as_alive(monkeypatch):
    """A child mid-stream (api_call_count frozen, last_activity_ts ticking)
    must never be stalled — streamed chunks tick _touch_activity, and the
    progress token includes that timestamp (same liveness signal as the
    compaction inactivity budget, PR #71508)."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    now = {"ts": 1000.0}

    def progress_fn():
        # api_call_count and current_tool frozen (long streaming response in
        # flight), but the activity timestamp advances with every chunk.
        now["ts"] += 1.0
        return ((1, None, now["ts"]),), False

    res = ad.dispatch_async_delegation(
        goal="streaming child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: (gate.wait(timeout=10), {"status": "completed", "summary": "streamed"})[1],
        progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    time.sleep(0.6)  # several sweeps past the shrunk idle threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stalled_event_carries_structured_stall_metadata(monkeypatch):
    """The terminal stalled event must expose machine-readable stall context
    (#51690) — quiet duration, tripped threshold, phase, grace — mirroring
    the sync path's timeout_seconds/timed_out_after_seconds/timeout_phase."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()

    res = ad.dispatch_async_delegation(
        goal="stall metadata", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: ((0, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["status"] == "stalled"
        assert evt["stalled_after_quiet_seconds"] >= 0.3  # in-tool threshold
        assert evt["stall_threshold_seconds"] == ad._STALE_IN_TOOL_SECONDS
        assert evt["stall_phase"] == "in_tool"
        assert evt["stall_grace_seconds"] == ad._STALL_GRACE_SECONDS
    finally:
        gate.set()


def test_list_async_delegations_exposes_live_activity(monkeypatch):
    """list_async_delegations must expose per-child live activity sampled
    from progress_fn plus seconds_since_progress, for /agents UIs (#51690)."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    gate = threading.Event()
    base_ts = time.time() - 12.0

    res = ad.dispatch_async_delegation(
        goal="live listing", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: (((3, "web_search", base_ts),), True),
    )
    try:
        time.sleep(0.1)  # let the monitor stamp _progress_ts at least once
        item = next(
            d for d in ad.list_async_delegations()
            if d["delegation_id"] == res["delegation_id"]
        )
        assert item["status"] == "running"
        assert item["in_tool"] is True
        assert "seconds_since_progress" in item
        (child,) = item["children_activity"]
        assert child["api_calls"] == 3
        assert child["current_tool"] == "web_search"
        assert 10.0 <= child["seconds_since_activity"] <= 20.0
        # Callables and private bookkeeping must never leak.
        assert "progress_fn" not in item
        assert "interrupt_fn" not in item
        assert not any(k.startswith("_") for k in item)
    finally:
        gate.set()


def test_in_tool_stall_uses_higher_threshold(monkeypatch):
    """A frozen child inside a tool gets the in-tool ceiling, not the idle one."""
    _fast_stale_monitor(monkeypatch, idle=0.1, in_tool=10.0, grace=0.1)
    gate = threading.Event()

    def runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "long tool finished"}

    res = ad.dispatch_async_delegation(
        goal="long tool child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=1,
        # Frozen token but in_tool=True — a legitimately slow terminal
        # command / web fetch. Must NOT be stalled at the idle threshold.
        progress_fn=lambda: ((1, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    time.sleep(0.5)  # far past idle threshold, well under in-tool threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_real_process_restart_restores_owned_completion_once(tmp_path):
    """Real-import E2E: a fresh interpreter restores a prior process's result."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import time
from tools import async_delegation as ad
r = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 5
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    delegation_id = first.stdout.strip().splitlines()[-1]

    consumer = r'''
import json
from tools.process_registry import process_registry
evt = process_registry.completion_queue.get_nowait()
print(json.dumps(evt, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    assert evt["delegation_id"] == delegation_id
    assert evt["session_key"] == "owner-session"
    assert evt["parent_session_id"] == "durable-parent"
    assert evt["summary"] == "after restart"

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [sys.executable, "-c", "from tools.process_registry import process_registry; print(process_registry.completion_queue.qsize())"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


def test_submit_failure_removes_durable_running_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _BrokenExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("submit failed")

    monkeypatch.setattr(ad, "_get_executor", lambda _max_workers: _BrokenExecutor())
    result = ad.dispatch_async_delegation(
        goal="never ran", context=None, toolsets=None, role="leaf", model="m",
        session_key="owner", runner=lambda: {},
    )

    assert result["status"] == "rejected"
    assert ad.list_durable_delegations() == []


def test_pending_retention_prunes_delivered_before_undelivered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 0)
    for index, delivery_state in enumerate(("pending", "delivered", "pending")):
        delegation_id = f"deleg_{index}"
        record = {
            "delegation_id": delegation_id,
            "session_key": "owner",
            "origin_ui_session_id": "",
            "parent_session_id": None,
            "dispatched_at": float(index + 1),
        }
        ad._persist_dispatch(record)
        ad._persist_completion(
            {
                "delegation_id": delegation_id,
                "status": "completed",
                "completed_at": float(index + 1),
            },
            {"status": "completed", "summary": delegation_id},
        )
        if delivery_state == "delivered":
            ad.mark_completion_delivered(delegation_id)

    ad._prune_durable_records()

    assert ad.get_durable_delegation("deleg_0") is not None
    assert ad.get_durable_delegation("deleg_1") is None
    assert ad.get_durable_delegation("deleg_2") is not None


def test_recovery_completes_exact_old_run_without_mutating_live_new_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = {
        "delegation_id": "deleg_abandoned", "session_key": "owner",
        "origin_ui_session_id": "", "parent_session_id": None, "dispatched_at": 1.0,
    }
    record["root_subagent_ids"] = ["sa-abandoned", "sa-resumed"]
    repository = ad._repository()
    initial = repository.register_initial_dispatch(
        record, owner_pid=99999999
    )
    repository.transition_attempt(
        initial["attempts"][1]["attempt_id"], {"starting"}, "completed"
    )
    resumed = repository.reserve_resumed_attempt(
        "sa-resumed", owner_pid=os.getpid(), physical_worker_id="worker-live"
    )
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    event = restored.get_nowait()
    assert (event["run_id"], event["status"]) == (initial["run_id"], "unknown")
    assert prepare_notification_delivery(event) == "deliver"
    assert ad.inspect_async_delivery_claim(
        "deleg_abandoned",
        event["_async_delivery_claim_token"],
        run_id=initial["run_id"],
    ) == "current"
    assert finish_notification_delivery(event, delivered=True)
    assert repository.inspect_delivery("deleg_abandoned", initial["run_id"])["completed_at"]
    assert repository.inspect_delivery(
        "deleg_abandoned", initial["run_id"]
    )["delivery_state"] == "delivered"
    live = repository.inspect_delivery("deleg_abandoned", resumed["run_id"])
    assert live["completed_at"] is None
    assert repository.snapshot("deleg_abandoned")["children"]["sa-resumed"]["status"] == "starting"
    assert ad.recover_abandoned_delegations() == 0


def test_durable_delivery_claim_is_exclusive_and_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = {
        "delegation_id": "deleg_claim", "session_key": "owner",
        "origin_ui_session_id": "", "parent_session_id": None,
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)
    ad._persist_completion(
        {"delegation_id": "deleg_claim", "status": "completed", "completed_at": 2.0},
        {"status": "completed", "summary": "done"},
    )

    assert ad.claim_completion_delivery("deleg_claim", "consumer-a")
    assert not ad.claim_completion_delivery("deleg_claim", "consumer-b")
    assert ad.release_completion_delivery("deleg_claim", "consumer-a")
    assert ad.claim_completion_delivery("deleg_claim", "consumer-b")
    assert ad.complete_completion_delivery("deleg_claim", "consumer-b")
    assert not ad.claim_completion_delivery("deleg_claim", "consumer-c")
    assert ad.get_durable_delegation("deleg_claim")["delivery_state"] == "delivered"


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_binds_exact_run_and_attempt_before_runner(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess-bind"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    child = MagicMock()
    child._delegate_role = "leaf"
    child._subagent_id = "sa-bound"
    observed = {}

    def run_bound(task_index, goal, child=None, parent_agent=None, **_kwargs):
        observed["run_id"] = getattr(child, "_delegation_run_id", None)
        observed["attempt_id"] = getattr(child, "_delegation_attempt_id", None)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": goal,
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(dt, "_run_single_child", run_bound)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *_a, **_k: creds)

    payload = json.loads(
        dt.delegate_task(goal="bind exact ids", background=True, parent_agent=parent)
    )
    event = _drain_for(payload["delegation_id"])
    assert event is not None
    snapshot = ad.get_durable_delegation(payload["delegation_id"])
    durable_child = snapshot["children"]["sa-bound"]
    assert observed == {
        "run_id": snapshot["run_id"],
        "attempt_id": durable_child["attempt_id"],
    }


def test_background_child_persists_reconstruction_metadata_before_execution(
    tmp_path, monkeypatch
):
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess-reconstruct"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    parent._current_task_id = None

    child = MagicMock()
    child._delegate_role = "leaf"
    child._delegate_depth = 1
    child._subagent_id = "sa-reconstruct"
    child._parent_subagent_id = None
    child._credential_pool = None
    child.tool_progress_callback = None
    child.model = "model-safe"
    child._delegation_session_ref = {"session_id": "child-session"}
    child._delegation_runtime_metadata = {
        "child_session_id": "child-session",
        "provider": "provider-safe",
        "model": "model-safe",
        "enabled_toolsets": ["terminal"],
        "disabled_toolsets": ["delegation"],
        "workdir": "/workspace/repo",
        "max_iterations": 8,
        "fallback_routes": [
            {"provider": "fallback-safe", "model": "fallback-model"}
        ],
    }
    entered = threading.Event()
    release = threading.Event()

    def run_conversation(user_message, task_id=None, stream_callback=None):
        entered.set()
        assert release.wait(5)
        return {"final_response": "done", "completed": True, "api_calls": 1}

    child.run_conversation.side_effect = run_conversation
    creds = {
        "model": "model-safe", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *_a, **_k: creds)

    payload = json.loads(
        dt.delegate_task(
            goal="persist reconstruction", background=True, parent_agent=parent
        )
    )
    try:
        assert entered.wait(5)
        snapshot = ad.get_durable_delegation(payload["delegation_id"])
        durable_child = snapshot["children"]["sa-reconstruct"]
        assert durable_child["child_session_id"] == "child-session"
        assert durable_child["provider"] == "provider-safe"
        assert durable_child["enabled_toolsets"] == ["terminal"]
        assert durable_child["workdir"] == "/workspace/repo"
        assert durable_child["fallback_routes"] == [
            {"provider": "fallback-safe", "model": "fallback-model"}
        ]
        assert child._delegation_session_ref == {
            "session_id": "child-session",
            "run_id": snapshot["run_id"],
            "attempt_id": durable_child["attempt_id"],
        }
    finally:
        release.set()
    assert _drain_for(payload["delegation_id"]) is not None


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert "ASYNC DELEGATION COMPLETE" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt


def test_single_task_truncation_banner_when_max_iterations():
    """A single async subagent that hit its iteration cap (exit_reason=
    max_iterations) must surface a TRUNCATED marker in the formatted result,
    even though status stays 'completed' (a summary exists)."""
    evt = _make_async_evt(
        status="completed",
        summary="Did part of the work then ran out of budget.",
        exit_reason="max_iterations",
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    assert "max_iterations" in text
    # The summary is still shown, just flagged.
    assert "Did part of the work" in text


def test_single_task_no_banner_when_clean():
    """A cleanly-finished subagent must NOT get a truncation banner."""
    evt = _make_async_evt(status="completed", summary="All done.", exit_reason="completed")
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" not in text


def test_batch_truncation_banner_marks_only_truncated_task():
    """In a batch, only the task that hit max_iterations gets the TRUNCATED
    marker; a clean sibling keeps the normal check icon."""
    evt = _make_async_evt(
        is_batch=True,
        goals=["clean task", "truncated task"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "finished cleanly",
                "api_calls": 5,
                "exit_reason": "completed",
                "truncated": False,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": "cut off mid-work",
                "api_calls": 250,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    # The clean task's summary and the truncated one's both render...
    assert "finished cleanly" in text
    assert "cut off mid-work" in text
    # ...but the banner is tied to the truncated task, not the clean one.
    trunc_pos = text.index("cut off mid-work")
    clean_pos = text.index("finished cleanly")
    banner_pos = text.index("TRUNCATED")
    # The header banner for task 2 appears after task 1's summary.
    assert banner_pos > clean_pos

