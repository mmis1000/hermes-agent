# Strict Design Review: Delegation-Profile Filesystem Isolation

## 1. Review identity

- **Review object:** `/home/prod/llm-uiux-investigation/docs/research/subagent-knowledge-boundary-enforcement/delegation-profile-filesystem-isolation-design.md`
- **Required SHA-256:** `fa5875fbc349387687a5fde3566d4cbfa78bd165d5addb5ad758da9325a0b5f8`
- **Verified SHA-256 before substantive review:** `fa5875fbc349387687a5fde3566d4cbfa78bd165d5addb5ad758da9325a0b5f8`
- **Verified size:** 22,939 bytes
- **Verified line count:** 531
- **Hermes source reviewed at:** commit `5d7fe8777b9795958011861a784aeca014721aff` (`2026-08-10T10:50:32-07:00`, `feat: add idle-based screen-attention notifications (#1220)`)
- **Relevant reviewed-source diff:** empty for the specific delegation, terminal, file, image, vision, browser, Docker, and delegation-documentation paths cited below (`git hash-object --stdin` of that empty diff: `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`). The wider Hermes worktree was already dirty; this review did not modify Hermes source or configuration.
- **Review mode:** strict, read-only source/design review. The only file written is this requested review artifact.

## 2. Verdict

# **REVISE**

The design has the right architectural direction and preserves the frozen product boundary: generic `delegate_task` remains, ordinary sessions retain their current behavior, protected sessions require a server-approved profile, the model orchestrator keeps free-form `goal`/`context`, packet construction, evidence routing, steering, and resume, and the proposal explicitly does **not** introduce malicious-orchestrator defenses, server-owned child prompts, rigid workflow state machines, or semantic packet policing. Its limitation to a delegated child's independent local-filesystem reachability is materially truthful as a target, and its Playwright/browser/visual-analysis caveats are mostly honest.

It is not ready for acceptance because four design deltas and three direct pre-existing source blockers leave the claimed protected boundary and lifecycle incomplete:

1. the batch contract contradicts its own schema proposal about per-child profiles;
2. the protected tool/read surface is not closed over host-backed tools such as `skill_view`, `skill_manage`, and `session_search`;
3. timeout/interruption cleanup can revoke routing while the existing worker thread is still alive, creating a fail-open late-tool-call risk unless attempt revocation is durable and checked at every tool boundary;
4. cleanup and persistence semantics do not define both Docker persistence dimensions or all attempt-scoped resources;
5. `read_file` structured-document extraction currently opens `.ipynb`, `.docx`, and `.xlsx` on the host before sandbox file operations;
6. the current skill/session tools read profile-local host state directly and do not honor a delegation attempt's mount set;
7. the current vision resolver intentionally permits host reads from every Hermes media-cache root under a non-local backend, not only evidence attached to the protected attempt.

These are bounded, repairable changes. They do not justify replacing generic delegation or changing the orchestrator's reasoning role, so `REVISE`, not `REJECT`, is appropriate.

## 3. Review standard and finding classes

Every finding below uses exactly one required class:

- **blocking delta defect** — the proposed design is internally contradictory or underspecified in a way that must be corrected before it is an adequate implementation contract.
- **direct pre-existing blocker** — current Hermes behavior directly defeats the proposed boundary unless the implementation explicitly changes it. These are not reasons to redesign orchestration, but they are implementation acceptance gates.
- **adjacent improvement** — useful hardening or auditability beyond the frozen boundary. These items do **not** block acceptance.

The assessment does not treat a cooperative parent/orchestrator as an attacker and does not demand controls over semantic leakage in the parent's chosen packet. That exclusion follows design Sections 2.2-2.3 and 12 and the frozen review scope.

## 4. Source-backed baseline

### 4.1 Ordinary delegation today

The current public contract is a generic, model-facing `delegate_task` with free-form goals and context, fresh child conversations, batch fan-out, background top-level execution, inherited tools, nested orchestrator roles, durable completion delivery, steering, and native resume:

- `website/docs/user-guide/features/delegation.md:7-11` describes fresh child context, inherited tools, and background top-level calls.
- `website/docs/user-guide/features/delegation.md:22-38` shows object-valued batch items with independent `goal` and `context` and says those fields are the child's supplied context.
- `website/docs/user-guide/features/delegation.md:116-141` documents consolidated background batch behavior, cancellation, durable completion, and the fact that a process crash does not resume execution automatically.
- `website/docs/user-guide/features/delegation.md:168-176` documents inherited tool access and current child blocks.
- `tools/delegate_tool.py:930-1005` builds the focused child prompt directly from free-form `goal`, `context`, workspace hint, and optional orchestrator-role guidance.
- `tools/delegate_tool.py:3065-3090` keeps generic single and batch entry points and per-task role override.
- `tools/delegate_tool.py:4208-4308` defines the current model schema; batch items contain `goal`, `context`, and `role`.
- `tools/delegate_tool.py:4315-4329` preserves automatic background execution for top-level agents and synchronous execution for orchestrator children.

The proposal's optional `profile` extension does not inherently disturb these semantics. Design Sections 5.1, 5.3, 8, 12, and 13 explicitly preserve the ordinary path and the model orchestrator.

### 4.2 Current filesystem routing is not delegated-child isolation

The design correctly identifies a direct baseline problem:

- `tools/delegate_tool.py:2577-2586` allocates a child tool `task_id` and seeds its cwd from the parent because children currently share the parent's container.
- `tools/terminal_tool.py:1175-1203` maps ordinary `subagent-*` and `sa-*` task IDs to `default` unless a task-specific override exists.
- `tools/file_tools.py:928-955` applies the same `_resolve_container_task_id` mapping to file operations, and its own docstring states that delegated children share the parent's container.
- `tests/tools/test_shared_container_task_id.py` passed all 12 tests during this review and confirms this is deliberate current behavior, not a speculative reading.

Therefore the current docs' phrase “their own terminal sessions” describes logical child identity/cwd, not a physical filesystem boundary. Design Sections 3-4 and 7.1 truthfully require a new attempt-specific environment override so protected attempts stop collapsing to `default` while ordinary children continue to do so.

### 4.3 Current environment overrides are too narrow for the proposed profile

Current task overrides cover only image and cwd-like fields:

- `tools/terminal_tool.py:1125-1172` stores per-task overrides and resolves them through parent task IDs.
- `tools/terminal_tool.py:1301-1364` applies those overrides to environment type, image, and cwd.
- `tools/terminal_tool.py:1408-1472` obtains volumes, forwarded environment, explicit Docker environment, extra arguments, network, persistent filesystem, and cross-process persistence from global configuration.
- `tools/file_tools.py:999-1084` creates file-operation environments from the same global container configuration; only image/cwd use task overrides.

The design is therefore correct that the profile must become an immutable effective environment configuration, not merely a prompt label or image override. Its Sections 4.2, 6.3, 7.1, and 9 are aligned with the source, provided the implementation extends the task-specific configuration path to every relevant field and does not mutate process-global configuration.

## 5. Requirement-by-requirement assessment

