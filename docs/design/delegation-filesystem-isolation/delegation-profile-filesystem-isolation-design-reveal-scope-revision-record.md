# Path-Preserving Reveal-Scope Revision Record

Date: 2026-08-10

## Artifact lineage

- Preserved predecessor: `revisions/delegation-profile-filesystem-isolation-design-source-target-reviewed.md`
- Predecessor SHA-256: `58d267ceefde68da9387146af14c86912baec1fa040df45ad82b98e3f08d001f`
- Revised design: `delegation-profile-filesystem-isolation-design.md`
- Revised frozen SHA-256: `5d35f95edaeff82de1572cb992cdd2c5993365593b269ce3262c63bb337e049b`
- Revised size: 29,547 bytes
- Revised line count: 480

## Authorized design change

Replace the model-facing filesystem contract from arbitrary host `source` plus child `target` rewriting to a path-preserving reveal scope:

```json
{
  "reveal": [
    {
      "path": "/work/ui-runs/job-123/handoffs/builder-1",
      "mode": "ro"
    }
  ]
}
```

A revealed regular file or directory retains the same canonical absolute path in the parent and child agent views. The child receives only selected leaves of the parent's admitted scope; it does not receive the parent root, ancestors' host contents, or siblings merely because their path prefixes overlap.

## Runtime qualification retained

“No model-visible path rewrite” does not mean Docker inherits the parent's scope or that every backend uses identical storage coordinates. Docker bind sources resolve in the daemon's namespace. Trusted runtime code must retain the canonical backing path, named-volume identity, or equivalent storage record; validate the requested reveal against the parent/session ceiling; and construct the child's filesystem view at the same agent-visible destination path.

Parent-private ephemeral files are not revealable. The orchestrator must stage delegable inputs and persistent outputs in admitted run storage.

## Preserved behavior and non-goals

The revision retains generic `delegate_task`, free-form goals and context, model-authored packets, evidence routing, steering, resume, repair loops, and ordinary shared delegation outside protected sessions. It does not add malicious-orchestrator controls, semantic packet policing, server-owned child prompts, or a general MCP authorization redesign.

## Verification state

The revised design passed a local semantic-presence/absence check and `git diff --check` before freezing. Independent strict review is recorded separately and is authoritative for the revised artifact verdict.
