# Reveal-Scope Review Disposition

Date: 2026-08-10

## Reviewed object

- Design: `delegation-profile-filesystem-isolation-design.md`
- SHA-256: `5d35f95edaeff82de1572cb992cdd2c5993365593b269ce3262c63bb337e049b`
- Size: 29,547 bytes
- Lines: 480

## Independent review

- Review: `delegation-profile-filesystem-isolation-design-reveal-scope-review.md`
- SHA-256: `4733b5fbf35e4a7641dea74df8b6b4cd73f66576d653c58ecb3d283324129404`
- Size: 27,411 bytes
- Logical lines: 196
- Verdict: **ACCEPT WITH NON-BLOCKING NOTES**
- Design blockers: none

The review verified the exact design identity before interpretation and cited 13 existing design headings plus 33 valid current-source ranges. An independent parent-side check confirmed the design hash/size/line count, verdict consistency, heading citations, and source citation bounds after the reviewer finalized the artifact.

## Finding disposition

### F-1 through F-10

**Accepted as confirmation of the design.** The reviewer found the path-preserving reveal contract coherent for a scoped parent, correctly qualified against Docker daemon namespace behavior, narrow enough to preserve ordinary orchestration, and complete at design-contract level across resume, batches, tool parity, browser/vision/MCP qualification, lifecycle, and ordinary-session compatibility.

The current Hermes mismatches listed by the reviewer are direct implementation gates. They do not require a further design edit because this artifact intentionally defines the target product/runtime contract rather than a source-file implementation plan.

### Direct implementation gates

**Accepted as release gates for any later implementation claim.** In particular:

- compile child reveals from trusted parent backing records rather than trusting the model-visible path as a Docker source;
- validate and pin the complete reveal plan before child side effects;
- cover symlink, hard-link, overlap, special-file, destination collision, TOCTOU, mode attenuation, and post-assembly workdir cases;
- preserve immutable reveal and backing-object identity across retry/resume;
- route every governed local retrieval surface by physical attempt identity and never fall protected attempts back to `default`;
- suppress ordinary automatic host mounts in strict profiles;
- constrain, disable, or truthfully exclude indirect host-control surfaces such as `computer_use`, along with direct path tools, host-backed stores, browsers, plugins, hooks, caches, spill paths, and filesystem-capable MCPs;
- retain ordinary shared behavior outside protected sessions.

This records conformance criteria without turning the design into an implementation checklist.

### Adjacent notes

1. **Target-side mountpoint hardening:** accepted for a later implementation/test plan; already within the design's protected-prefix, canonicalization, and no-substitution requirements.
2. **Deployment assumptions:** accepted as documentation work after a concrete backend choice; the design intentionally defers host-bind versus named-volume binding while requiring stable agent-visible paths.
3. **Maintained host-store manifest:** recorded as a useful implementation-maintenance option, not authority to add a general authorization framework.
4. **Browser wording:** already satisfied by the design's location-specific reporting and external-authority qualification.
5. **Threat model:** confirmed unchanged; no malicious-orchestrator, semantic-packet, or server-owned-prompt controls are added.

## Reconciliation result

No post-review design edit is warranted. The non-blocking notes refine later implementation and verification obligations without changing the accepted path-preserving reveal-scope contract. The frozen design remains unchanged at the reviewed SHA-256.
