# task-context

Bundled Hermes context-engine plugin. It retains exact admitted human task
wording in a profile-local SQLite store and uses the native compressor for
summaries, budgets, cooldowns, cancellation and telemetry. It does not add an
embedding index, summary DAG, completion policy or scheduler.

## Host requirements

Requires the generic host interfaces listed in [INTERFACES.md](INTERFACES.md):
`configure_compressor(**native_constructor_kwargs)`, synchronous admitted
`on_input(...)`, and the selected engine's `read_task_contract` hook. Root
`__init__.py` and `plugin.yaml` use general bundled-plugin discovery from
`plugins/task-context/`, registering both the engine and the contract-read hook.
Host consumers use generic interfaces, never task-context implementation imports
or the old `hermes_cli.task_intents` modules. Keeping implementation here localizes
the feature's rebase surface without requiring a separate repository.

Both settings are required for activation (not applied to a live profile):

```yaml
plugins:
  enabled: [task-context]  # add alongside any other enabled plugins
context:
  engine: task-context
```

Both settings are required: `plugins.enabled` registers the engine and general
`read_task_contract` hook; `context.engine` selects the registered engine. Without
plugin enablement, the engine is unavailable and guard/restart consumers see no
provider. Enabling the plugin alone leaves the native compressor selected and its
contract provider disabled. Bundling does not enable it in any live profile.

## Behavior

- Store: `$HERMES_HOME/task-context/tasks.sqlite3` (private directory/file).
  Each connection is short-lived; fresh engine clones never copy locks or DBs.
- Only `agent_context='primary'` plus admitted `source_kind='human'` can change
  human state. Machine/unknown records remain separate evidence. No inference
  from role-like text, summaries, tool output or subagent prose.
- Canonical wording is exact adapter `text`; available `platform_text` remains a
  separately named provenance variant. Occurrence identity—not repeated text—
  deduplicates retries. Ordered batch constituents retain sender attribution.
- Relationship classifier and exact-quote/non-rewriting validator were migrated
  from Hermes. Existing `task_intents.relationship_judge` config, opt-in default,
  auxiliary `task_intent` routing, limits and conservative fallback remain.
  The outer deadline is configured timeout + 0.5 seconds. A detached judge worker
  cannot publish anything. No classifier runs in the compression worker.
- Atomic admission/publication, per-session cross-process lock, and version CAS
  prevent duplicate transitions and lost stale results. Classifiers hold no SQL
  transaction. A crash leaves pending inputs for the next admitted turn to drain.
- Canonical supplements and provenance have no old 16/32-item retention caps.
  Related questions do not become obligations. Unclear input preserves the
  active contract and its raw evidence. New unrelated tasks do not silently
  cancel older unresolved tasks; the consumer DTO includes their literal
  obligations. This intentionally differs from the old single-task baseline,
  where `start_new` replaced the previous task. Explicit replacement/cancellation
  acts on the current task; no new completion transition is inferred.
- Compression puts a bounded JSON exact-slice view in the **native valid summary
  carrier before native finalization/accounting**. It adds no separate contract
  user row and does not change the system prompt. Native replay/merge remains for
  in-flight related questions or other requests absent from the contract. For
  primary agents the native generic
  system compaction note is suppressed; historical summary framing is replaced
  with the host-recognized legacy summary prefix plus active-contract framing.
- `task_context_read(entry, offset, limit)` restores exact retained contract slices.
  Unchanged full-history requests are no-ops. Conditional resume restoration
  appends only to an identified current incoming request copy; retries are byte
  stable. No persisted transcript or earlier request row is rewritten.
- Model reset does not erase task state. Compression rotation aliases the same
  conversation authority, so an in-flight judge cannot lose its result during
  rotation (version CAS retries it). Explicit branches inherit isolated snapshots
  and occurrence receipts; new/reset IDs start empty. Same-ID replay is not a fresh
  instruction. Same-ID rewind retains task state and receipts, matching the old
  state-meta behavior; missing transcript rows do not roll back obligations.

## Migration / rollback

