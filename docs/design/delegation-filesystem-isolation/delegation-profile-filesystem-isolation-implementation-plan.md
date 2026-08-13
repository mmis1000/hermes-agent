# Delegated Filesystem Isolation Implementation Plan

> **For the implementer:** Use the `test-driven-development`, `implementation-validation-policy`, and `bounded-change-validation` skills. Implement one red/green/refactor slice at a time. Do not enable protected execution until every release gate in this plan passes.

**Goal:** Add generic Hermes delegation infrastructure that can give each delegated-child attempt a fresh, path-preserving, attenuated filesystem view without changing ordinary unprofiled delegation.

**Architecture:** A trusted session policy pins the parent's delegable filesystem ceiling and admitted execution profiles. `delegate_task` validates invocation-wide `profile`, `workdir`, and `reveal` fields against that ceiling before any child runs. Each protected physical attempt receives immutable resolved authority, a fresh Docker environment, explicit identity mounts, and a task key that all governed tools share. Resume reconstructs the saved authority in another fresh attempt; cleanup revokes authority before removing attempt resources.

**Reviewed input:** `docs/design/delegation-filesystem-isolation/delegation-profile-filesystem-isolation-design.md`

**Planning baseline:** Hermes revision `6b83b37056049432d864198bbab3e2ed5d1fd43f`.

---

## 1. Scope lock

### 1.1 Required guarantee

A protected delegated child cannot autonomously retrieve filesystem material that its parent/session did not reveal into that child's isolated environment.

Enforce these invariants:

```text
resolved_visible_objects(child) subset_of resolved_visible_objects(parent/session)
visible_path_in_child(object) = visible_path_in_parent(object)
access_mode(child, object) <= access_mode(parent/session, object)
```

### 1.2 Preserve unchanged

- Generic `delegate_task`, free-form `goal` and `context`, single and batch dispatch.
- Model/provider/reasoning overrides.
- Child role selection, nested orchestrators, steering, interruption, retries, resume, live transcripts, and result routing.
- Ordinary-session behavior: when no protected policy requires a profile and `profile` is omitted, task IDs continue to collapse to the shared/default environment exactly as today.
- Prompt caching: the model-facing schema must not include per-attempt backing paths, object IDs, or mutable scope state.

### 1.3 Explicit non-goals

- Do not treat the trusted/model-driven parent orchestrator as malicious.
- Do not police what the parent writes into `goal` or `context`.
- Do not build a general authorization framework or whole-agent process sandbox.
- Do not expose raw Docker mounts, images, capabilities, `extra_args`, host sources, or child targets to the model.
- Do not reprofile an already-running parent. V1 makes the child-isolation claim only. A consumer may separately place the parent in a base environment if it needs a whole-session claim.
- Do not change ordinary Docker sharing, persistence, or current-directory behavior outside protected attempts.

### 1.4 V1 backend and profile boundary

- Enforce protected attempts with the existing Docker backend first. Reject unsupported protected backends; never fall back to local/shared execution.
- Support the two profile classes in the reviewed design and provide operator-owned examples; do not hard-code their names as mandatory Hermes built-ins:
  - an illustrative `filesystem-isolated` profile for general terminal/file workloads and code run through those existing scoped paths;
  - an illustrative `filesystem-isolated-browser` profile with pinned Playwright/Chromium dependencies and browser resource defaults.
- The browser-ready profile supports Playwright inside the scoped container through terminal/code execution. Existing host-launched `browser_*` tools remain excluded until they gain a task-scoped runner.
- Current host-control tools (`computer_use`) and unqualified filesystem-capable MCPs remain excluded from protected children.
- Preserve current delegate hard blocks, including `execute_code` where the existing child policy blocks it. Task 7 makes the code-execution environment seam scope-correct for any independently admitted protected agent; this plan does not use filesystem isolation as authority to unban that tool for ordinary leaf children.

---

## 2. Frozen implementation contracts

### 2.1 Trusted policy types

Create `agent/delegation_policy.py` with immutable, credential-free types conceptually equivalent to:

```python
class AccessMode(str, Enum):
    RO = "ro"
    RW = "rw"

@dataclass(frozen=True)
class BackingObjectRef:
    object_id: str
    kind: Literal["host_path", "named_volume"]
    identity: str             # trusted runtime value, never model-visible
    revision: str

@dataclass(frozen=True)
class VisibleObjectGrant:
    visible_path: PurePosixPath
    mode: AccessMode
    backing: BackingObjectRef
    object_type: Literal["file", "directory"]

@dataclass(frozen=True)
class DelegationSessionPolicy:
    profile_required: bool
    allow_profile_none: bool
    allowed_profiles: frozenset[str]
    profile_snapshots: Mapping[str, "ExecutionProfile"]
    visible_objects: tuple[VisibleObjectGrant, ...]
    protected_prefixes: tuple[PurePosixPath, ...]
```

Define the credential-free `ExecutionProfile` snapshot in this same module so `run_agent.py` does not depend on tool implementation code. Freeze nested mappings with an immutable representation such as sorted tuples or `MappingProxyType`; `frozen=True` alone is not sufficient for a mutable `dict` value. `tools/delegation_scope.py` parses operator config into these snapshots and owns runtime/backing behavior.

Add an internal-only `delegation_policy` keyword to `AIAgent.__init__` and `agent.agent_init.init_agent`. An ordinary agent gets an explicit ordinary policy or `None`; a trusted session initiator passes the protected policy object. Do not derive this policy from model text.

