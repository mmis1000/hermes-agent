"""Bundled ``plugins/subagent-watchdog/``: behaviour contracts driven through its hook/tool entry points with a fake
host, plus one end-to-end pass through real plugin discovery, hook dispatch, the tool registry and the classic-CLI
injection path."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_watchdog_module():
    spec = importlib.util.spec_from_file_location(
        "subagent_watchdog_under_test", REPO / "plugins" / "subagent-watchdog" / "watchdog.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


wm = _load_watchdog_module()


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeHost:
    """Records injections and timer threads instead of running them, so ticks are driven explicitly."""

    def __init__(self, accept=True, session_key=""):
        self.accept, self.key, self.injected, self.threads = accept, session_key, [], []

    def inject(self, text, **kwargs):
        self.injected.append((text, kwargs))
        return self.accept

    def session_key(self):
        return self.key

    def spawn(self, target, *, name, args=()):
        host = self

        class _Thread:
            def start(self):
                host.threads.append((name, args))

        return _Thread()


@pytest.fixture
def make_watchdog(tmp_path):
    """Watchdog instances sharing one store file: two instances = two processes across a restart."""
    db_path = tmp_path / "watchdog.db"
    clock = FakeClock()

    def make(host=None, activity=None):
        host = host or FakeHost()
        wd = wm.Watchdog(inject=host.inject, session_key=host.session_key, spawn=host.spawn,
                         db=lambda: sqlite3.connect(db_path), activity=activity or (lambda _ids: {}), clock=clock)
        return wd, host

    make.clock = clock
    return make

PARENT = "ses-parent"


def _spawn(wd, child="child-1", subagent="sa-0-aaaa", parent=PARENT, goal="index the repo"):
    wd.on_subagent_start(parent_session_id=parent, child_session_id=child, child_subagent_id=subagent,
                         child_goal=goal)


def _arm(wd, minutes=5, session=PARENT):
    return json.loads(wd.handle_tool({"action": "arm", "interval_minutes": minutes}, session_id=session))


def _note(wd, session=PARENT, user_message="hi"):
    out = wd.pre_llm_call(session_id=session, user_message=user_message)
    return out["context"] if out else ""


def test_restart_reports_lost_armed_watch_once_and_never_rearms(make_watchdog):
    before, _ = make_watchdog()
    assert _arm(before)["mode"] == "armed"

    after, host = make_watchdog()  # a new process over the same per-profile store
    note = _note(after)
    assert "NOT running" in note and 'subagent_watchdog(action="arm", interval_minutes=5)' in note
    assert _note(after) == ""  # reported exactly once
    assert json.loads(after.handle_tool({"action": "status"}, session_id=PARENT))["mode"] == "passive"
    assert host.threads == []  # nothing was re-armed behind the orchestrator's back


def test_off_survives_restart_and_silences_notes(make_watchdog):
    before, _ = make_watchdog()
    before.handle_tool({"action": "off"}, session_id=PARENT)

    after, _ = make_watchdog()
    _spawn(after)
    assert _note(after) == ""
    assert json.loads(after.handle_tool({"action": "status"}, session_id=PARENT))["mode"] == "off"


def test_ticks_never_interrupt_never_stack_and_back_off(make_watchdog):
    wd, host = make_watchdog()
    _arm(wd, minutes=5)
    watch = wd._watches[PARENT]
    clock = make_watchdog.clock

    clock.now += 10 * 60
    assert not wd.tick(PARENT, watch)  # no live children -> no tick
    _spawn(wd)
    assert wd.tick(PARENT, watch)
    text, kwargs = host.injected[-1]
    assert text.startswith(wm.TICK_MARKER) and kwargs["interrupt"] is False

    clock.now += 9 * 60
    assert not wd.tick(PARENT, watch)  # previous tick not read yet: never stack a second one
    wd.pre_llm_call(session_id=PARENT, user_message=text)  # the tick turn starts
    assert wd.tick(PARENT, watch)

    clock.now += 5 * 60 + 1  # unchanged children: interval has doubled
    wd.pre_llm_call(session_id=PARENT, user_message=host.injected[-1][0])
    assert not wd.tick(PARENT, watch)
    _spawn(wd, child="child-2", subagent="sa-1-bbbb")  # a change drops the backoff at once
    assert wd.tick(PARENT, watch)

    clock.now += 2 * 5 * 60 + 1  # a tick never read within 2 intervals was dropped by its host: retry it
    assert wd.tick(PARENT, watch)


def _live(api, quiet=10.0, tool=None, stalling=False):
    return {"known": True, "delegation_id": "deleg-1", "api_calls": api, "current_tool": tool,
            "seconds_since_activity": quiet, "iterations_used": api, "iterations_max": 50, "stall_suspected": stalling}


def test_tick_reports_activity_and_switches_wording_when_a_child_looks_wrong(make_watchdog):
    """Each tick carries per-child activity, shows a goal only on first mention, and only tells the orchestrator to
    stand down when every child shows recent activity; quiet, flagged or missing telemetry asks for a closer look."""
    feed = {"sa-0-aaaa": _live(5)}
    wd, host = make_watchdog(activity=lambda ids: {sid: feed.get(sid, {"known": False}) for sid in ids})
    _arm(wd, minutes=5)
    watch, clock = wd._watches[PARENT], make_watchdog.clock
    _spawn(wd)

    clock.now += 5 * 60
    assert wd.tick(PARENT, watch)
    first = host.injected[-1][0]
    assert "index the repo" in first and "5 API calls" in first and "5/50 iterations" in first
    assert "No stall signal" in first and "Look closer" not in first

    wd.pre_llm_call(session_id=PARENT, user_message=first)
    feed["sa-0-aaaa"] = _live(9, quiet=wm.QUIET_IDLE_SECONDS + 1)
    _spawn(wd, child="child-2", subagent="sa-1-bbbb", goal="check every link")  # no telemetry for this one
    clock.now += wm.CHANGE_MIN_GAP_SECONDS
    assert wd.tick(PARENT, watch)  # health classes changed: reported without waiting out the interval
    second = host.injected[-1][0]
    assert "index the repo" not in second and "check every link" in second  # goals only on first mention
    assert "+4 since last check" in second and "QUIET" in second
    assert "Look closer at sa-0-aaaa " in second and "not proof of a stall" in second
    assert "sa-1-bbbb: no live telemetry" in second  # listed, but not raised as fresh suspicion
    assert "end the turn" not in second


def test_backoff_follows_health_class_not_counters(make_watchdog):
    feed = {"sa-0-aaaa": _live(1)}
    wd, host = make_watchdog(activity=lambda ids: {sid: feed[sid] for sid in ids})
    _arm(wd, minutes=5)
    watch, clock = wd._watches[PARENT], make_watchdog.clock
    _spawn(wd)
    clock.now += 5 * 60
    assert wd.tick(PARENT, watch)
    for api in (2, 3):  # counters move, class stays "ok": the interval keeps stretching
        wd.pre_llm_call(session_id=PARENT, user_message=host.injected[-1][0])
        feed["sa-0-aaaa"] = _live(api)
        clock.now += 5 * 60 * watch.backoff
        assert wd.tick(PARENT, watch)
    assert watch.backoff == 4
    wd.pre_llm_call(session_id=PARENT, user_message=host.injected[-1][0])
    feed["sa-0-aaaa"] = _live(3, stalling=True)  # a class change is news: report at the next eligible poll
    clock.now += 1
    assert not wd.tick(PARENT, watch)
    clock.now += wm.CHANGE_MIN_GAP_SECONDS
    assert wd.tick(PARENT, watch)
    flagged = host.injected[-1][0]
    assert "STALL MONITOR TRIGGERED" in flagged and "already interrupting" in flagged and watch.backoff == 1


def test_passive_note_follows_changes_not_every_turn(make_watchdog):
    wd, _ = make_watchdog()
    _spawn(wd)
    assert "sa-0-aaaa" in _note(wd)
    assert _note(wd) == ""  # unchanged and recent
    wd.on_subagent_stop(child_session_id="child-1")
    assert "no background subagents" in _note(wd)
    assert _note(wd) == ""


def test_stale_stop_of_old_attempt_keeps_resumed_child(make_watchdog):
    wd, _ = make_watchdog()
    _spawn(wd, child="attempt-1", subagent="sa-0-aaaa")
    _spawn(wd, child="attempt-2", subagent="sa-0-aaaa")  # resume: same logical id, new physical session
    wd.on_subagent_stop(child_session_id="attempt-1")
    live = json.loads(wd.handle_tool({"action": "status"}, session_id=PARENT))["live_subagents"]
    assert [c["subagent_id"] for c in live] == ["sa-0-aaaa"]


def test_compression_switch_carries_children_watch_and_stored_mode(make_watchdog):
    wd, host = make_watchdog()
    _spawn(wd)
    _arm(wd)
    wd.on_session_switch(session_id="ses-child", parent_session_id=PARENT)

    status = json.loads(wd.handle_tool({"action": "status"}, session_id="ses-child"))
    assert status["mode"] == "armed" and [c["subagent_id"] for c in status["live_subagents"]] == ["sa-0-aaaa"]
    wd.on_subagent_stop(child_session_id="child-1")
    assert json.loads(wd.handle_tool({"action": "status"}, session_id="ses-child"))["live_subagents"] == []

    after, _ = make_watchdog()  # the stored row followed the conversation to its new id
    assert "NOT running" in _note(after, session="ses-child")
    assert _note(after, session=PARENT) == ""


def test_refused_injection_is_reported_and_drops_to_passive(make_watchdog):
    wd, host = make_watchdog(host=None)
    host.accept = False
    _arm(wd)
    _spawn(wd)
    make_watchdog.clock.now += 6 * 60
    assert not wd.tick(PARENT, wd._watches[PARENT])
    assert "allow_gateway_injection" in _note(wd)
    assert json.loads(wd.handle_tool({"action": "status"}, session_id=PARENT))["mode"] == "passive"


SCRIPT = textwrap.dedent('''
    import json, queue
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
    from tools.registry import registry

    discover_plugins()
    manager = get_plugin_manager()
    out = {"loaded": "subagent_watchdog" in registry.get_all_tool_names()}

    def status(session):
        return json.loads(registry.dispatch("subagent_watchdog", {"action": "status"}, session_id=session))

    invoke_hook("subagent_start", parent_session_id="ses-a", child_session_id="child-1",
                child_subagent_id="sa-0-e2e", child_goal="crawl docs")
    out["live"] = [c["subagent_id"] for c in status("ses-a")["live_subagents"]]

    invoke_hook("on_session_switch", session_id="ses-b", parent_session_id="ses-a", reason="compression")
    out["after_switch"] = [c["subagent_id"] for c in status("ses-b")["live_subagents"]]
    notes = [r for r in invoke_hook("pre_llm_call", session_id="ses-b", user_message="hello") if r]
    out["note"] = notes[0]["context"] if notes else ""

    class Cli:  # the classic CLI host, mid-turn
        _agent_running = True
        _interrupt_queue = queue.Queue()
        _pending_input = queue.Queue()

    manager._cli_ref = Cli()
    registry.dispatch("subagent_watchdog", {"action": "arm", "interval_minutes": 2}, session_id="ses-b")
    wd = next(h.__self__ for h in manager._hooks["pre_llm_call"]
              if type(getattr(h, "__self__", None)).__name__ == "Watchdog")
    watch = wd._watches["ses-b"]
    watch.armed_at -= 3 * 60
    out["ticked"] = wd.tick("ses-b", watch)
    out["interrupt_queue"] = Cli._interrupt_queue.qsize()
    out["pending_input"] = Cli._pending_input.qsize()
    watch.stop.set()
    print(json.dumps(out))
''')


def test_real_discovery_hooks_tool_and_noninterrupting_cli_tick(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [subagent-watchdog]\n", encoding="utf-8")
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1", "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", SCRIPT], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["loaded"]
    assert out["live"] == ["sa-0-e2e"]
    assert out["after_switch"] == ["sa-0-e2e"]  # state followed the compression switch
    assert "sa-0-e2e" in out["note"]
    assert out["ticked"]
    assert (out["interrupt_queue"], out["pending_input"]) == (0, 1)  # queued behind the turn, never interrupting


def test_in_tool_health_follows_tool_age_not_heartbeated_activity():
    """Activity is heartbeated inside a tool, so a hung tool always looks freshly active; the tick must judge it by how
    long it has been in the tool, and say so."""
    fresh = wm._health({**_live(4, quiet=1.0, tool="terminal"), "seconds_in_tool": 30.0}, None)
    stuck = wm._health({**_live(4, quiet=1.0, tool="terminal"), "seconds_in_tool": wm.QUIET_IN_TOOL_SECONDS + 1}, None)
    assert fresh[0] == "ok" and "in terminal for 30s" in fresh[1]
    edge = {**_live(4, quiet=1.0, tool="terminal")}
    assert wm._health({**edge, "seconds_in_tool": wm.QUIET_IN_TOOL_SECONDS - 1}, None)[0] == "ok"
    assert wm._health({**edge, "seconds_in_tool": wm.QUIET_IN_TOOL_SECONDS}, None)[0] == "long_tool"
    assert stuck[0] == "long_tool" and "inside terminal for 15m01s" in stuck[1]
    assert "Look closer at sa-a" in wm._advice({"sa-a": stuck}) and "end the turn" not in wm._advice({"sa-a": stuck})


def test_control_state_survives_missing_samples_and_blocks_stand_down():
    """What Hermes is already doing to a child (stall monitor interrupting, interruption requested) is reported even
    without an activity sample, and the tick never tells the orchestrator to stand down over it."""
    flagged = wm._health({"known": True, "delegation_id": "deleg-1", "stall_suspected": True}, None)
    interrupting = wm._health({"known": True, "unit_state": "interrupt_requested", "seconds_since_activity": 1.0}, None)
    assert flagged[0] == "stall" and "deleg-1" in flagged[1]
    assert interrupting[0] == "interrupting"
    for health in ({"sa-a": flagged}, {"sa-a": interrupting}):
        assert "end the turn" not in wm._advice(health)


def test_child_that_stops_while_sampling_is_not_reported_running(make_watchdog):
    holder = {}

    def activity(ids):  # the child finishes while telemetry is being read
        holder["wd"].on_subagent_stop(child_session_id="child-1")
        return {sid: _live(3) for sid in ids}

    wd, host = make_watchdog(activity=activity)
    holder["wd"] = wd
    _arm(wd, minutes=5)
    _spawn(wd)
    make_watchdog.clock.now += 5 * 60
    assert not wd.tick(PARENT, wd._watches[PARENT])
    assert host.injected == []
