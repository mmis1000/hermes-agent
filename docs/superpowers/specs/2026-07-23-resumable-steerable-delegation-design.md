# Resumable and Steerable Delegation Design

**Date:** 2026-07-23

**Status:** Approved for implementation planning

**Scope:** Parent-agent control of delegated subagents through the model-facing `delegation` tool

## 1. Objective

Extend Hermes' delegation lifecycle so a parent agent can interact with a delegated subagent as a durable conversation:

- steer a currently running subagent;
- cooperatively interrupt it;
- resume an ended subagent from its persisted transcript with a new message.

A resumed execution is not presented as resurrection of a dead Python thread. Hermes preserves one stable **logical subagent identity** and records every execution as a distinct, immutable **attempt**.

## 2. Confirmed Product Semantics

The controls are available to the parent agent through delegation tool calls. This design does not add a dedicated user-facing subagent chat or Discord thread.

### 2.1 Stable logical identity

A subagent keeps one logical ID across all attempts:

```text
sa-0-ab12cd34
├── attempt 1: completed
├── attempt 2: resumed, then interrupted
└── attempt 3: resumed and running
```

The parent addresses `sa-0-ab12cd34`. Status and tail responses expose the active attempt and bounded attempt history.

### 2.2 Strict steering

`steer` affects only a live or starting attempt. It never starts a new attempt implicitly.

If the logical subagent is terminal, Hermes returns an honest response such as:

```json
{
  "action": "steer",
  "status": "already_terminal",
  "subagent_id": "sa-0-ab12cd34",
  "terminal_status": "completed",
  "resume_available": true,
  "suggested_action": "resume"
}
```

The parent decides whether the prior work remains relevant before paying for another attempt.

### 2.3 Resume means conversational continuation

`resume` loads the persisted subagent transcript and appends a required new user message. It preserves the original effective role, tool restrictions, provider/model routing, workspace, parent relationship, and delegation depth unless a future separately designed API explicitly permits overrides.

### 2.4 Interrupt remains cooperative

The existing `interrupt` action remains the stop mechanism. It is idempotent, can be queued during startup, and targets the active attempt. A startup interruption takes precedence over queued steering.

## 3. Non-goals

This change does not:

- revive an ended thread, future, process, or in-memory `AIAgent` object;
- let unrelated sessions inspect or control a delegation;
- expose hidden reasoning;
- silently resume in response to `steer`;
- allow multiple concurrent attempts for one logical subagent;
- add direct end-user chat routing to subagent sessions;
- change the default `delegate_task` background behavior.

## 4. Public Tool Contract

Extend `delegation` with `steer` and `resume`. Keep strict JSON-schema and runtime validation with `additionalProperties: false`.

### 4.1 Steer

```python
delegation(
    action="steer",
    delegation_id="dg-...",
    subagent_id="sa-0-ab12cd34",
    message="Prioritize the failing integration test.",
)
```

Rules:

- `delegation_id`, `subagent_id`, and non-empty `message` are required.
- The target must belong to the caller's authorized session-scoped delegation.
- The action targets the current attempt.
- A starting attempt stores the message in a durable startup mailbox.
- A live attempt forwards the message through `AIAgent.steer(message)`.
- A terminal target returns `already_terminal` and advertises `resume_available`.
- A stale or unknown target remains indistinguishable from an unauthorized target.

Representative success response:

```json
{
  "action": "steer",
  "status": "accepted",
  "delegation_id": "dg-...",
  "subagent_id": "sa-0-ab12cd34",
  "attempt_id": "at-2-...",
  "disposition": "pending_injection"
}
```

`accepted` means Hermes durably accepted the message for the active attempt. It does not claim that the model has already observed it. Attempt status and completion metadata distinguish `injected`, `superseded_by_interrupt`, and `too_late_after_completion` outcomes.

### 4.2 Resume

```python
delegation(
    action="resume",
    delegation_id="dg-...",
    subagent_id="sa-0-ab12cd34",
    message="Continue from the persisted transcript and address the review feedback.",
)
```

Rules:

- `delegation_id`, `subagent_id`, and non-empty `message` are required.
- Resume is allowed after `completed`, `interrupted`, `error`, `timeout`, or owner-loss `unknown` states.
- Resume during a live or starting attempt returns `already_running`.
- An atomic transition creates exactly one new attempt.
- The call returns immediately; the resumed attempt runs in the background.
- Completion uses the existing automatic parent-session delivery path, extended to address a specific execution run.

Representative success response:

```json
{
  "action": "resume",
  "status": "dispatched",
  "delegation_id": "dg-...",
  "subagent_id": "sa-0-ab12cd34",
  "attempt_id": "at-3-...",
  "attempt_number": 3
}
```

### 4.3 Interrupt

The existing call remains valid:

```python
delegation(
    action="interrupt",
    delegation_id="dg-...",
    subagent_id="sa-0-ab12cd34",
    reason="Requirements changed.",
)
```

Responses gain `attempt_id` when a current attempt exists. Repeated requests remain idempotent.

### 4.4 Status and tail

`status` adds:

- `active_attempt_id`;
- `latest_attempt_number`;
- bounded attempt summaries;
- `resume_available` for terminal logical subagents.

`tail` continues to expose only bounded, redacted assistant text and tool lifecycle events. It defaults to the current/latest attempt and may include an optional validated `attempt_id` selector. Combined cross-attempt conversational history is reconstructed from persisted session lineage, not by concatenating unbounded live-tail buffers.

### 4.5 Wait and compatibility projections

Existing `wait(delegation_id=...)` remains FIFO-compatible: it consumes the oldest authorized undelivered terminal execution run. If no terminal run is waiting, it atomically binds its hold to the latest active run visible when the wait begins. An optional validated `run_id` selector permits exact waiting without changing legacy callers.

Delegation-level compatibility fields are projections, never separate authority: `worker_status` reflects the active attempt when one exists and otherwise the latest attempt; top-level `result` and `delivery_disposition` reflect the latest execution run. Status also exposes `active_run_id`, `latest_run_id`, and pending-run count so callers do not need to infer multi-run state. Every completion event, claim, and dedupe key uses `run_id`, including when an initial batch overlaps a resumed child run.

## 5. State Model

### 5.1 Separate logical identity, execution attempts, and delivery

The design uses three layers:

1. **Delegation container** — session ownership, original task metadata, and logical subagent membership.
2. **Subagent attempt** — one immutable execution of one logical subagent.
3. **Execution-run delivery** — claimable completion delivery for an initial dispatch or resumed execution.

Worker state and delivery state remain independent.

### 5.2 Attempt worker states

```text
starting → running → completed
                   → interrupted
                   → error
                   → timeout
                   → unknown
starting/running → interrupt_requested → interrupted|completed|error
```

Terminal attempts never transition back to `running`. Resume creates a new attempt.

### 5.3 Delivery states

Each deliverable execution run uses the established lifecycle:

```text
pending ↔ held_by_wait → delivering → delivered
                         ↘ consumed
pending/delivering       → suppressed
```

A resumed attempt must not overwrite the already-delivered completion of an earlier run.

## 6. Persistence Design

The current `async_delegations` record becomes the durable logical container. Normalized logical-subagent, attempt, execution-run, and mailbox rows are the sole mutable worker/delivery authority for every new or materialized record. Legacy rows are materialized transactionally once; after the migration marker is set, old child/result/claim JSON and columns are frozen compatibility input rather than a second writable projection. Compatibility responses are derived from normalized rows.

### 6.1 Logical subagents

A normalized logical-subagent record stores:

- `delegation_id`;
- stable `subagent_id`;
- logical parent subagent ID and depth;
- original goal, context, role, named model/provider route, effective enabled/disabled tool restrictions, workspace metadata, and non-secret named fallback-route policy;
- latest and active attempt IDs;
- creation and update timestamps.

### 6.2 Attempts

Each immutable attempt stores:

- `attempt_id` and monotonic attempt number;
- `delegation_id` and logical `subagent_id`;
- prior attempt ID;
- persisted child-session ID or continuation lineage ID;
- effective execution configuration needed for reconstruction;
- owner PID and process-start identity;
- worker state and timestamps;
- bounded archived observable tail;
- interruption details;
- durable steer-mailbox entries and their dispositions.

