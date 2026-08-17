"""Public force-steer lifecycle coverage through a real concurrent terminal."""

import json
import threading
import time
from types import SimpleNamespace

import tools.delegate_tool as dt
from tests.run_agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call
from tests.tools.test_delegation_control import _starting_steer_attempt
from tools.delegation_control import delegation_control
from tools.process_registry import process_registry
from tools.terminal_tool import cleanup_vm


def test_public_force_steer_handoffs_worker_before_existing_parent_marker():
    repository, initial, attempt = _starting_steer_attempt(
        "deleg-force-integration", "sa-force-integration"
    )
    agent = _make_agent("terminal")
    messages = []
    errors = []
    task_id = "task-force-integration"
    command = (
        "python -u -c \"import os,time; "
        "print('BEFORE:'+str(os.getpid()),flush=True); "
        "time.sleep(1.5); print('AFTER',flush=True)\""
    )

    dt._register_subagent(
        {
            "subagent_id": "sa-force-integration",
            "delegation_attempt_id": attempt["attempt_id"],
            "delegation_run_id": initial["run_id"],
            "status": "running",
            "events": [],
            "assistant_text_tail": "",
            "agent": agent,
        }
    )
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[
            _mock_tool_call(
                "terminal",
                json.dumps({"command": command, "background": False}),
                "call-force-integration",
            )
        ],
    )

    def execute_tool_batch():
        try:
            agent._execute_tool_calls_concurrent(
                assistant_message, messages, task_id
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute_tool_batch)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        foreground_waits = getattr(agent, "_foreground_waits")
        while not foreground_waits.snapshot() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [slot.kind for slot in foreground_waits.snapshot()] == ["terminal"]

        ordinary = json.loads(
            delegation_control(
                action="steer",
                delegation_id="deleg-force-integration",
                subagent_id="sa-force-integration",
                message="parent guidance",
                session_key="owner",
            )
        )
        assert ordinary["status"] == "foreground_wait"
        assert ordinary["hint"] == (
            "Retry this steer with force=true to move the foreground wait to background."
        )
        assert agent._drain_pending_steer_envelopes() == []

        forced = json.loads(
            delegation_control(
                action="steer",
                delegation_id="deleg-force-integration",
                subagent_id="sa-force-integration",
                message="parent guidance",
                force=True,
                session_key="owner",
            )
        )
        assert forced["status"] == "accepted"
        worker.join(3)
        assert not worker.is_alive()
        assert errors == []

        tool_content = messages[0]["content"]
        marker_position = tool_content.index("[OUT-OF-BAND USER MESSAGE")
        guidance_position = tool_content.index("parent guidance")
        tool_payload = json.loads(tool_content[:marker_position].rstrip())
        advertised = tool_payload["foreground_handoff"]
        session_id = advertised["session_id"]
        handoff_position = tool_content.index(session_id)
        assert handoff_position < marker_position < guidance_position
        assert advertised["continue"] == (
            f'process(action="wait", session_id="{session_id}")'
        )
        assert advertised["inspect"] == (
            f'process(action="log", session_id="{session_id}")'
        )
        assert advertised["stop"] == (
            f'process(action="kill", session_id="{session_id}")'
        )

        original = process_registry.get(session_id)
        assert original is not None
        assert not original.exited
        assert "BEFORE:" in original.output_buffer
        waited = process_registry.wait(session_id, timeout=3)
        assert waited["status"] == "exited"
        assert waited["exit_code"] == 0
        assert waited["output"].count("BEFORE:") == 1
        assert waited["output"].count("AFTER") == 1
        assert repository.inspect_steer(forced["mailbox_id"])["status"] == "injected"
    finally:
        worker.join(3)
        cleanup_vm(task_id)
        dt._unregister_subagent(
            "sa-force-integration", str(attempt["attempt_id"])
        )
