"""Shared Hermes-side execution flow for Modal transports.

This module deliberately stops at the Hermes boundary:
- command preparation
- cwd/timeout normalization
- stdin/sudo shell wrapping
- common result shape
- interrupt/cancel polling

Direct Modal and managed Modal keep separate transport logic, persistence, and
trust-boundary decisions in their own modules.
"""

from __future__ import annotations

import shlex
import threading
import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from tools.environments.base import BaseEnvironment
from tools.interrupt import is_interrupted


@dataclass(frozen=True)
class PreparedModalExec:
    """Normalized command data passed to a transport-specific exec runner."""

    command: str
    cwd: str
    timeout: int
    stdin_data: str | None = None


@dataclass(frozen=True)
class ModalExecStart:
    """Transport response after starting an exec."""

    handle: Any | None = None
    immediate_result: dict | None = None


def wrap_modal_stdin_heredoc(command: str, stdin_data: str) -> str:
    """Append stdin as a shell heredoc for transports without stdin piping."""
    marker = f"HERMES_EOF_{uuid.uuid4().hex[:8]}"
    while marker in stdin_data:
        marker = f"HERMES_EOF_{uuid.uuid4().hex[:8]}"
    return f"{command} << '{marker}'\n{stdin_data}\n{marker}"


def wrap_modal_sudo_pipe(command: str, sudo_stdin: str) -> str:
    """Feed sudo via a shell pipe for transports without direct stdin piping."""
    return f"printf '%s\\n' {shlex.quote(sudo_stdin.rstrip())} | {command}"


