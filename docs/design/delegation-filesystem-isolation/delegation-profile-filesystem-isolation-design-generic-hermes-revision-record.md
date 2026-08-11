# Generic Hermes Capability Revision Record

Date: 2026-08-10

## Reason for revision

The reviewed reveal-scope design was technically acceptable, but its framing treated UI/archive investigation as the architectural owner. The capability is instead generic Hermes delegation infrastructure. UI investigation is one consuming workflow among software development, research, data processing, document analysis, browser work, and other flows that need delegated-child filesystem isolation.

## Artifact lineage

- Preserved reviewed predecessor: `revisions/delegation-profile-filesystem-isolation-design-ui-use-case-reviewed.md`
- Predecessor SHA-256: `5d35f95edaeff82de1572cb992cdd2c5993365593b269ce3262c63bb337e049b`
- Predecessor review: `delegation-profile-filesystem-isolation-design-reveal-scope-review.md`
- Revised design: `delegation-profile-filesystem-isolation-design.md`
- Revised SHA-256: `d6049d25fbd3a9d55aa6e05ea2b14b40a756e442d11f046fc73d701522607da2`
- Revised size: 31,144 bytes
- Revised lines: 485

## Authorized delta

- Reframe ownership from a UI/archive feature to a generic Hermes runtime/delegation feature.
- Use generic protected-session, run-root, profile, input, workspace, and worker examples.
- Make profile handles illustrative server/session definitions rather than UI-owned or mandatory Hermes built-ins.
- Keep UI source-blind investigation as one explicit motivating and compatibility use case.
- Add an acceptance condition that core schema and enforcement contain no UI-specific role, packet, layout, or skill dependency.

## Technical contract intentionally unchanged

- Generic `delegate_task` with free-form `goal` and `context` remains.
- The model selects among trusted session-admitted profile handles.
- Public child access uses path-preserving `reveal: [{path, mode}]`.
- Trusted runtime code resolves backing objects and constructs the child filesystem view.
- Child authority is a subset of the parent/session ceiling and cannot upgrade access mode.
- Every protected attempt gets a fresh isolated environment.
- Retry/resume retains profile, reveal, and backing-object identity without widening.
- Every governed local retrieval surface must share the same attempt environment or be excluded/qualified.
- Ordinary unprofiled delegation remains unchanged outside protected sessions.
- The threat model constrains autonomous child filesystem retrieval, not orchestrator-authored semantics.

## Non-goals of this revision

- No Hermes implementation work.
- No cross-platform expansion beyond the already designed deployment qualifications.
- No malicious-orchestrator controls, semantic packet policing, server-owned prompts, or general authorization framework.
- No UI workflow redesign.

## Review state

The exact frozen artifact was independently reviewed in `delegation-profile-filesystem-isolation-design-generic-hermes-review.md`.

- Review verdict: **ACCEPT WITH NON-BLOCKING NOTES**
- Review SHA-256: `31f5187c2df4cfc9d10a32ae977e43f7f460a433b5c183ac6a12eb866a5f1c65`
- Design blockers: none

The disposition is recorded in `delegation-profile-filesystem-isolation-design-generic-hermes-review-disposition.md`. No post-review design edit was made.