Reconstruction metadata is an explicit allowlist. It excludes API keys, base URLs, headers, arbitrary request bodies, credential leases, clients, callbacks, ACP commands/arguments, and live objects. Resume resolves named routes and credentials from current authorized configuration and fails closed if a required route is unavailable. Approval policy is also re-evaluated from current configuration so security tightening applies to resumed attempts instead of replaying a stale callback or historical auto-approval value.

### 6.3 Execution runs and delivery

An execution run groups the work whose result is delivered to the parent:

- the initial single or batch dispatch is one run;
- resuming one logical subagent creates a new single-subagent run;
- each run has independent result, event, claim token, attempt counter, and delivery disposition.

This prevents a resumed completion from mutating or redelivering the original completion. Existing durable rows are treated as initial-run compatibility records during migration. The migration must preserve pending or held delivery claims and must not duplicate user-visible injection.

### 6.4 Retention

Never prune:

- an active attempt;
- a terminal attempt whose run result is not in a terminal delivery disposition;
- the latest transcript segment required to resume a retained logical subagent.

Terminal, delivered history may be bounded by configured age/count retention. Pruning a resumable transcript changes `resume_available` to false with an explicit `transcript_unavailable` reason.

## 7. Transcript Continuation

Resume reconstructs a child agent through a shared session-hydration path rather than inventing a second transcript parser.

The canonical persisted transcript can include provider-facing thinking/reasoning carriers on assistant messages, including `reasoning`, `reasoning_content`, signed or redacted `reasoning_details` blocks, and opaque Codex reasoning/message items. Resume preserves and replays these fields when required for provider continuity; it must not summarize, expose, or reinterpret them. Some carriers are encrypted or signed protocol state rather than readable chain-of-thought.

Subagent replay uses a bounded child-only lineage. Attempt 1 is parented to the owning parent-agent session. Every resumed attempt is parented to the prior child tip, while `_delegate_from` continues to identify the original owning parent session. The replay walker starts at the latest child tip, includes child compression continuations and every prior resumed-attempt segment in chronological order, and stops before the owning parent-agent session. It never prepends the parent transcript.

The resumed attempt must:

1. resolve the latest child-session continuation in the subagent's lineage;
2. load persisted messages in canonical order;
3. restore the child system/task prompt and effective execution restrictions;
4. create a new attempt/session segment linked to the prior child session;
5. append the resume message as the next user instruction;
6. run through the standard child execution path.

A new attempt receives a fresh iteration budget. It does not replay already executed tools. Filesystem continuity follows the configured terminal backend; Hermes guarantees transcript continuity, not restoration of external state that no longer exists.

If the transcript or required execution configuration is missing or corrupt, resume fails before dispatch with a structured error. It must not silently fall back to rerunning only the summarized original goal.

## 8. Live Control and Mailbox Semantics

### 8.1 Live registry

Extend the live subagent registry so each entry carries both logical `subagent_id` and immutable `attempt_id`. Control functions verify that the registry entry is still the logical subagent's active attempt before acting.

### 8.2 Durable startup steer mailbox

Steers accepted while an attempt is `starting` are persisted before returning success. Immediately after live-agent registration, Hermes atomically claims them in order and forwards them through `AIAgent.steer()`.

For a running attempt, acceptance and mailbox insertion occur before best-effort live forwarding. Mailbox state is explicit: `pending → forwarded → injected`, with terminal alternatives `superseded_by_interrupt` and `too_late_after_completion`. Calling `AIAgent.steer()` only establishes `forwarded`. Pending steers retain ordered envelopes with exact mailbox IDs across drain and requeue. Only successful marker insertion in either the pre-API or post-tool path establishes `injected`; final-response and interrupt paths acknowledge their distinct terminal outcomes. Atomic claim/drain prevents startup registration and concurrent live forwarding from injecting one mailbox item twice.

### 8.3 Precedence

- Interrupt supersedes pending steer messages for that attempt.
- Completion closes the mailbox for that attempt.
- A steer that loses the completion race is marked `too_late_after_completion`; it is never carried into a future resume automatically.
- Resume messages are explicit new-turn inputs and are not represented as steers.

## 9. Concurrency and Race Guarantees

All attempt creation and active-attempt selection use one transactional authority.

Required guarantees:

- at most one `starting`, `running`, or `interrupt_requested` attempt per logical subagent;
- concurrent resume calls yield one dispatched attempt and deterministic `already_running` responses;
- stale attempts cannot clear or overwrite a newer active attempt;
- late completion events remain archived against their own attempt and run;
- child-targeted interrupt does not affect siblings;
- queued startup interrupt is consumed exactly once;
- accepted completion delivery is never injected twice, including after a bookkeeping failure;
- authorization checks occur before existence-sensitive responses.
- ownership accepts the exact origin session or a proven compression continuation of that session, but not an arbitrary session sharing the same conversation root; a delegated orchestrator controls only delegations it originated and their descendants.

## 10. Recovery Behavior

After process loss:

- dead active attempts become `unknown` using PID plus process-start identity;
- pending or held completion delivery follows the existing durable recovery path;
- the logical subagent remains resumable only if its transcript and configuration are intact;
- a new resume creates a fresh attempt rather than claiming the old worker survived;
- stale live-registry state is never trusted across processes.

## 11. Security and Observability

- Controls remain scoped to the delegation's originating session or proven session lineage.
- Foreign and unknown IDs return indistinguishable errors.
- Messages and reasons are bounded and passed through secret redaction at model-facing and observable serialization boundaries.
- Persisted provider-facing reasoning carriers are available only to the internal resume/replay path; `status`, `tail`, completion summaries, and parent-facing tool results never expose them.
- Live/archive tails include assistant text plus `tool.started` and `tool.completed` metadata only.
- Hidden reasoning is never returned through parent-facing observability. Private raw redaction carry buffers are never persisted or returned.
- Status responses expose operational counters, not provider reasoning payloads.

## 12. Compatibility

- Existing `delegate_task`, `list`, `status`, `tail`, `wait`, `interrupt`, and `abandon` calls continue to work.
- Existing delegation IDs and subagent IDs remain valid logical identifiers.
- Old durable records without normalized attempts are projected as attempt 1 when enough metadata exists.
- Records lacking a resumable child transcript remain observable but return `resume_available: false`.
- Existing initial batch completion remains one delivery event; resumed single-subagent runs create independent completion events under the same delegation container.

## 13. Verification Plan

Use deterministic barriers/events for races and isolate global completion queues in tests.

### Public contract

- strict schema and runtime validation for `message` and attempt selectors;
- unknown fields, empty messages, wrong types, and oversized values;
- cross-session lookup indistinguishable from unknown IDs.

### Steering

- steer during startup is persisted and injected once;
- steer during a live tool loop reaches the next model iteration;
- multiple steers preserve order;
- interrupt supersedes queued steer;
- completion race records `injected` or `too_late_after_completion` honestly;
- terminal steer returns `resume_available` without starting work.

### Resume

- resume after completion, interruption, error, timeout, and owner-loss unknown;
- persisted transcript and required execution configuration are restored;
- signed, redacted, and opaque provider reasoning carriers survive resume replay unchanged without appearing in parent-facing observability;
- the resume message is the next user instruction;
- concurrent resumes create one attempt;
- resume while active returns `already_running`;
- missing transcript/configuration fails without goal-only fallback;
- fresh iteration budget and original tool restrictions are preserved.

### Attempts and delivery

- stable logical ID with monotonic immutable attempts;
- stale-attempt completion cannot mutate the active attempt;
- resumed completion is delivered once without redelivering the initial run;
- delivery enrichment, formatting, injection, and commit failures release or perform bookkeeping-only retry as appropriate;
- retention never prunes undelivered results or active resumable state.

### Recovery and observability

- restart classifies dead attempts honestly and permits explicit resume;
- bounded status/tail across attempts;
- split-delta credentials remain redacted;
- no hidden reasoning appears in live or archived output;
- focused tests, broader delegation/gateway/TUI suites, then a fresh-context review.

## 14. Implementation Boundaries

Implementation should be split into independently verifiable layers:

1. durable logical-subagent, attempt, run, and migration state;
2. strict `steer` and `resume` control surface;
3. live registry, startup mailbox, and transcript hydration;
4. automatic delivery and gateway/TUI consumer integration;
5. race, recovery, security, and broad integration verification.

Each layer should land with focused tests before the next begins. The final review should explicitly examine duplicate delivery, stale-attempt mutation, startup steer/interrupt races, transcript authorization, and redaction boundaries.
