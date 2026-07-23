"""Focused tests for model-facing delegation lifecycle control."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import async_delegation as ad
from tools import delegate_tool as dt
from tools.process_registry import process_registry

def _observe_interrupt(home, delegation_id, output):
    os.environ["HERMES_HOME"] = home
    output.put(ad.interrupt_async_delegation(delegation_id, session_key="owner", reason="observer"))

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    with dt._active_subagents_lock:
        dt._active_subagents.clear()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    with dt._active_subagents_lock:
        dt._active_subagents.clear()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

def _dispatch(runner, *, session_key="owner", roots=None, interrupt_fn=None):
    return ad.dispatch_async_delegation(
        goal="controlled task",
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
        max_async_children=4,
    )

def _wait_terminal(delegation_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = ad.get_durable_delegation(delegation_id)
        if row and row["state"] not in ad._ACTIVE_STATES:
            return row
        time.sleep(0.005)
    raise AssertionError("delegation did not become terminal")

def _interrupt_twice(monkeypatch, delegation_id, while_running=None):
    barrier = threading.Barrier(2)
    real_get = ad.get_async_delegation
    synchronized = threading.local()

    def synchronized_initial_snapshot(*args, **kwargs):
        snapshot = real_get(*args, **kwargs)
        if not getattr(synchronized, "done", False):
            synchronized.done = True
            barrier.wait(timeout=2)
        return snapshot

    monkeypatch.setattr(ad, "get_async_delegation", synchronized_initial_snapshot)
    outcomes, returned = [], threading.Event()

    def request(reason):
        outcomes.append(ad.interrupt_async_delegation(
            delegation_id, session_key="owner", reason=reason
        ))
        returned.set()

    threads = [threading.Thread(target=request, args=(reason,)) for reason in ("one", "two")]
    for thread in threads:
        thread.start()
    if while_running:
        while_running(outcomes, returned)
    for thread in threads:
        thread.join(4)
        assert not thread.is_alive()
    return outcomes

def test_tool_schema_and_runtime_validation_are_strict():
    import tools.delegation_control  # noqa: F401
    from tools.delegation_control import _handle_delegation_args, delegation_control
    from tools.registry import registry

    definitions = registry.get_definitions({"delegation"})
    control = next(item for item in definitions if item["function"]["name"] == "delegation")
    assert control["function"]["parameters"]["additionalProperties"] is False

    cases = [
        _handle_delegation_args({"action": "list", "unexpected": True}),
        _handle_delegation_args(
            {"action": "interrupt", "delegation_id": "d", "cascade": "false"}
        ),
        _handle_delegation_args(
            {"action": "wait", "delegation_id": "d", "timeout_seconds": "1"}
        ),
        _handle_delegation_args(
            {"action": "tail", "delegation_id": "d", "limit": 1.5}
        ),
        delegation_control(action="tail", delegation_id="d", limit=65),
    ]
    for raw in cases:
        payload = json.loads(raw)
        assert payload["status"] == "invalid_arguments"
        assert payload["action"]
        assert payload["error"]

def test_list_and_status_hide_foreign_session_like_unknown():
    release = threading.Event()
    dispatched = _dispatch(
        lambda: (release.wait(2), {"status": "completed", "summary": "done"})[1],
        roots=["sa-owner"],
    )
    from tools.delegation_control import delegation_control

    try:
        own = json.loads(delegation_control(action="list", session_key="owner"))
        foreign = json.loads(delegation_control(action="list", session_key="foreign"))
        hidden = json.loads(
            delegation_control(
                action="status",
                delegation_id=dispatched["delegation_id"],
                session_key="foreign",
            )
        )
        unknown = json.loads(
            delegation_control(
                action="status", delegation_id="deleg_unknown", session_key="foreign"
            )
        )
        assert [item["delegation_id"] for item in own["delegations"]] == [
            dispatched["delegation_id"]
        ]
        assert foreign["delegations"] == []
        assert hidden == unknown | {"delegation_id": dispatched["delegation_id"]}
    finally:
        release.set()

def test_tail_filters_reasoning_and_redacts_split_stream_secret():
    release = threading.Event()
    dispatched = _dispatch(
        lambda: (release.wait(2), {"status": "completed", "summary": "done"})[1],
        roots=["sa-tail"],
    )
    agent = MagicMock()
    dt._register_subagent(
        {
            "subagent_id": "sa-tail",
            "parent_id": None,
            "depth": 0,
            "goal": "controlled task",
            "status": "running",
            "started_at": time.time(),
            "agent": agent,
        }
    )
    callback = dt._build_child_progress_callback(
        0,
        "controlled task",
        MagicMock(_delegate_spinner=None, tool_progress_callback=None),
        subagent_id="sa-tail",
    )
    assert callback is not None
    callback("tool.started", tool_name="terminal", args={"command": "pytest -q"})
    secret = "Author" + "ization: " + "Bear" + "er " + ("s" * 48)
    callback("subagent.text", preview=secret[:9])
    callback("subagent.text", preview=secret[9:])
    callback("reasoning.available", preview="hidden chain of thought")

    from tools.delegation_control import delegation_control

    try:
        payload = json.loads(
            delegation_control(
                action="tail",
                delegation_id=dispatched["delegation_id"],
                session_key="owner",
                limit=8,
            )
        )
        encoded = json.dumps(payload)
        assert "pytest -q" in encoded
        assert "hidden chain of thought" not in encoded
        assert "s" * 48 not in encoded
        assert payload["subagents"][0]["events"][0]["type"] == "tool.started"
    finally:
        release.set()
        dt._unregister_subagent("sa-tail")

def test_starting_child_interrupt_is_queued_then_applied_once():
    runner_started = threading.Event()
    allow_registration = threading.Event()
    allow_finish = threading.Event()
    registered = threading.Event()
    agent = MagicMock()

    def runner():
        runner_started.set()
        assert allow_registration.wait(2)
        dt._register_subagent(
            {
                "subagent_id": "sa-starting",
                "parent_id": None,
                "depth": 0,
                "goal": "controlled task",
                "status": "running",
                "started_at": time.time(),
                "agent": agent,
            }
        )
        registered.set()
        assert allow_finish.wait(2)
        dt._unregister_subagent("sa-starting")
        return {"status": "interrupted", "summary": "stopped"}

    dispatched = _dispatch(runner, roots=["sa-starting"])
    assert runner_started.wait(2)
    from tools.delegation_control import delegation_control

    try:
        payload = json.loads(
            delegation_control(
                action="interrupt",
                delegation_id=dispatched["delegation_id"],
                subagent_id="sa-starting",
                session_key="owner",
                reason="stop before startup",
            )
        )
        assert payload["status"] == "interrupt_requested"
        agent.interrupt.assert_not_called()
        allow_registration.set()
        assert registered.wait(2)
        agent.interrupt.assert_called_once_with("stop before startup")
    finally:
        allow_registration.set()
        allow_finish.set()

@pytest.mark.parametrize("supplied_attempt", ["stale", None, "", 17, "attempt_missing"])
def test_invalid_attempt_registration_and_archive_fail_closed(supplied_attempt):
    repository = ad._repository()
    initial = repository.register_initial_dispatch(
        {
            "delegation_id": "deleg-exact-attempt",
            "session_key": "owner",
            "origin_ui_session_id": "ui-owner",
            "parent_session_id": "parent-owner",
            "dispatched_at": 1.0,
            "goal": "original",
            "root_subagent_ids": ["sa-exact"],
        }
    )
    stale_attempt_id = initial["attempts"][0]["attempt_id"]
    assert repository.transition_attempt(
        stale_attempt_id, {"starting"}, "completed"
    )["status"] == "updated"
    resumed = repository.reserve_resumed_attempt(
        "sa-exact", physical_worker_id="worker-current"
    )

    invalid_record = {
        "subagent_id": "sa-exact",
        "delegation_attempt_id": (
            stale_attempt_id if supplied_attempt == "stale" else supplied_attempt
        ),
        "delegation_run_id": initial["run_id"],
        "parent_id": None,
        "goal": "invalid callback",
        "status": "running",
        "started_at": time.time(),
        "agent": MagicMock(),
    }
    dt._register_subagent(invalid_record)
    invalid_record["status"] = "error"
    dt._unregister_subagent("sa-exact")

    current = repository.snapshot("deleg-exact-attempt")["children"]["sa-exact"]
    assert current["attempt_id"] == resumed["attempt_id"]
    assert current["run_id"] == resumed["run_id"]
    assert current["status"] == "starting"
    assert current.get("goal") == "original"


def test_stale_attempt_callbacks_cannot_replace_mutate_or_remove_live_resume():
    repository = ad._repository()
    initial = repository.register_initial_dispatch(
        {
            "delegation_id": "deleg-live-resume",
            "session_key": "owner",
            "dispatched_at": 1.0,
            "root_subagent_ids": ["sa-resumed"],
        }
    )
    old_attempt = initial["attempts"][0]["attempt_id"]
    assert repository.transition_attempt(
        old_attempt, {"starting"}, "completed"
    )["status"] == "updated"
    resumed = repository.reserve_resumed_attempt(
        "sa-resumed", physical_worker_id="sa-resumed"
    )
    current_record = {
        "subagent_id": "sa-resumed",
        "delegation_attempt_id": resumed["attempt_id"],
        "delegation_run_id": resumed["run_id"],
        "status": "running",
        "events": [],
        "assistant_text_tail": "current",
        "agent": MagicMock(),
    }
    dt._register_subagent(current_record)

    stale_record = {
        "subagent_id": "sa-resumed",
        "delegation_attempt_id": old_attempt,
        "delegation_run_id": initial["run_id"],
        "status": "running",
        "agent": MagicMock(),
    }
    dt._register_subagent(stale_record)
    dt._append_live_event(
        "sa-resumed", {"type": "tool.started"}, attempt_id=old_attempt
    )
    dt._append_live_text("sa-resumed", "stale", attempt_id=old_attempt)
    dt._unregister_subagent("sa-resumed", old_attempt)

    with dt._active_subagents_lock:
        assert dt._active_subagents["sa-resumed"] is current_record
        assert current_record["events"] == []
        assert current_record["assistant_text_tail"] == "current"


def test_nested_child_without_attempt_id_allocates_propagates_and_archives_exact_id():
    repository = ad._repository()
    repository.register_initial_dispatch(
        {
            "delegation_id": "deleg-nested-attempt",
            "session_key": "owner",
            "dispatched_at": 1.0,
            "root_subagent_ids": ["sa-parent"],
        }
    )
    parent = dict(
        subagent_id="sa-parent", parent_id=None, status="running", agent=MagicMock()
    )
    child = {
        "subagent_id": "sa-child",
        "parent_id": "sa-parent",
        "goal": "nested",
        "status": "running",
        "agent": MagicMock(),
    }
    dt._register_subagent(parent)
    dt._register_subagent(child)
    attempt_id = child.get("delegation_attempt_id")
    assert isinstance(attempt_id, str) and attempt_id
    child.update(status="completed", tool_count=3)
    dt._unregister_subagent("sa-child")
    archived = repository.snapshot("deleg-nested-attempt")["children"]["sa-child"]
    assert (archived["attempt_id"], archived["status"], archived["tool_count"]) == (
        attempt_id, "completed", 3
    )

def test_cross_process_interrupt_cannot_claim_owner_callback_success():
    release = threading.Event()
    callback_entered, release_callback = threading.Event(), threading.Event()
    successful_calls = []

    def failed_interrupt():
        callback_entered.set()
        assert release_callback.wait(5)
        raise RuntimeError("signal failed")

    dispatched = _dispatch(
        lambda: (release.wait(10), {"status": "completed", "summary": "done"})[1],
        roots=["sa-interrupt-retry"],
        interrupt_fn=failed_interrupt,
    )
    delegation_id = dispatched["delegation_id"]
    try:
        owner_outcome = []
        owner = threading.Thread(target=lambda: owner_outcome.append(
            ad.interrupt_async_delegation(delegation_id, session_key="owner", reason="first")
        ))
        owner.start()
        assert callback_entered.wait(2)
        ctx = multiprocessing.get_context("spawn"); output = ctx.Queue()
        observer = ctx.Process(target=_observe_interrupt, args=(os.environ["HERMES_HOME"], delegation_id, output))
        observer.start()
        observer.join(10)
        assert observer.exitcode == 0
        assert output.get(timeout=1)["status"] == "interrupt_unavailable"
        release_callback.set()
        owner.join(2)
        assert owner_outcome[0]["status"] == "interrupt_failed"
        snapshot = ad.get_durable_delegation(delegation_id)
        assert (snapshot["state"], snapshot["interrupt_requests"]) == ("running", {})

        with ad._records_lock:
            ad._records[delegation_id]["interrupt_fn"] = lambda: successful_calls.append(True)
        assert ad.interrupt_async_delegation(
            delegation_id, session_key="owner", reason="second"
        )["status"] == "interrupt_requested"
        assert successful_calls == [True]
        assert ad.take_pending_subagent_interrupt("sa-interrupt-retry") == (True, "second")
    finally:
        release_callback.set()
        release.set()

def test_concurrent_interrupt_callbacks_serialize_failure_then_success(monkeypatch):
    release = threading.Event()
    callback_rendezvous, successful_callback = threading.Barrier(2), threading.Event()
    callback_guard, counts = threading.Lock(), [0, 0, 0]  # calls, active, peak

    def interrupt_callback():
        with callback_guard:
            counts[0] += 1
            call_number = counts[0]
            counts[1] += 1
            counts[2] = max(counts[2], counts[1])
        try:
            try:
                callback_rendezvous.wait(timeout=0.5)
                if call_number == 1:
                    assert successful_callback.wait(2)
            except threading.BrokenBarrierError:
                pass
            if call_number == 1:
                raise RuntimeError("first signal failed")
            successful_callback.set()
        finally:
            with callback_guard:
                counts[1] -= 1

    dispatched = _dispatch(
        lambda: (release.wait(10), {"status": "completed", "summary": "done"})[1],
        roots=["sa-concurrent-interrupt"],
        interrupt_fn=interrupt_callback,
    )
    delegation_id = dispatched["delegation_id"]
    try:
        outcomes = _interrupt_twice(monkeypatch, delegation_id)
        statuses = sorted(item["status"] for item in outcomes)
        assert statuses == ["interrupt_failed", "interrupt_requested"]
        assert counts == [2, 0, 1]
        snapshot = ad.get_durable_delegation(delegation_id)
        assert snapshot["state"] == "interrupt_requested"
        assert list(snapshot["interrupt_requests"]) == ["sa-concurrent-interrupt"]
        pending_reason = snapshot["interrupt_requests"]["sa-concurrent-interrupt"]
        assert ad.take_pending_subagent_interrupt("sa-concurrent-interrupt") == (
            True, pending_reason
        )
        assert ad.take_pending_subagent_interrupt("sa-concurrent-interrupt") == (False, "")
    finally:
        release.set()

def test_concurrent_idempotent_interrupt_waits_for_callback_owner(monkeypatch):
    release = threading.Event()
    callback_entered, release_callback = threading.Event(), threading.Event()
    callback_calls = []

    def successful_interrupt():
        callback_calls.append(True)
        callback_entered.set()
        assert release_callback.wait(2)

    dispatched = _dispatch(
        lambda: (release.wait(10), {"status": "completed", "summary": "done"})[1],
        roots=["sa-idempotent-interrupt"],
        interrupt_fn=successful_interrupt,
    )
    delegation_id = dispatched["delegation_id"]

    def release_owner(outcomes, returned):
        assert callback_entered.wait(2)
        assert not returned.wait(0.2)
        assert outcomes == []
        release_callback.set()

    try:
        outcomes = _interrupt_twice(monkeypatch, delegation_id, release_owner)
        assert [item["status"] for item in outcomes] == ["interrupt_requested"] * 2
        assert callback_calls == [True]
        assert ad.take_pending_subagent_interrupt("sa-idempotent-interrupt")[0] is True
        assert ad.take_pending_subagent_interrupt("sa-idempotent-interrupt") == (False, "")
    finally:
        release_callback.set()
        release.set()

def test_wait_consumes_result_and_registry_auto_delivery_drops_it():
    dispatched = _dispatch(lambda: {"status": "completed", "summary": "one owner"})
    _wait_terminal(dispatched["delegation_id"])
    from tools.delegation_control import delegation_control

    waited = json.loads(
        delegation_control(
            action="wait",
            delegation_id=dispatched["delegation_id"],
            session_key="owner",
            timeout_seconds=1,
        )
    )
    assert waited["claimed_delivery"] is True
    assert waited["result"]["summary"] == "one owner"
    assert process_registry.drain_notifications(session_key="owner") == []

def test_formatter_failure_releases_claim_and_defers_without_spin(monkeypatch):
    dispatched = _dispatch(lambda: {"status": "completed", "summary": "retry"})
    _wait_terminal(dispatched["delegation_id"])
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification",
        lambda _event: (_ for _ in ()).throw(RuntimeError("format failed")),
    )

    assert process_registry.drain_notifications(session_key="owner") == []
    row = ad.get_durable_delegation(dispatched["delegation_id"])
    assert row["delivery_state"] == "pending"
    assert process_registry.completion_queue.qsize() == 1

def test_cli_style_commit_failure_retries_bookkeeping_only(monkeypatch):
    dispatched = _dispatch(lambda: {"status": "completed", "summary": "retry ack"})
    _wait_terminal(dispatched["delegation_id"])
    drained = process_registry.drain_notifications(session_key="owner")
    assert len(drained) == 1
    event, _text = drained[0]

    real_finish = ad.finish_async_delivery
    monkeypatch.setattr(
        ad,
        "finish_async_delivery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db busy")),
    )
    assert ad.complete_event_delivery(event, event["_async_delivery_claim_token"]) is False
    assert process_registry.completion_queue.qsize() == 1

    monkeypatch.setattr(ad, "finish_async_delivery", real_finish)
    # The accepted marker makes the next drain commit-only and suppresses a
    # second synthetic message.
    assert process_registry.drain_notifications(session_key="owner") == []
    row = ad.get_durable_delegation(dispatched["delegation_id"])
    assert row["delivery_state"] == "delivered"

@pytest.mark.asyncio
async def test_gateway_ack_failure_retries_without_duplicate_injection(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 32
    runner._inject_watch_notification = AsyncMock(return_value=True)

    monkeypatch.setattr(
        ad,
        "get_durable_delegation",
        lambda *_args, **_kwargs: {"delegation_id": "deleg-ack"},
    )
    monkeypatch.setattr(ad, "claim_completion_delivery", lambda *_args, **_kwargs: True)
    acknowledgements = iter([False, True])
    monkeypatch.setattr(
        ad, "complete_completion_delivery", lambda *_args, **_kwargs: next(acknowledgements)
    )
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-ack",
        "completed_at": 1.0,
    }

    first = await runner._deliver_completion_notification("result", event)
    second = await runner._deliver_completion_notification("result", event)
    assert first is False
    assert second is True
    runner._inject_watch_notification.assert_awaited_once_with("result", event)
    assert "_gateway_async_delivery_claim" not in event
    assert "_gateway_async_delivery_accepted" not in event