A protected orchestrator child receives a new policy whose visible-object ceiling equals that child's resolved authority. This is what makes nested delegation attenuate rather than regain the original parent's ceiling.

### 2.2 Execution profile registry

Create `tools/delegation_scope.py` to parse the operator-owned registry from a narrow config block such as:

```yaml
delegation:
  filesystem_isolation:
    enabled: false
    profiles:
      filesystem-isolated:
        backend: docker
        image: ghcr.io/nousresearch/hermes-agent-sandbox@sha256:...
        default_workdir: /workspace
        allowed_toolsets: [web, terminal, file]
        qualified_mcp_servers: []
        network: inherit
        cpu: 2
        memory_mb: 4096
        pids_limit: 512
      filesystem-isolated-browser:
        backend: docker
        image: ghcr.io/nousresearch/hermes-agent-browser-sandbox@sha256:...
        default_workdir: /workspace
        allowed_toolsets: [web, terminal, file, vision]
        qualified_mcp_servers: []
        network: inherit
        cpu: 2
        memory_mb: 4096
        shm_mb: 1024
        pids_limit: 512
```

The parser accepts only fixed profile policy fields. It rejects unknown authority-expanding keys such as volumes, host paths, forwarded environment variables, capabilities, privileged mode, Docker socket, host IPC, or arbitrary runtime arguments.

Pin each admitted session to canonical profile snapshots and hashes. A registry reload must not silently widen an existing session.

### 2.3 Backing-object registration

The trusted session initiator registers revealable objects before delegation. A model-supplied path is never used directly as a Docker source.

For local Docker V1:

- Backing objects must live in server-owned run storage or a server-owned named volume.
- The registry records canonical visible path, backing identity, object type, mode ceiling, device/inode or volume identity, and manifest revision.
- The backing object's parent anchor must not be replaceable by a delegated child.
- Reject root symlinks, special files, sockets, devices, Docker sockets, protected-prefix collisions, and unresolved objects.
- Revalidate the stored identity immediately before materialization. A changed identity/revision fails closed.
- Reject RW grants whose inode is shared with an object outside the admitted RW set. RO hard links may be admitted only when the revealed object itself is intentionally readable.

This restriction is deliberate: arbitrary host bind roots can be added later only with an equally strong non-substitution mechanism.

### 2.4 Public invocation shape

Extend top-level `delegate_task` only:

```python
profile: Optional[str] = None
workdir: Optional[str] = None
reveal: Optional[list[dict[str, str]]] = None
```

Do not add these fields to individual `tasks[]` items. A batch has one shared profile/workdir/reveal declaration and fresh physical authority per item.

The ordinary-session schema describes `profile` as an optional string and `reveal` as `{path, mode}` items. For a protected agent, Hermes copies/post-processes that schema so `profile.enum` contains only the session-pinned allowed profile handles. The tool-definition cache key includes the immutable policy schema fingerprint (required/optional state plus allowed profile names), never reveal paths, backing IDs, or mutable attempt state. Runtime policy remains independently authoritative.

An agent without a trusted policy cannot activate a protected profile merely by naming it. In an ordinary session, omitted `profile`, `workdir`, and `reveal` preserve legacy behavior; supplying `workdir` or `reveal` without an admitted protected profile is rejected rather than silently ignored.

### 2.5 Resolved attempt authority

`tools/delegation_scope.py` owns immutable records with at least:

- scope ID and fresh physical attempt ID;
- logical child ID and delegation/run IDs when known;
- pinned profile name/hash and image digest;
- canonical workdir;
- original reveal declaration;
- effective visible objects with mode and non-secret backing class/reference;
- effective tool allow/deny sets;
- lifecycle state: `starting`, `active`, `revoked`, `cleaned`;
- attempt-owned resource ledger.

Never use the logical subagent ID as the protected environment identity. Retry/resume always allocates another physical attempt ID.

---

## 3. Coherent implementation sequence

### Execution ownership and continuation rule

This section is one coherent implementation assignment owned by one implementation lineage. The numbered stages below are dependency-ordered engineering and verification checkpoints; they are **not** delegation boundaries, separate ownership units, or reasons to create narrower replacement tasks.

- Keep one stable implementation goal and acceptance contract from the first unfinished stage through Stage 15.
- If an execution attempt reaches a tool, token, time, or infrastructure limit, resume the same logical worker with the instruction to continue the existing assignment from the exact current state. Do not redefine, subdivide, rename, or re-dispatch the work merely because an attempt ended.
- Preserve the same working tree, implementation ownership, TDD evidence, and unresolved checklist across continuations.
- Change ownership or split the assignment only for a genuine architectural boundary, an independent validation role, a concrete blocker requiring another specialty, or explicit user direction.
- Treat attempt boundaries as internal execution bookkeeping. Do not present each continuation as a new user-facing task or milestone.

### TDD execution rule inside the coherent assignment

Treat every numbered or bulleted behavior below as its own RED/GREEN microcycle within the same implementation assignment. Create only importable interface scaffolding when a new module is needed, with unimplemented functions raising `NotImplementedError`. Then, for one behavior at a time: add one test, run that exact test node and confirm an assertion or `NotImplementedError` failure (not a collection/import failure), implement the minimum behavior, rerun the exact node to green, and only then continue.

