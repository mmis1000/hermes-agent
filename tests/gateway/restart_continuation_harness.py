"""Production-path harness for the explicit restart-continuation tests.

One "boot" = a real ``GatewayRunner`` on the current ``HERMES_HOME`` with its real ``SessionStore``,
a real ``BasePlatformAdapter`` subclass whose only fake is the external transport (sends are
recorded), and a scripted fake model installed as ``run_agent.AIAgent``. Inbound messages enter
through ``adapter.handle_message`` exactly like a platform delivery. ``request_restart`` is recorded
instead of executed (no stop(), no detached helper), the task-intent relationship judge is
scripted instead of calling an LLM, and nothing reaches a real service.

Run as a script, a boot is a separate OS process: ``python restart_continuation_harness.py
<scenario> <json>`` prints one JSON line; that is the fresh-process boundary the tests rely on.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

if __name__ == "__main__":  # child process: make the checkout importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.config import GatewayConfig, Platform, PlatformConfig  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter, SendResult  # noqa: E402
from gateway.platforms.event import MessageEvent, MessageType  # noqa: E402
from gateway.session import SessionSource  # noqa: E402

DM_CHAT = "dm-chat"


def dm_source(chat_id: str = DM_CHAT) -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id="u1")


class RecordingAdapter(BasePlatformAdapter):
    """Real adapter machinery (admission, session guards, pending queue); fake transport."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent: List[str] = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id=f"out-{len(self.sent)}")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class ScriptedAgent:
    """Fake model. Each ``run_conversation`` consumes one script step:
    ``{"tool": {"action": ..., "note": ...}, "final": str, "result": {...}}``."""

    script: List[Dict[str, Any]] = []
    calls: List[Dict[str, Any]] = []
    runner: Any = None

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self._gateway_session_key = kwargs.get("gateway_session_key")
        self._session_db = kwargs.get("session_db")
        self.tools: list = []
        self.model = "fake-model"

    def _persist_user_row(self, message, kwargs) -> None:
        """What the real agent's turn-start flush writes: the user row with the gateway's metadata."""
        db = self._session_db
        if db is None:
            return
        content = kwargs.get("persist_user_message")
        db.append_message(
            self.session_id, "user", content=content if isinstance(content, str) else message,
            display_metadata=kwargs.get("persist_user_display_metadata"),
            platform_message_id=kwargs.get("persist_user_platform_id"))

    def _await_turn_promotion(self) -> None:
        """The runner publishes the turn's agent asynchronously; a real model call takes longer."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = ScriptedAgent.runner._peek_session_state(self._gateway_session_key)
            if state is not None and state.turn.agent is self:
                return
            time.sleep(0.02)

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        step = ScriptedAgent.script.pop(0) if ScriptedAgent.script else {}
        call: Dict[str, Any] = {"message": message, "session_id": self.session_id}
        ScriptedAgent.calls.append(call)
        self._persist_user_row(message, kwargs)
        if step.get("tool"):
            from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
            self._await_turn_promotion()
            call["tool_result"] = json.loads(INLINE_TOOL_EXECUTORS["restart_continuation"](
                self, dict(step["tool"]), InlineToolContext(effective_task_id=task_id or "t")))
        return {"final_response": step.get("final", "done"), "messages": [], "api_calls": 1,
                **step.get("result", {})}


def install_fakes(setattr_fn, judge_decisions: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Install the fake model and scripted relationship judge (``setattr_fn`` = monkeypatch.setattr
    in pytest, plain setattr in a child process)."""
    import gateway.run as gateway_run
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ScriptedAgent
    sys.modules["run_agent"] = fake_run_agent
    setattr_fn(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    decisions = dict(judge_decisions or {})

    async def scripted_judge(self, *, current_message, **_kw):
        return decisions.get(current_message)

    setattr_fn(gateway_run.GatewayRunner, "_judge_direct_task_relationship", scripted_judge)


def boot(setattr_fn) -> tuple[Any, RecordingAdapter, List[Dict[str, Any]]]:
    """One gateway process's runner on the current HERMES_HOME. Returns (runner, adapter, restarts)."""
    import gateway.run as gateway_run
    from hermes_constants import get_hermes_home
    setattr_fn(gateway_run, "_hermes_home", get_hermes_home())
    runner = gateway_run.GatewayRunner(
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}))
    adapter = RecordingAdapter()
    adapter.set_message_handler(runner._handle_message)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._gateway_loop = asyncio.get_running_loop()
    restarts: List[Dict[str, Any]] = []

    def record_restart(*, detached: bool = False, via_service: bool = False) -> bool:
        restarts.append({"detached": detached, "via_service": via_service})
        return True

    runner.request_restart = record_restart
    ScriptedAgent.runner = runner
    return runner, adapter, restarts


