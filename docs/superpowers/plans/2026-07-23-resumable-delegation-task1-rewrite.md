# Task 1 Rewrite: Minimal Delegation Persistence Foundation

## Decision

Rewrite Task 1 from `bdeafcc7e`. Keep the existing `qol/integration` patchset as reference only.

The current Task 1 grew across persistence, gateway, ProcessRegistry, and TUI before resume created multiple runs. The rewrite establishes a small durable model first; consumer cutover remains in the later integration task.

## Task 1 goal

Persist stable logical subagents, immutable execution attempts, and independent result/delivery runs while preserving current single-run delegation behavior.

## Non-goals

- No `steer` or `resume` control action.
- No gateway, TUI, or CLI delivery rewrite.
- No support for intermediate schemas created only by the abandoned Task 1 patchset.
- No second mutable compatibility authority.
- No general “latest run” mutation primitive.

## Ownership boundaries

### Schema/bootstrap

`hermes_state.py` remains the declarative schema authority. Add the final normalized delegation DDL to its shared bootstrap rather than running conditional DDL on every delegation operation.

Support only:

1. the released pre-feature schema; and
2. the final normalized schema.

A one-time, idempotent legacy data migration runs under the repository write transaction.

### Repository

Create one focused `DelegationRepository` boundary. It owns:

- SQLite connections and `write_txn` use;
- legacy materialization;
- every lifecycle CAS;
- delivery leases;
- retention;
- snapshot projection.

Callers never compose SQL transactions or mutate normalized rows directly.

### Live registry

The in-memory delegation registry retains only live capabilities: worker futures, callbacks, progress, and capacity accounting. It is not an identity or durable state authority.

Attempt and run IDs are allocated before worker submission and passed directly into worker, registration, archival, and completion callbacks.

## Cardinality-minimal data model

### Delegation container

Keep authorization/routing, original task payload, dispatch time, and abandonment metadata. Legacy worker/result/delivery columns remain only for one-time migration and read compatibility; new/materialized records never update them.

### Logical subagent

- stable logical subagent ID;
- delegation ID;
- optional parent logical ID;
- immutable root ordinal for root children only;
- reconstruction/spec JSON;
- timestamps.

Root ordinal exists only here.

### Attempt

The sole worker lifecycle, owner, and interrupt authority:

- immutable attempt ID and attempt number;
- logical subagent ID and run ID;
- physical worker/subagent ID;
- state;
- owner PID and process-start identity;
- timestamps and attempt-local metadata;
- interrupt reason/request time/taken time.

Do not persist `current_attempt_id`. Derive current/latest by attempt number. Enforce at most one active attempt per logical subagent with a partial unique index over active states.

Startup interruption is attempt-local. Consume it once with an atomic conditional update on the exact attempt.

### Run/outbox

The sole result and delivery authority:

- immutable run ID and run number;
- delegation ID and kind;
- timestamps;
- terminal event/result payloads;
- delivery disposition, lease token/time, attempt count, delivered time.

Do not duplicate worker state or owner fields on runs. A run is incomplete when `completed_at IS NULL`.

## Repository operations

Every write returns a structured outcome; no ambiguous booleans.

1. `register_initial_dispatch(...)`
   - creates logical roots, attempt 1 per root, and one initial run atomically;
   - preserves request order via logical-root ordinal.
2. `reserve_resumed_attempt(logical_id, ...)`
   - inserts a new run and attempt in one transaction;
   - partial unique active-attempt constraint provides the concurrency fence;
   - losers return `already_running`.
3. `transition_attempt(attempt_id, expected_states, new_state, ...)`
   - exact-attempt CAS; row count determines outcome.
4. `request_interrupt(attempt_id, reason)` / `take_interrupt(attempt_id)`
   - exact-attempt, atomic, exactly once;
   - terminal/stale attempts cannot affect later attempts.
5. `complete_run(run_id, event, result)`
   - exact-run conditional completion;
   - rejected stale completion is not published.
6. `claim_run_delivery`, `release_run_delivery`, `commit_run_delivery`
   - exact-run lease protocol.
7. `recover_orphaned_attempts(...)`
   - uses current attempt owner identity and exact-attempt CAS.
8. `snapshot(delegation_id)`
   - one read-time compatibility projector derives legacy fields from attempts/runs.

No mutation API silently chooses the latest run.

## Compatibility strategy

- Existing current APIs remain callable during Task 1.
- Their implementation delegates to the repository.
- Legacy events without a run ID are valid only while a delegation has exactly one run.
- Multiple-run consumer cutover is deferred until resume/integration is implemented.
- Gateway, TUI, CLI, and ProcessRegistry are unchanged in Task 1 unless a current single-run regression requires a minimal adapter fix.

## Compact contract tests

Target roughly 10–15 behavior-level tests, using public/authorized surfaces where practical:

1. Single and batch initial dispatch shape.
2. Batch results map by logical-root ordinal, independent of UUID order.
3. Resume reservation has one concurrent winner.
4. Late attempt lifecycle events cannot mutate a newer attempt.
5. Late run completion cannot overwrite or publish for another run.
6. Attempt-local interrupt is accepted/consumed once; terminal/stale attempts reject it.
7. Recovery uses current attempt owner and permits a clean later resume.
8. Legacy disposition migration is idempotent.
9. One multiprocess migration/open test for the released pre-feature schema.
10. Initial and resumed run delivery leases remain independent.
11. Retention protects active attempts and nonterminal delivery.
12. Compatibility snapshot derives state from normalized authority.

Use one shared test harness for dispatch, completion, snapshots, barriers, and spawned processes. Avoid private SQL assertions except the single migration/schema contract.

## Verification gates

- Focused repository/async-delegation tests.
- Existing delegation control and durable lifecycle tests.
- Existing current single-run gateway/TUI/CLI tests remain green without Task 1-specific consumer patches.
- Multiprocess migration and resume-reservation tests run repeatedly.
- `py_compile` and `git diff --check`.
- Independent spec review, then code-quality review.

## Size guard

Pause for design review if Task 1 exceeds either:

- about 1,000 net production lines; or
- about 800 net test lines.

These are review triggers, not hard correctness limits. Any exceedance must be justified by a new contract rather than compatibility with an unshipped intermediate implementation.
