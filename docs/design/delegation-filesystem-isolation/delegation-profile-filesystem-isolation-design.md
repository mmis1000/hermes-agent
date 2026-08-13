# Hermes Delegation Execution Profiles and Filesystem Isolation — Design Plan

Date: 2026-08-10  
Status: Revised as a generic Hermes capability; pending strict re-review  
Scope: Product and runtime contract; not an implementation plan

## 1. Decision summary

Hermes should retain the existing model-orchestrated `delegate_task` workflow and add an optional, named **execution profile** that places each delegated child attempt in a dedicated container with an explicit filesystem view.

This is a generic Hermes delegation/runtime capability. It is not owned by, embedded in, or limited to a UI investigation skill. UI investigation is one motivating consumer; software development, research, data processing, document review, and other delegated workflows may use the same contract without adopting UI-specific methodology.

For ordinary Hermes sessions, omitting the profile preserves the current shared delegation environment.

For protected sessions, trusted session configuration supplies an allowlist of execution profiles and disables the ordinary unprofiled/default delegation mode. The orchestrator remains free to choose among the allowed profiles and continues to provide normal free-form goals, context, task or evidence packets, feedback, steering, and resumes. The profile constrains the child’s technical execution environment; it does not replace the caller's domain workflow or move orchestration decisions into a rigid server workflow.

The first contract is deliberately narrow:

- prevent delegated children from independently reading filesystem material that was not revealed into their container;
- prevent protected runs from reaching Hermes credentials, Hermes source, the host home directory, unrelated projects or runs, or sibling workspaces through governed filesystem tools;
- preserve current orchestration and evidence-routing behavior;
- support a browser-ready profile for Playwright and visual analysis;
- keep existing delegation behavior unchanged when no protected session policy is active.

## 2. Objective

### 2.1 Generic delegated filesystem isolation

A protected delegated workflow should be able to read its authorized inputs, use its approved tools and MCP services, and produce writable scratch/output artifacts without gaining autonomous access to unrelated host material. The capability must be selectable by any trusted Hermes session initiator and usable by any domain workflow that needs a proper child filesystem boundary.

### 2.2 UI investigation as one motivating use case

In the current UI investigation use case, a source-blind builder, reviewer, comparator, or other bounded worker should be technically unable to search sibling or source-aware filesystem lanes merely because a model forgets or disobeys a prompt restriction. The UI orchestrator still owns packet construction and routing under its existing skill, but that methodology is a consumer of the generic Hermes boundary rather than the feature definition.

Nothing in the Hermes runtime contract names UI roles, archive objects, source-blind packets, or a UI directory layout. Each consuming workflow supplies its own session policy, profile handles, reveal paths, and domain semantics.

The desired guarantee is:

> A delegated child cannot autonomously retrieve local filesystem material outside the path-preserving reveal scope and container-local state granted to its selected execution profile and invocation.

It is not a guarantee that a trusted orchestrator cannot communicate information to its own child through the goal, context, or an authorized packet.

## 3. Authority and threat model

### 3.1 Actors

- **Trusted session initiator:** caller or Hermes service that admits a protected session and selects its session-level delegation policy; the UI archive server is one example.
- **Orchestrator:** model-driven coordinator that retains normal `delegate_task` behavior inside the admitted policy.
- **Delegated child:** potentially less reliable model whose filesystem exploration must be technically bounded.
- **Hermes runtime:** trusted enforcement layer that resolves profiles, validates reveal scopes, routes governed tools, and owns container lifecycle.
- **Container runtime:** operating-system isolation mechanism.

### 3.2 In-scope failures

- A child recursively searches a parent or sibling directory.
- A child uses an absolute path outside its assigned workspace.
- A child’s terminal, file tool, or code execution reaches the shared default container.
- An implicit credential, cache, current-working-directory, global-volume, or host-home mount makes forbidden files visible despite the explicit reveal list.
- Parallel child lanes see each other’s ephemeral files or shared workspace by default.
- A protected-session child omits the profile and falls back to ordinary shared delegation.
- A resumed or reconstructed child silently loses its original profile boundary.
- Profile-specific artifacts exist only inside the child container and cannot be consumed or preserved as intended; browser screenshots are one example.

