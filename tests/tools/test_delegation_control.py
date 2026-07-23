"""Focused tests for model-facing delegation lifecycle control."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import async_delegation as ad
from tools import delegate_tool as dt
from tools.process_registry import process_registry


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


def test_stale_real_registration_and_archive_cannot_mutate_new_attempt():
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

    stale_record = {
        "subagent_id": "sa-exact",
        "delegation_attempt_id": stale_attempt_id,
        "delegation_run_id": initial["run_id"],
        "parent_id": None,
        "goal": "stale callback",
        "status": "running",
        "started_at": time.time(),
        "agent": MagicMock(),
    }
    dt._register_subagent(stale_record)
    stale_record["status"] = "error"
    dt._unregister_subagent("sa-exact")

    current = repository.snapshot("deleg-exact-attempt")["children"]["sa-exact"]
    assert current["attempt_id"] == resumed["attempt_id"]
    assert current["run_id"] == resumed["run_id"]
    assert current["status"] == "starting"
    assert current.get("goal") == "original"


def test_interrupt_callback_failure_rolls_back_pending_marker_for_retry():
    release = threading.Event()
    successful_calls = []

    def failed_interrupt():
        raise RuntimeError("signal failed")

    dispatched = _dispatch(
        lambda: (release.wait(2), {"status": "completed", "summary": "done"})[1],
        roots=["sa-interrupt-retry"],
        interrupt_fn=failed_interrupt,
    )
    delegation_id = dispatched["delegation_id"]
    try:
        first = ad.interrupt_async_delegation(
            delegation_id, session_key="owner", reason="first"
        )
        assert first["status"] == "interrupt_failed"
        snapshot = ad.get_durable_delegation(delegation_id)
        assert snapshot["state"] == "running"
        assert snapshot["interrupt_requests"] == {}

        with ad._records_lock:
            ad._records[delegation_id]["interrupt_fn"] = lambda: successful_calls.append(True)
        second = ad.interrupt_async_delegation(
            delegation_id, session_key="owner", reason="second"
        )
        assert second["status"] == "interrupt_requested"
        assert successful_calls == [True]
        assert ad.take_pending_subagent_interrupt("sa-interrupt-retry") == (True, "second")
        assert ad.take_pending_subagent_interrupt("sa-interrupt-retry") == (False, "")
    finally:
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
