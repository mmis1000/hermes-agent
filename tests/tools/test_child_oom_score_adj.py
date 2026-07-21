import os
from unittest.mock import patch

import pytest

import tools.browser_tool as browser_mod
import tools.environments.base as base_mod
import tools.environments.local as local_mod
import tools.process_registry as process_mod
from tools.environments.local import LocalEnvironment
from tools.process_registry import ProcessRegistry


class _DummyThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


@pytest.mark.parametrize("raw", ["", "0", "-1", "1001", "not-an-int"])
def test_child_oom_score_adj_rejects_non_positive_or_out_of_range_values(
    monkeypatch, raw
):
    monkeypatch.setattr(base_mod.sys, "platform", "linux")
    monkeypatch.setenv("HERMES_CHILD_OOM_SCORE_ADJ", raw)

    assert base_mod._child_oom_score_adj_kwargs() == {}


def test_child_oom_score_adj_composes_existing_callback(monkeypatch):
    events = []

    monkeypatch.setattr(base_mod.sys, "platform", "linux")
    monkeypatch.setenv("HERMES_CHILD_OOM_SCORE_ADJ", "450")
    monkeypatch.setattr(base_mod.os, "open", lambda path, flags: events.append(("open", path, flags)) or 7)
    monkeypatch.setattr(base_mod.os, "write", lambda fd, data: events.append(("write", fd, data)))
    monkeypatch.setattr(base_mod.os, "close", lambda fd: events.append(("close", fd)))

    def existing_callback():
        events.append("existing")

    kwargs = base_mod._child_oom_score_adj_kwargs(existing_callback)
    callback = kwargs["preexec_fn"]

    assert callback is not existing_callback
    callback()
    assert events == [
        "existing",
        ("open", "/proc/self/oom_score_adj", os.O_WRONLY),
        ("write", 7, b"450"),
        ("close", 7),
    ]


def test_child_oom_score_adj_is_fail_safe_and_non_linux_is_noop(monkeypatch):
    monkeypatch.setattr(base_mod.sys, "platform", "linux")
    monkeypatch.setenv("HERMES_CHILD_OOM_SCORE_ADJ", "300")
    monkeypatch.setattr(base_mod.os, "open", lambda *_args: (_ for _ in ()).throw(PermissionError()))

    base_mod._child_oom_score_adj_kwargs()["preexec_fn"]()

    marker = lambda: None
    monkeypatch.setattr(base_mod.sys, "platform", "darwin")
    assert base_mod._child_oom_score_adj_kwargs() == {}
    assert base_mod._child_oom_score_adj_kwargs(marker) == {"preexec_fn": marker}


def test_popen_bash_composes_existing_preexec(monkeypatch):
    captured = {}
    events = []

    class _FakeProc:
        pid = 321
        stdin = None

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(base_mod.sys, "platform", "linux")
    monkeypatch.setenv("HERMES_CHILD_OOM_SCORE_ADJ", "250")
    monkeypatch.setattr(base_mod.os, "open", lambda *_args: 8)
    monkeypatch.setattr(base_mod.os, "write", lambda fd, data: events.append((fd, data)))
    monkeypatch.setattr(base_mod.os, "close", lambda _fd: None)
    monkeypatch.setattr(base_mod.subprocess, "Popen", fake_popen)

    existing = lambda: events.append("existing")
    proc = base_mod._popen_bash(["/bin/echo", "hi"], preexec_fn=existing)

    assert proc.pid == 321
    assert captured["preexec_fn"] is not existing
    captured["preexec_fn"]()
    assert events == ["existing", (8, b"250")]