### 3.3 Explicitly out of scope

This design does not attempt to defend against a malicious orchestrator that deliberately places prohibited facts in a child’s prompt or authorized packet. It does not replace caller- or domain-owned rules for semantic sanitization, contamination handling, evidence freezing, source-blind feedback, or worker reuse.

Also out of scope:

- server-owned child prompts;
- banning or replacing `delegate_task`;
- a hard-coded workflow-transition state machine;
- prohibiting ordinary free-form goal/context fields;
- prohibiting orchestrator-created or orchestrator-routed packets;
- automatic semantic leakage detection;
- general outbound-network denial;
- general MCP authorization redesign;
- changing the existing steer/resume protocol beyond retaining the selected execution boundary;
- erasing model pretraining or recognition of visible identities;
- running the entire child Hermes process inside the container;
- treating model/provider allowlists as security controls;
- changing ordinary delegation defaults outside protected sessions.

### 3.4 Boundary qualification

This is a **filesystem-complete tool boundary**, not whole-agent process isolation. Every child tool that can directly read or write a local path must either resolve through the same child container or be excluded from the filesystem-isolation claim.

MCP services remain independent capability channels. An MCP that can read host files can disclose those files regardless of the child container. Approved MCPs may remain available normally, but the isolation claim covers an MCP only when that MCP’s own authority does not expose prohibited host filesystem material.

## 4. Core concepts

### 4.1 Execution profile

An execution profile is a server/operator-owned, named sandbox policy. It is distinct from a Hermes configuration profile.

A profile may define:

- pinned container image or image digest;
- default container workdir;
- suppression of implicit mounts and inherited global volumes;
- permitted reveal roots and maximum access mode;
- reserved/protected absolute path prefixes;
- container persistence policy;
- resource settings needed by the workload;
- browser runtime requirements where applicable;
- network behavior where the profile intentionally differs from the session default;
- tool-routing requirements needed to keep local-path access inside the boundary.

The orchestrator selects a profile by name. It does not define or mutate the profile.

### 4.2 Session delegation policy

A protected orchestrator session carries trusted, immutable delegation constraints conceptually equivalent to:

```yaml
delegation_policy:
  profile_required: true
  allow_profile_none: false
  allow_raw_container_spec: false
  allowed_profiles:
    - filesystem-isolated
    - filesystem-isolated-browser
```

The profile names shown here are illustrative server/session-defined handles, not UI-owned names or mandatory Hermes built-ins. The policy is attached by the trusted session-initiator/runtime path, not supplied by model text. It is pinned for the admitted session so later profile-registry changes do not silently widen or alter that run.

An ordinary Hermes session may retain:

```yaml
delegation_policy:
  profile_required: false
  allow_profile_none: true
```

### 4.3 Protected session base environment

The child-profile allowlist constrains delegation; it does not by itself constrain local-path tools used directly by the parent orchestrator. A protected run that claims the **whole orchestrated session** cannot inspect the host therefore also receives a fixed base execution profile at admission.

That base profile routes the orchestrator's governed local-path tools into an isolated session environment with only the run-level staging roots and approved durable material it needs. It excludes Hermes credentials and source, the host home directory, unrelated projects or runs, Docker control endpoints, and other sessions. The orchestrator may construct and route packets freely within those admitted roots.

The delegation policy is then an attenuation of that session ceiling:

```text
effective child filesystem authority
  = admitted session roots
  ∩ selected child profile roots
  ∩ invocation reveal scope
```

Selecting a child profile does not reprofile the already-running orchestrator, and no allowed child profile can grant a host root absent from the admitted session ceiling. If a deployment constrains only delegated children and does not assign a base environment to the parent, it may claim child isolation only—not whole-session host isolation.

### 4.4 Path-preserving reveal scope

The orchestrator may continue to select dynamic input and output directories for a child. The public contract names each directory once:

```json
{
  "path": "/work/hermes-runs/job-123/inputs/worker-1",
  "mode": "ro"
}
```

- `path` is an absolute path in the parent orchestrator's admitted filesystem namespace.
- The child sees the revealed object at that same absolute path.
- `mode` is exactly `ro` or `rw`.