Run the narrow affected regression file after each coherent cluster of related microcycles and run each stage-level command once before crossing that dependency checkpoint. Do not rerun a complete stage suite after every individual assertion when the exact node already proves the microcycle; that consumes execution budget without improving the contract.

### Current implementation checkpoint

- Stages 1–5: implemented; the last aggregate run reported 453 passing targeted tests.
- Stage 6: implementation completed through durable authority tombstones and restart reconstruction; final canonical and Stages 1–6 aggregate verification was interrupted before it ran.
- Stages 7–15: not started.
- Working tree: uncommitted; frozen design/review artifacts remain unchanged.

## Stage 1: Add pure policy and attenuation primitives

**Files**

- Create: `agent/delegation_policy.py`
- Create: `tests/agent/test_delegation_policy.py`

**RED**

Create importable interface scaffolding with no domain behavior. Then execute one RED/GREEN microcycle for each of:

1. RO/RW attenuation (`rw -> ro|rw`, `ro -> ro`, reject `ro -> rw`).
2. Absolute POSIX visible paths only.
3. Exact path identity preservation.
4. Immutable profile snapshots and visible-object grants.
5. Child-policy derivation from resolved child authority.
6. Rejection of duplicate/conflicting grants.

Run after each RED microcycle:

```bash
pytest -q tests/agent/test_delegation_policy.py
```

**GREEN**

Implement only the minimum immutable data type, normalization, or attenuation helper required by the current test. No Docker or global registry access belongs in this module.

Run after each GREEN microcycle:

```bash
pytest -q tests/agent/test_delegation_policy.py
```

**REFACTOR**

Keep value objects serialization-safe and add round-trip tests for the credential-free forms used by durable delegation.

---

## Stage 2: Add strict execution-profile and backing-object resolution

**Files**

- Create: `tools/delegation_scope.py`
- Create: `tests/tools/test_delegation_scope.py`
- Modify: `tools/delegate_tool.py:3978-4014` only to call the new parser through the existing read-only delegation config loader.

**RED**

Create importable resolver/profile interfaces that raise `NotImplementedError`. Then execute one RED/GREEN microcycle for each of these failures before container creation:

- protected policy + omitted/empty/`none`/`default`/`shared` profile;
- ordinary policy + explicit unadmitted profile, or `workdir`/`reveal` without a profile;
- unknown, disallowed, stale, or non-Docker profile;
- malformed reveal declaration or mode;
- relative path, `..` normalization escape, nonexistent object;
- reveal outside the parent/session ceiling;
- RW request over parent RO;
- duplicate/overlapping conflicting reveals;
- root symlink or changed backing revision;
- `/`, host home, Hermes home/source, credentials, protected prefixes, device/socket, and `/var/run/docker.sock`;
- workdir outside valid container-local space or outside an RW reveal when it targets revealed storage.

Also test valid leaf-only reveals without siblings and valid RW-to-RO attenuation.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_delegation_scope.py
```

**GREEN**

Implement only the minimum branch required by the current test, converging on:

- strict profile parser and canonical hash;
- backing-object registry interface;
- `resolve_invocation_scope(policy, profile, workdir, reveal)`;
- deterministic, user-facing errors;
- no side effects during resolution.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegation_scope.py
```

**REFACTOR**

Separate pure validation from runtime registration so atomic batch preflight can validate once and instantiate N attempt records later.

---

## Stage 3: Extend `delegate_task` schema and dispatch without changing ordinary behavior

**Files**

- Modify: `tools/delegate_tool.py:3065-3297`
- Modify: `tools/delegate_tool.py:4185-4373`
- Modify: `run_agent.py:6451-6484`
- Modify: `agent/agent_init.py:1222-1234`
- Modify: `model_tools.py:279-463` (including `get_tool_definitions`, its cache key, and definition assembly)
- Modify: `tests/tools/test_delegate.py`
- Create: `tests/tools/test_delegate_filesystem_scope.py`

**RED**

Add tests that assert:

- `profile`, `workdir`, and `reveal` exist only at invocation level;
- a protected agent's copied schema enumerates only its pinned allowed profile handles and includes `profile` in `required`, while an ordinary agent retains the generic optional field;
- agents with the same policy fingerprint reuse the schema cache, while different allowed-profile sets cannot share a poisoned schema object;
- registry and `run_agent._dispatch_delegate_task()` forward all three fields;
- free-form goal/context and routing overrides are unchanged;
- an ordinary parent with omitted profile follows the legacy path exactly;
- a protected parent rejects invalid scope before `create_live_transcripts`, `_build_child_agent`, executor submission, or Docker calls;
- a protected batch resolves the complete shared scope once and starts zero children when validation fails;
- no dedicated structured authority fields exist for host source, container target, raw image, volumes, capabilities, or Docker arguments;
- arbitrary prose mentioning those concepts remains valid inside free-form `goal` and `context`.

Run:

```bash
pytest -q tests/tools/test_delegate.py tests/tools/test_delegate_filesystem_scope.py
```

**GREEN**

