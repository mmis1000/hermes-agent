# Spike 001: reveal-tree intersection

## Question

Can delegated reveal authority be modeled as an intersectable path-policy tree that:

1. preserves a deeper parent RO carve-out when a child selects a broader RW subtree;
2. supports a deeper parent RW exception beneath an RO ancestor;
3. never grants more authority than either the parent policy or the child request; and
4. produces a compact reveal list suitable for the existing `{path, mode}` representation?

## Model

A policy is a finite set of canonical absolute path rules. The effective mode at a path is the mode of its most-specific matching ancestor rule.

```text
none < ro < rw
```

The mathematical child policy is the pointwise minimum:

```text
effective_child(path) = min(parent(path), child_request(path))
```

The solver evaluates the union of parent and child transition paths and emits only points where the resulting effective mode changes. Separately, it reports any explicit child rule whose requested mode exceeds the parent's effective mode at that exact path. This preserves Hermes' current fail-closed behavior instead of silently accepting an explicit widening request.

## Files

- `solver.py` — standalone library and CLI
- `test_solver.py` — executable acceptance cases

## Run

```bash
pytest -q spikes/001-reveal-tree-intersection/test_solver.py
python spikes/001-reveal-tree-intersection/solver.py
```

Custom policy:

```bash
python spikes/001-reveal-tree-intersection/solver.py \
  --parent /a=rw \
  --parent /a/b/c=ro \
  --child /a/b=rw
```

An inadmissible explicit request exits with status `2` while still printing the mathematical intersection for diagnosis.

## Observed cases

| Parent | Child request | Canonical intersection | Admission |
|---|---|---|---|
| `/a rw`, `/a/b/c ro` | `/a/b rw` | `/a/b rw`, `/a/b/c ro` | accepted |
| `/a ro`, `/a/b rw` | `/a ro` | `/a ro` | accepted; broad RO caps the RW exception |
| `/a ro`, `/a/b rw` | `/a ro`, `/a/b rw` | `/a ro`, `/a/b rw` | accepted |
| `/a ro`, `/a/b rw` | `/a/b rw` | `/a/b rw` | accepted |
| `/a ro`, `/a/b rw` | `/a rw` | `/a ro`, `/a/b rw` | rejected because `/a rw` explicitly exceeds the parent at `/a` |
| `/a ro` | `/x ro` | empty | rejected as outside the parent scope |

The test suite also covers input-order independence, alternating nested transitions, redundant-rule compression, duplicate paths, and noncanonical paths.

## Verdict: VALIDATED

### What worked

- Both restrictive and permissive descendant exceptions are representable with the existing path/mode shape.
- Pointwise tree intersection automatically carries the deeper RO restriction in the motivating inheritance case.
- A child can receive a parent RW exception beneath RO only by selecting that exception explicitly; a broad child RO request does not accidentally widen itself.
- The result can be normalized into a compact ordered reveal list without adding a new policy type or database shape.

### What did not change

- This spike does not modify production Hermes resolution or Docker materialization.
- It does not decide whether future product semantics should silently intersect explicit widening requests. The spike keeps the current rejection behavior and exposes the safe mathematical result only as diagnostic evidence.

### Recommendation for the real build

Implement the same longest-prefix intersection in `tools/delegation_scope.py`: resolve the effective child transition list first, validate explicit requests against the parent at their exact paths, then pass the expanded list through the existing child-policy, persistence, and mount paths.
