#!/usr/bin/env python3
"""``restart_continuation``: arm an explicit one-shot continuation of THIS messaging-gateway
conversation across the next gateway restart (``arm``), optionally requesting the restart in the
same call (``arm_and_restart``, same permission as ``/restart``). Lives in the ``gateway_restart``
toolset of the messaging platform bundles only, and is reachable only while a GatewayRunner lives
in this process. Dispatch is agent-level (``agent/inline_tool_executors.py``): the gateway binds
the marker to the agent it is running for the session, never to a caller-supplied key or an env
var. Semantics: ``gateway/restart_continuation.py``."""

import sys

from tools.registry import registry, tool_error


def restart_continuation_tool(agent, action: str = "", note=None) -> str:
    from gateway.restart_continuation import handle_tool_call

    return handle_tool_call(agent, action, note=note)


def check_restart_continuation_requirements() -> bool:
    """Reachable only inside a live messaging gateway process (no import of the gateway here)."""
    module = sys.modules.get("gateway.run")
    ref = getattr(module, "_gateway_runner_ref", None) if module is not None else None
    return callable(ref) and ref() is not None


RESTART_CONTINUATION_SCHEMA = {
    "name": "restart_continuation",
    "description": (
        "Make this conversation continue automatically after the next gateway restart. Use it "
        "BEFORE a restart when work in this conversation must resume afterwards (e.g. verifying "
        "a change that only takes effect after the restart); otherwise a restart just ends the "
        "turn and nobody continues. 'arm_and_restart' arms it and requests a graceful restart "
        "that starts after this turn ends; 'arm' only arms it when someone else restarts. After "
        "the restart a continuation turn runs with the task you were working on; the marker is "
        "consumed once such a turn completes successfully (an interrupted or failed one is retried "
        "after the next restart). It expires if no restart happens soon, and /stop, /new or "
        "/reset withdraw it. Actions are not replayed: the continuation must verify what already "
        "happened. 'status' and 'cancel' inspect or withdraw it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["arm", "arm_and_restart", "status", "cancel"],
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short context for the continuation turn (what is left to do or "
                    "verify). The original request that started the task is captured automatically."
                ),
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="restart_continuation", toolset="gateway_restart", schema=RESTART_CONTINUATION_SCHEMA,
    # Agent-level tool: the inline executor carries the live agent. Any other dispatch path has
    # no trustworthy caller binding and is refused.
    handler=lambda args, **kw: tool_error(
        "restart_continuation can only be called directly by the agent of a gateway conversation."),
    check_fn=check_restart_continuation_requirements, emoji="🔁",
)
