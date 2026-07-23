# Resumable and Steerable Delegation Implementation Plan

**Date:** 2026-07-23

**Design:** `docs/superpowers/specs/2026-07-23-resumable-steerable-delegation-design.md`

**Goal:** Add parent-agent `steer` and transcript-based `resume` controls while preserving one logical subagent ID, immutable execution attempts, strict session ownership, and exactly-once completion delivery.

## Operating constraints

- Work on `qol/integration`; do not touch unrelated `.playwright-cli/` artifacts.
- Follow RED → GREEN → REFACTOR for every behavioral slice.
- Extend the existing durable authority in `tools/async_delegation.py`; do not create a second independent claim/delivery stack.
- Keep worker state separate from delivery disposition.
- Persist provider-facing reasoning carriers for internal replay, but never expose them through delegation observability.
- Commit each completed layer before starting the next so interrupted work remains recoverable.
- Run focused tests after each edit and rerun affected tests after every substantive change.

## Task 1 — Establish the normalized attempt/run persistence contract

**Files**

- Modify: `tools/async_delegation.py`
- Add: `tests/tools/test_delegation_attempt_lifecycle.py`
- Extend: `tests/tools/test_delegation_durable_lifecycle.py`

**RED tests**

1. A newly dispatched logical child receives attempt number 1 and an immutable `attempt_id`.
2. A logical subagent has at most one active attempt.
3. Concurrent terminal→resume reservations create exactly one attempt; losing callers receive `already_running`.
4. A stale attempt cannot clear or overwrite a newer `active_attempt_id`.
5. Initial dispatch and resumed execution runs have independent delivery state/result fields.
6. Existing rows without normalized records project safely as initial run/attempt compatibility data.
7. Migration preserves `pending`, `held_by_wait`, claimed, consumed, suppressed, and delivered dispositions without creating a second deliverable event.
8. Retention never deletes active attempts or attempts associated with non-terminal delivery.
9. Transactional legacy materialization creates normalized rows once, marks lifecycle version 2, and never dual-writes old child/result/claim columns afterward.
10. Delegation compatibility fields are derived from normalized rows: worker status from active/latest attempt and result/delivery from the latest run.

**Implementation**

- Extend `_connect()` with normalized tables for logical subagents, immutable attempts, execution runs, and durable steer mailbox entries.
- Keep `async_delegations` as the session-owned logical container. Normalized rows become the sole mutable authority for new/materialized records.
- Add atomic helpers for:
  - registering logical children and attempt 1;
  - reserving a resumed attempt with compare-and-set semantics;
  - marking an attempt running/terminal only when it is still current;
  - creating and claiming per-run completion delivery;
  - projecting legacy rows without eagerly duplicating delivery.
- Include owner PID plus process-start identity on attempts.
- Return structured outcomes rather than ambiguous booleans for reserve/claim operations.
- Materialize legacy state in one transaction and freeze legacy mutable JSON/claim/result columns once the lifecycle-version marker is set; derive compatibility responses instead of dual-writing.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_attempt_lifecycle.py \
  tests/tools/test_delegation_durable_lifecycle.py \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): persist logical subagent attempts