| Review question | Assessment | Reason |
| --- | --- | --- |
| Is the artifact internally consistent? | **No, revision required.** | The main direction is consistent, but Sections 5.2 and 7.2 disagree on whether batch items can select profiles, and lifecycle/tool-surface details are incomplete. |
| Is the claimed boundary technically truthful? | **Truthful as a narrow target, not yet as an implementable guarantee.** | Sections 2.2-2.3 and 4 correctly limit the claim to independent local reads through governed tools. The proposal must explicitly close the host-backed readers and late-worker path identified below. |
| Does ordinary delegation remain unchanged? | **Yes.** | Sections 5.1, 5.3, 8, 12, and 13 preserve unprofiled ordinary sessions and generic delegation. |
| Do protected sessions fail closed? | **Mostly in request validation; not yet throughout runtime teardown.** | Unknown/missing/disallowed profiles and setup failures are correctly rejected, but late calls from an abandoned worker need a durable revoked-attempt denial rather than removal of routing followed by default fallback. |
| Are profile/mount semantics coherent? | **Mostly.** | Canonicalized allowlisted mounts, explicit RO/RW, collision rejection, and suppression of inherited mounts are sound. Host-backed tools must also honor the same evidence set. |
| Are resume semantics coherent? | **Yes in concept, with implementation gates.** | Section 7.4 correctly persists the selected profile, allocates a fresh attempt/container, revalidates policy, and forbids fallback. The profile must be added to the durable resume allowlist and reconstruction path. |
| Are batch semantics coherent? | **No.** | Per-child selection is asserted but not represented in the proposed schema or precedence rules. |
| Are cleanup semantics coherent? | **No, revision required.** | The design omits quiescence/revocation ordering, both Docker persistence controls, browser/process cleanup, and the existing session-id/attempt-id mismatch. |
| Are Playwright claims honest? | **Yes.** | The design says Playwright/Chromium can run in the child container and explicitly does not equate Hermes browser-session separation with filesystem isolation. |
| Are visual-analysis claims honest? | **Yes in wording, but current cache behavior is a blocker.** | Section 10 correctly says image visibility requires an allowed mount or approved evidence path and declines to claim a full visual-modality boundary. Current `vision_analyze` cache handling must be narrowed per attempt. |
| Is the existing model orchestrator preserved? | **Yes.** | Free-form packet construction, routing, steering, resume, and generic `delegate_task` remain. No malicious-orchestrator defense is smuggled in. |
| Is the UI investigation skill preserved? | **Yes semantically; technical skill loading needs an explicit allowlist mechanism.** | Section 6.2 retains the project skill and rule without replacing their workflow logic; the current host-backed skill tools cannot implement that claim safely without change. |

## 6. Blocking findings

### F-01 — **blocking delta defect** — Batch profile selection contradicts the proposed schema

**Design references:** Sections 5.2, 5.3, 7.2, and 11 (“Protected Batch” / “Resume”).

Section 5.2 proposes only one top-level field:

```json
"profile": {
  "type": "string",
  "description": "Named server-configured delegation environment profile"
}
```

Section 7.2 then states: “Each item may carry its own profile.” The current batch schema is object-valued and supports per-task `goal`, `context`, and `role` (`tools/delegate_tool.py:4265-4282`), but no per-task `profile`. The design does not define:

- the per-task JSON field;
- whether top-level `profile` is a default, a hard override, or mutually exclusive with per-item profiles;
- what happens when one item omits a profile in a protected session;
- whether a disallowed profile rejects the whole batch before any child starts or yields a partial batch;
- whether all profiles/mounts are validated before fan-out;
- how each selected profile is persisted against the correct durable attempt.

This is not a cosmetic schema issue. Protected batch fail-closed behavior and unique attempt/container ownership depend on resolving every item before any worker starts.

**Required bounded revision:** Extend the batch-item schema with `profile`; define deterministic precedence (for example, item profile > top-level default); require every resulting item profile to be present and allowed in protected sessions; validate the entire batch and all mount plans before creating any child; reject atomically on any invalid item; persist the resolved profile per attempt. This does not alter packet semantics or generic delegation.

### F-02 — **blocking delta defect** — The exact protected capability/read surface is underdefined

**Design references:** Sections 4.1-4.2, 6.1-6.3, 9, 10, 11, and 13.

Section 4 requires every local-file-capable tool path to route through the attempt-specific environment, but the concrete profile in Section 6.1 disables only `delegate_task`, while Section 6.2 describes a “skill profile” without specifying how it constrains the host-side `skills_list`, `skill_view`, or `skill_manage` APIs. The implementation checklist and test matrix test terminal/file access and visual paths, but not host-backed local stores.