Call preflight after task-list validation at `delegate_tool.py:3185-3219` and before live transcript/child construction at `3227-3258`. Pass the resolved template into `_build_child_agent`; do not alter assignment text. Apply the profile-enum overlay through a policy-aware `get_tool_definitions` path or equivalent per-agent copy, with the immutable schema fingerprint in its cache key.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegate.py tests/tools/test_delegate_filesystem_scope.py
```

**REFACTOR**

Use one forwarding helper for the registry fallback and `run_agent` intercept so future invocation-wide fields cannot drift.

---

## Stage 4: Allocate and bind fresh protected physical attempts atomically

**Files**

- Modify: `tools/delegate_tool.py:3249-3707`
- Modify: `tools/async_delegation.py`
- Modify: `tools/delegation_repository.py:180-302`
- Modify: `tests/tools/test_async_delegation.py`
- Modify: `tests/tools/test_delegation_repository.py`
- Modify: `tests/tools/test_delegation_durable_lifecycle.py`

**RED**

Test that:

- single, batch, nested synchronous, retry, replacement, and resume attempts each get a unique physical attempt ID;
- protected parallel batch members never share an environment key;
- supplied initial attempt IDs are registered in the same SQLite transaction as the dispatch record;
- authority registration failure starts no runner and revokes every `starting` record;
- ordinary child IDs still collapse to `default` when no protected authority exists.

Run:

```bash
pytest -q tests/tools/test_async_delegation.py \
  tests/tools/test_delegation_repository.py \
  tests/tools/test_delegation_durable_lifecycle.py \
  tests/tools/test_shared_container_task_id.py
```

**GREEN**

Preallocate attempt IDs after child logical IDs exist but before any runner starts. Extend `dispatch_async_delegation_batch` to accept the trusted mapping, persist it, then bind child agents and activate scope records. For synchronous nested children, allocate and activate local attempt records through the same scope registry.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_async_delegation.py \
  tests/tools/test_delegation_repository.py \
  tests/tools/test_delegation_durable_lifecycle.py \
  tests/tools/test_shared_container_task_id.py
```

**REFACTOR**

Remove callback ordering assumptions: runner submission must require all protected children to be in `active` state.

---

## Stage 5: Propagate the attenuated policy into child and nested orchestrator construction

**Files**

- Modify: `run_agent.py:424-573`
- Modify: `agent/agent_init.py`
- Modify: `tools/delegate_tool.py:1398-1537`, `1590-1760`, and `1934-1978`
- Modify: `tools/delegate_tool.py:2405-2645`
- Modify: `tests/tools/test_delegate_toolset_scope.py`
- Modify: `tests/tools/test_delegate_filesystem_scope.py`

**RED**

Test that:

- protected children receive the intersection of caller-requested and profile-allowed toolsets, followed by current role hard blocks;
- an orchestrator child retains generic `delegate_task` but its session ceiling equals only its own effective reveals;
- a grandchild reveal can attenuate that ceiling but cannot regain a grandparent sibling/root;
- leaf/orchestrator role behavior and existing blocked-tool logic remain unchanged;
- `_run_single_child` uses physical attempt ID as tool `task_id`.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_delegate_toolset_scope.py tests/tools/test_delegate_filesystem_scope.py
```

**GREEN**

Add `delegation_policy` to agent initialization. Pass the derived child policy and protected tool restrictions through `_build_child_agent`. Keep model/provider credentials and prompt construction unchanged.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegate_toolset_scope.py tests/tools/test_delegate_filesystem_scope.py
```

**REFACTOR**

Keep runtime authority off the model-facing prompt. Only a concise, non-authoritative workdir hint may be shown.

---

## Stage 6: Persist immutable authority and reconstruct resume without widening

**Files**

- Modify: `tools/async_delegation.py:327-560`
- Modify: `tools/delegation_repository.py`
- Modify: `tools/delegate_tool.py` resume metadata/build helpers
- Modify: `tests/tools/test_delegation_transcript_resume.py`
- Modify: `tests/tools/test_delegation_repository.py`
- Create: `tests/tools/test_delegation_scope_resume.py`

**RED**

Add an immutable, versioned `DelegationAuthoritySpec` stored once under `delegation_logical_subagents.spec_json["authority"]`. Reuse this existing immutable logical-child specification instead of adding another column. It is separate from mergeable attempt `metadata_json` and child result observations. Ordinary historical rows may omit the key; protected logical children require it, and repository methods reject attempts to mutate the logical spec after creation.

The spec contains the selected profile snapshot/hash, workdir, original reveal declaration, effective visible-object ceiling, protected prefixes, nested orchestrator's pinned allowed profile snapshots/hashes, positive tool/toolset and qualified-capability snapshot, and daemon-resolvable backing object identity/revision. It contains no credentials or ephemeral container ID.

Test that the durable record contains that exact immutable authority and no credentials or container IDs. Test resume outcomes:

- same authority + valid backing objects -> fresh attempt/container;
- changed invocation scope -> rejected;
- missing/stale profile or backing revision -> `resume_unavailable`;
- omitted protected metadata -> no fallback to ordinary/default;
- RW reveal state persists because backing storage persists;
- private container scratch does not persist;
- transcript replay remains child-only.
- after clearing all in-memory scope/profile/backing registries to simulate process restart, a resumed orchestrator reconstructs the same derived ceiling and pinned profile allowlist from `spec_json["authority"]`;
- trusted backing re-resolution from the durable identity succeeds only when the same object/revision is available and otherwise fails closed;
- an attempted repository update cannot merge over or replace `spec_json["authority"]`.

Run:

```bash
pytest -q tests/tools/test_delegation_transcript_resume.py \
  tests/tools/test_delegation_repository.py \
  tests/tools/test_delegation_scope_resume.py
```

**GREEN**

