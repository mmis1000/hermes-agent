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

Export one connection-level `ensure_state_schema(conn)` bootstrap from `hermes_state.py`. Both `SessionDB._init_schema()` and the repository connection factory call that same function before their first query; neither copies DDL or assumes the other opened the database first. The repository gates the call once per process/database path, while the bootstrap itself remains cross-process idempotent.

Support only:

1. the released pre-feature schema; and
2. the final normalized schema.

A one-time, idempotent legacy data migration runs under the repository write transaction.

The delegation container gains `lifecycle_version INTEGER NOT NULL DEFAULT 1`. Version 1 is the released pre-feature representation; version 2 is the final normalized representation. The migration transaction inserts every normalized row, transfers delivery/result/claim state, freezes the legacy mutable payloads, and sets `lifecycle_version=2` last. Readers never import later writes from legacy fields after version 2.

### Exact released-schema migration

Migration preserves released identifiers and dispositions rather than inventing new identities:

- every existing `sa-*` child ID becomes its stable logical-subagent ID;
- root order comes from `root_subagent_ids_json`; non-root children have no root ordinal and retain their recorded parent ID;
- each legacy child becomes attempt 1 in the initial run;
- child states map as follows:
  - `starting`, `running`, `finalizing`, `interrupt_requested` remain their canonical active states;
  - `completed` and `success` become `completed`;
  - `error`, `failed`, and `budget_exhausted` become `error`;
  - `interrupted` and `cancelled` become `interrupted`;
  - `timeout` becomes `timeout`;
  - missing or unrecognized terminal values become `unknown`;
- delivery dispositions remain exactly `pending`, `held_by_wait`, `delivering`, `delivered`, `consumed`, or `suppressed`;
- released `pending` plus a non-null claim token is canonicalized to `delivering`;
- claim token/time, delivery attempts, delivered time, event payload, and result payload move to the initial run atomically;
- existing owner PID/process-start data moves to active attempt 1, not the run.

Migration is idempotent: a version-2 container is read only from normalized rows, and concurrent losers observe version 2 after acquiring the write transaction.

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

Keep authorization/routing, original task payload, dispatch time, abandonment metadata, and the `lifecycle_version` cutover marker. Legacy worker/result/delivery columns remain only for one-time migration and read compatibility; new/materialized records never update them.

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

Repository writes return structured outcomes; no ambiguous booleans. Compatibility wrappers may retain their established boolean, token, string, or dictionary shapes as specified below.

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
- Existing wrapper names retain their exact return shapes and released missing-row behavior. In particular, boolean legacy-delivery claims still return `True` for a missing durable row, event claims retain `""`/token/`None`, and `claim_async_delivery()` retains its `legacy`, `stale`, `not_ready`, `held`, and `claimed` dictionary outcomes.
- Legacy events without a run ID are valid only while a delegation has exactly one run.
- Repository resolution of an omitted run ID returns `ambiguous_run` when a durable delegation has more than one run; it never selects the latest. Compatibility wrappers fail closed without changing shape: boolean wrappers return `False`, token wrappers return `None`, and structured automatic claim returns `{"status": "stale", "reason": "ambiguous_run"}`.
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