This is a direct gap because current child tool inheritance can include the entire `skills` and `session_search` toolsets. `tools/delegate_tool.py:1657-1715` derives child toolsets from the parent's effective tools and blocks only the small `DELEGATE_BLOCKED_TOOLS` set. That set is `delegate_task`, `clarify`, `memory`, `send_message`, `execute_code`, and `cronjob` (`tools/delegate_tool.py:50-60`); it does not contain skills or session search. `toolsets.py:41-65` includes `skills_list`, `skill_view`, `skill_manage`, and `session_search` in the core surface, and `toolsets.py:167-170` groups all three skill operations together.

A truthful protected profile must distinguish:

1. tools whose local-path reads go through terminal/file routing;
2. tools that read server-local state directly;
3. tools that write server-local state directly;
4. host-created evidence paths granted to exactly one attempt;
5. tools intentionally available only for ordinary sessions.

Merely unmounting `~/.hermes/skills`, caches, and credentials from Docker does not affect a host-side Python tool.

**Required bounded revision:** Add a profile-level exact tool allow/deny policy and a central protected-attempt authorization resolver used by every governed host-backed reader/writer. For the UI reference profile, either preload the two approved project documents and disable the host skills toolset, or make `skill_view` task-aware and allowlist only that bundle while disabling `skill_manage`. Disable `session_search` and other host-state readers in this profile unless they are explicitly part of its evidence set. Add negative tests for host-backed tools, not just terminal/file paths. This remains a filesystem/tool-routing control, not malicious-orchestrator or packet-semantic policing.

### F-03 — **blocking delta defect** — Timeout/interruption lacks a fail-closed late-worker state

**Design references:** Sections 4.2 (“no default fallback”), 5.3, 7.3, 7.4, 9, and 11 (“Failure-path validation” and “attempt A cannot read attempt B”).

The design says cleanup removes task-specific routing on completion, interruption, timeout, or failure. Current hard-timeout behavior cannot stop a running Python worker thread:

- `tools/delegate_tool.py:2739-2755` catches the executor timeout, sets an interrupt flag, calls `future.cancel()`, and shuts the executor down with `wait=False` specifically because the child thread may remain stuck on blocking I/O.
- `_run_single_child` cleanup happens only when that still-running function eventually unwinds (`tools/delegate_tool.py:2989-3039`).

If a new outer profile owner tears down the container and *removes* the attempt's override as soon as the timeout is reported, the abandoned thread can later issue another tool call. Under current routing, a task without an override collapses back to the default container (`tools/terminal_tool.py:1175-1203`). That is precisely the fail-open fallback Section 4.2 prohibits.

The same issue applies to cooperative interruption: an interrupt request is not proof that every in-flight or future call has stopped. Cleanup ordering must not turn revocation into loss of authorization state.

**Required bounded revision:** Define an attempt authorization lifecycle such as `starting -> active -> revoked -> cleaned`, with a durable/in-process tombstone for protected attempt IDs. Every governed tool dispatch must reject `revoked`, `cleaned`, unknown-protected, or profile-missing attempt IDs; it must never treat them as ordinary/default tasks. On timeout/interruption, revoke first, stop/close resources, and clear mutable routing only after no late call can fall through. Where worker quiescence cannot be proven, retain the denial tombstone beyond resource cleanup. Add a deterministic test in which a timed-out worker attempts a later file/terminal/vision call and receives a protected-attempt denial rather than the default environment.

### F-04 — **blocking delta defect** — Cleanup/persistence ownership is not fully specified

**Design references:** Sections 6.1, 7.3, 7.4, 9, 11 (“Cleanup”), and 13.

The design's example has a single `persistence: false` field, while current Docker has two independent persistence dimensions:

- `container_persistent` controls bind-mounted `/workspace` and `/root` versus tmpfs (`tools/terminal_tool.py:1450-1451`; `tools/environments/docker.py:645-701`).
- `docker_persist_across_processes` controls whether cleanup actually stops/removes the container (`tools/terminal_tool.py:1456-1464`, `1528-1540`; `tools/environments/docker.py:1380-1397`).

