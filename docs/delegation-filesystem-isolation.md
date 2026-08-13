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

## Enabling profiles for standard CLI and gateway sessions

Standard CLI and gateway agent construction can snapshot operator-owned
profiles from the active Hermes profile's `config.yaml`. Isolation is disabled
unless `enabled` is the boolean `true`:

```yaml
delegation:
  filesystem_isolation:
    enabled: true
    # Required when enabled. Only these named definitions are admitted.
    allowed_profiles:
      - filesystem-isolated
    profiles:
      filesystem-isolated:
        backend: docker
        image: registry.example/hermes-delegation@sha256:<approved-digest>
        default_workdir: /workspace
        allowed_toolsets: [delegation, terminal, file, code, vision]
        qualified_mcp_servers: []
        network: none
        cpu: 1.0
        memory_mb: 512
        shm_mb: 64
        pids_limit: 64
```

`allowed_profiles` must be an explicit, non-empty list of unique names from
`profiles`. The profile definitions use the strict execution-profile parser;
malformed definitions, empty selections, and unknown names abort agent/session
construction rather than silently falling back to ordinary delegation. The
configuration is loaded from the active profile-local Hermes home and is
snapshotted for the lifetime of the session, so changing profiles requires a
new session/agent construction.

Standard admission deliberately starts with an empty visible-object/reveal
ceiling and no backing registry. It therefore supports private scratch children
with no `reveal`, but cannot expose a host path, Docker socket, credential, or
other backing object. A trusted custom initiator may instead pass an explicit
`DelegationSessionPolicy` with `VisibleObjectGrant` entries plus a matching
`BackingObjectRegistry`; that explicit policy remains authoritative and is not
replaced by global configuration. Model-authored `profile`, `workdir`, or
`reveal` arguments can only select or attenuate the trusted snapshot.

Omitting `filesystem_isolation`, or setting `enabled: false`, preserves the
ordinary delegation path and leaves the standard session without a delegation
policy.

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
