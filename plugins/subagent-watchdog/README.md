# subagent-watchdog

A Hermes plugin that keeps an orchestrating agent aware of its background subagents (`delegate_task` spawns), so a long-running child isn't forgotten while the conversation moves on.

Hermes already delivers each child's result, or "outcome unknown" after a restart, as a new turn. What it doesn't do is remind the orchestrator that children are *still running*. This plugin fills that gap.

## Modes

The orchestrator chooses a mode per conversation with the `subagent_watchdog` tool.

| Mode | What happens |
|---|---|
| `passive` (default) | A one-line note is added to the next turn when the set of running children changes, and again every 10 minutes while it stays non-empty. |
| `arm` | Everything `passive` does, plus a status turn every `interval_minutes` (minimum 2, default 10) while any child is running. |
| `off` | Nothing. |
| `status` | Not a mode: reports the current mode and the live children. |

In `arm` mode, status turns:

- **never interrupt a running turn.** They queue behind it, like messages that arrive while the agent is busy.
- **never stack up.** A new one is sent only after the previous one has started.
- **back off while nothing changes,** stretching the interval up to 4×. Any change in the set of children resets it.
- **stop while no child is running.**

## Restarts and compression

- **Restarts.** Children never outlive their Hermes process, so an armed watchdog is **not** re-armed after a restart. On the conversation's next turn the orchestrator is told the watchdog was lost and how to re-arm it. This mirrors how Hermes reports the children themselves: as outcome unknown, with a resume call. `off` is remembered across restarts.
- **Compression.** When compression moves the conversation to a new session id, the watchdog's state moves with it.

## Enable

The plugin is bundled but opt-in, like other general plugins:

```bash
hermes plugins enable subagent-watchdog
```

Outside the classic CLI (gateway, TUI, desktop), timed status turns also need injection permission:

```yaml
plugins:
  entries:
    subagent-watchdog:
      allow_gateway_injection: true
```

Without it, `arm` falls back to `passive` and tells the orchestrator why.

## Boundaries

The plugin uses only the documented plugin surface: `ctx.register_tool`, the `subagent_start`, `subagent_stop`, `pre_llm_call` and `on_session_switch` hooks, `ctx.inject_message(..., interrupt=False)`, `ctx.current_session_key()`, and `plugins.plugin_storage`. Core does not import it. Tests: `tests/plugins/test_subagent_watchdog.py`.