With cross-process persistence left true, `cleanup_vm(task_id)` removes the in-process handle but Docker cleanup intentionally leaves the container running. `force_remove=True` exists, but current comments state no caller uses it (`tools/environments/docker.py:1355-1361`). Therefore “persistence: false” is ambiguous unless the design maps it to both controls or mandates forced removal.

The attempt owner also needs to cover more than container, file routing, and attached-evidence references:

- browser sessions are keyed by tool `task_id` and require `cleanup_browser(attempt_id)` (`tools/browser_tool.py:4384-4427`);
- background processes are killed by task ID;
- file-operation cache, session cwd, creation lock/last-activity state, and environment overrides all need attempt-keyed removal;
- current `AIAgent.close()` derives its cleanup key from `self.session_id`, not `_current_task_id` or delegation attempt ID (`run_agent.py:3846-3864`), while `_run_single_child` simply calls `child.close()` (`tools/delegate_tool.py:3032-3039`). A resumed child session ID and a delegation attempt ID are intentionally distinct.

**Required bounded revision:** Define one attempt-owned resource ledger and an idempotent teardown order covering process registry, browser sessions, terminal environment, file cache, cwd/override/creation-lock state, and attached-evidence grants. Specify that protected Docker attempts set both filesystem persistence and cross-process reuse false **and/or** use `cleanup_vm(attempt_id, force_remove=True)`. Cleanup must be keyed by the same physical attempt ID used for tool routing, never by the child transcript session ID. State what “always removed” means for ordinary exceptions/timeouts versus uncatchable host death; the latter may use bounded orphan recovery without weakening the normal-path guarantee.

## 7. Direct pre-existing blockers

### F-05 — **direct pre-existing blocker** — Structured-document `read_file` bypasses the sandbox

**Design references affected:** Sections 3, 4.1-4.2, 7.1, 9, 10, 11 (“Forbidden path via file tool”), and 13.

For container-backed tasks, `_resolve_path_for_task` intentionally returns a container-style path without host dereference (`tools/file_tools.py:364-380`). However, before obtaining sandbox file operations, `read_file_tool` sends that path to host-side document extraction:

- `tools/file_tools.py:1126-1139` calls `extract_document_text(str(_resolved))` before `_get_file_ops(task_id)`.
- `tools/file_tools.py:1147` calls host `os.path.getsize(_resolved)`.
- `tools/read_extract.py:61-66` uses host `open()` for notebooks.
- `tools/read_extract.py:107-114` uses host `zipfile.ZipFile(path)` for DOCX.
- `tools/read_extract.py:133-159` uses host `zipfile.ZipFile(path)` for XLSX.

Thus a protected child asking to read an existing host `.ipynb`, `.docx`, or `.xlsx` outside its mounts can obtain host content despite a Docker attempt. The source is sufficient to establish the bypass; this review did not probe private user documents.

**Implementation acceptance gate:** Route structured-document bytes through the attempt's sandbox/file operations first and extract only from those bytes or an attempt-owned temporary copy. Add Docker/profile tests for an identically named host document outside the mount set and an allowed document inside it. Until fixed, the design cannot truthfully claim every file tool is confined.

### F-06 — **direct pre-existing blocker** — Skill and session tools read/write server-local state outside terminal/file routing

**Design references affected:** Sections 4.1-4.2, 6.1-6.3, 7.1, 9, 11, and 13.

Current `skill_view` receives `task_id` but uses it only for preprocessing/session hints; discovery and content reads are host-side:

- `tools/skills_tool.py:139-158` resolves the active Hermes profile's host skills directory.
- `tools/skills_tool.py:684-729` scans active and external host skill directories and reads every `SKILL.md`.
- `tools/skills_tool.py:1366-1402` reads linked files using host `Path.read_text()`.
- `tools/skills_tool.py:1528-1547` can register credential files as a side effect of viewing a skill.
- `tools/skills_tool.py:1718-1759` exposes `skills_list` and `skill_view` to the registry with `task_id`, but no mount/profile authorization check.
- `tools/skill_manager_tool.py:1340-1377` manages host skills and is fail-open if the optional write gate import is unavailable (`tools/skill_manager_tool.py:1279-1298`).