```

## Task 2 — Persist child reconstruction metadata and attempt identity

**Files**

- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Extend: `tests/tools/test_delegate.py`
- Extend: `tests/tools/test_delegation_live_log.py`
- Extend: `tests/tools/test_delegation_attempt_lifecycle.py`

**RED tests**

1. Child registration records logical `subagent_id`, immutable `attempt_id`, child session ID, parent ID, depth, role, model, toolsets, workspace, and effective execution restrictions.
2. Live registry snapshots expose operational attempt identity but no reasoning carriers.
3. Archive-before-live-removal stores the attempt ID and child session ID.
4. Initial single and batch dispatches associate every root child with attempt 1 before execution can finish.
5. Nested children preserve logical parent/depth metadata and remain independently targetable.
6. Attempt-local task IDs, file-state keys, progress callbacks, and live registry writes use `attempt_id`, preventing a stale timed-out thread from mutating a resumed attempt.

**Implementation**

- Generate or reserve attempt identity before child execution starts.
- Thread `attempt_id` and execution-run ID through child construction, progress callbacks, live registry records, archival, completion events, and hooks.
- Persist the effective configuration actually used by the child, not only the caller's requested overrides.
- Store the child `session_id` as soon as `AIAgent` construction creates it.
- Preserve current toolset inheritance and role/depth restrictions.
- Persist only an explicit reconstruction allowlist. Never persist API keys, credential leases, callbacks, clients, approval functions, or live agent objects.
- The allowlist is: original `goal` and `context` payloads already stored by delegation; effective model/provider identifier; role; canonical enabled **and disabled** toolset names after role/depth restriction; workdir; depth; logical parent ID; iteration/token-budget limits; reasoning configuration; and an ordered non-secret fallback route policy containing only named provider/model references plus safe routing flags. Normalize fallback entries through the provider registry and strip API keys, base URLs, headers, credential leases, clients, callbacks, ACP commands/arguments, and arbitrary request bodies before persistence. Resume resolves every route and credential from current authorized configuration and fails closed if a required named route no longer resolves.
- Approval policy is deliberately not frozen in attempt metadata. Resume re-evaluates current `delegation.subagent_auto_approve` and gateway approval policy so later security tightening takes effect; tests assert the same policy mechanism, not a historical boolean or callback object.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegate.py \
  tests/tools/test_delegation_live_log.py \
  tests/tools/test_delegation_attempt_lifecycle.py \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): track subagent execution attempts
```

## Task 3 — Add durable strict steering

**Files**

- Modify: `tools/delegation_control.py`
- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Modify: `run_agent.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `agent/conversation_loop.py`
- Modify: `agent/turn_finalizer.py`
- Extend: `tests/tools/test_delegation_control.py`
- Extend: `tests/tools/test_delegation_attempt_lifecycle.py`
- Extend: `tests/run_agent/test_run_agent.py`
- Extend: `tests/run_agent/test_steer.py`

**RED tests**

1. `steer` requires authorized `delegation_id`, `subagent_id`, and bounded non-empty `message`.
2. Unknown fields, wrong types, empty text, and oversized text return structured `invalid_arguments` responses.
3. A starting attempt durably queues steering before returning `accepted`.
4. Live steering atomically claims the mailbox item and calls `AIAgent.steer()` against the current attempt, marking only `forwarded`.
5. Multiple steering messages preserve order.
6. A terminal target returns `already_terminal`, `resume_available`, and `suggested_action: resume` without starting work.
7. Interrupt supersedes pending steering; completion-race losers become `too_late_after_completion` and never leak into a future attempt.
8. A stale live entry whose attempt is no longer current cannot receive steering.
9. Foreign-session and unknown IDs remain indistinguishable.
10. The tool-result steer-injection seam acknowledges the exact mailbox item as `injected`; acceptance/forwarding alone never claims model observation.
11. Startup and live-forward paths cannot claim or inject the same mailbox item twice.
12. Direct pre-API injection acknowledges the exact mailbox IDs only after the marker is appended; a drain with no valid tool result requeues the same envelopes and acknowledges nothing.
13. Final-response leftovers mark tracked delegated envelopes `too_late_after_completion` instead of returning them as an untracked next-turn steer; interrupt marks exact tracked envelopes `superseded_by_interrupt`.

**Implementation**

- Add `steer` plus `message` to the strict tool schema and runtime field matrix.
- Insert a durable mailbox item before live forwarding and use atomic mailbox claim/drain transitions `pending → forwarded → injected`, with `superseded_by_interrupt` and `too_late_after_completion` terminal alternatives.
- Extend `_register_subagent()` to consume queued startup interrupt first, then queued steering only if the attempt is still live/current.
- Add a live steering helper returning honest outcomes rather than a bare boolean.
- Replace the lossy pending-steer string accumulator with an internal ordered envelope carrying text plus an optional durable mailbox ID/outcome callback. Preserve the public local `/steer` behavior for envelopes without a mailbox ID.
- Give the internal queue explicit `drain`, `requeue`, `ack_injected`, `ack_superseded`, and `ack_too_late` operations. Requeue preserves original envelope identity and order; it never concatenates away mailbox IDs.
- Enumerate every drain path: pre-API injection in `agent/conversation_loop.py`, post-tool injection/requeue in `agent/agent_runtime_helpers.py`, final-response handling in `agent/turn_finalizer.py`, and interrupt clearing in `run_agent.py`. Only successful marker insertion calls `ack_injected`; draining by itself never does.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_control.py \
  tests/tools/test_delegation_attempt_lifecycle.py \
  tests/run_agent/test_run_agent.py \
  tests/run_agent/test_steer.py \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): steer active subagent attempts
```

