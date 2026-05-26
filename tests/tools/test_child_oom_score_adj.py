import io
import sys
from types import SimpleNamespace

import pytest

import tools.environments.base as base_mod
import tools.environments.local as local_mod
import tools.process_registry as pr_mod
from tools.environments.local import LocalEnvironment
from tools.process_registry import ProcessRegistry


class _DummyThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/oom_score_adj is Linux-only")
def test_apply_child_oom_score_adj_writes_proc_file(monkeypatch):
    written = {}

    class _Writer(io.StringIO):
        def close(self):
            written["data"] = self.getvalue()
            super().close()

    def fake_open(path, mode="r", encoding=None):
        written["path"] = path
        written["mode"] = mode
        written["encoding"] = encoding
        return _Writer()

    monkeypatch.setenv("HERMES_CHILD_OOM_SCORE_ADJ", "450")
    monkeypatch.setattr(base_mod, "open", fake_open, raising=False)

    base_mod._apply_child_oom_score_adj(1234)

    assert written == {
        "path": "/proc/1234/oom_score_adj",
        "mode": "w",
        "encoding": "utf-8",
        "data": "450",
    }


def test_popen_bash_applies_child_oom_score_adj(monkeypatch):
    called = []

    class _FakeProc:
        pid = 321
        stdin = None

    monkeypatch.setattr(base_mod.subprocess, "Popen", lambda *args, **kwargs: _FakeProc())
    monkeypatch.setattr(base_mod, "_apply_child_oom_score_adj", called.append)

    proc = base_mod._popen_bash(["/bin/echo", "hi"])

    assert proc.pid == 321
    assert called == [321]


def test_local_run_bash_applies_child_oom_score_adj(monkeypatch, tmp_path):
    called = []

    class _FakeProc:
        pid = 654
        stdin = None

    monkeypatch.setattr(local_mod, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(local_mod, "_make_run_env", lambda env: env)
    monkeypatch.setattr(local_mod, "_resolve_safe_cwd", lambda cwd: cwd)
    monkeypatch.setattr(local_mod.subprocess, "Popen", lambda *args, **kwargs: _FakeProc())
    monkeypatch.setattr(local_mod, "_apply_child_oom_score_adj", called.append)
    monkeypatch.setattr(local_mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(LocalEnvironment, "init_session", lambda self: None)

    env = LocalEnvironment(cwd=str(tmp_path), timeout=5)
    proc = env._run_bash("echo hi")

    assert proc.pid == 654
    assert called == [654]


def test_process_registry_spawn_local_pipe_applies_child_oom_score_adj(monkeypatch, tmp_path):
    called = []

    class _FakeProc:
        pid = 777
        stdout = None
        stdin = None

    monkeypatch.setattr(pr_mod, "_find_shell", lambda: "/bin/bash")
    monkeypatch.setattr(pr_mod, "_sanitize_subprocess_env", lambda base_env, extra_env: dict(base_env))
    monkeypatch.setattr(pr_mod.subprocess, "Popen", lambda *args, **kwargs: _FakeProc())
    monkeypatch.setattr(pr_mod, "_apply_child_oom_score_adj", called.append)
    monkeypatch.setattr(pr_mod.threading, "Thread", _DummyThread)
    monkeypatch.setattr(ProcessRegistry, "_write_checkpoint", lambda self: None)

    registry = ProcessRegistry()
    session = registry.spawn_local("echo hi", cwd=str(tmp_path), use_pty=False)

    assert session.pid == 777
    assert called == [777]


@pytest.mark.skipif(sys.platform == "win32", reason="PTY path test is POSIX-oriented")
def test_process_registry_spawn_local_pty_applies_child_oom_score_adj(monkeypatch, tmp_path):
    called = []

    class _FakePtyProc:
        pid = 888

    class _FakePtyProcess:
        @staticmethod
        def spawn(*args, **kwargs):
            return _FakePtyProc()

    monkeypatch.setattr(pr_mod, "_find_shell", lambda: "/bin/bash")
    monkeypatch.setattr(pr_mod, "_sanitize_subprocess_env", lambda base_env, extra_env: dict(base_env))
    monkeypatch.setattr(pr_mod, "_apply_child_oom_score_adj", called.append)
    monkeypatch.setattr(pr_mod.threading, "Thread", _DummyThread)
    monkeypatch.setattr(ProcessRegistry, "_write_checkpoint", lambda self: None)
    monkeypatch.setitem(sys.modules, "ptyprocess", SimpleNamespace(PtyProcess=_FakePtyProcess))

    registry = ProcessRegistry()
    session = registry.spawn_local("echo hi", cwd=str(tmp_path), use_pty=True)

    assert session.pid == 888
    assert called == [888]
