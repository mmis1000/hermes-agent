# Review Reconciliation — Delegation Profile Filesystem Isolation

Date: 2026-08-10<br>
Design reviewed: `delegation-profile-filesystem-isolation-design.md`<br>
Initial design SHA-256: `fa5875fbc349387687a5fde3566d4cbfa78bd165d5addb5ad758da9325a0b5f8`<br>
Initial review SHA-256: `4391e6d7c98d4312eca19d9a135e464b4dc2a80ea3ccaceb5b82459f6b283ec2`<br>
Revised design SHA-256: `58d267ceefde68da9387146af14c86912baec1fa040df45ad82b98e3f08d001f`

## 1. Provenance discrepancy

The strict review is retained unchanged, but it cannot be accepted verbatim as a review of the frozen initial artifact.

The frozen design actually had:

- 20,897 bytes;
- 400 lines;
- SHA-256 `fa5875fbc349387687a5fde3566d4cbfa78bd165d5addb5ad758da9325a0b5f8`.

The review recorded the same hash but claimed the object had 22,939 bytes and 531 lines. It also cited clauses not present in that object, including per-item batch profiles and section contents that conflict with the actual design. One byte sequence cannot have the recorded hash while also having both sets of sizes and contents.

Therefore each finding was checked against the real file and the source evidence rather than accepted from the review's self-assessment.

## 2. Finding dispositions

### F-01 — Rejected as a review-object mismatch

The actual initial design explicitly said that `profile`, `workdir`, and `mounts` were invocation-wide, every batch child received a separate container, and mixed-profile batches required separate `delegate_task` calls. It did not assert per-item profile selection.

The revised design makes the behavior still more explicit: the invocation-wide profile and mount plan are validated before fan-out, and an invalid protected batch is rejected atomically.

### F-02 — Accepted in bounded form

The review correctly identified host-backed stores that container mounts do not constrain. The revised design now classifies those stores explicitly:

- unrestricted session history and host-side skill mutation are absent from protected UI lanes;
- the existing UI methodology remains available as a preloaded or specifically admitted read-only bundle;
- unrelated network and MCP behavior remains unchanged unless it independently exposes prohibited host data.

This closes a local retrieval bypass without introducing semantic packet policing or a general workflow policy engine.

### F-03 — Accepted

A timed-out worker can outlive the reported timeout. Removing its task routing could therefore make a later tool call fall back to the ordinary environment. The revised design adds a protected-attempt lifecycle (`starting → active → revoked → cleaned`), revocation-before-cleanup, and a retained denial tombstone. Revoked, cleaned, unknown-protected, or profile-missing attempts fail closed.

### F-04 — Accepted

The revised design now assigns one physical attempt ID to a complete resource ledger: container, environment routing, processes, browser sessions, file cache/cwd state, mount/evidence grants, and creation records. It explicitly disables both filesystem persistence and cross-process reuse for ephemeral profiles, requires normal-path force removal, and distinguishes physical attempt identity from conversational/session identity.

### F-05 — Accepted as a direct implementation acceptance gate

The source-backed structured-document bypass is relevant to the actual design's all-local-path-tools claim. The revised contract requires notebook, Word, and spreadsheet extraction to consume bytes obtained through the attempt environment or an attempt-owned temporary copy, never a host path directly.

### F-06 — Accepted together with F-02

The underlying host-backed skill/session access issue is valid. The revised design preserves the UI skill via a specifically admitted read-only bundle while excluding unrestricted session history and host mutation from the protected child lane.

### F-07 — Accepted

The broad shared-media-cache exception can bypass an attempt mount set. The revised design requires exact attempt-scoped visual evidence grants in protected attempts while allowing ordinary sessions to preserve their current cache behavior.

### F-08 — Already satisfied by the actual design

The initial design already required a pinned profile hash, image digest where available, effective mounts, workdir, and attempt identity in audit records. No additional change was required.

### F-09 — Not applicable to the actual design

The actual design did not define a `public_web` profile value. It intentionally preserved session-default network behavior and kept broader network policy outside this filesystem design.

### F-10 — Accepted as an adjacent operational qualification

The revised design now states that uncatchable host death can leave orphan resources and requires protected ownership labels suitable for bounded recovery. This does not weaken normal-path teardown or make orphan containers reusable.

### F-11 — Already satisfied by the actual design

The initial design already required the model-facing profile choices to reflect the session allowlist and independently required runtime enforcement.

## 3. Additional correction made during reconciliation

The initial design constrained delegated children but could be read as claiming whole archive-session isolation. A child profile does not constrain local tools used directly by the parent orchestrator.

The revised design therefore distinguishes:

- a fixed protected base execution profile admitted for the top-level archive orchestrator; and
- the child profile allowlist exposed through `delegate_task`.

Child filesystem authority is the intersection of the admitted session roots, selected child profile roots, and invocation mounts. A deployment without the parent base profile may claim child-lane isolation only.

This correction preserves the orchestrator's free-form reasoning, packet construction, feedback, steering, and evidence routing. It changes only the technical local-filesystem ceiling.

## 4. Result

The initial review verdict was `REVISE`. The valid bounded findings were incorporated into the design, while findings based on nonexistent clauses were rejected transparently. Because of the review-object discrepancy, the revised artifact requires a fresh strict review against its exact hash before final acceptance.