Likewise, `session_search` opens the local session database when no DB object is supplied (`tools/session_search_tool.py:619-652`) and can explicitly target another Hermes profile. That is unmounted server-local material and may contain unrelated project/transcript knowledge.

**Implementation acceptance gate:** Implement the exact profile capability/host-reader policy required by F-02. The UI skill bundle must not imply that all global or external skills remain queryable. Protected profiles must disable `skill_manage` and `session_search` unless a narrowly scoped profile-specific implementation is deliberately provided and tested.

### F-07 — **direct pre-existing blocker** — Vision's host-cache exception is broader than attached evidence

**Design references affected:** Sections 4.2, 6.1, 7.1, 9, 10, 11 (“Visual Path”), and 13.

The current image-source resolver has good general Docker confinement: non-cache paths are read through the active sandbox environment and fail closed when no environment exists (`tools/image_source.py:273-316`). The targeted tests passed all 16 cases in `tests/tools/test_image_source.py` during this review.

But the resolver deliberately host-reads **any** path under broad Hermes media-cache roots:

- `tools/image_source.py:210-227` defines cache-wide host roots.
- `tools/image_source.py:230-259` permits any path that resolves under one of those roots when the terminal backend is non-local.
- `tests/tools/test_image_source.py:117-122` explicitly asserts that a cache path is host-read without a sandbox environment.

This is broader than Design Section 10's “explicitly approved attached-evidence path.” A protected child that learns or guesses a cache path could analyze another session's cached image even though it is not mounted into the attempt.

**Implementation acceptance gate:** Replace the cache-wide exception for protected attempts with an attempt-scoped evidence-grant registry (exact canonical path or opaque artifact ID, origin, owner attempt, lifetime). Ordinary non-local sessions can retain current behavior. Add negative cross-attempt and stale-grant tests. This is a local evidence-path control, not a claim that all visual knowledge channels are solved.

## 8. Non-blocking adjacent improvements

### F-08 — **adjacent improvement** — Bind the effective profile definition, not only its name, in audit metadata

**Design references:** Sections 5.1, 6.3, 7.4, 11, and 13.

Persisting the selected profile name is sufficient for the frozen server-trust boundary, and the design correctly revalidates that name on resume. For stronger reproducibility, also record a hash of the normalized effective profile (image reference/digest, canonical mount map, RO/RW modes, tool policy, network setting, and persistence settings) and the realized container ID. A same-named profile can otherwise change between attempts. This is auditability/reproducibility, not required malicious-server defense, and must not block acceptance.

### F-09 — **adjacent improvement** — Define `public_web` more precisely or rename it

**Design references:** Sections 6.1, 10, and 12.

Current Docker networking is effectively on/off (`--network=none` when false; `tools/environments/docker.py:642-643`). A profile value named `public_web` suggests private-network exclusion that a normal Docker network does not itself enforce. The design's narrow filesystem claim does not depend on this, and broader network/data-egress controls are explicitly out of scope. For honesty, document `public_web` as ordinary outbound networking unless a real proxy/egress policy exists, or use a neutral value such as `enabled`. This is adjacent and non-blocking.

### F-10 — **adjacent improvement** — Add crash-orphan recovery without weakening normal teardown

**Design references:** Sections 7.3, 11, and 12.

No in-process `finally` can guarantee cleanup after `SIGKILL`, kernel failure, or host loss. Attempt IDs and `persist_across_processes=false` prevent intentional reuse, but stopped/running orphan containers can consume resources. Label protected containers with the physical attempt/profile and reap stale protected orphans on startup under conservative ownership rules. This is operational hardening beyond normal completion/interruption/timeout/failure and does not block design acceptance.

### F-11 — **adjacent improvement** — Improve profile discoverability without weakening runtime checks

**Design references:** Sections 5.1-5.3 and 8.

A dynamic schema could present the server-approved profile names for the current session, improving model reliability. The runtime must remain independently authoritative exactly as Section 5.3 says; schema presentation is not enforcement. This is UX only and non-blocking.

