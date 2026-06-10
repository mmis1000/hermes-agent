# Task intent raw-wording preservation execution plan

## Objective

Make Hermes task-intent relationship/completion judges act only as annotators/classifiers. Raw user wording is canonical. A judge may label relationship/completion, cite exact copied spans, or consume ellipsis-clamped raw text for budget, but it must not paraphrase, summarize, normalize, or rewrite user wording.

## Non-negotiable invariants

1. Persist exact raw direct-user message text before skill/context expansion.
2. Persist exact machine-generated continuation/follow-up text when it becomes part of the task contract.
3. Store structured relationship/completion metadata only as annotation.
4. Do not let any judge-authored text replace or become the canonical task wording.
5. If text is too long for a judge, clamp by exact prefix + ellipsis + exact suffix only.
6. Drop/pause previous requirements only on explicit replacement/stop/ignore instruction or a future high-confidence non-rewriting relevance judge; absent that, keep active raw context.
7. Completion/done vetoes must reason from preserved raw wording, not rewritten summaries.

## Implementation phases

### Phase 1 — Append-only raw state

- Add `RawTaskMessage` records to `hermes_cli/task_intents.py`.
- Add `raw_messages` to `TaskIntentState` with backward-compatible loading.
- On every direct user message, append a raw record instead of overwriting the active raw contract.
- Preserve compatibility fields (`raw_text`, `task_contract.raw_primary_text`, `task_contract.raw_supplements`) as derived/legacy views, but never use judge output to populate them.

### Phase 2 — No-rewrite helper surface

- Add `clamp_raw_text(text, max_chars)`:
  - return exact text if within budget;
  - otherwise exact prefix + `…` + exact suffix.
- Add `validate_judge_payload_no_rewrite(payload)`:
  - reject/strip forbidden keys such as `summary`, `rewritten_task`, `normalized_user_request`, `cleaned_text`, `paraphrase`;
  - validate any evidence quotes are exact substrings if quotes are used.
- Prefer span offsets in judge annotations; do not introduce any rewrite fields.

### Phase 3 — Conservative relationship policy

- Keep explicit stop/switch/ignore patterns only as high-precision replacement detection.
- Treat ordinary follow-ups while a task is active as `supplement` or `unclear`, not `new_task`.
- Do not classify a message as `new_task` merely because it lacks a supplement prefix.
- Preserve previous raw task text even when a latest message is ambiguous.

### Phase 4 — Gateway / goal wiring

- Ensure `gateway/run.py` keeps using `_raw_inbound_user_text` for task-intent state.
- Ensure internal/generated continuation messages do not become fresh direct-user tasks.
- Ensure notices quote raw stored text verbatim.
- Keep `/goal` done-veto using raw goal/task contract text.

### Phase 5 — Regression tests

Add or adjust tests for:

- exact raw primary text persistence;
- supplement append without primary overwrite;
- conservative follow-ups such as `what flag did you use?` and `then materialize the full plan`;
- explicit replacement/stop required to supersede old requirements;
- ellipsis clamping exactness;
- forbidden judge rewrite fields rejected/ignored;
- notices quote raw preserved text;
- multiplicity done veto for direct messages, supplements, machine-preserved messages, and `/goal`.

### Phase 6 — Verification

Run:

```bash
python -m pytest tests/hermes_cli/test_task_intents.py -q -o 'addopts='
python -m pytest tests/hermes_cli/test_goals.py -q -o 'addopts='
python -m pytest tests/hermes_cli/test_task_intents.py tests/hermes_cli/test_goals.py -q -o 'addopts='
git diff -- hermes_cli/task_intents.py hermes_cli/goals.py gateway/run.py tests/hermes_cli/test_task_intents.py tests/hermes_cli/test_goals.py plans/task-intent-raw-wording-preservation-plan.md
git status --short --branch
```

## Rollout note

If gateway code/state behavior changes, the patch is on disk only until the running gateway is restarted. Do not claim live Discord behavior changed unless a restart is performed and verified.
