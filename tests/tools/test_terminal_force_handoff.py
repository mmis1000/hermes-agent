"""Foreground terminal-to-process-registry handoff regression tests."""

import json
import threading
import time
import uuid

from tools.foreground_wait import ForegroundWaitSlot, set_current_foreground_wait
from tools.process_registry import process_registry
from tools.terminal_tool import cleanup_vm, terminal_tool


def test_forced_foreground_terminal_handoff_preserves_original_process_and_output():
    task_id = f"force-terminal-{uuid.uuid4().hex}"
    slot = ForegroundWaitSlot("call-terminal", "terminal")
    outcome = {}

    def run_terminal():
        set_current_foreground_wait(slot)
        try:
            outcome.update(
                json.loads(
                    terminal_tool(
                        command=(
                            "python3 -c \"import sys,time; "
                            "print('BEFORE', flush=True); time.sleep(0.5); "
                            "print('AFTER', flush=True)\""
                        ),
                        task_id=task_id,
                        timeout=5,
                    )
                )
            )
        finally:
            set_current_foreground_wait(None)

    thread = threading.Thread(target=run_terminal)
    thread.start()
    try:
        time.sleep(0.1)
        slot.background_requested.set()
        thread.join(1)

        assert not thread.is_alive()
        assert outcome["status"] == "backgrounded"
        handoff = outcome["foreground_handoff"]
        assert handoff["kind"] == "process"
        assert handoff["session_id"].startswith("proc_")
        assert 'action="wait"' in handoff["continue"]
        assert 'action="log"' in handoff["inspect"]
        assert 'action="kill"' in handoff["stop"]

        waited = process_registry.wait(handoff["session_id"], timeout=2)
        assert waited["status"] == "exited"
        assert waited["exit_code"] == 0
        assert "BEFORE" in waited["output"]
        assert "AFTER" in waited["output"]
    finally:
        cleanup_vm(task_id)


def test_forced_foreground_terminal_handoff_can_kill_original_process():
    task_id = f"force-terminal-kill-{uuid.uuid4().hex}"
    slot = ForegroundWaitSlot("call-terminal-kill", "terminal")
    outcome = {}

    def run_terminal():
        set_current_foreground_wait(slot)
        try:
            outcome.update(
                json.loads(
                    terminal_tool(
                        command=(
                            "python3 -c \"import time; print('START', flush=True); "
                            "time.sleep(5); print('SHOULD_NOT_PRINT', flush=True)\""
                        ),
                        task_id=task_id,
                        timeout=10,
                    )
                )
            )
        finally:
            set_current_foreground_wait(None)

    thread = threading.Thread(target=run_terminal)
    thread.start()
    try:
        time.sleep(0.1)
        slot.background_requested.set()
        thread.join(1)
        session_id = outcome["foreground_handoff"]["session_id"]

        killed = process_registry.kill_process(session_id)

        assert killed["status"] == "killed"
        assert killed["completion_reason"] == "killed"
        assert "SHOULD_NOT_PRINT" not in killed["output"]
        waited = process_registry.wait(session_id, timeout=1)
        assert waited["status"] == "exited"
        assert waited["completion_reason"] == "killed"
    finally:
        cleanup_vm(task_id)


def test_failed_process_adoption_reports_failure_and_keeps_foreground_waiting(
    monkeypatch,
):
    task_id = f"force-terminal-fail-{uuid.uuid4().hex}"
    slot = ForegroundWaitSlot("call-terminal-fail", "terminal")
    outcome = {}

    def reject_adoption(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(
        process_registry, "adopt_foreground_process", reject_adoption
    )

    def run_terminal():
        set_current_foreground_wait(slot)
        try:
            outcome.update(
                json.loads(
                    terminal_tool(
                        command=(
                            "python3 -c \"import time; time.sleep(0.2); "
                            "print('FINISHED', flush=True)\""
                        ),
                        task_id=task_id,
                        timeout=5,
                    )
                )
            )
        finally:
            set_current_foreground_wait(None)

    thread = threading.Thread(target=run_terminal)
    thread.start()
    try:
        time.sleep(0.05)
        slot.background_requested.set()
        failed = slot.wait_for_resolution(1)

        assert failed == {
            "status": "failed",
            "error": "Could not adopt the foreground process: registry unavailable",
        }
        thread.join(2)
        assert not thread.is_alive()
        assert outcome["exit_code"] == 0
        assert "FINISHED" in outcome["output"]
    finally:
        cleanup_vm(task_id)


def test_modal_transport_foreground_wait_is_also_recoverable():
    from tools.environments.modal_utils import BaseModalExecutionEnvironment, ModalExecStart

    release = threading.Event()

    class FakeModal(BaseModalExecutionEnvironment):
        cwd = "/tmp"
        timeout = 5
        _stdin_mode = "payload"

        def cleanup(self):
            return None

        def _prepare_command(self, command):
            return command, None

        def _start_modal_exec(self, prepared):
            del prepared
            return ModalExecStart(handle=object())

        def _poll_modal_exec(self, handle):
            del handle
            if release.is_set():
                return {"output": "modal done", "returncode": 0}
            return None

        def _cancel_modal_exec(self, handle):
            del handle
            release.set()

    slot = ForegroundWaitSlot("call-modal", "terminal")
    outcome = {}

    def run_modal():
        set_current_foreground_wait(slot)
        try:
            outcome.update(
                FakeModal(cwd="/tmp", timeout=5).execute(
                    "modal command",
                    foreground_handoff={
                        "command": "modal command",
                        "task_id": "modal-task",
                        "session_key": "modal-owner",
                        "cwd": "/tmp",
                    },
                )
            )
        finally:
            set_current_foreground_wait(None)

    thread = threading.Thread(target=run_modal)
    thread.start()
    time.sleep(0.05)
    slot.background_requested.set()
    thread.join(1)

    assert outcome["status"] == "backgrounded"
    session_id = outcome["foreground_handoff"]["session_id"]
    release.set()
    waited = process_registry.wait(session_id, timeout=2)
    assert waited["exit_code"] == 0
    assert waited["output"] == "modal done"