## 9. Positive conclusions

### 9.1 The boundary is correctly narrow

Design Sections 2.2-2.3, 4, 10, and 12 do not overclaim full model secrecy or hostile-orchestrator security. The protected guarantee is limited to a child's independent reach to unmounted local material through governed tools. That is technically defensible once F-02 through F-07 are addressed. The design also honestly notes that packets can reveal anything the orchestrator includes and that network content, model priors, and administrator/container escape are separate concerns.

### 9.2 Ordinary delegation is preserved

Design Sections 5.1, 5.3, 8, and 13 are clear that:

- `profile` is optional in ordinary sessions;
- omission follows the current path;
- ordinary child IDs can continue collapsing to the shared/default environment;
- generic `delegate_task` remains;
- no mandatory global sandbox is introduced.

That is compatible with the current code and docs. The implementation should add regression tests around the existing single, batch, background, nested-orchestrator, steering, and resume paths, but no product redesign is required.

### 9.3 Protected request validation is appropriately fail closed

Design Sections 5.1, 5.3, 6.3, 7.1, 7.4, and 9 correctly require rejection before child creation for missing, unknown, disallowed, invalid, or uncreatable profiles/mounts. Resume correctly requires the persisted profile and a fresh attempt/container, and explicitly rejects fallback to ordinary shared behavior. This is the correct control plane. F-03 and F-04 refine runtime teardown, not the initial policy decision.

### 9.4 Mount semantics are sound in principle

Design Sections 6.1, 6.3, 7.1, and 9 correctly require:

- server-configured profiles rather than model-supplied raw mount specs;
- canonical host-source validation;
- explicit read-only/read-write destinations;
- duplicate/overlap rejection;
- suppression of default cwd, skill, cache, credential, and user Docker mounts;
- immutable mount configuration for the attempt;
- no silent default fallback.

Current Docker supports explicit `:ro` bind mounts, but it also automatically adds credentials, skills, and cache mounts (`tools/environments/docker.py:709-785`) and appends operator `docker_extra_args` after security/mount arguments (`tools/environments/docker.py:836-854`). The implementation must therefore build a complete protected effective config that bypasses those global additions rather than trying to subtract them after container creation. The design already points in that direction.

### 9.5 Resume is conceptually coherent

Current resume reconstructs a new child from a durable allowlist (`tools/async_delegation.py:327-380`; `tools/delegate_tool.py:1430-1512`) and allocates a new attempt (`tools/async_delegation.py:417-476`). Adding `delegation_profile` to the durable metadata allowlist and requiring it during reconstruction matches Design Section 7.4. A new attempt/container per resume is the right choice: it preserves conversational continuity without reusing physical filesystem state.

The resume implementation must validate the persisted profile before creating the continuation child and must not accept a model-supplied replacement profile. Those requirements are already present in the design.

### 9.6 Playwright and visual-analysis language is honest

Design Sections 6.1 and 10 correctly separate three things:

1. Chromium/Playwright installed **inside** the child Docker image;
2. Hermes browser-session separation, which is not a filesystem proof;
3. image bytes delivered through an allowed mount or approved evidence path.

A Playwright process launched by the protected child's `terminal` runs inside the same Docker mount boundary; it cannot directly open host files that were not mounted. Network reach remains a separate profile property. The design does not claim that a host browser daemon is the sandbox.

Current `browser_vision` is task-keyed, writes a screenshot into the Hermes cache, and directly returns the screenshot bytes/multimodal envelope (`tools/browser_tool.py:4033-4236`). That is compatible with an approved tool-flow evidence grant. Current `vision_analyze` forwards `task_id` into the unified image resolver (`tools/vision_tools.py:959-970`, `1130-1143`). F-07 is the precise remaining gap: cache membership must become attempt-specific evidence authorization.

### 9.7 The model orchestrator and UI investigation method remain intact

The design does not replace the orchestrator with a server workflow engine. Sections 5.2, 8, and 12 preserve model-selected profiles, free-form goals/context, packet construction, evidence routing, steering, resume, and generic delegation. Sections 2.3 and 12 expressly reject server-authored prompts, semantic packet policing, and malicious-orchestrator defenses.