Extend the existing logical-child `spec_json` projection and write the immutable authority key in the same transaction that creates the logical child. Do not add a parallel persistence column. Keep lifecycle/results in mutable attempt metadata. Extend resume bundle validation and `dispatch_resumed_subagent` to require the spec for protected children. Reserve a fresh attempt, reconstruct the child ceiling/profile allowlist, re-resolve the pinned backing authority, activate it, and only then construct/submit the resumed runner.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegation_repository.py tests/tools/test_delegation_transcript_resume.py tests/tools/test_delegation_scope_resume.py
```

**REFACTOR**

Centralize versioned credential-free authority serialization in `tools/delegation_scope.py`; do not hand-maintain field lists in multiple modules. `_RESUME_METADATA_FIELDS` may continue to carry mutable runtime fields, but it is not the authority source of truth.

---

## Stage 7: Centralize task-environment acquisition before adding protected materialization

**Files**

- Modify: `tools/terminal_tool.py:1125-1227` and `2156-2339`
- Modify: `tools/file_tools.py:928-1097`
- Modify: `tools/code_execution_tool.py:687-787`
- Modify: `tests/tools/test_shared_container_task_id.py`
- Modify: `tests/tools/test_file_tools_container_config.py`
- Modify: `tests/tools/test_code_execution_modes.py`

**RED**

Capture current ordinary behavior in tests, including:

- plain child/session IDs collapse to `default`;
- CWD-only overrides do not isolate;
- benchmark image/backend overrides do isolate;
- terminal, file, and execute-code use the same cached environment regardless of which tool runs first;
- existing container config and CWD sanitization are preserved.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_shared_container_task_id.py tests/tools/test_file_tools_container_config.py tests/tools/test_code_execution_modes.py
```

**GREEN**

Extract one `acquire_task_environment(raw_task_id, *, timeout=None)` path in `terminal_tool.py` and make terminal, file, and code execution call it. It returns the environment, backend, and effective task key. Add `delegation_scope_id` to the set of isolation keys.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_shared_container_task_id.py tests/tools/test_file_tools_container_config.py tests/tools/test_code_execution_modes.py
```

**REFACTOR**

Delete the duplicated environment-construction blocks only after parity tests pass. Do not change ordinary config precedence in this task.

---

## Stage 8: Materialize protected Docker environments from structured trusted mounts

**Files**

- Modify: `tools/terminal_tool.py` environment acquisition/config assembly
- Modify: `tools/environments/docker.py:568-884`
- Modify: `tests/tools/test_docker_environment.py`
- Modify: `tests/tools/test_docker_cgroup_limits.py`
- Create: `tests/tools/test_delegation_scope_docker_args.py`

**RED**

Inspect captured `docker run` argv and assert for protected attempts:

- one fresh container per physical attempt;
- structured identity mounts use trusted backing source and canonical visible destination;
- RO/RW flags match attenuation;
- no global `docker_volumes`, host CWD, persistent workspace/home, credentials, skills, caches, forwarded env, global env, or `extra_args`;
- no Docker socket, privileged mode, host PID/IPC/network namespace, or host capabilities;
- ephemeral `/workspace`, `/home`, and `/root` scratch exists;
- profile resources/network are applied;
- typed `shm_mb` and `pids_limit` become `--shm-size` and `--pids-limit` arguments rather than raw `extra_args`; preserve current `_cgroup_limits_available()` gating so PID limits are omitted, not fatal, on unsupported hosts;
- labels include protected scope/attempt IDs;
- ordinary `DockerEnvironment` mount behavior remains unchanged.
- substitution-race case: preflight succeeds, then the backing inode/manifest/volume identity changes; materialization fails before any `docker run` call.

Run:

```bash
pytest -q tests/tools/test_docker_environment.py \
  tests/tools/test_docker_cgroup_limits.py \
  tests/tools/test_delegation_scope_docker_args.py
```

**GREEN**

Add internal-only `trusted_mounts`, `suppress_implicit_mounts`, `shm_mb`, and configurable `pids_limit` constructor inputs. The trusted mount compiler revalidates every backing identity/revision while attaching the mount plan immediately before container creation; a mismatch aborts without invoking Docker. Generate Docker `--mount` arguments from typed resolved mounts. In strict mode bypass lines `650-787` that add user/global/credential/skill/cache mounts. Set persistence off and `persist_across_processes=False`.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_docker_environment.py tests/tools/test_docker_cgroup_limits.py tests/tools/test_delegation_scope_docker_args.py
```

**REFACTOR**

Keep the ordinary volume-string parser isolated from trusted mount assembly; never convert trusted mounts back into model/user-authored volume strings.

---

## Stage 9: Make terminal, file, code, process, and workdir behavior scope-complete

**Files**

- Modify: `tools/terminal_tool.py`
- Modify: `tools/file_tools.py`
- Modify: `tools/code_execution_tool.py`
- Modify: `tools/process_registry.py`
- Modify: `tools/file_operations.py` if byte/stat operations are missing
- Create: `tests/tools/test_delegation_scope_tool_parity.py`

**RED**

For each tool-first order (file -> terminal, code -> file, terminal -> code), test:

- same physical environment and canonical workdir;
- revealed file readable, hidden sibling and parent root absent;
- RO write fails and RW write persists to backing object;
- relative paths resolve from validated workdir without creating reveals;
- background processes are keyed to the attempt and killed on revocation;
- `process` poll/log/wait/write/submit/close/kill calls cannot address a process owned by another protected attempt, even when its process ID is guessed or disclosed;
- late process/file calls after revocation fail closed instead of creating a default environment.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_delegation_scope_tool_parity.py
```

**GREEN**

Make every environment acquisition check protected authority state first. A known protected attempt in `revoked` or `cleaned` state returns a terminal error and cannot collapse. Route file/stat/byte operations through the scoped environment. Forward the calling task ID into process handlers and enforce owner-attempt equality for protected process sessions.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegation_scope_tool_parity.py
```

**REFACTOR**

Use one fail-closed lookup function for every governed tool; avoid scattered `if protected` policy logic.

---

## Stage 10: Remove host-side structured-document and visual read escapes

**Files**

- Modify: `tools/file_tools.py:1126-1148`
- Modify: `tools/read_extract.py`
- Modify: `tools/image_source.py:89-145` and host-cache permission logic
- Modify: `tools/vision_tools.py` only where task context is not forwarded
- Modify: `tests/tools/test_read_extract.py`
- Modify: `tests/tools/test_image_source.py`
- Modify: `tests/integration/test_vision_docker_resolve.py`
- Create: `tests/tools/test_delegation_scope_host_readers.py`

**RED**

Test that protected tasks:

- extract `.ipynb`, `.docx`, and `.xlsx` from bytes read through their scoped environment;
- never call host `Path.read_bytes`, `open`, or `os.path.getsize` on a container-visible path;
- analyze a revealed image and an attempt-created screenshot through the scoped environment;
- cannot use media-cache path exceptions to read unrelated host cache material;
- reject hidden host image/document paths even if those paths exist on the Hermes host.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_image_source.py tests/tools/test_read_extract.py tests/integration/test_vision_docker_resolve.py tests/tools/test_delegation_scope_host_readers.py
```

**GREEN**

Add byte-oriented document extractors (`extract_document_bytes`) and scoped stat/read helpers. For vision, make `ResolveContext.task_id` authoritative: protected local paths always use the scoped environment unless the path is an exact attempt-owned visual grant. Replace global-only `_is_local_terminal_backend()` decisions with task-aware environment/scope lookup for protected attempts. Exact grants are runtime records, not prefix-wide cache exceptions.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_image_source.py tests/tools/test_read_extract.py tests/integration/test_vision_docker_resolve.py tests/tools/test_delegation_scope_host_readers.py
```

**REFACTOR**

Delete direct host path assumptions from structured-document code. Preserve current ordinary/local behavior and existing URL/data-URL handling.

---

## Stage 11: Qualify tool surfaces and external capabilities per profile

**Files**

- Modify: `tools/delegate_tool.py` profile tool-policy integration
- Modify: `model_tools.py` only as needed to preserve the immutable positive allowlist across tool-definition refresh
- Modify: `tools/mcp_tool.py:5964-6069` to expose operator-owned capability metadata and preserve qualification during dynamic refresh, not model choices
- Modify: `tools/web_tools.py:480-577`, `743-1000`, and `1224-1232` to route `_store_full_text`/`_truncate_with_footer`, `web_extract_tool`, its truncation call, and the registered handler through protected `task_id`
- Modify: `tests/tools/test_delegate_toolset_scope.py`
- Create: `tests/tools/test_delegation_scope_capabilities.py`

**RED**

Test that strict profiles:

- use a positive operator-owned toolset allowlist and retain only approved non-filesystem network tools;
- exclude a newly registered/unclassified tool automatically rather than relying on a denylist that will become stale;
- exclude `computer_use`, host-side browser launch, unrestricted skill reads/mutation, session history, host-backed memory stores, host-path media generation, and host-global filesystem MCPs unless an exact bundle/output grant is implemented;
- disable the current `browser`, `computer_use`, `video`, `image_gen`, `tts`, `skills`, `session_search`, and `memory` toolsets by default; `vision` may be enabled only after Task 10's scoped image path tests pass;
- prevent `video_analyze`, local-path `image_generate`, and `text_to_speech(output_path=...)` from becoming host read/write side channels merely because they are not terminal tools;
- permit `delegate_task` only for orchestrator role as today;
- allow an MCP only when its server config has an operator-owned qualification that does not expose prohibited host-local filesystem material;
- preserve the allowlist across MCP refresh, plugin/tool-registry generation changes, and agent tool-definition rebuilds;
- apply the protected snapshot as the final filter after normal toolset expansion, `HERMES_KANBAN_TASK` auto-addition, context-engine injection, and other non-registry additions;
- when `web_extract` exceeds the inline budget, write the full text into attempt-private scoped storage and return that container-visible path; if scoped writing fails, return bounded inline text without creating or disclosing a host-cache path;
- report external MCP/browser authority separately from container filesystem authority.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_delegate_toolset_scope.py tests/tools/test_delegation_scope_capabilities.py
```

**GREEN**

Have each profile produce a session-pinned positive tool/toolset snapshot, role-required additions (for example `delegate_task` for an orchestrator), current delegate hard blocks, and qualified MCP names. Intersect caller-requested `enabled_toolsets` with the profile allowlist; a caller may narrow but never widen it. Reapply the pinned snapshot as the final filter after every normal expansion/injection path. Default unknown plugin/local tools and unknown MCP capability to excluded for protected attempts, including tools registered after admission. Thread `task_id` into `web_extract`; preserve its ordinary host-cache behavior only outside protected attempts. Do not globally disable unrelated MCPs or network tools outside those attempts.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegate_toolset_scope.py tests/tools/test_delegation_scope_capabilities.py
```