There is no model-visible host-source/container-target rewrite. The runtime resolves the path through the parent's admitted backing-storage table and establishes an identity mapping for the child. A revealable object is a regular file or directory already backed by admitted run storage; output locations are normally directories.

With local Docker and host-backed run storage, the lower-level operation may be conceptually:

```text
-v /work/hermes-runs/job-123/inputs/worker-1:/work/hermes-runs/job-123/inputs/worker-1:ro
```

That identity bind works only when the Docker daemon can resolve the source path in its own host namespace. Docker does not derive or inherit the parent agent's logical filesystem scope. If the parent is itself containerized, a path visible inside that parent may not exist at the same daemon-side host path. Trusted runtime code must therefore retain a canonical backing-path or volume record and construct the child view from that record; it must not blindly pass the parent-visible string to Docker as the source.

A deployment may use a server-created Docker named volume or another backend-specific mechanism underneath while preserving the same canonical agent-visible destination path. Backing paths, volume IDs, and raw Docker mount specifications are trusted runtime state, not model arguments. “No path rewrite” is an agent-facing contract, not a claim that every backend uses identical storage coordinates internally.

The parent may see the complete admitted job root while a child receives only selected leaf files or directories. Creating the ancestor directory structure inside the child does not reveal unmounted siblings or ancestors' host contents. A file that exists only in the parent's private ephemeral container root is not revealable; anything intended for delegation must first be written into admitted run storage.

The selected profile and session policy define the maximum filesystem authority. An invocation may reveal a subset of that authority but cannot exceed it.

### 4.5 Protected host-backed data sources

Some Hermes tools read named server-local stores without accepting an arbitrary path—for example skill stores, session history, or shared media caches. Container reveal scopes do not constrain those tools, so protected profiles must classify them explicitly rather than treating them as ordinary container-routed tools.

For strict filesystem-isolated profiles:

- unrestricted session-history search and host-side skill mutation are not part of the child filesystem lane;
- a domain methodology remains usable by preloading its approved skill/document bundle or by allowing read-only skill access only to the specifically admitted bundle;
- other network and MCP tools remain unchanged unless they independently expose prohibited host-local data;
- tool-specific stores admitted intentionally are recorded separately from the container reveal list.

This is a narrow closure of local retrieval paths. It does not inspect prompt semantics, constrain evidence wording, or replace the orchestrator's packet decisions.

## 5. `delegate_task` contract

### 5.1 Public shape

The existing tool remains. Its protected-session extension is conceptually:

```json
{
  "goal": "Process the supplied inputs and write the requested outputs",
  "context": "Normal standalone assignment text",
  "profile": "filesystem-isolated",
  "workdir": "/work/hermes-runs/job-123/workspaces/worker-1",
  "reveal": [
    {
      "path": "/work/hermes-runs/job-123/inputs/worker-1",
      "mode": "ro"
    },
    {
      "path": "/work/hermes-runs/job-123/workspaces/worker-1",
      "mode": "rw"
    }
  ]
}
```

`profile`, `workdir`, and `reveal` apply to the complete invocation. In a batch, the same profile declaration applies to all items, while each child attempt receives a distinct container. Mixed-profile batches use separate `delegate_task` calls.

Hermes validates the invocation-wide profile and complete reveal plan before starting any item in a batch. A protected batch is rejected atomically if that shared plan is invalid; it never starts a partial fan-out under an unvalidated or fallback environment.

### 5.2 Ordinary-session behavior

When `profile` is omitted and the session does not require one, Hermes preserves the current shared parent/child delegation environment without behavioral change.

### 5.3 Protected-session behavior

When the session requires a profile:

- `profile` is mandatory;
- the available model-facing values are the session’s allowed profile names;
- `none`, `default`, an empty value, omission, and unknown profiles fail closed before child creation;
- raw image, Docker argument, capability, host-source/container-target mapping, or unrestricted container specifications are unavailable;
- the normal `goal`, `context`, single/batch dispatch, steering, and result behavior remain available.

A representative failure is:

```text
delegate_task: profile is required in this session. Unprofiled/default delegation is disabled. Choose one of: filesystem-isolated, filesystem-isolated-browser.
```

The displayed schema is guidance; the runtime enforces the same rule independently.

### 5.4 Reveal validation contract

