# Delegation Tool Summary Design

**Date:** 2026-07-24
**Status:** Approved for implementation

## Problem

The `delegation` lifecycle-control tool emits full structured arguments, but the shared display preview builder has no `delegation` case. Gateway progress therefore renders only `delegation...`, hiding whether the operation is `steer`, `wait`, `resume`, or another action. It also hides the user-authored steer/resume message.

## Desired output

User-facing progress must lead with the control action and surface its useful payload:

```text
⚙️ delegation: "steer: Focus on the failing lifecycle test"
⚙️ delegation: "resume: Continue implementation from the saved transcript"
⚙️ delegation: "wait · 30s"
⚙️ delegation: "status"
⚙️ delegation: "interrupt: superseded by newer work"
```

## Design

Add a focused delegation-preview helper in `agent/display.py` and call it from `build_tool_preview()` when `tool_name == "delegation"`.

Rules:

1. Normalize `action` and put it first. Missing or malformed actions use a safe `manage` fallback.
2. For `steer` and `resume`, append the `message` after `: `.
3. For `interrupt` and `abandon`, append the optional `reason` after `: `.
4. For `wait`, append a positive timeout as ` · Ns`.
5. For inspection/control actions without text, retain the action alone; compact target selectors may be appended only when they do not displace the action or message.
6. Collapse whitespace so the preview remains one line.
7. Force-redact recognizable secrets in displayed message/reason text, regardless of the global redaction preference.
8. Apply the existing `max_len` truncation once to the complete preview.
9. Do not mutate raw tool arguments, repository state, or delegation execution semantics.

The existing tool-progress callback already passes `build_tool_preview()` output to all gateway adapters. The CLI completion renderer also falls back to this preview. Add a dedicated CLI completion branch only if its generic nine-character tool-name formatting obscures the action during tests.

ACP does not consume the progress callback preview when building its title, so add a small `delegation` title case in `acp_adapter/tools.py` that reuses the shared preview builder. Keep this import local to avoid widening import-time dependencies.

## Verification

Add focused tests for:

- every supported action remains visible;
- `steer` and `resume` include their exact one-line message;
- `interrupt` and `abandon` include reason text;
- wait includes timeout;
- message/reason text respects preview truncation;
- recognizable secrets are redacted;
- missing, `None`, and non-string fields do not crash;
- gateway rendering includes the generated action/message preview;
- ACP titles include the action/message;
- existing `delegate_task` goal previews remain unchanged.

Run the focused display, gateway stream-event, ACP tool-rendering, and delegation-control suites, followed by lint/compile/diff checks.