**REFACTOR**

Keep capability qualification independent of domain roles and UI workflow names.

---

## Stage 12: Deliver the browser-ready profile without claiming host browser containment

**Files**

- Create: `containers/delegation/Dockerfile`
- Create: `containers/delegation-browser/Dockerfile`
- Create: `containers/delegation/README.md`
- Create: `tests/integration/test_delegation_scope_browser_profile.py`

**RED**

Under the browser profile, integration-test that:

- pinned Playwright and matching Chromium launch headlessly;
- a page renders using deterministic fonts/CA bundle;
- screenshot, trace, and download paths stay inside private scratch or an explicit RW reveal;
- `vision_analyze` consumes the screenshot before cleanup through the same task environment;
- artifacts in private scratch disappear after cleanup;
- artifacts in explicit RW reveal survive;
- Docker socket, privileged mode, host IPC, and host browser profile are absent;
- host-launched `browser_*` tools remain unavailable.

Run on a Docker-capable host:

```bash
pytest -q -m docker tests/integration/test_delegation_scope_browser_profile.py
```

**GREEN**

Pin Node, Playwright, Chromium, required libraries, fonts, and CA certificates in the browser sandbox image. Select approximately 2 CPU, 4 GiB RAM, 1 GiB shared memory, and a browser-suitable PID limit in the operator profile; Task 8's typed Docker plumbing applies and tests those values.

Run after each GREEN microcycle:

```bash
pytest -q -m docker tests/integration/test_delegation_scope_browser_profile.py
```

**REFACTOR**

Document that browser execution is containerized Playwright through terminal/code. A later scoped `browser_*` runner is a separate enhancement, not required for this profile's truthful claim.

---

## Stage 13: Revoke first, then clean every attempt-owned resource

**Files**

- Modify: `tools/delegation_scope.py`
- Modify: `tools/delegate_tool.py:2965-3039`
- Modify: `tools/async_delegation.py` completion/cancellation/recovery paths
- Modify: `tools/terminal_tool.py:1636-1840`
- Modify: `run_agent.py:3833-3865` only as needed for protected child cleanup
- Modify: `tools/environments/docker.py` cleanup/reaper labels
- Create: `tests/tools/test_delegation_scope_cleanup.py`

**RED**

Cover success, child error, construction failure, batch partial startup, cancellation, timeout, resume failure, replacement, parent shutdown/process loss, and repeated cleanup. Assert:

1. authority becomes `revoked` before process/container teardown;
2. late tool calls fail closed;
3. background processes die;
4. file-op/env/browser caches clear;
5. ephemeral containers are force-removed regardless of ordinary persistence settings;
6. cleanup is idempotent;
7. orphan reaping identifies protected attempt labels without touching active sibling attempts;
8. disabling the registry during a live protected attempt terminates/revokes it and retains its durable tombstone instead of downgrading it to ordinary/default execution.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_delegation_scope_cleanup.py tests/tools/test_delegation_durable_lifecycle.py
```

**GREEN**

Implement an attempt resource ledger and one `revoke_and_cleanup_attempt()` path. Call it from every protected terminal state. Retain a non-secret revoked/cleaned tombstone for at least the delegation-record lifetime (and across process recovery when durable) so a late known attempt ID cannot become "unknown" and collapse to `default`. Do not rely on `AIAgent.close()` using `session_id`, because protected tool calls are keyed by physical attempt ID.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegation_scope_cleanup.py tests/tools/test_delegation_durable_lifecycle.py
```

**REFACTOR**

Make cleanup outcomes observable without logging backing paths or credentials.

---

## Stage 14: Add truthful observability and operator documentation

**Files**

- Modify: delegation lifecycle event/record formatting in `tools/delegate_tool.py`, `tools/async_delegation.py`, and `tools/delegation_repository.py`
- Create: `website/docs/user-guide/delegation-filesystem-isolation.md`
- Modify: `website/docs/user-guide/configuration.md`
- Modify: package README under `docs/design/delegation-filesystem-isolation/`
- Create: `tests/tools/test_delegation_scope_observability.py`

**RED**

Test redacted records for:

- profile name/hash and image digest;
- physical attempt ID, workdir, canonical reveal paths/modes;
- non-secret backing kind/object reference/revision;
- implicit-mount suppression;
- effective tool/MCP qualifications;
- lifecycle and cleanup result;
- no credentials, provider keys, raw secret environment, or unredacted protected host paths where policy marks them secret.
- canonical agent-visible reveal paths remain present and auditable; only daemon-side backing identities/paths and credentials are redacted.

Run after each RED microcycle:

```bash
pytest -q tests/tools/test_delegation_scope_observability.py
```

**GREEN**

Emit stable audit fields through current delegation snapshots/events. Add operator examples showing trusted policy construction, profile registry, ordinary compatibility, protected failure modes, and the child-only claim.

Run after each GREEN microcycle:

```bash
pytest -q tests/tools/test_delegation_scope_observability.py
```

**REFACTOR**

Keep observability factual: distinguish container-contained paths, admitted host-backed objects, and independently authorized external capabilities.

---

## Stage 15: Run adversarial integration and compatibility gates

**Files**

- Create: `tests/integration/test_delegation_filesystem_isolation.py`
- Modify existing regression tests only when behavior intentionally changes for protected attempts.

**Adversarial Docker matrix**