Before a child container exists, Hermes validates that:

- `path` is absolute, normalized, exists, and is visible in the parent orchestrator's admitted namespace;
- `path` is backed by storage marked revealable by the parent base profile rather than by the parent's private container root;
- canonical and symlink resolution cannot escape the parent reveal root;
- `mode` is `ro` or `rw`;
- requested access does not exceed the parent grant: parent `rw` may attenuate to child `rw` or `ro`, while parent `ro` permits child `ro` only;
- the path remains identical in parent and child agent views;
- duplicate or overlapping reveals with conflicting modes are rejected;
- protected absolute path prefixes cannot be shadowed;
- device files, sockets, Docker control endpoints, and other explicitly forbidden object types are rejected;
- validation and backing-object attachment do not leave a substitutable path interval;
- no hidden or automatic reveals are added after validation in strict profiles.

This validation prevents delegation from becoming a route to `/`, the host home directory, Hermes directories, credential stores, or the Docker socket while preserving exact orchestrator-selected paths under the admitted run roots.

### 5.5 Workdir contract

`workdir` is an absolute path inside the child container after reveals are established. It must be either inside an appropriately writable revealed directory or a valid container-local directory. Setting it never creates an implicit reveal or host-current-directory mount.

## 6. Profile semantics

### 6.1 `filesystem-isolated`

Purpose: general delegated workload requiring a technically bounded child filesystem.

Required properties:

- fresh container per child attempt;
- ephemeral container root;
- no automatic Hermes credential, source, skill, cache, home, current-directory, persistent-workspace, or global-volume mounts;
- explicit path-preserving RO/RW reveals only;
- container-routed terminal, file, and code-execution paths;
- only explicitly admitted read-only host-backed data bundles, with session history and host-side mutation absent by default;
- writable container-local temporary space;
- deterministic cleanup;
- session-default network and approved MCP behavior preserved unless separately configured.

### 6.2 `filesystem-isolated-browser`

Purpose: the same filesystem boundary with browser execution and screenshot production.

Additional properties:

- pinned Playwright package and matching browser binary;
- required browser libraries, CA certificates, and deterministic fonts;
- headless Chromium by default;
- approximately 2 CPU, 4 GiB memory, 1 GiB shared memory, and a browser-suitable PID ceiling as an operator-tunable baseline;
- explicit artifact/output reveal when screenshots, traces, downloads, or video must survive cleanup;
- full Chromium, Xvfb, and optional display transport only when a separately selected headed workload requires them;
- no Docker socket, privileged mode, or host IPC requirement.

Playwright may run directly through the containerized terminal. A screenshot written to a container path must remain readable by `vision_analyze` through the same task environment. If an MCP-based browser service runs outside the container, its authority is reported separately and is not misrepresented as container-contained.

### 6.3 Profile count

The initial design uses the smallest useful profile set: one general isolated profile and one browser-ready variant. Domain roles do not automatically require separate profiles when different explicit reveal sets provide the needed filesystem lanes. Additional profiles require an actual difference in execution policy, not merely a role name.

## 7. Runtime and lifecycle behavior

### 7.1 Isolation unit

Each single child attempt receives a fresh container. Each child in a batch receives a different container even though the invocation-wide profile declaration is shared.

A deliberately shared RW reveal remains shared state; separate containers do not change that fact.

### 7.2 Persistence and resume

The durable record stores the resolved declarative profile identity/hash, workdir, validated reveal specification, and trusted backing-object identity or manifest revision—not an ephemeral container ID or only a path string.

A resumed child:

- retains the same profile and reveal declaration;
- receives a fresh container;
- sees only state preserved through explicit RW reveals;
- reattaches only the originally validated backing objects under the pinned session ceiling and fails closed if they cannot be re-established;
- cannot switch profile or add authority as part of resume.

Normal conversational continuity and existing caller- or workflow-defined worker reuse remain unchanged.

### 7.3 Attempt authorization state

A protected physical attempt has an authorization state independent of whether its container routing entry currently exists:

```text
starting → active → revoked → cleaned
```

Every governed tool dispatch checks that state. A revoked, cleaned, unknown-protected, or profile-missing attempt fails closed; it never falls back to the ordinary shared/default environment.