## Task 4 — Build a canonical subagent transcript-hydration seam

**Files**

- Modify: `hermes_state.py`
- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Modify: `tools/delegation_control.py`
- Add: `tests/tools/test_delegation_transcript_resume.py`
- Extend: `tests/hermes_state/test_resolve_resume_session_id.py`
- Reference existing behavior in: `tui_gateway/server.py`, `acp_adapter/session.py`

**RED tests**

1. Hydration resolves the latest child-only continuation without prepending the parent transcript.
2. Replay uses alternation-repaired canonical history and drops an unanswered interrupted tool-call tail using the existing replay sanitizer.
3. Assistant `reasoning`, `reasoning_content`, `reasoning_details`, `codex_reasoning_items`, `codex_message_items`, tool calls, and `api_content` survive round-trip unchanged.
4. Parent-facing projections omit reasoning carriers.
5. Missing transcript, wrong/non-subagent source, foreign lineage, or missing reconstruction metadata fails closed.
6. Resume creates a new child session segment linked to the previous child session while preserving the stable `_delegate_from` marker and original parent ownership.
7. Attempt 3 replay includes attempts 1 and 2 plus child compression continuations in order, stops before the owning parent session, and excludes every parent-agent message.
8. Ownership accepts the original session and a proven compression continuation, but rejects foreign sessions and unrelated delegate children sharing the same conversation root.

**Implementation**

- Add a small SessionDB/helper API that returns a validated subagent resume bundle: canonical child-only replay history, stored runtime metadata, prior child session ID, and ownership markers.
- Implement a bounded child-lineage walker: begin at the newest child tip, follow child compression/resume parents, require the stable `_delegate_from` owner marker, and stop before the owning parent-agent session.
- Add one shared fail-closed control-ownership resolver used by list/status/tail/wait/steer/resume/interrupt/abandon: accept the exact origin or a proven compression continuation, reject arbitrary sessions with the same conversation root, and keep an orchestrator scoped to delegations it originated plus their descendants.
- Reuse `get_resume_conversations()` / `sanitize_replay_history()` semantics instead of implementing a second transcript parser.
- Keep provider-facing reasoning carriers solely in the model-fed replay bundle.
- Add a child-session continuation creation helper so resumed attempts do not append ambiguously to an already terminal attempt's session segment.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_transcript_resume.py \
  tests/hermes_state/test_resolve_resume_session_id.py \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): hydrate persisted subagent transcripts
```

## Task 5 — Dispatch resumed attempts through the existing child runner

**Files**

- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Modify: `tools/delegation_control.py`
- Extend: `tests/tools/test_delegation_transcript_resume.py`
- Extend: `tests/tools/test_delegation_control.py`
- Extend: `tests/tools/test_async_delegation.py`

**RED tests**

1. `resume` requires a bounded non-empty message and a terminal authorized logical subagent.
2. Resume after completed, interrupted, error, timed-out, and owner-loss unknown attempts creates the next monotonic attempt.
3. Resume while starting/running/interrupt-requested returns `already_running`.
4. The new message is appended as the next user instruction after hydrated history.
5. The resumed child receives a fresh iteration budget and the saved effective provider/model, non-secret fallback route policy, role, enabled/disabled tool restrictions, workspace, and depth. Credentials and approval policy are re-resolved from current authorized configuration; stale or missing named routes fail closed.
6. Missing/corrupt transcript or reconstruction metadata fails before dispatch; it never falls back to rerunning only the original goal.
7. The tool returns immediately with stable logical ID plus new attempt/run identity.
8. A child construction/submission failure terminates the reserved attempt honestly and leaves it resumable.
9. A child from an initial batch may resume while siblings still run; initial-batch and resumed-run completions remain independently claimable and deliverable.
10. Late progress/completion from a timed-out older attempt remains attached to its own attempt/run and cannot mutate the new active attempt.

**Implementation**

- Add a resume builder that reconstructs `AIAgent` from the validated bundle and passes `conversation_history` into the standard child `run_conversation()` path.
- Reuse the daemon executor, activity tracking, timeout, credential leasing, terminal backend, progress relay, and cleanup behavior already used by initial delegation.
- Add `resume` to `delegation_control` with strict validation and atomic reservation before any expensive construction.
- Keep the logical `subagent_id`; generate a new `attempt_id`, run ID, and child session segment.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_transcript_resume.py \
  tests/tools/test_delegation_control.py \
  tests/tools/test_async_delegation.py \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): resume persisted subagent conversations
```

