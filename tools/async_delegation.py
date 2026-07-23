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

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.delegation_repository import (
    DelegationRepository,
    _ACTIVE_STATES,
    _TERMINAL_DELIVERY_STATES,
    _attempt_state,
)
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
_STATE_CONDITION = threading.Condition()
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
        record["root_subagent_ids"] = [f"sa-{record['delegation_id']}"]
    outcome = _repository().register_initial_dispatch(
        record, owner_pid=os.getpid(), owner_started_at=owner_started_at
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
    _repository().prune(
        cutoff=time.time() - _DURABLE_RETENTION_SECONDS,
        max_terminal=_MAX_RETAINED_COMPLETED,
    )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> bool:
    run_id = event.get("run_id")
    if not run_id:
        resolved = _repository().resolve_run_id(event["delegation_id"])
        if resolved.get("status") != "found":
            return False
        run_id = resolved["run_id"]
    return _changed(
        _repository().complete_run(str(run_id), event, result), "completed"
    )


def recover_abandoned_delegations() -> int:
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
    runs = {(item["delegation_id"], item["run_id"]) for item in recovered}
    for delegation_id, run_id in runs:
        current = repository.snapshot(delegation_id, run_id=run_id)
        if (
            not current or current["completed_at"] is not None
            or any(child["status"] in _ACTIVE_STATES for child in current["children"].values())
        ):
            continue
        now = time.time()
        error = "Delegation owner exited before recording a terminal result; outcome unknown."
        event = {
            "type": "async_delegation",
            "delivery_managed": True,
            "delegation_id": delegation_id,
            "run_id": run_id,
            "session_key": current["session_key"],
            "origin_ui_session_id": current["origin_ui_session_id"],
            "parent_session_id": current["parent_session_id"],
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
            "dispatched_at": current["dispatched_at"],
            "completed_at": now,
        }
        result = {"status": "unknown", "summary": None, "error": error}
        if repository.complete_run(run_id, event, result).get("status") == "completed":
            affected += 1
    if affected:
        _notify_state_change()
    return affected


def restore_undelivered_completions(target_queue) -> int:
    recover_abandoned_delegations()
    rows = _repository().pending_events()
    for row in rows:
        event = dict(row["event"])
        event.update(restored=True, delivery_managed=True, run_id=row["run_id"])
        target_queue.put(event)
    return len(rows)


def restore_stale_wait_completions(
    target_queue,
    *,
    session_key: str = "",
    owns_event: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> int:
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
    return str(snapshot.get("state") or "") not in _ACTIVE_STATES


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    return _repository().snapshot(delegation_id)


def get_async_delegation(
    delegation_id: str, *, session_key: str, run_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
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
    return _repository().list_snapshots(session_keys=session_keys, limit=limit)


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


def load_subagent_resume_bundle(
    delegation_id: str,
    logical_id: str,
    *,
    session_key: str,
) -> Dict[str, Any]:
    """Load one authorized child's validated provider-facing replay bundle."""
    snapshot = get_async_delegation(delegation_id, session_key=session_key)
    if snapshot is None:
        return {"status": "not_found"}
    child = (snapshot.get("children") or {}).get(logical_id)
    if not isinstance(child, dict):
        return {"status": "not_found"}
    metadata = {
        key: child.get(key)
        for key in _RESUME_METADATA_FIELDS
        if key in child
    }
    child_session_id = metadata.get("child_session_id")
    try:
        from hermes_state import SessionDB

        bundle = SessionDB().get_subagent_resume_bundle(
            child_session_id, metadata
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {"status": "resume_unavailable", "reason": str(exc)}
    return {
        "status": "ready",
        "delegation_id": delegation_id,
        "subagent_id": logical_id,
        "attempt_id": child.get("attempt_id"),
        "attempt_number": child.get("attempt_number"),
        "run_id": child.get("run_id"),
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
    try:
        from gateway.status import get_process_start_time

        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    reserved = _repository().reserve_resumed_attempt(
        logical_id,
        physical_worker_id=logical_id,
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

    try:
        child = _delegate.build_resumed_child_agent(
            bundle=bundle,
            logical_id=logical_id,
            goal=str(event_record["goal"] or "resumed subagent"),
            parent_agent=parent_agent,
            continuation=continuation,
        )
        child._delegation_run_id = run_id
        child._delegation_attempt_id = attempt_id
        child._delegation_session_ref.update(
            {"run_id": run_id, "attempt_id": attempt_id}
        )
        metadata = dict(child._delegation_runtime_metadata)
        metadata.update(
            {"child_session_id": bundle["prior_child_session_id"]}
        )
        child._delegation_runtime_metadata = dict(metadata)
    except Exception as exc:
        result = {
            "status": "error",
            "summary": None,
            "error": f"{type(exc).__name__}: {exc}",
            # Construction failed before the continuation session existed;
            # keep the last persisted child segment as the retry anchor.
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
            "error": result["error"],
        }

    executor = _get_executor(max_async_children)

    def worker() -> None:
        result: Dict[str, Any]
        status = "error"
        try:
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
        finish(result, status)

    try:
        executor.submit(propagate_context_to_thread(worker))
    except Exception as exc:
        try:
            child.close()
        except Exception:
            pass
        result = {
            "status": "error",
            "summary": None,
            "error": f"{type(exc).__name__}: {exc}",
            # Submission failed before run_conversation could persist the new
            # segment, so preserve the prior retry anchor.
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
            "error": result["error"],
        }

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
    return _changed(_repository().acknowledge_pending(delegation_id), "delivered")


def claim_completion_delivery(
    delegation_id: str, claim_id: str, *, run_id: Optional[str] = None
) -> bool:
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
    changed = _repository().recover_stale_wait_holds(
        cutoff=time.time() - _WAIT_HOLD_STALE_SECONDS,
        delegation_id=delegation_id,
    )
    if changed:
        _notify_state_change()
    return changed


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    if evt.get("type") != "async_delegation":
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
    return _delivery_claim_action(
        delegation_id, claim_id, delivered=False, run_id=run_id
    )


def complete_completion_delivery(
    delegation_id: str, claim_id: str, *, run_id: Optional[str] = None
) -> bool:
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


def hold_completion_for_wait(
    delegation_id: str, claim_id: str, *, session_key: str,
    run_id: Optional[str] = None,
) -> bool:
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
        return _wait_bound()
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
            if current is record:
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
                if current is record and current.get("status") == "interrupt_requested":
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
    worker = str(interrupted.get("status") or "interrupt_unavailable")
    return {
        "status": "delivery_too_late" if suppression == "too_late" else "abandoned",
        "delegation_id": delegation_id,
        "suppression": suppression,
        "worker": worker,
    }


def register_subagent_lifecycle(record: Dict[str, Any]) -> Optional[str]:
    outcome = _repository().register_subagent(record)
    if not outcome:
        return None
    record["delegation_attempt_id"] = outcome["attempt_id"]
    _notify_state_change()
    return outcome["delegation_id"]


def delegation_contains_subagent(
    delegation_id: str, subagent_id: str, *, session_key: str
) -> bool:
    return _repository().find_attempt(
        subagent_id, delegation_id=delegation_id, session_key=session_key
    ) is not None


def enqueue_subagent_steer(
    delegation_id: str,
    subagent_id: str,
    *,
    session_key: str,
    message: str,
) -> Dict[str, Any]:
    outcome = _repository().enqueue_steer(
        delegation_id, subagent_id, session_key, message
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
        "_interrupt_lock": threading.Lock(),
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
        event_record = {
            k: v for k, v in record.items() if k != "_interrupt_lock"
        }

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
        "run_id": record.get("run_id"),
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
    if not _persist_completion(evt, result):
        logger.warning(
            "Async delegation %s rejected stale completion for run %s",
            record.get("delegation_id"), record.get("run_id"),
        )
        return
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
    _bind_attempts: Optional[Callable[[str, Dict[str, str]], None]] = None,
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
        "_interrupt_lock": threading.Lock(),
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
    if _bind_attempts is not None:
        _bind_attempts(
            str(record["run_id"]),
            dict(zip(record["root_subagent_ids"], record["attempt_ids"])),
        )
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
        event_record = {
            k: v for k, v in record.items() if k != "_interrupt_lock"
        }

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
        "run_id": event_record.get("run_id"),
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
    if not _persist_completion(evt, combined):
        logger.warning(
            "Async delegation batch %s rejected stale completion for run %s",
            delegation_id, event_record.get("run_id"),
        )
        return
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
            {
                k: v
                for k, v in r.items()
                if k not in {"interrupt_fn", "_interrupt_lock"}
            }
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