For single, batch, nested orchestrator, retry/resume, cancellation, and timeout:

- reveal one leaf and attempt to list/read its ancestor, sibling, host home, Hermes home/source, credentials, unrelated run, archive/cache, `/proc` host material, and Docker socket;
- attempt path traversal, symlink-root substitution, conflicting overlap, RO writes, RW hard-link aliasing, and workdir escape;
- start with file/code/vision before terminal to catch environment-creation drift;
- attempt late calls after revocation;
- verify each batch child has a different container ID and private root;
- verify only explicit shared RW reveals share state;
- verify resume uses a fresh container and original authority;
- verify ordinary unprofiled delegation still shares `default`.

**Targeted regression command**

```bash
pytest -q \
  tests/agent/test_delegation_policy.py \
  tests/tools/test_delegation_scope.py \
  tests/tools/test_delegate_filesystem_scope.py \
  tests/tools/test_delegate.py \
  tests/tools/test_delegate_toolset_scope.py \
  tests/tools/test_async_delegation.py \
  tests/tools/test_delegation_durable_lifecycle.py \
  tests/tools/test_delegation_repository.py \
  tests/tools/test_delegation_transcript_resume.py \
  tests/tools/test_delegation_scope_resume.py \
  tests/tools/test_shared_container_task_id.py \
  tests/tools/test_file_tools_container_config.py \
  tests/tools/test_file_tools.py \
  tests/tools/test_code_execution_modes.py \
  tests/tools/test_docker_environment.py \
  tests/tools/test_docker_cgroup_limits.py \
  tests/tools/test_delegation_scope_docker_args.py \
  tests/tools/test_delegation_scope_tool_parity.py \
  tests/tools/test_delegation_scope_host_readers.py \
  tests/tools/test_delegation_scope_capabilities.py \
  tests/tools/test_delegation_scope_observability.py \
  tests/tools/test_read_extract.py \
  tests/tools/test_image_source.py \
  tests/tools/test_delegation_scope_cleanup.py
```

**Docker integration command**

```bash
pytest -q -m docker \
  tests/integration/test_delegation_filesystem_isolation.py \
  tests/integration/test_delegation_scope_browser_profile.py \
  tests/integration/test_vision_docker_resolve.py
```

**Final repository checks**

```bash
git diff --check
python -m compileall -q agent tools
pytest -q tests/tools/test_delegate.py tests/tools/test_shared_container_task_id.py
```

Run the repository's broader CI suite before merge.

---

## 4. Release gates

Do not enable `delegation.filesystem_isolation.enabled` by default until all gates pass:

1. **Runtime policy:** protected profile omission, `none`, default/shared, unknown, stale, and disallowed profiles fail before child creation.
2. **Attenuation:** path set and access mode are subsets of the pinned parent/session ceiling.
3. **Path identity:** canonical visible path is identical in parent and child.
4. **Atomic batch:** invalid shared scope starts zero children.
5. **Fresh attempts:** parallel, retry, replacement, and resume never share private roots.
6. **No implicit authority:** strict Docker argv contains no global volume, credential, skill, cache, home, CWD, persistent workspace, Docker socket, or arbitrary extra mount.
7. **Tool completeness:** every enabled local-path consumer routes through the attempt environment or exact attempt-owned grant; every other such tool is excluded.
8. **Resume:** profile/workdir/reveals/backing revisions reconstruct exactly or fail closed.
9. **Revocation:** revoked/cleaned attempts cannot recreate or collapse into ordinary environments.
10. **Cleanup:** success, failure, cancellation, timeout, partial startup, and process recovery remove attempt-owned resources deterministically.
11. **Browser profile:** Playwright screenshot creation and visual analysis work inside the scoped environment without host browser authority.
12. **Compatibility:** existing ordinary shared-container tests and ordinary delegation lifecycle tests remain green.
13. **Observability:** records state effective authority truthfully and contain no secrets.
14. **Independent validation:** a reviewer runs negative-path and ordinary-compatibility tests from a clean worktree and records actual command output.

---

## 5. Rollout

1. Merge pure policy, resolver, schema, and persistence behind `filesystem_isolation.enabled: false`.
2. Merge centralized environment acquisition with ordinary behavior unchanged.
3. Add strict Docker materialization and tool parity; keep profiles unavailable to sessions.
4. Enable only controlled programmatic sessions that pass a trusted `DelegationSessionPolicy`.
5. Run the adversarial matrix on local Docker and CI.
6. Admit `filesystem-isolated` for selected consumers.
7. Admit `filesystem-isolated-browser` only after the browser integration gate passes.
8. Keep current ordinary delegation as the default outside protected sessions.

Rollback is configuration-level while the feature remains opt-in: stop admitting protected session policies and disable the registry. Existing ordinary sessions continue using the unchanged default path. Never roll back a live protected child into unprofiled/shared execution; terminate it instead.

Add a lifecycle test that disables the registry during an active protected attempt and verifies termination/revocation plus durable tombstone retention; it must never drop the policy record and continue as ordinary/default execution.

---

## 6. Deferred work

The following are deliberately outside this implementation unless separately authorized:

- whole-parent/session base-environment admission;
- non-Docker protected backends;
- arbitrary external host bind roots;
- a scoped adapter for existing host-launched `browser_*` commands;
- task-scoped filesystem MCP sidecars;
- malicious-parent prompt or packet controls;
- general semantic provenance policy;
- replacing generic `delegate_task` with workflow-specific tools.