## Task 6 — Extend status, tail, interrupt, and abandonment projections

**Files**

- Modify: `tools/delegation_control.py`
- Modify: `tools/async_delegation.py`
- Modify: `tools/delegate_tool.py`
- Extend: `tests/tools/test_delegation_control.py`
- Extend: `tests/tools/test_delegation_live_log.py`

**RED tests**

1. `status` reports active/latest attempt identity, bounded summaries, and correct `resume_available`.
2. `tail` defaults to current/latest attempt and accepts a strict optional `attempt_id` selector.
3. Tail never exposes reasoning carriers or raw private redaction buffers.
4. Interrupt reports the targeted attempt and remains idempotent during starting/running/interrupt-requested states.
5. Startup interrupt wins over queued steering exactly once.
6. Abandon suppresses every pending run delivery belonging to the logical delegation and interrupts active attempts without erasing resumable transcript state.
7. Nested child targeting does not interrupt siblings.
8. Unqualified `wait` consumes the oldest authorized undelivered terminal run; when none is terminal it binds to the latest active run at call start.
9. `wait(run_id=...)` targets one exact authorized run, while delegation-level status projects active/latest attempt plus latest result and reports active/latest run IDs and pending-run count.
10. If unqualified wait binds active run A and run B is created and completes first, that waiter remains bound to A; a later wait consumes B according to FIFO terminal ordering.

**Implementation**

- Project normalized attempts into bounded model-facing responses.
- Add optional `attempt_id` only to `tail`; add optional `run_id` only to `wait`; steer/interrupt always target the current attempt to avoid stale-control surprises.
- Preserve the current list/status response compatibility fields.
- Extend abandon/interrupt helpers to operate through attempt/run authority without inferring delivery from worker state.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_control.py \
  tests/tools/test_delegation_live_log.py \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): expose resumable attempt status
```

## Task 7 — Integrate per-run completion delivery and crash recovery

**Files**

- Modify: `tools/async_delegation.py`
- Modify: `tools/process_registry.py`
- Modify: `gateway/run.py`
- Modify: `cli.py`
- Modify: `tui_gateway/server.py`
- Extend: `tests/tools/test_delegation_durable_lifecycle.py`
- Extend: `tests/tools/test_restored_delegation_ownership.py`
- Extend: `tests/tui_gateway/test_delegation_session_lifecycle.py`
- Extend: `tests/cli/test_cli_async_delegation_delivery.py`
- Add: `tests/gateway/test_async_delegation_run_delivery.py`
- Add: `tests/tui_gateway/test_delegation_run_delivery.py`
- Add or extend focused gateway completion-consumer tests under `tests/gateway/`

**RED tests**

1. Original completion and resumed completion each inject once under separate run IDs.
2. Accepted user-visible injection followed by commit failure triggers bookkeeping-only retry, not duplicate injection.
3. Formatting, enrichment, routing, status-emission, and pre-injection failures release/requeue the correct run claim.
4. Process restart restores pending resumed-run completion only to a provable owner.
5. Dead active attempts become `unknown` by PID plus process-start identity and remain explicitly resumable when transcript state survives.
6. Stale wait holds recover per run without stealing a foreign session's completion.
7. CLI, gateway, and TUI consumers all use the same run-level claim protocol.
8. Legacy events without normalized run rows retain compatibility behavior without infinite retry.
9. Gateway live dedupe keys by `run_id`, not stable `delegation_id`.
10. CLI idle/post-turn delivery and TUI autonomous-poller, shutdown-drain, and post-turn paths all handle resumed run IDs.
11. Initial batch and resumed child run may overlap and complete out of order without suppressing or duplicating either result.

**Implementation**

- Key managed completion claims by execution run, with delegation ID retained for routing/authorization.
- Include logical subagent and attempt identity in resumed completion source blocks.
- Put every post-claim operation inside one event-scoped release/requeue guard.
- Update startup restore, wait-hold recovery, consume/commit, suppression, and retention paths for run identity.
- Preserve compatibility for the initial legacy delegation event shape.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_durable_lifecycle.py \
  tests/tools/test_restored_delegation_ownership.py \
  tests/tui_gateway/test_delegation_session_lifecycle.py \
  tests/cli/test_cli_async_delegation_delivery.py \
  tests/gateway/test_async_delegation_run_delivery.py \
  tests/tui_gateway/test_delegation_run_delivery.py \
  tests/gateway/ \
  -o 'addopts=' -q
```