That is compatible with the existing boundary documents:

- `subagent-knowledge-boundary-enforcement/SKILL.md:19-23` defines the default-deny knowledge rule while distinguishing experimental integrity from a security claim.
- `SKILL.md:163-209` requires fresh context, actual filesystem/network isolation, runtime-leak control, write containment, and closing every enabled retrieval path.
- `SKILL.md:211-249` leaves packet construction and lane semantics with the orchestrator and says the prompt preamble does not substitute for technical isolation.
- `theme-transfer-subagent-knowledge-boundary-rule.md:38-58` gives the orchestrator evidence staging, hashing, routing, contamination, and aggregation authority.
- The theme rule's Sections 7.1-7.4 (`lines 223-269`) require exact evidence paths, tools/network policy, inaccessible prohibited directories, fresh workers, and post-run freezing without prescribing a rigid implementation state machine.

Design Section 6.2's project-owned skill bundle preserves this method. F-02/F-06 require a safe technical delivery mechanism for that bundle; they do not require changing its orchestration logic.

## 10. Required acceptance deltas

The design can reach `ACCEPT` with the following bounded revisions. These are all inside the frozen scope:

1. **Batch contract:** add per-item `profile`, define top-level/item precedence, validate every item atomically, and persist the resolved profile per attempt.
2. **Protected attempt authorization:** define an attempt lifecycle with active/revoked/cleaned states; unknown or revoked protected attempt IDs must fail closed at every governed tool dispatch and never collapse to `default`.
3. **Exact tool policy:** enumerate/derive the protected profile's effective tool set; disable or task-scope host-backed local readers/writers (`skill_manage`, unrestricted `skill_view`/`skills_list`, `session_search`, and any equivalent path).
4. **Structured documents:** make `.ipynb`/`.docx`/`.xlsx` extraction consume bytes read through the attempt environment, never the host path directly.
5. **Attached visual evidence:** replace cache-wide host visibility with exact attempt-scoped evidence grants for protected attempts while retaining current behavior for ordinary sessions.
6. **Resource ledger:** key container, browser, process, file cache, cwd/override/creation state, and evidence grants by the same physical attempt ID; teardown idempotently on every normal terminal path.
7. **Persistence mapping:** explicitly set both `container_persistent=false` and `docker_persist_across_processes=false` for ephemeral profiles and/or force-remove by attempt ID.
8. **Resume/nesting:** include the profile in the durable resume allowlist; validate it before child construction; allocate a fresh attempt/container; inherit the protected-session requirement into any permitted nested `delegate_task` call.
9. **Tests:** add the missing batch-profile, host-backed tool, structured-document, stale/late worker, cross-attempt image grant, browser cleanup, wrong-session-ID cleanup, and ordinary-path regression tests.

None of these deltas requires server-owned prompts, rigid workflow state machines, semantic packet inspection, removal of `delegate_task`, or defenses against a malicious orchestrator.

## 11. Validation performed

Read-only targeted tests were run with bytecode and pytest cache writes disabled:

- `PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/tools/test_shared_container_task_id.py` -> **12 passed**.
- `PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/tools/test_image_source.py` -> **16 passed**.
- `PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/tools/test_delegate_subagent_timeout_diagnostic.py` -> **7 passed**.

These tests confirm the current shared-container mapping, current image-source behavior (including the cache exception), and current timeout behavior. They do not validate the unimplemented proposal.

## 12. Final decision

**REVISE.**

The proposed design should be retained as the basis of the implementation. Its product boundary, ordinary-session compatibility, protected-profile direction, mount model, resume strategy, Playwright distinction, visual-analysis caveat, and preservation of the model orchestrator/UI investigation method are fundamentally sound. Acceptance is blocked only until the specific schema, host-backed tool, late-worker, structured-document, attached-evidence, and resource-lifecycle gaps above are incorporated into the design and its test matrix.
