# Gateway Service Scope and Self-Restart Design

## Goal

Make the installed system-level Hermes gateway the unambiguous default for existing-service lifecycle operations, and allow an agent running through the gateway to restart or stop that gateway when authorized.

## Scope

1. Remove the stale disabled user-level `hermes-gateway.service` from this host while retaining the active system-level service.
2. For Linux `status`, `start`, `stop`, and `restart`, automatically select system scope when:
   - `--system` was not supplied,
   - no user unit is installed, and
   - a system unit is installed.
3. Preserve explicit `--system` behavior and preserve the current user-scope default when both or neither unit is installed.
4. Remove the terminal-tool gateway lifecycle hard-block.
5. Remove the CLI self-targeting restart/stop blocks so `hermes gateway restart|stop` can run from a gateway-served agent.
6. Retain the cron lifecycle guard. Recurring unattended restart jobs are outside the user's authorization for on-demand agent restarts and can create persistent restart loops.

## Architecture

Reuse the existing `_select_systemd_scope()` helper in `hermes_cli/gateway.py`. Existing-service operations resolve scope before dispatching to systemd and before building status snapshots. Installation and uninstallation remain explicitly scoped because they create or remove ownership rather than operate on an existing unit.

The terminal tool returns to its normal command-approval path. It no longer performs a gateway-specific pre-execution rejection. The CLI likewise performs normal service dispatch without checking `_HERMES_GATEWAY` for restart or stop.

This is not a privilege or security boundary: the gateway runs as root on this host and already has stronger machine-control capabilities. Denying only lifecycle command spellings adds friction without constraining authority. Correctness comes from supervisor-owned restart handoff and post-operation verification, not capability theater.

Cron approval identity must be task-local. The built-in scheduler runs inside the long-lived gateway process, so setting process-wide `HERMES_CRON_SESSION=1` would poison later Discord turns and make the retained cron guard behave like the removed gateway guard. Bind this identity through `gateway.session_context` instead, while preserving environment fallback for standalone cron/CLI compatibility.

## Runtime Flow

For a host with only `/etc/systemd/system/hermes-gateway.service`:

1. `hermes gateway status` resolves to system scope.
2. `hermes gateway restart` resolves to system scope.
3. The service manager executes the lifecycle operation.
4. The gateway is restarted under `/system.slice/hermes-gateway.service`.

When both user and system units exist, Hermes keeps the existing warning and user-scope default so it does not silently choose between conflicting installations.

## Safety and Error Handling

- Normal terminal approval and dangerous-command scanning remain active.
- System-scope mutations still require root through the existing CLI checks.
- The systemd-owned delayed restart handoff remains preferred when restarting from inside the gateway so the command survives cgroup teardown.
- Cron lifecycle commands remain blocked to prevent unattended restart loops.
- A completed cron job cannot leave later live gateway turns in cron approval mode.

## Verification

- Unit tests for sole-system, sole-user, dual-unit, and no-unit scope resolution.
- Regression test proving gateway-session lifecycle commands reach the normal terminal execution/approval path.
- CLI tests proving restart/stop no longer reject `_HERMES_GATEWAY=1` before systemd dispatch.
- Existing gateway restart-loop and gateway CLI suites.
- Live checks after removing the user unit:
  - `hermes gateway status` reports the system service.
  - the reported `MainPID` matches `/system.slice/hermes-gateway.service`.
  - a manager-owned delayed restart produces a new `MainPID` and returns to active state.