Timeout and interruption revoke the attempt before resource teardown. Because a blocked worker thread may outlive the reported timeout, the runtime retains a denial tombstone after cleanup for as long as a late call can still arrive. Removing mutable environment routing is not equivalent to restoring ordinary authority.

### 7.4 Cleanup and resource ownership

One physical attempt ID owns its complete resource ledger, including:

- container and environment routing;
- background processes;
- browser sessions;
- file-operation environment/cache and cwd state;
- reveal grants and attached visual-evidence grants;
- creation locks and other attempt-scoped runtime records.

The owner removes or revokes those resources on:

- successful completion;
- child exception;
- cancellation or interruption;
- timeout;
- partial startup failure;
- parent shutdown cleanup.

An ephemeral profile disables both persistent container filesystem reuse and cross-process container reuse. Teardown force-removes the attempt container when normal cleanup can run. Cleanup is idempotent, keyed by the physical attempt ID used for tool routing rather than the child's conversational/session identity, and removes only resources owned by that attempt. It must not collapse back to or delete the ordinary shared environment.

Uncatchable host death can leave an orphan resource despite these normal-path guarantees. Protected resources therefore carry attempt ownership labels suitable for bounded orphan recovery, without making them reusable by a later attempt.

### 7.5 Tool consistency

All governed local-path tools for a child must resolve the same effective environment, workdir, and reveals regardless of which tool creates the environment first. A terminal-isolated child whose file tool still reads the host does not satisfy this design.

Structured-document support is part of the same rule. Notebook, Word, and spreadsheet extraction must consume bytes obtained through the attempt's environment or an attempt-owned temporary copy; a container-style path must never be opened directly by a host-side parser.

`vision_analyze` may receive a container path and resolve its bytes through the task-specific environment. Images that must remain available after container cleanup must be written to an explicit RW artifact reveal.

A protected attempt may read host-cached visual bytes only through an exact attempt-scoped evidence grant. General membership in a shared Hermes media-cache directory is not authorization. Ordinary sessions may preserve their current broad cache behavior.

## 8. Preservation of existing orchestration and domain workflows

This generic Hermes feature intentionally does not alter an orchestrator’s methodological authority. Any consuming workflow may continue to:

- write standalone free-form child assignments;
- construct and audit domain-specific task or evidence packets;
- choose which authorized folders to reveal;
- route complete reviewer evidence and bounded feedback;
- steer active workers and resume persistent workers;
- apply its own contamination or integrity rules;
- preserve rejected evidence and truthful workflow status;
- use its existing repair budgets, review authority, publication rules, and domain-specific acceptance criteria.

If an orchestrator passes prohibited information through text or reveals a semantically incorrect but technically authorized packet, the consuming workflow's integrity rules apply. The runtime does not attempt to replace those rules with a new policy engine. In the UI investigation use case, this preserves its existing source-aware/source-blind packet, feedback, repair, and review behavior without making those concepts part of the generic Hermes API.

The profile changes one fact only: a child can no longer independently traverse the host filesystem beyond its effective reveals merely because the model disregards or forgets a folder restriction.

## 9. MCP, browser, and external capability boundary

MCP services remain available according to the admitted Hermes session and selected profile. The design does not add a general MCP policy system.

The effective-access report must distinguish:

- container-routed filesystem authority;
- host- or network-side MCP authority;
- browser authority and location;
- network access.

An MCP that exposes broad host filesystem reads is incompatible with a claim that the child can access only revealed files unless the MCP is independently constrained. This is an audit requirement, not a reason to disable unrelated MCPs.

For Playwright:

- direct Playwright inside `filesystem-isolated-browser` is container-contained;
- a task-scoped MCP inside that container may later provide equivalent containment;
- an existing host-global Playwright MCP remains usable if intentionally allowed, but its browser process is outside this filesystem boundary and must be labeled as such.

## 10. Effective configuration and observability

For each profiled child, Hermes records enough non-secret information to audit the boundary:

- selected profile name and pinned profile hash;
- container image digest when available;
- resolved workdir;
- effective reveals with canonical `path`, `mode`, non-secret backing class, and a non-secret backing-identity or manifest-revision reference;
- implicit-mount suppression status;
- admitted host-backed data bundles and exact visual-evidence grants;
- attempt authorization state and revocation outcome;
- environment/attempt owner identity;
- browser mode where applicable;
- MCP/browser location qualification where applicable;
- creation and cleanup outcome.

Credentials remain redacted. Paths and ordinary operational details are preserved rather than replaced by generic status.

This record supports a truthful distinction between:

- **enforced filesystem lane:** prohibited local paths were absent from the child’s governed execution environment and reveal set;
- **instruction-only lane:** technical paths or external tools still exposed prohibited material;
- **workflow contamination:** the orchestrator or an authorized packet supplied prohibited semantics despite the filesystem boundary.

## 11. Design acceptance conditions

The design is acceptable when all of the following are true:

1. Ordinary `delegate_task` calls without a profile retain current shared behavior outside protected sessions.
2. A protected session cannot use unprofiled/default delegation or a profile outside its pinned allowlist.
3. A whole-session isolation claim requires an admitted base environment for the parent orchestrator; otherwise the claim is explicitly child-only.
4. The orchestrator retains ordinary goals, context, packet construction, steering, resume, and evidence-routing behavior.
5. Every child attempt receives its own container and ephemeral root.
6. A child can read only explicit path-preserving reveals, container-local files, and specifically admitted host-backed bundles through every governed local retrieval path.
7. No implicit Hermes credential, source, home, cache, workspace, or global-volume reveal appears in a strict profile.
8. Unrestricted session history, global skill reads/mutation, and cache-wide visual reads cannot bypass the protected attempt boundary; explicitly admitted read-only skill bundles remain usable.
9. Structured-document extraction reads through the attempt environment rather than opening the host path.
10. RO reveals reject writes; RW reveals preserve intended outputs at the same absolute path visible to the parent.
11. Sibling containers cannot see each other except through an intentionally shared reveal.
12. Resume reconstructs the same declarative boundary in a fresh container.
13. Revoked, cleaned, or late protected attempts fail closed and never collapse to the ordinary default environment.
14. Success, failure, cancellation, timeout, and partial startup clean the attempt-owned resource ledger and force-remove normal-path ephemeral containers.
15. Playwright can render and save screenshots inside the browser-ready profile, and visual analysis can consume those screenshots before cleanup, from an explicit artifact reveal, or through an exact attempt-owned visual grant.
16. Effective records truthfully distinguish container filesystem access from admitted host-backed stores and external MCP/browser authority.
17. The design makes no claim to prevent orchestrator-authored semantic leakage, malicious orchestration, model memorization, or access through an independently broad MCP.
18. The core schema and enforcement require no UI-specific role, packet type, directory layout, or skill; a UI workflow may supply those only as application-level policy and content.

## 12. Deferred decisions

These choices are intentionally deferred until implementation planning or concrete deployment binding:

- the profile-registry configuration location and exact serialization format;
- exact names of the initial profiles;
- whether canonical reveal paths are backed by host bind mounts or server-created named volumes in each supported Docker deployment;
- exact resource defaults for browser workloads on different hosts;
- whether task-scoped Playwright MCP is needed after direct Playwright proves sufficient;
- exact approval UX for host bind access outside trusted-service-controlled runs;
- whole-agent container execution;
- heterogeneous per-item profiles inside one batch.

None of these deferred choices changes the accepted behavioral boundary described above.

## 13. Review brief

A strict design review should judge this document only against the following frozen object:

- it must technically bound child filesystem exploration with path-preserving reveal scopes;
- it must remain a generic Hermes capability and keep orchestrators and consuming domain workflows intact; the UI investigation skill is one compatibility case, not the feature boundary;
- it must retain `delegate_task` and allow the orchestrator to select among session-approved profiles;
- it must disable unprofiled/default delegation only in protected sessions;
- it must preserve ordinary delegation elsewhere;
- it must not reintroduce malicious-orchestrator defenses, server-owned prompts, rigid workflow state machines, or needless restrictions on evidence routing;
- it must state honestly what containers do not constrain, especially MCP and host-side browser authority.

Findings that require broader workflow redesign, semantic packet policing, removal of free-form delegation, or a general authorization framework are adjacent proposals, not blockers for this design.