async def wait_idle(runner, adapter, timeout: float = 20.0) -> None:
    """Until every adapter session task finished and no turn holds a slot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = [t for t in adapter._session_tasks.values() if not t.done()]
        startup = [t for t in getattr(runner, "_startup_restore_tasks", None) or [] if not t.done()]
        if not tasks and not startup and not runner._running_agents and not adapter._pending_messages:
            await asyncio.sleep(0.05)
            if not [t for t in adapter._session_tasks.values() if not t.done()]:
                return
        await asyncio.sleep(0.02)
    raise TimeoutError("gateway did not go idle")


async def deliver(runner, adapter, text: str, message_id: str, source: Optional[SessionSource] = None) -> None:
    """A platform delivery of a real user message, through the adapter's own admission path."""
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source or dm_source(),
                         message_id=message_id)
    await adapter.handle_message(event)
    await wait_idle(runner, adapter)


async def startup_restore(runner, adapter) -> int:
    """The boot-time auto-resume pass, then drain everything it started."""
    runner._startup_restore_in_progress = True
    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)
    runner._startup_restore_in_progress = False
    await wait_idle(runner, adapter)
    return scheduled


def session_snapshot(runner, source: Optional[SessionSource] = None) -> Dict[str, Any]:
    key = runner._session_key_for_source(source or dm_source())
    entry = runner.session_store.lookup_by_session_key(key)
    return {"key": key, "session_id": entry.session_id if entry else None,
            "marker": entry.restart_continuation if entry else None}


# ── child-process scenarios ──────────────────────────────────────────────────────────────


async def _scenario_arm(args: Dict[str, Any]) -> Dict[str, Any]:
    """Boot 1: the user's task, a supplement, then a follow-up turn that arms + requests a restart."""
    install_fakes(setattr, args.get("judge"))
    runner, adapter, restarts = boot(setattr)
    ScriptedAgent.script = list(args["script"])
    for index, text in enumerate(args["messages"]):
        await deliver(runner, adapter, text, f"in-{index}")
    return {"restarts": restarts, "calls": ScriptedAgent.calls, "sent": adapter.sent,
            **session_snapshot(runner)}


async def _scenario_boot(args: Dict[str, Any]) -> Dict[str, Any]:
    """A later boot: the startup auto-resume pass only."""
    install_fakes(setattr)
    runner, adapter, _ = boot(setattr)
    ScriptedAgent.script = list(args.get("script") or [])
    scheduled = await startup_restore(runner, adapter)
    return {"scheduled": scheduled, "calls": ScriptedAgent.calls, "sent": adapter.sent,
            **session_snapshot(runner)}


_SCENARIOS = {"arm": _scenario_arm, "boot": _scenario_boot}


def main() -> None:
    scenario, raw = sys.argv[1], sys.argv[2]
    result = asyncio.run(_SCENARIOS[scenario](json.loads(raw)))
    print("RESULT " + json.dumps(result, default=str))


if __name__ == "__main__":
    main()