def test_local_run_bash_adds_oom_hook_and_preserves_session_semantics(monkeypatch, tmp_path):
    captured = {}
    marker = lambda: None

    class _FakeProc:
        pid = 654
        stdin = None

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(local_mod, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(local_mod, "_make_run_env", lambda env: env)
    monkeypatch.setattr(local_mod, "_resolve_safe_cwd", lambda cwd: cwd)
    monkeypatch.setattr(local_mod, "_child_oom_score_adj_kwargs", lambda: {"preexec_fn": marker})
    monkeypatch.setattr(local_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(LocalEnvironment, "init_session", lambda self: None)

    env = LocalEnvironment(cwd=str(tmp_path), timeout=5)
    proc = env._run_bash("echo hi")

    assert proc.pid == 654
    assert captured["preexec_fn"] is marker
    assert captured["start_new_session"] is True
    assert captured["cwd"] == str(tmp_path)


def _prepare_process_registry(monkeypatch):
    monkeypatch.setattr(process_mod, "_find_shell", lambda: "/bin/bash")
    monkeypatch.setattr(
        process_mod,
        "_sanitize_subprocess_env",
        lambda base_env, extra_env: dict(base_env),
    )
    monkeypatch.setattr(process_mod.threading, "Thread", _DummyThread)
    monkeypatch.setattr(ProcessRegistry, "_write_checkpoint", lambda self: None)
    monkeypatch.setattr(ProcessRegistry, "_safe_host_start_time", lambda self, pid: 10)


def test_process_registry_pipe_adds_oom_hook_and_preserves_session_semantics(
    monkeypatch, tmp_path
):
    captured = {}
    marker = lambda: None

    class _FakeProc:
        pid = 777
        stdout = None
        stdin = None

    _prepare_process_registry(monkeypatch)
    monkeypatch.setattr(
        process_mod, "_child_oom_score_adj_kwargs", lambda: {"preexec_fn": marker}
    )
    monkeypatch.setattr(
        process_mod.subprocess,
        "Popen",
        lambda *args, **kwargs: captured.update(kwargs) or _FakeProc(),
    )

    session = ProcessRegistry().spawn_local(
        "echo hi", cwd=str(tmp_path), use_pty=False
    )

    assert session.pid == 777
    assert captured["preexec_fn"] is marker
    assert captured["start_new_session"] is True
    assert captured["cwd"] == str(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ptyprocess path")
def test_process_registry_pty_adds_oom_hook_and_preserves_pty_options(
    monkeypatch, tmp_path
):
    captured = {}
    marker = lambda: None

    class _FakePtyProc:
        pid = 888

    class _FakePtyProcess:
        @staticmethod
        def spawn(*args, **kwargs):
            captured.update(kwargs)
            return _FakePtyProc()

    _prepare_process_registry(monkeypatch)
    monkeypatch.setattr(
        process_mod, "_child_oom_score_adj_kwargs", lambda: {"preexec_fn": marker}
    )

    with patch.dict(
        "sys.modules", {"ptyprocess": type("PtyModule", (), {"PtyProcess": _FakePtyProcess})}
    ):
        session = ProcessRegistry().spawn_local(
            "echo hi", cwd=str(tmp_path), use_pty=True
        )

    assert session.pid == 888
    assert captured["preexec_fn"] is marker
    assert captured["cwd"] == str(tmp_path)
    assert captured["dimensions"] == (30, 120)


class _BrowserProc:
    returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


def _browser_popen(captured):
    def fake_popen(*args, **kwargs):
        captured.append(kwargs)
        os.write(kwargs["stdout"], b'{"success": true, "data": {}}\n')
        return _BrowserProc()

    return fake_popen


def test_chrome_fallback_commands_add_oom_hook(monkeypatch, tmp_path):
    captured = []
    marker = lambda: None

    monkeypatch.setattr(
        browser_mod,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": True,
            "data": {"result": "https://example.test"},
        },
    )
    monkeypatch.setattr(browser_mod, "_find_agent_browser", lambda: "/agent-browser")
    monkeypatch.setattr(browser_mod, "_chromium_installed", lambda: True)
    monkeypatch.setattr(browser_mod, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(browser_mod, "_build_browser_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(browser_mod, "_merge_browser_path", lambda value: value)
    monkeypatch.setattr(
        browser_mod, "_child_oom_score_adj_kwargs", lambda: {"preexec_fn": marker}
    )
    monkeypatch.setattr(browser_mod.subprocess, "Popen", _browser_popen(captured))

    result = browser_mod._run_chrome_fallback_command(
        "task", "snapshot", [], timeout=5
    )

    assert result["success"] is True
    assert len(captured) == 3  # open, requested command, close
    assert all(kwargs["preexec_fn"] is marker for kwargs in captured)
    assert all(kwargs["stdin"] is browser_mod.subprocess.DEVNULL for kwargs in captured)


def test_browser_command_adds_oom_hook(monkeypatch, tmp_path):
    captured = []
    marker = lambda: None

    monkeypatch.setattr(browser_mod, "_find_agent_browser", lambda: "/agent-browser")
    monkeypatch.setattr(
        browser_mod, "_requires_real_termux_browser_install", lambda _cmd: False
    )
    monkeypatch.setattr(browser_mod, "_is_local_mode", lambda: False)
    monkeypatch.setattr(
        browser_mod,
        "_get_session_info",
        lambda _task: {
            "cdp_url": "ws://example.test",
            "session_name": "session-test",
        },
    )
    monkeypatch.setattr(browser_mod, "_get_browser_engine", lambda: "auto")
    monkeypatch.setattr(browser_mod, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_mod, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(browser_mod, "_write_owner_pid", lambda *args: None)
    monkeypatch.setattr(browser_mod, "_build_browser_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(browser_mod, "_merge_browser_path", lambda value: value)
    monkeypatch.setattr(
        browser_mod, "_child_oom_score_adj_kwargs", lambda: {"preexec_fn": marker}
    )
    monkeypatch.setattr(browser_mod.subprocess, "Popen", _browser_popen(captured))
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = browser_mod._run_browser_command(
        "task", "snapshot", [], timeout=5
    )

    assert result["success"] is True
    assert len(captured) == 1
    assert captured[0]["preexec_fn"] is marker
    assert captured[0]["stdin"] is browser_mod.subprocess.DEVNULL
