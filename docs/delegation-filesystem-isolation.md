# Protected delegation filesystem isolation

Protected delegation is an opt-in, operator-owned policy for running a delegated child in a private Docker attempt. It does **not** sandbox the parent agent. An ordinary delegation with no trusted `DelegationSessionPolicy` retains the existing shared `default` environment behavior.

## Authority model

Only trusted application code constructs `DelegationSessionPolicy`. Model-authored `profile`, `workdir`, and `reveal` values select or attenuate that policy; they never define images, host paths, mounts, network access, credentials, or tool authority.

```python
from agent.delegation_policy import (
    AccessMode, BackingObjectRef, DelegationSessionPolicy,
    ExecutionProfile, VisibleObjectGrant,
)

profile = ExecutionProfile(
    name="filesystem-isolated",
    backend="docker",
    image="registry.example/hermes-delegation@sha256:<approved-digest>",
    default_workdir="/workspace",
    allowed_toolsets={"terminal", "file", "code", "vision"},
    network="none",
    cpu=1.0,
    memory_mb=512,
    shm_mb=64,
    pids_limit=64,
)

grant = VisibleObjectGrant(
    visible_path="/workspace/input",
    mode=AccessMode.RO,
    object_type="directory",
    backing=BackingObjectRef(
        object_id="dataset-2026-08",
        kind="host_path",
        identity="/operator/approved/dataset",  # daemon-side only
        revision="<pinned identity/revision>",
    ),
)

policy = DelegationSessionPolicy(
    profile_required=True,
    allow_profile_none=False,
    allowed_profiles={profile.name},
    profile_snapshots={profile.name: profile},
    visible_objects=(grant,),
    protected_prefixes=("/workspace",),
)
```

The backing object must also be registered in the trusted `BackingObjectRegistry` with matching identity, revision, and object type. Protected dispatch fails closed if the profile or backing is unknown, stale, missing, replaced, malformed, or outside the session ceiling.

## Delegating

A protected child may request only admitted values:

```json
{
  "goal": "Analyze the admitted dataset",
  "profile": "filesystem-isolated",
  "workdir": "/workspace",
  "reveal": [{"path": "/workspace/input", "mode": "ro"}]
}
```

The canonical visible path is identical in parent and child. A child may narrow `rw` to `ro`, but may not widen `ro` to `rw`, reveal an ancestor/sibling, escape its workdir, add another mount, or select an unapproved profile. Batch preflight is atomic: one invalid item starts zero children.

## Runtime properties

Each protected physical attempt receives a fresh container and private writable root. Strict materialization suppresses ambient Docker volumes, host CWD, credentials, skills, caches, forwarded environment, arbitrary Docker arguments, persistence, and the Docker socket. Only typed, registry-validated reveals are mounted. Backing identity is validated during acquisition and again immediately before `docker run`.

Tool authority is a positive, admission-time snapshot. Later plugin or MCP registration cannot widen it. File, code, terminal, image/vision, and stored web-extract paths route through the physical attempt environment. Host-launched `browser_*` and `computer_use` are not admitted; browser-capable protected profiles use the pinned Playwright image under `containers/delegation-browser/` and save screenshots/traces/downloads inside the private workspace or an explicit `rw` reveal.

Cancellation, timeout, failure, retry, resume, and cleanup revoke the physical attempt before reverse-order teardown. Revoked or cleaned IDs cannot recreate an environment or fall back to ordinary execution. Retry and resume preserve immutable logical authority but allocate a fresh physical attempt.

## Observability

Audit records report the effective profile/hash/image, visible paths and modes, tool snapshot, workdir, scope/attempt lineage, lifecycle state, and implicit-mount suppression. Daemon-side backing identities and credentials are redacted. A path shown in a container is not evidence of host-global access; only admitted host-backed objects carry such authority.

## Rollback and compatibility

Isolation remains opt-in. Stop admitting protected session policies to roll back new protected dispatches. Never downgrade a live protected child to ordinary/shared execution; revoke and terminate it. Existing ordinary sessions continue using their unchanged compatibility path.
