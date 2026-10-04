"""Per-session watchdog over an orchestrator's live background subagents.

Hermes already delivers every child's completion (or its "outcome unknown" after a restart) as a new
turn. What it does not do is remind an orchestrator that children are STILL running while the
conversation drifts elsewhere. Three modes per conversation:

- ``passive`` (default, no stored row): a one-line note on the next turn when the live set changes,
  and again after ``REMIND_SECONDS`` while it stays non-empty.
- ``armed``: passive, plus a status turn every N minutes while children are live. Each tick carries a
  health line per child from Hermes' live telemetry (``ctx.subagent_activity``): API calls and the change
  since the last tick, current tool, time since last activity, iteration budget. Wording switches when a
  child looks quiet, flagged or unknown. Ticks never interrupt (``inject_message(interrupt=False)``), never
  pile up (one unread tick at a time), and back off while every child keeps the same health class.
- ``off``: nothing.

``armed`` and ``off`` are persisted per session. Children never survive their process, so after a
restart an ``armed`` row is NOT re-armed: the next turn says the watchdog was lost and how to re-arm,
and the orchestrator decides — the same treatment Hermes gives the children themselves.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Optional

PLUGIN_NAME = "subagent-watchdog"
TICK_MARKER = "[Subagent watchdog — automatic status check, not a user message]"
DEFAULT_INTERVAL_MINUTES = 10
MIN_INTERVAL_MINUTES = 2
MAX_BACKOFF_FACTOR = 4  # unchanged children stretch the interval up to 4x
REMIND_SECONDS = 600
POLL_SECONDS = 15.0
GOAL_PREVIEW_CHARS = 100
# A change in any child's health class (new suspicion, recovery, telemetry lost) is reported this soon after the
# previous tick instead of waiting out the interval.
CHANGE_MIN_GAP_SECONDS = 60
# Worth a look, not proof of a stall. Idle: no activity this long (Hermes' stall monitor acts at 7.5 min). In a tool:
# the tool has run this long — activity is heartbeated inside tools, so the stall monitor never sees a hung one and
# the tool's own age is the only signal; builds and test suites can legitimately take longer.
QUIET_IDLE_SECONDS = 300
QUIET_IN_TOOL_SECONDS = 900


@dataclass
class Child:
    subagent_id: str
    goal: str
    started_at: float


@dataclass
class Watch:
    interval_s: float
    session_key: str
    armed_at: float
    stop: threading.Event = field(default_factory=threading.Event)
    last_fired: float = 0.0
    fired_digest: str = ""
    backoff: int = 1
    unread_since: float = 0.0  # a tick is queued but its turn has not started yet
    failed: str = ""  # injection refused; reported once, then the watch drops to passive
    introduced: set = field(default_factory=set)  # child sessions whose goal a tick already showed
    last_api: Dict[str, int] = field(default_factory=dict)  # api_calls per child session at the last tick


def _digest(children: Dict[str, Child]) -> str:
    return ",".join(sorted(children))


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    return _age(seconds) if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"


def _health(activity: Optional[dict], previous_api: Optional[int]) -> tuple:
    """``(class, detail)`` for one child: ok, quiet, long_tool, stall, interrupting or unknown. Control state (stall monitor,
    interruption) is reported even without an activity sample; missing telemetry is ``unknown``, never ``ok``;
    ``quiet`` means look closer, not that the child is stuck."""
    a = activity or {}
    handle = f", delegation {a['delegation_id']}" if a.get("delegation_id") else ""
    if a.get("stall_suspected"):
        facts = []
        if isinstance(a.get("stalled_after_quiet_seconds"), (int, float)):
            facts.append(f"no activity for {_span(a['stalled_after_quiet_seconds'])}")
        if isinstance(a.get("stall_threshold_seconds"), (int, float)):
            facts.append(f"threshold {_span(a['stall_threshold_seconds'])}")
        if a.get("stall_in_tool"):
            facts.append("inside a tool")
        return "stall", ("STALL MONITOR TRIGGERED for its delegation; interruption initiated"
                         + (f" ({', '.join(facts)})" if facts else "") + handle)
    if a.get("unit_state") == "interrupt_requested":
        return "interrupting", "INTERRUPTION REQUESTED; its result will arrive as its own turn" + handle
    if not a.get("known"):
        return "unknown", "telemetry unavailable (resumed children and other processes report none)"
    if "seconds_since_activity" not in a:
        return "unknown", "telemetry sample unavailable" + handle
    quiet, tool, api = float(a["seconds_since_activity"]), a.get("current_tool"), a.get("api_calls")
    parts = []
    if isinstance(api, int):
        parts.append(f"{api} API calls" + (f" (+{api - previous_api} since last check)"
                                            if isinstance(previous_api, int) and api >= previous_api else ""))
    in_tool = a.get("seconds_in_tool") if tool and isinstance(a.get("seconds_in_tool"), (int, float)) else None
    if tool:
        parts.append(f"in {tool}" + (f" for {_span(in_tool)}" if in_tool is not None else ""))
    budget = (f"{a['iterations_used']}/{a['iterations_max']} iterations"
              if isinstance(a.get("iterations_used"), int) and isinstance(a.get("iterations_max"), int) else "")
    if in_tool is not None:
        # Activity is heartbeated all through a tool, so inside one the only staleness signal is the tool's age.
        if in_tool >= QUIET_IN_TOOL_SECONDS:
            rest = ", ".join(p for p in (*[x for x in parts if not x.startswith("in ")], budget) if p)
            return "long_tool", f"LONG-RUNNING TOOL: inside {tool} for {_span(in_tool)} — {rest}{handle}"
        return "ok", ", ".join(p for p in (*parts, budget) if p) + handle
    if quiet >= (QUIET_IN_TOOL_SECONDS if tool else QUIET_IDLE_SECONDS):
        where = f" while inside {tool}" if tool else ""
        rest = ", ".join(p for p in (*[x for x in parts if not x.startswith("in ")], budget) if p)
        return "quiet", f"QUIET: no activity for {_span(quiet)}{where} — {rest}{handle}"
    return "ok", ", ".join(p for p in (*parts, f"last activity {_span(quiet)} ago", budget) if p) + handle


_TAGS = {"ok": "active {ago} ago", "quiet": "QUIET {ago}", "long_tool": "in {tool} {in_tool}",
         "stall": "stall monitor triggered", "interrupting": "interrupting", "unknown": "no telemetry"}


def _tag(activity: Optional[dict]) -> str:
    a = activity or {}
    cls, _ = _health(a, None)
    if cls == "ok" and a.get("current_tool") and isinstance(a.get("seconds_in_tool"), (int, float)):
        return f"in {a['current_tool']} {_span(a['seconds_in_tool'])}"
    return _TAGS[cls].format(ago=_span(float(a.get("seconds_since_activity") or 0)), tool=a.get("current_tool"),
                             in_tool=_span(float(a.get("seconds_in_tool") or 0)))


def _advice(health: Dict[str, tuple]) -> str:
    by_class: Dict[str, list] = {}
    for sid, (cls, _) in health.items():
        by_class.setdefault(cls, []).append(sid)
    lines = []
    if by_class.get("stall"):
        lines.append(f"{', '.join(by_class['stall'])}: Hermes is already interrupting it; its result arrives as its "
                     "own turn — then decide whether to resume it.")
    if by_class.get("quiet") or by_class.get("long_tool"):
        look = by_class.get("quiet", []) + by_class.get("long_tool", [])
        lines.append(f"Look closer at {', '.join(look)} with delegate_task(action=\"status\" or \"tail\", "
                     "delegation_id=...) before steering or interrupting; quiet or a long tool is not proof of a stall — "
                     "builds and test suites can run long and healthy.")
    if by_class.get("interrupting"):
        lines.append(f"{', '.join(by_class['interrupting'])}: interruption already underway; wait for its result.")
    if by_class.get("unknown"):
        lines.append(f"{', '.join(by_class['unknown'])}: no live telemetry here; check its status only if it matters.")
    if not any(by_class.get(c) for c in ("stall", "quiet", "long_tool", "interrupting")):
        lines.append("No stall signal; results arrive as their own turns. Reply in one short line and end the turn.")
    lines.append("Do not re-dispatch running work.")
    return "\n".join(lines)


class Watchdog:
    """All state for one plugin load. ``inject`` is ``ctx.inject_message``; ``spawn`` starts a thread under
    the caller's context (profile scope); ``db`` opens this plugin's per-profile SQLite store."""

    def __init__(self, *, inject: Callable[..., bool], session_key: Callable[[], str],
                 spawn: Callable[..., threading.Thread], db: Callable[[], Any],
                 activity: Callable[[list], dict] = lambda _ids: {}, clock: Callable[[], float] = time.time):
        self._inject, self._session_key, self._spawn, self._db, self._clock = inject, session_key, spawn, db, clock
        self._activity = activity
        self._lock = threading.RLock()
        self._children: Dict[str, Dict[str, Child]] = {}  # parent session id -> child session id -> child
        self._parent_of: Dict[str, str] = {}  # child session id -> parent session id
        self._watches: Dict[str, Watch] = {}  # armed sessions in THIS process
        self._off: set = set()
        self._checked: set = set()  # sessions whose stored row this process has reconciled
        self._noted: Dict[str, tuple] = {}  # session -> (digest, time) of the last passive note
        self._pending_notes: Dict[str, str] = {}

    # ── persistence ───────────────────────────────────────────────────────
    def _rows(self):
        conn = self._db()
        conn.execute("CREATE TABLE IF NOT EXISTS watches (session_id TEXT PRIMARY KEY, mode TEXT NOT NULL, "
                     "interval_s REAL NOT NULL DEFAULT 0, session_key TEXT NOT NULL DEFAULT '', "
                     "updated_at REAL NOT NULL)")
        return conn

    def _store(self, session_id: str, mode: Optional[str], interval_s: float = 0.0, session_key: str = "") -> None:
        conn = self._rows()
        try:
            with conn:
                if mode is None:
                    conn.execute("DELETE FROM watches WHERE session_id=?", (session_id,))
                else:
                    conn.execute("INSERT OR REPLACE INTO watches VALUES (?,?,?,?,?)",
                                 (session_id, mode, interval_s, session_key, self._clock()))
        finally:
            conn.close()

    def _load(self, session_id: str) -> Optional[tuple]:
        conn = self._rows()
        try:
            return conn.execute("SELECT mode, interval_s FROM watches WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()

    def _reconcile(self, session_id: str) -> None:
        """First sight of a session in this process: honour a stored ``off``; report a stored ``armed`` as lost."""
        if session_id in self._checked:
            return
        self._checked.add(session_id)
        row = self._load(session_id)
        if row is None:
            return
        mode, interval_s = row
        if mode == "off":
            self._off.add(session_id)
        elif mode == "armed" and session_id not in self._watches:
            minutes = max(MIN_INTERVAL_MINUTES, round(interval_s / 60))
            self._store(session_id, None)
            self._pending_notes[session_id] = (
                f"Subagent watchdog: the timed status check (every {minutes} min) was armed before Hermes restarted "
                "and is NOT running now. Children did not survive the restart either; their recovery notices say "
                "which can be resumed. If you still want timed checks for what you run next, call "
                f'subagent_watchdog(action="arm", interval_minutes={minutes}).')

    # ── hooks ─────────────────────────────────────────────────────────────
    def on_subagent_start(self, parent_session_id=None, child_session_id=None, child_subagent_id=None,
                          child_goal=None, **_):
        if not parent_session_id or not child_session_id:
            return
        with self._lock:
            self._children.setdefault(parent_session_id, {})[child_session_id] = Child(
                str(child_subagent_id or child_session_id), " ".join(str(child_goal or "").split()), self._clock())
            self._parent_of[child_session_id] = parent_session_id

    def on_subagent_stop(self, child_session_id=None, **_):
        # Keyed on the physical child session: a resumed attempt gets a new one, so a late stop of the old
        # attempt can never remove its successor.
        with self._lock:
            parent = self._parent_of.pop(child_session_id, None) if child_session_id else None
            if parent is not None:
                self._children.get(parent, {}).pop(child_session_id, None)

    def on_session_switch(self, session_id=None, parent_session_id=None, **_):
        if not session_id or not parent_session_id or session_id == parent_session_id:
            return
        with self._lock:
            moved = self._children.pop(parent_session_id, None)
            if moved:
                self._children.setdefault(session_id, {}).update(moved)
                for child in moved:
                    self._parent_of[child] = session_id
            for table in (self._watches, self._noted, self._pending_notes):
                if parent_session_id in table:
                    table[session_id] = table.pop(parent_session_id)
            for marks in (self._off, self._checked):
                if parent_session_id in marks:
                    marks.discard(parent_session_id)
                    marks.add(session_id)
            row = self._load(parent_session_id)
            if row is not None:
                watch = self._watches.get(session_id)
                self._store(session_id, row[0], row[1], watch.session_key if watch else "")
                self._store(parent_session_id, None)
                if watch is not None:
                    watch.stop.set()  # the old timer is bound to the old id; restart it on the new one
                    self._start_timer(session_id, replace(watch, stop=threading.Event()))

    def pre_llm_call(self, session_id=None, user_message=None, **_) -> Optional[Dict[str, str]]:
        if not session_id:
            return None
        due: Optional[list] = None
        with self._lock:
            self._reconcile(session_id)
            notes = []
            if session_id in self._pending_notes:
                notes.append(self._pending_notes.pop(session_id))
            watch = self._watches.get(session_id)
            is_tick = isinstance(user_message, str) and user_message.startswith(TICK_MARKER)
            if watch is not None and is_tick:
                watch.unread_since = 0.0
            if watch is not None and watch.failed:
                notes.append(watch.failed)
                self._disarm(session_id, store=None)
            if session_id not in self._off and not is_tick:
                due = self._passive_due(session_id)
            now = self._clock()
        if due == []:
            notes.append("Subagent watchdog: no background subagents of this conversation are running now.")
        elif due:
            activity = self._sample([c.subagent_id for c in due])
            parts = [f"{c.subagent_id} ({_age(now - c.started_at)}, {_tag(activity.get(c.subagent_id))}): "
                     f"{c.goal[:GOAL_PREVIEW_CHARS]}" for c in due]
            notes.append(f"Subagent watchdog: {len(due)} background subagent(s) still running — " + "; ".join(parts)
                         + '. Their results arrive as their own turns; delegate_task(action="list") shows activity '
                         "and status.")
        return {"context": "\n".join(notes)} if notes else None

    def _passive_due(self, session_id: str) -> Optional[list]:
        """Children to mention in a passive note now (``[]`` = say none are left), or None when no note is due."""
        children = self._children.get(session_id) or {}
        now, digest = self._clock(), _digest(children)
        last_digest, last_at = self._noted.get(session_id, ("", 0.0))
        if not children:
            if last_digest:
                self._noted[session_id] = ("", now)
                return []
            return None
        if digest == last_digest and now - last_at < REMIND_SECONDS:
            return None
        self._noted[session_id] = (digest, now)
        return sorted(children.values(), key=lambda c: c.started_at)

    def _sample(self, subagent_ids: list) -> dict:
        """Core telemetry, called outside the watchdog lock. A failing sampler degrades every child to unknown
        rather than killing the timer thread or the turn."""
        try:
            return self._activity(subagent_ids) or {}
        except Exception:
            return {}

    # ── tool ──────────────────────────────────────────────────────────────
    def handle_tool(self, args: dict, session_id: str = "", **_) -> str:
        action = str((args or {}).get("action") or "status").strip().lower()
        if not session_id:
            return json.dumps({"error": "subagent_watchdog needs a conversation session; none is bound to this call"})
        with self._lock:
            self._reconcile(session_id)
            if action == "arm":
                minutes = (args or {}).get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
                if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < MIN_INTERVAL_MINUTES:
                    return json.dumps({"error": f"interval_minutes must be a number >= {MIN_INTERVAL_MINUTES}"})
                self._disarm(session_id, store=None)
                self._off.discard(session_id)
                self._pending_notes.pop(session_id, None)  # a "lost" note would now contradict the arm
                watch = Watch(float(minutes) * 60, self._session_key(), armed_at=self._clock())
                self._store(session_id, "armed", watch.interval_s, watch.session_key)
                self._start_timer(session_id, watch)
            elif action == "passive":
                self._disarm(session_id, store=None)
                self._off.discard(session_id)
            elif action == "off":
                self._disarm(session_id, store="off")
                self._off.add(session_id)
            elif action != "status":
                return json.dumps({"error": f"unknown action {action!r}; use status, arm, passive or off"})
        return json.dumps(self._status(session_id))

    def _status(self, session_id: str) -> dict:
        with self._lock:
            watch, now = self._watches.get(session_id), self._clock()
            children = list((self._children.get(session_id) or {}).values())
            mode = "off" if session_id in self._off else "armed" if watch is not None else "passive"
        activity = self._sample([c.subagent_id for c in children]) if children else {}
        live = []
        for c in children:
            cls, detail = _health(activity.get(c.subagent_id), None)
            live.append({"subagent_id": c.subagent_id, "running_for": _age(now - c.started_at),
                         "goal": c.goal[:GOAL_PREVIEW_CHARS], "health": cls, "activity": detail})
        status = {"mode": mode, "live_subagents": live}
        if watch is not None:
            status.update(interval_minutes=round(watch.interval_s / 60, 1), backoff_factor=watch.backoff,
                          ticks="only while subagents are live; never interrupt a running turn")
        return status

    def _disarm(self, session_id: str, *, store: Optional[str]) -> None:
        watch = self._watches.pop(session_id, None)
        if watch is not None:
            watch.stop.set()
        self._store(session_id, store)

    # ── timer ─────────────────────────────────────────────────────────────
    def _start_timer(self, session_id: str, watch: Watch) -> None:
        self._watches[session_id] = watch
        self._spawn(self._run_timer, name=f"{PLUGIN_NAME}-{session_id[:12]}", args=(session_id, watch)).start()

    def _run_timer(self, session_id: str, watch: Watch) -> None:
        while not watch.stop.wait(POLL_SECONDS):
            self.tick(session_id, watch)

    def tick(self, session_id: str, watch: Watch) -> bool:
        """Inject one status turn if due. Returns whether a tick was sent (the timer ignores it; tests read it)."""
        with self._lock:
            if self._watches.get(session_id) is not watch or watch.failed:
                return False
            snapshot, now = dict(self._children.get(session_id) or {}), self._clock()
            if not snapshot:
                watch.backoff = 1
                return False
            if watch.unread_since and now - watch.unread_since < 2 * watch.interval_s:
                return False  # the last tick is still queued behind other work; never stack another
        activity = self._sample([c.subagent_id for c in snapshot.values()])
        with self._lock:
            current = self._children.get(session_id) or {}
            # A child that stopped (or was replaced by a resumed attempt) while sampling must not be reported running.
            children = {cs: c for cs, c in snapshot.items() if current.get(cs) is c}
            if self._watches.get(session_id) is not watch or not children:
                return False
            ordered = sorted(children.items(), key=lambda item: item[1].started_at)
            health = {c.subagent_id: _health(activity.get(c.subagent_id), watch.last_api.get(cs)) for cs, c in ordered}
            # Backoff follows each child's health CLASS, not its counters: rising API calls alone are not news.
            digest = ",".join(sorted(f"{cs}:{health[c.subagent_id][0]}" for cs, c in ordered))
            unchanged = digest == watch.fired_digest
            wait = watch.interval_s * watch.backoff if unchanged or not watch.fired_digest else CHANGE_MIN_GAP_SECONDS
            if now - (watch.last_fired or watch.armed_at) < wait:
                return False
            watch.backoff = min(MAX_BACKOFF_FACTOR, watch.backoff * 2) if unchanged else 1
            watch.last_fired, watch.fired_digest, watch.unread_since = now, digest, now
            lines = []
            for cs, child in ordered:
                goal = "" if cs in watch.introduced else f": {child.goal[:GOAL_PREVIEW_CHARS]}"
                lines.append(f"- {child.subagent_id} (running {_age(now - child.started_at)}){goal} — "
                             f"{health[child.subagent_id][1]}")
                api = (activity.get(child.subagent_id) or {}).get("api_calls")
                if isinstance(api, int):
                    watch.last_api[cs] = api
            # Baselines are per physical child session: a resumed attempt starts fresh, never inherits deltas.
            watch.introduced = {cs for cs, _ in ordered}
            watch.last_api = {cs: n for cs, n in watch.last_api.items() if cs in children}
            text = (f"{TICK_MARKER}\n{len(ordered)} background subagent(s) running (activity, not measured "
                    f"progress):\n" + "\n".join(lines) + "\n" + _advice(health))
            session_key = watch.session_key
        accepted = self._inject(text, session_key=session_key or None, interrupt=False)
        if not accepted:
            with self._lock:
                watch.failed = (
                    "Subagent watchdog: the timed status check could not reach this conversation, so it was switched "
                    "back to passive notes. Outside the classic CLI the operator must allow it with "
                    f"plugins.entries.{PLUGIN_NAME}.allow_gateway_injection: true in config.yaml.")
                self._store(session_id, None)
        return bool(accepted)
