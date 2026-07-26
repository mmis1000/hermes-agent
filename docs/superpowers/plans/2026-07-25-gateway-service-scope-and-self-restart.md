# Gateway Service Scope and Self-Restart Implementation Plan

1. Add unit tests for `_select_systemd_scope()` covering explicit system scope, sole system unit, sole user unit, dual units, and no units.
2. Replace gateway self-target guard tests with tests proving `_HERMES_GATEWAY=1` reaches the normal stop/restart dispatch path.
3. Replace terminal hard-block tests with a regression proving lifecycle commands reach normal terminal approval/execution inside a gateway session.
4. Reuse `_select_systemd_scope()` for Linux lifecycle dispatch and resolve it before status runtime snapshot creation.
5. Remove the `_HERMES_GATEWAY` restart/stop rejections from `hermes_cli/gateway.py` and the lifecycle hard-block from `tools/terminal_tool.py`; leave cron lifecycle checks unchanged.
6. Move `HERMES_CRON_SESSION` from process-global environment mutation to the existing task-local session context so the retained cron guard cannot leak into later Discord turns; retain environment fallback for standalone callers.
7. Run focused gateway/terminal/cron-approval tests, syntax checks, and `git diff --check`.
8. Remove the disabled user unit with `hermes gateway uninstall`, verify only the system unit remains, and confirm plain `hermes gateway status` resolves to it.
9. Commit the implementation.
10. Schedule a manager-owned delayed system restart, verify a new `MainPID`, active service state, system cgroup ownership, and plain status output after the gateway returns.