First provider read imports `state_meta['task_intent:<session_id>']` before
returning. Data and import receipt commit together; IDs, status, Unicode,
whitespace and lineage are preserved. Host state is only read. Verified persisted
compression-parent lineage can be followed after restart. Persisted
`model_config._branched_from == parent_session_id` creates an independent branch
snapshot even when lifecycle callbacks omit the branch reason. Inherited markers
pointing to an ancestor do not turn compression descendants into new branches.
An immediate `_delegate_from` edge never inherits human ownership. Corrupt
records return `status='error'`, not a successful empty lookup.
The registered provider returns `status='disabled'` when this engine is unselected.

Branch first import uses copied occurrence IDs, or copied message/absorbed UIDs
matched to accepted input links, to locate a parent input-sequence boundary. It
replays the **entire prefix**, starting from the retained import anchor, using
published decisions and original task/provenance IDs. Thus compacted-away older
obligations survive, later parent changes do not enter the branch, and copied
receipts never invoke the classifier again. `branch_boundaries` records the
source, witnesses and cutoff atomically with the independent copy. No text or
time matching is used. The patched host retains `display_kind`, `display_metadata`
(including copied constituent IDs) and message identity across TUI/Desktop
`_persist_branch`, CLI `_BRANCH_COPY_KEYS`, and gateway `_branch_row`; the gateway
also preserves tool UIDs. Historical UID-only compatibility is tested with actual
`SessionDB` and `_persist_branch` calls after explicitly stripping fixture metadata.

Limits are explicit, not silent narrowing:
- Pre-cutover/empty histories with no usable identity retain the parent's current
  obligations and record `no_identity_conservative`. This is **not** a proven
  historical snapshot and may include later parent work.
- A UID-only branch with no journal matches may use a nonempty **pre-journal
  legacy anchor** (`pre_journal_anchor`, empty input prefix) only when it has no
  explicit occurrence IDs, every journal decision is published, and every human
  input has a UID link absent from the copied branch. Original task IDs and the
  anchor survive import/export; no classifier runs. This excludes post-cutover
  journal work, but the legacy snapshot can contain later **pre-cutover** changes:
  it is conservative migration availability, not historical precision.
- Outside that narrow legacy-anchor case, copied identities with no matching
  canonical boundary return a provider error. Explicit unmatched occurrence IDs,
  unlinked/unpublished human inputs, and missing/empty/too-recent anchors cannot
  justify the fallback; no successful receipt is written and retry remains possible.
- Historical UID-only rows may share a UID that later absorbed several inputs.
  Without copied constituent provenance, that UID is ambiguous and still returns
  a degraded provider error rather than choosing a possibly contaminated cutoff.
  Modern patched branch writers on all three surfaces preserve the stronger
  occurrence witness by default; no additional hook is required.

Export (stdout only; no live host write):

```sh
PYTHONPATH=plugins/task-context:. python -m task_context.migration \
  --home /isolated/hermes-home --session SESSION_ID > task-intent-backup.json
```

The result has the old `TaskIntentState` JSON shape plus a lossless
`task_context_export` extension for all tasks, inputs, message links, initial
branch-replay anchor and boundary receipt. Importing that export into an empty
plugin store preserves receipts, exact obligations and historical prefix replay.
Older exports without an anchor are pinned at their imported current state;
branch boundaries before that anchor are not guessed.
The old reader sees full primary/supplements; the old writer's historical caps
still apply if it is reactivated. A deployment owner must account for that before
rollback—export is not authorization to write legacy state or enable two writers.

## Tests

From the repository root, use the host's canonical test runner (set
`HERMES_PYTHON` if the test interpreter is outside the checkout):

```sh
scripts/run_tests.sh tests/plugins/task_context/ -j 2
```

The shared host conftest isolates the home before collection imports. Tests exercise actual SQLite,
concurrent admission/CAS, legacy crash recovery, exports, native compressor
assembly, settings parity, fresh clones, role isolation, restoration and timeout
late-result exclusion. External model responses are mocked; no live deployment,
channel smoke or provider network test is claimed.

Original policy/micro-judge tests were migrated from
`tests/hermes_cli/test_task_intents.py` and `test_task_intent_micro_judge.py`.
Their storage fixture now drives the real plugin SQLite store; machine evidence
is materialized from its separate input table. Assertions are retained except the
new DB location and the documented no-cap retention count. MIT source attribution
is retained in `LICENSE`.