class BaseModalExecutionEnvironment(BaseEnvironment):
    """Execution flow for the *managed* Modal transport (gateway-owned sandbox).

    This deliberately overrides :meth:`BaseEnvironment.execute` because the
    tool-gateway handles command preparation, CWD tracking, and env-snapshot
    management on the server side.  The base class's ``_wrap_command`` /
    ``_wait_for_process`` / snapshot machinery does not apply here — the
    gateway owns that responsibility.  See ``ManagedModalEnvironment`` for the
    concrete subclass.
    """

    _stdin_mode = "payload"
    _poll_interval_seconds = 0.25
    _client_timeout_grace_seconds: float | None = None
    _interrupt_output = "[Command interrupted]"
    _unexpected_error_prefix = "Modal execution error"

    def execute(
        self,
        command: str,
        cwd: str = "",
        *,
        timeout: int | None = None,
        stdin_data: str | None = None,
        rewrite_compound_background: bool = True,
        bounded_capture: bool = False,
        foreground_handoff: dict | None = None,
    ) -> dict:
        # Managed/remote modal transports execute commands via explicit transport
        # and do not rely on shell background rewriters. Keep parameter for
        # compatibility with BaseEnvironment callers.
        _ = rewrite_compound_background
        # bounded_capture: accepted for BaseEnvironment.execute() signature
        # parity (the terminal tool passes it). Modal transports return the
        # remote function's result in one payload, so streaming-time bounding
        # does not apply; the terminal tool's final truncation still caps it.
        _ = bounded_capture
        self._before_execute()
        prepared = self._prepare_modal_exec(
            command,
            cwd=cwd,
            timeout=timeout,
            stdin_data=stdin_data,
        )

        try:
            start = self._start_modal_exec(prepared)
        except Exception as exc:
            return self._error_result(f"{self._unexpected_error_prefix}: {exc}")

        if start.immediate_result is not None:
            return start.immediate_result

        if start.handle is None:
            return self._error_result(
                f"{self._unexpected_error_prefix}: transport did not return an exec handle"
            )

        deadline = None
        if self._client_timeout_grace_seconds is not None:
            deadline = time.monotonic() + prepared.timeout + self._client_timeout_grace_seconds

        _now = time.monotonic()
        _activity_state = {
            "last_touch": _now,
            "start": _now,
        }
        handoff_failed = False

        while True:
            if is_interrupted():
                try:
                    self._cancel_modal_exec(start.handle)
                except Exception:
                    pass
                return self._result(self._interrupt_output, 130)

            try:
                result = self._poll_modal_exec(start.handle)
            except Exception as exc:
                return self._error_result(f"{self._unexpected_error_prefix}: {exc}")

            if result is not None:
                return result

            from tools.foreground_wait import current_foreground_wait, process_handoff

            wait_slot = current_foreground_wait()
            if (
                not handoff_failed
                and wait_slot is not None
                and wait_slot.kind == "terminal"
                and wait_slot.background_requested.is_set()
            ):
                metadata = foreground_handoff or {}
                original_command = metadata.get("command")
                if not isinstance(original_command, str) or not original_command:
                    handoff_failed = True
                    wait_slot.fail_background(
                        "foreground terminal handoff metadata is unavailable"
                    )
                else:
                    from tools.process_registry import process_registry

                    try:
                        session = process_registry.adopt_foreground_process(
                            start.handle,
                            command=original_command,
                            task_id=str(metadata.get("task_id") or ""),
                            session_key=str(metadata.get("session_key") or ""),
                            cwd=str(metadata.get("cwd") or self.cwd or ""),
                            env_ref=self,
                            kill_callback=lambda: self._cancel_modal_exec(start.handle),
                            notify_on_complete=True,
                        )
                    except Exception as exc:
                        handoff_failed = True
                        wait_slot.fail_background(
                            f"Could not adopt the foreground process: {exc}"
                        )
                        continue
                    on_adopted = metadata.get("on_adopted")
                    if callable(on_adopted):
                        try:
                            on_adopted(session)
                        except Exception:
                            pass

                    def _finish_adopted_modal() -> None:
                        final_result = None
                        timed_out = False
                        while final_result is None:
                            try:
                                final_result = self._poll_modal_exec(start.handle)
                            except Exception as exc:
                                final_result = self._error_result(
                                    f"{self._unexpected_error_prefix}: {exc}"
                                )
                            if final_result is not None:
                                break
                            if deadline is not None and time.monotonic() >= deadline:
                                timed_out = True
                                try:
                                    self._cancel_modal_exec(start.handle)
                                except Exception:
                                    pass
                                final_result = self._timeout_result_for_modal(
                                    prepared.timeout
                                )
                                break
                            time.sleep(self._poll_interval_seconds)
                        on_complete = metadata.get("on_complete")
                        if callable(on_complete):
                            try:
                                on_complete(self.cwd)
                            except Exception:
                                pass
                        process_registry.finish_adopted_process(
                            session,
                            output=str(final_result.get("output") or ""),
                            exit_code=final_result.get("returncode"),
                            completion_reason=(
                                "timed_out" if timed_out else "exited"
                            ),
                            termination_source=(
                                "foreground_timeout" if timed_out else ""
                            ),
                        )

                    finalizer = threading.Thread(
                        target=_finish_adopted_modal,
                        daemon=True,
                        name=f"proc-adopted-{session.id}",
                    )
                    session._reader_thread = finalizer
                    finalizer.start()
                    handoff = process_handoff(session.id)
                    wait_slot.complete_background(handoff)
                    return {
                        "status": "backgrounded",
                        "output": "",
                        "returncode": None,
                        "foreground_handoff": handoff,
                    }

            if deadline is not None and time.monotonic() >= deadline:
                try:
                    self._cancel_modal_exec(start.handle)
                except Exception:
                    pass
                return self._timeout_result_for_modal(prepared.timeout)

            # Periodic activity touch so the gateway knows we're alive
            try:
                from tools.environments.base import touch_activity_if_due
                touch_activity_if_due(_activity_state, "modal command running")
            except Exception:
                pass

            time.sleep(self._poll_interval_seconds)

    def _before_execute(self) -> None:
        """Hook for backends that need pre-exec sync or validation."""
        pass

    def _prepare_modal_exec(
        self,
        command: str,
        *,
        cwd: str = "",
        timeout: int | None = None,
        stdin_data: str | None = None,
    ) -> PreparedModalExec:
        effective_cwd = cwd or self.cwd
        effective_timeout = timeout or self.timeout

        exec_command = command
        exec_stdin = stdin_data if self._stdin_mode == "payload" else None
        if stdin_data is not None and self._stdin_mode == "heredoc":
            exec_command = wrap_modal_stdin_heredoc(exec_command, stdin_data)

        exec_command, sudo_stdin = self._prepare_command(exec_command)
        if sudo_stdin is not None:
            exec_command = wrap_modal_sudo_pipe(exec_command, sudo_stdin)

        return PreparedModalExec(
            command=exec_command,
            cwd=effective_cwd,
            timeout=effective_timeout,
            stdin_data=exec_stdin,
        )

    def _result(self, output: str, returncode: int) -> dict:
        return {
            "output": output,
            "returncode": returncode,
        }

    def _error_result(self, output: str) -> dict:
        return self._result(output, 1)

    def _timeout_result_for_modal(self, timeout: int) -> dict:
        return self._result(f"Command timed out after {timeout}s", 124)

    @abstractmethod
    def _start_modal_exec(self, prepared: PreparedModalExec) -> ModalExecStart:
        """Begin a transport-specific exec."""

    @abstractmethod
    def _poll_modal_exec(self, handle: Any) -> dict | None:
        """Return a final result dict when complete, else ``None``."""

    @abstractmethod
    def _cancel_modal_exec(self, handle: Any) -> None:
        """Cancel or terminate the active transport exec."""
