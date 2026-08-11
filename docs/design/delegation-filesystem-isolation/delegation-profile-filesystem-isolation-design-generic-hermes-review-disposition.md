# Generic Hermes Review Disposition

Date: 2026-08-10

## Reviewed object

- Design: `delegation-profile-filesystem-isolation-design.md`
- SHA-256: `d6049d25fbd3a9d55aa6e05ea2b14b40a756e442d11f046fc73d701522607da2`
- Size: 31,144 bytes
- Lines: 485

## Independent review

- Review: `delegation-profile-filesystem-isolation-design-generic-hermes-review.md`
- SHA-256: `31f5187c2df4cfc9d10a32ae977e43f7f460a433b5c183ac6a12eb866a5f1c65`
- Size: 17,441 bytes
- Logical lines: 154
- Verdict: **ACCEPT WITH NON-BLOCKING NOTES**
- Design blockers: none

The reviewer verified the exact frozen design identity before interpretation, compared the preserved predecessor, audited all 13 top-level design sections, and checked current Hermes source at the stated revision. Parent-side verification independently confirmed the design and predecessor hashes, verdict consistency, all 13 heading mappings, and 24 cited Hermes source ranges.

## Genericization findings

### Ownership and architecture

**Accepted.** Session admission, execution-profile resolution, reveal validation, attempt routing, durable authority, and lifecycle are defined as generic Hermes runtime/delegation responsibilities. No UI skill owns or implements the boundary.

### Contract independence

**Accepted.** The public examples, profile classes, run paths, lifecycle, acceptance conditions, and review brief are domain-neutral. The names `filesystem-isolated` and `filesystem-isolated-browser` are illustrative session/operator handles, not mandatory Hermes built-ins.

### UI references

**Accepted as bounded use-case material.** Remaining UI, archive, and source-blind language is explicitly labeled as a motivating compatibility case and does not define profile resolution, reveal authority, tool routing, or lifecycle. The core acceptance conditions forbid a dependency on UI roles, packet types, directory layouts, or skills.

### Technical contract

**Accepted unchanged.** The revision preserves path-preserving reveals, scoped-parent attenuation, Docker daemon/backing-object resolution, fresh physical attempts, immutable retry/resume authority, all-tool and host-store parity, revocation, cleanup, MCP/browser qualification, free-form orchestration, and ordinary shared compatibility.

### Threat model and scope

**Accepted unchanged.** The design constrains autonomous child filesystem retrieval. It does not introduce malicious-orchestrator defenses, semantic packet policing, server-owned prompts, rigid domain workflows, or a general authorization system. It remains a product/runtime design contract rather than an implementation checklist.

## Later implementation gates

The review restates existing implementation gates; none was introduced by the genericization delta:

1. propagate `profile`, `workdir`, and `reveal` through every generic delegation surface under immutable protected-session policy;
2. compile a trusted daemon-resolvable backing/mount plan rather than treating the model-visible path as host authority;
3. route every governed local retrieval surface by fresh physical attempt identity and fail closed after revocation;
4. persist and reconstruct immutable profile, reveal, ceiling, and backing-object identity across retry/resume;
5. own cleanup, late-call tombstones, partial-start rollback, and orphan recovery by physical attempt;
6. route or exclude host-side readers, stores, and structured-document parsers;
7. qualify vision, browser, MCP, and indirect host-control channels such as `computer_use` without creating a general authorization redesign;
8. validate every ordinary and protected compatibility condition.

## Adjacent notes

- Keep `computer_use` explicit in later protected-profile inventory and effective-access documentation.
- Keep illustrative profile-handle wording next to generated schemas and operator documentation.
- Do not reopen malicious-orchestrator, semantic packet, server-prompt, general MCP authorization, or rigid workflow policy.

## Reconciliation result

No post-review design edit is warranted. The reviewed artifact correctly defines a reusable Hermes capability with UI investigation as one consumer. The frozen design remains unchanged at the reviewed SHA-256.
