"""subagent-watchdog — keeps an orchestrator aware of its live background subagents. See watchdog.py."""

from __future__ import annotations

from .watchdog import DEFAULT_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, PLUGIN_NAME, Watchdog

SCHEMA = {
    "name": "subagent_watchdog",
    "description": (
        "Watch this conversation's background subagents (delegate_task spawns) so they are not forgotten while "
        "you work on other things. Modes: 'passive' (default) adds a one-line note to your next turn when the "
        "set of running subagents changes; 'arm' also wakes you with a status turn every interval_minutes while "
        "any subagent is still running, with each child's live activity (API calls, current tool, time since last "
        "activity, iteration budget) and a warning when one looks quiet or flagged (never interrupts a running "
        "turn, backs off while nothing changes); "
        "'off' disables both. 'status' reports the mode and live subagents. After a Hermes restart an armed "
        "watchdog is not re-armed: you are told it was lost and can arm it again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "arm", "passive", "off"],
                       "description": "Default 'status'."},
            "interval_minutes": {"type": "number", "minimum": MIN_INTERVAL_MINUTES,
                                 "description": f"For 'arm': minutes between status turns (default "
                                                f"{DEFAULT_INTERVAL_MINUTES})."},
        },
        "additionalProperties": False,
    },
}


def register(ctx) -> None:
    from agent.memory_provider import spawn_context_thread
    from plugins.plugin_storage import plugin_db

    watchdog = Watchdog(inject=ctx.inject_message, session_key=ctx.current_session_key,
                        spawn=spawn_context_thread, db=lambda: plugin_db(PLUGIN_NAME),
                        activity=ctx.subagent_activity)
    ctx.register_tool(name="subagent_watchdog", toolset="subagent_watchdog", schema=SCHEMA,
                      handler=watchdog.handle_tool, emoji="🐕")
    ctx.register_hook("subagent_start", watchdog.on_subagent_start)
    ctx.register_hook("subagent_stop", watchdog.on_subagent_stop)
    ctx.register_hook("pre_llm_call", watchdog.pre_llm_call)
    ctx.register_hook("on_session_switch", watchdog.on_session_switch)