**Commit**

```text
feat(delegation): deliver resumed attempt results exactly once
```

## Task 8 — Update schemas, tool references, and UI compatibility

**Files**

- Modify as generated/required: `tools/delegation_control.py`, `toolsets.py`
- Modify: `website/docs/reference/tools-reference.md`
- Modify: `website/docs/user-guide/configuration.md` only if retention/config keys are added
- Extend relevant TUI/API schema tests under `tests/tui_gateway/`
- Extend: `tests/tools/test_delegation_control.py`

**RED tests**

1. Registered tool schema enumerates `steer` and `resume` with strict action-specific fields.
2. Toolset exposure remains unchanged for authorized parent agents and blocked leaf subagents.
3. TUI/desktop readers tolerate new attempt/run fields without requiring them for legacy records.
4. Documentation examples match the actual runtime schema.

**Implementation**

- Update the model-facing description to teach strict steer-versus-resume semantics.
- Document stable logical IDs, immutable attempts, transcript requirements, and parent-only control.
- Avoid adding new configuration knobs unless retention needs cannot reuse existing delegation lifecycle retention.

**Verification**

```bash
python -m pytest \
  tests/tools/test_delegation_control.py \
  tests/tui_gateway/ \
  -o 'addopts=' -q
```

**Commit**

```text
docs(delegation): document steer and resume controls
```

## Task 9 — Cross-consumer verification and fresh-context review

**Files**

- No planned product edits; fixes discovered by verification must be committed separately and invalidate prior review approval.

**Verification sequence**

1. Run focused attempt/control/resume suites.
2. Run all delegation tests.
3. Run gateway and TUI delegation/consumer tests.
4. Run the broader relevant suite.
5. Inspect the full branch diff against the pre-feature base.
6. Dispatch a fresh-context subagent review of the final snapshot. Require an explicit `APPROVED` verdict; `REQUEST_CHANGES`, timeout, malformed summary, or inconclusive output blocks completion.
7. If code changes after review, rerun affected tests and dispatch a new final review.

```bash
python -m pytest \
  tests/tools/test_async_delegation.py \
  tests/tools/test_delegate.py \
  tests/tools/test_delegation_control.py \
  tests/tools/test_delegation_durable_lifecycle.py \
  tests/tools/test_delegation_live_log.py \
  tests/tools/test_delegation_attempt_lifecycle.py \
  tests/tools/test_delegation_transcript_resume.py \
  tests/tools/test_restored_delegation_ownership.py \
  tests/tui_gateway/test_delegation_session_lifecycle.py \
  tests/cli/test_cli_async_delegation_delivery.py \
  tests/gateway/test_async_delegation_run_delivery.py \
  tests/tui_gateway/test_delegation_run_delivery.py \
  -o 'addopts=' -q

python -m pytest tests/tools/ tests/gateway/ tests/tui_gateway/ tests/cli/ -o 'addopts=' -q
```

**Required final review focus**

- duplicate completion delivery and claim leaks;
- stale-attempt mutation;
- concurrent resume reservations;
- startup steer/interrupt precedence;
- transcript ownership and provider-reasoning replay;
- hidden-reasoning leakage;
- legacy durable-row migration;
- compatibility across CLI, gateway, TUI, and restart recovery.

## Completion criteria

The feature is complete only when:

- all focused and broader relevant suites pass on the final tree;
- real tool-schema inspection shows `steer` and `resume`;
- a deterministic smoke test demonstrates dispatch → steer → complete → resume → interrupt with one stable logical subagent ID and distinct attempts;
- completion results inject exactly once for both initial and resumed runs;
- parent-facing observability contains no reasoning carriers;
- the final fresh-context reviewer returns `APPROVED` on the unchanged reviewed snapshot;
- `git status` contains no feature-related unstaged/untracked files.
