"""Focused regressions for the six delegation lifecycle agreements.

These tests use the public dispatch/control/schema entry points and retain the
canonical durable row as the evidence source.  Docker materialization remains
in the optional integration suite because this checkout has no Docker socket.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from agent.delegation_policy import (
    AccessMode,
    DelegationSessionPolicy,
    ExecutionProfile,
)
from tools import async_delegation as ad
from tools import delegate_tool as dt
from tools.delegation_control import delegation_control
from tools.delegation_scope import (
    admit_trusted_run_execution,
    deserialize_delegation_authority,
    resolve_invocation_scope,
    serialize_delegation_authority,
)
from tools.process_registry import prepare_notification_delivery, process_registry


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _profile_policy(*, visible_objects=()):
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example/isolated@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets=frozenset({"terminal", "delegation"}),
        network="none",
    )
    return DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={profile.name},
        profile_snapshots={profile.name: profile},
        visible_objects=visible_objects,
        protected_prefixes=(),
    )


def _drain_for(delegation_id: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            event = process_registry.completion_queue.get_nowait()
            if event.get("delegation_id") == delegation_id:
                return event
        time.sleep(0.005)
    raise AssertionError(f"completion for {delegation_id} did not arrive")


def test_exported_spawn_receipt_has_logical_children_and_exact_run_without_physical_identity():
    release = threading.Event()

    def runner():
        release.wait(2)
        return {
            "results": [
                {"status": "completed", "summary": "one"},
                {"status": "completed", "summary": "two"},
            ]
        }

    result = ad.dispatch_async_delegation_batch(
        goals=["first", "second"],
        context=None,
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key="owner",
        runner=runner,
        max_async_children=2,
    )

    assert result["status"] == "dispatched"
    assert result["run_id"]
    assert len(result["subagent_ids"]) == 2
    assert all(
        item.startswith(f"sa-{result['delegation_id']}-")
        for item in result["subagent_ids"]
    )
    assert [item["subagent_id"] for item in result["subagents"]] == result["subagent_ids"]
    assert all(item["run_id"] == result["run_id"] for item in result["subagents"])
    assert all("physical_worker_id" not in item for item in result["subagents"])
    assert all("child_session_id" not in item for item in result["subagents"])

    release.set()
    assert _drain_for(result["delegation_id"])["run_id"] == result["run_id"]


def test_wait_exposes_final_result_once_and_queue_copy_is_dropped_by_exact_run():
    result = ad.dispatch_async_delegation(
        goal="wait for final result",
        context=None,
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key="owner",
        runner=lambda: {"status": "completed", "summary": "canonical final"},
        max_async_children=1,
    )
    event = _drain_for(result["delegation_id"])

    waited = ad.wait_for_delegation(
        result["delegation_id"], session_key="owner", timeout_seconds=0,
        run_id=result["run_id"],
    )
    assert waited["run_id"] == result["run_id"]
    assert waited["result"]["summary"] == "canonical final"
    assert waited["result_ready"] is True
    assert waited["delivery_consumed"] is True
    assert waited["claimed_delivery"] is True

    # The producer event is still a queue item, but the exact consumed run is
    # no longer eligible for async injection.
    assert prepare_notification_delivery(event) == "drop"
    assert ad._repository().inspect_delivery(
        result["delegation_id"], result["run_id"]
    )["delivery_state"] == "consumed"


def test_wait_fifo_and_exact_run_leave_sibling_results_deliverable():
    repository = ad._repository()
    delegation_id = "deleg-fifo-exact"
    created = repository.register_initial_dispatch({
        "delegation_id": delegation_id,
        "session_key": "owner",
        "dispatched_at": time.time(),
        "root_subagent_ids": ["logical-fifo"],
    })
    first_run = created["run_id"]
    first_attempt = created["attempts"][0]["attempt_id"]
    repository.transition_attempt(first_attempt, {"starting"}, "completed")
    repository.complete_run(
        first_run,
        {"status": "completed", "completed_at": time.time()},
        {"status": "completed", "summary": "first"},
    )
    resumed = repository.reserve_resumed_attempt("logical-fifo")
    second_run = resumed["run_id"]
    repository.transition_attempt(resumed["attempt_id"], {"starting"}, "completed")
    repository.complete_run(
        second_run,
        {"status": "completed", "completed_at": time.time()},
        {"status": "completed", "summary": "second"},
    )

    exact = ad.wait_for_delegation(
        delegation_id,
        session_key="owner",
        timeout_seconds=0,
        run_id=second_run,
    )
    assert exact["run_id"] == second_run
    assert exact["result"]["summary"] == "second"
    assert repository.inspect_delivery(delegation_id, first_run)["delivery_state"] == "pending"

    fifo = ad.wait_for_delegation(
        delegation_id, session_key="owner", timeout_seconds=0
    )
    assert fifo["run_id"] == first_run
    assert fifo["result"]["summary"] == "first"


def test_wait_does_not_consume_before_run_result_publication():
    repository = ad._repository()
    delegation_id = "deleg-finalization-gap"
    created = repository.register_initial_dispatch({
        "delegation_id": delegation_id,
        "session_key": "owner",
        "dispatched_at": time.time(),
        "root_subagent_ids": ["logical-finalization"],
    })
    run_id = created["run_id"]
    attempt_id = created["attempts"][0]["attempt_id"]
    repository.transition_attempt(attempt_id, {"starting"}, "completed")
    assert repository.snapshot(delegation_id)["worker_status"] == "finalizing"

    outcome = {}

    def wait_for_publication():
        outcome.update(
            ad.wait_for_delegation(
                delegation_id,
                session_key="owner",
                timeout_seconds=2,
                run_id=run_id,
            )
        )

    waiter = threading.Thread(target=wait_for_publication)
    waiter.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        row = repository.inspect_delivery(delegation_id, run_id)
        if row.get("delivery_state") == "held_by_wait":
            break
        time.sleep(0.005)
    else:
        raise AssertionError("wait did not establish its exact hold")

    repository.complete_run(
        run_id,
        {"status": "completed", "completed_at": time.time()},
        {"status": "completed", "summary": "published after child terminal"},
    )
    waiter.join(2)
    assert not waiter.is_alive()
    assert outcome["result"]["summary"] == "published after child terminal"
    assert outcome["result_ready"] is True
    assert outcome["claimed_delivery"] is True
    assert repository.inspect_delivery(delegation_id, run_id)["delivery_state"] == "consumed"


def test_default_control_views_are_compact_and_detail_tail_are_explicit():
    goal = "G" * 1000
    result = ad.dispatch_async_delegation(
        goal=goal,
        context="context",
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key="owner",
        root_subagent_ids=["logical-compact"],
        runner=lambda: {"status": "completed", "summary": "full durable result"},
        max_async_children=1,
    )
    _drain_for(result["delegation_id"])

    compact = json.loads(
        delegation_control(
            action="status",
            delegation_id=result["delegation_id"],
            session_key="owner",
        )
    )
    assert "goal" not in compact
    assert len(compact["goal_preview"]) < len(goal)
    assert "authority_audit" not in compact["subagents"][0]
    assert "assistant_text_tail" not in compact["subagents"][0]
    assert compact["result_ready"] is True

    detailed = json.loads(
        delegation_control(
            action="status",
            delegation_id=result["delegation_id"],
            session_key="owner",
            detail=True,
        )
    )
    assert detailed["goal"] == goal
    assert ad.get_async_delegation(result["delegation_id"], session_key="owner")[
        "result"
    ]["summary"] == "full durable result"


def test_list_is_concise_inventory_without_dropping_durable_child_history(monkeypatch):
    from tools import delegation_control as control_module

    huge_goal = "goal-" + ("x" * 10000)
    huge_tail = "tail-" + ("y" * 10000)
    record = {
        "delegation_id": "deleg-large-list",
        "session_key": "owner",
        "goal": huge_goal,
        "worker_status": "running",
        "state": "running",
        "result": None,
        "root_subagent_ids": ["logical-large"],
        "children": {
            "logical-large": {
                "subagent_id": "logical-large",
                "goal": huge_goal,
                "status": "running",
                "live": False,
                "assistant_text_tail": huge_tail,
                "events": [{"type": "tool.completed", "result": huge_tail}],
                "steers": [{"status": "injected", "message": huge_tail} for _ in range(20)],
                "authority_audit": {"visible_objects": [{"backing": {"identity": huge_tail}}]},
            }
        },
    }
    monkeypatch.setattr(control_module._async, "list_durable_delegations", lambda **_: [record])
    monkeypatch.setattr(control_module._async, "pending_subagent_interrupt_ids", lambda *a, **k: set())
    monkeypatch.setattr(control_module._delegate, "list_active_subagents", lambda: [])

    compact = delegation_control(action="list", session_key="owner")
    assert len(compact) < 3000
    assert huge_tail not in compact
    assert "authority_audit" not in compact
    assert "goal" not in json.loads(compact)["delegations"][0]

    monkeypatch.setattr(
        control_module._async,
        "list_durable_delegations",
        lambda **_: [dict(record, delegation_id=f"deleg-large-{index}") for index in range(71)],
    )
    assert len(delegation_control(action="list", session_key="owner")) < 50000

    detailed = delegation_control(action="list", session_key="owner", detail=True)
    assert "authority_audit" in detailed

    monkeypatch.setattr(control_module._async, "get_async_delegation", lambda *a, **k: record)
    tail = delegation_control(
        action="tail",
        delegation_id=record["delegation_id"],
        session_key="owner",
        detail=True,
    )
    assert huge_tail in tail


def test_control_schema_omits_spawn_only_profile_requirement_but_spawn_runtime_still_requires_it():
    import model_tools

    policy = _profile_policy()
    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["delegation"], quiet_mode=True, delegation_policy=policy
    )
    schema = next(
        item["function"] for item in definitions
        if item["function"]["name"] == "delegate_task"
    )
    params = schema["parameters"]
    assert "profile" not in params.get("required", [])
    assert any("profile" in item.get("then", {}).get("required", []) for item in params["allOf"])

    parent = SimpleNamespace(delegation_policy=policy, _delegate_depth=0)
    control = json.loads(
        dt.delegate_task(
            action="status",
            delegation_id="missing",
            parent_agent=parent,
        )
    )
    assert control["status"] == "not_found"

    rejected = dt.delegate_task(goal="protected spawn", parent_agent=parent)
    assert "profile is required" in rejected


def test_cli_parent_session_authorizes_returned_run_handle_without_gateway_context():
    release = threading.Event()

    def runner():
        release.wait(2)
        return {"status": "completed", "summary": "done"}

    spawned = ad.dispatch_async_delegation(
        goal="cli-owned child",
        context=None,
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key="cli-parent-session",
        runner=runner,
        max_async_children=1,
    )
    parent = SimpleNamespace(session_id="cli-parent-session")

    status = json.loads(
        delegation_control(
            action="status",
            delegation_id=spawned["delegation_id"],
            parent_agent=parent,
        )
    )
    assert status["status"] in {"running", "starting", "finalizing"}
    assert status["run_id"] == spawned["run_id"]

    release.set()
    event = _drain_for(spawned["delegation_id"])
    waited = json.loads(
        delegation_control(
            action="wait",
            delegation_id=spawned["delegation_id"],
            run_id=spawned["run_id"],
            timeout_seconds=0,
            parent_agent=parent,
        )
    )
    assert waited["status"] == "completed"
    assert waited["result"]["summary"] == "done"
    assert event["run_id"] == spawned["run_id"]


def test_read_only_workdir_preserves_ro_rw_grants_and_resume_authority(tmp_path):
    repository = tmp_path / "repo"
    nested = repository / "nested"
    scratch = tmp_path / "scratch"
    nested.mkdir(parents=True)
    scratch.mkdir()
    policy = _profile_policy()
    admitted = admit_trusted_run_execution(
        policy,
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [
                {"path": str(repository), "mode": "ro"},
                {"path": str(scratch), "mode": "rw"},
            ],
        },
        inherited_network=False,
    )
    scope = admitted.invocation_scope
    assert scope.workdir == PurePosixPath(str(repository))
    assert [(grant.visible_path, grant.mode) for grant in scope.visible_objects] == [
        (PurePosixPath(str(repository)), AccessMode.RO),
        (PurePosixPath(str(scratch)), AccessMode.RW),
    ]

    nested_scope = resolve_invocation_scope(
        admitted.policy,
        "isolated",
        str(nested),
        [{"path": str(repository), "mode": "ro"}, {"path": str(scratch), "mode": "rw"}],
        backing_registry=admitted.backing_registry,
    )
    assert nested_scope is not None
    assert nested_scope.workdir == PurePosixPath(str(nested))
    assert next(grant for grant in nested_scope.visible_objects
                if grant.visible_path == PurePosixPath(str(repository))).mode is AccessMode.RO

    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("terminal",),
        disabled_toolsets=(),
        scope_id="scope-ro-cwd",
        attempt_id="attempt-ro-cwd",
    )
    restored = deserialize_delegation_authority(
        authority,
        backing_registry=admitted.backing_registry,
    )
    assert restored.workdir == scope.workdir
    assert [(grant.visible_path, grant.mode) for grant in restored.visible_objects] == [
        (PurePosixPath(str(repository)), AccessMode.RO),
        (PurePosixPath(str(scratch)), AccessMode.RW),
    ]


def test_steer_receipt_distinguishes_queued_without_cancelling_or_reauthorizing():
    repository = ad._repository()
    repository.register_initial_dispatch({
        "delegation_id": "deleg-steer-receipt",
        "session_key": "owner",
        "dispatched_at": time.time(),
        "root_subagent_ids": ["logical-steer"],
    })
    payload = json.loads(
        delegation_control(
            action="steer",
            delegation_id="deleg-steer-receipt",
            subagent_id="logical-steer",
            message="queued guidance",
            session_key="owner",
        )
    )
    assert payload["status"] == "accepted"
    assert payload["steer_status"] == "queued"
    assert payload["mailbox_status"] == "pending"
    assert repository.inspect_steer(payload["mailbox_id"])["status"] == "pending"
