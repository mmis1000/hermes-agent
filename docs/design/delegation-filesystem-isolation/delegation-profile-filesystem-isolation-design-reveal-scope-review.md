# Independent Reveal-Scope Design Review

**Verdict: ACCEPT WITH NON-BLOCKING NOTES**

## 1. Reviewed object and exact identity

I reviewed the complete frozen object:

`/home/prod/llm-uiux-investigation/docs/research/subagent-knowledge-boundary-enforcement/delegation-profile-filesystem-isolation-design.md`

I independently measured the file before substantive review. The identity gate passed exactly:

| Measure | Required | Observed | Result |
|---|---:|---:|---|
| SHA-256 | `5d35f95edaeff82de1572cb992cdd2c5993365593b269ce3262c63bb337e049b` | `5d35f95edaeff82de1572cb992cdd2c5993365593b269ce3262c63bb337e049b` | PASS |
| Bytes | 29,547 | 29,547 | PASS |
| Physical lines | 480 | 480 | PASS |

The current Hermes source reviewed was `/home/prod/.hermes/hermes-agent` at exact revision `6b83b37056049432d864198bbab3e2ed5d1fd43f`; its working tree was clean when checked.

No design or Hermes source file was edited.

## 2. Auditable map of the reviewed design

The reviewed object is identified by these quoted headings and ranges:

| Quoted heading | Lines |
|---|---:|
| “## 1. Decision summary” | 7–21 |
| “## 2. Objective” | 23–37 |
| “## 3. Authority and threat model” | 39–84 |
| “## 4. Core concepts” | 86–190 |
| “## 5. `delegate_task` contract” | 192–263 |
| “## 6. Profile semantics” | 265–301 |
| “## 7. Runtime and lifecycle behavior” | 303–369 |
| “## 8. Preservation of the existing UI investigation skill” | 371–386 |
| “## 9. MCP, browser, and external capability boundary” | 388–405 |
| “## 10. Effective configuration and observability” | 407–429 |
| “## 11. Design acceptance conditions” | 431–451 |
| “## 12. Deferred decisions” | 453–466 |
| “## 13. Review brief” | 468–480 |

## 3. Scope assessment

The design stays within the requested narrow contract.

It preserves generic, model-driven `delegate_task`, ordinary free-form `goal` and `context`, orchestrator packet construction and evidence routing, steering, resume, and ordinary shared delegation when no protected policy is active (design lines 9–21, 60–78, 192–241, 371–386, 431–451). It constrains autonomous child filesystem retrieval rather than prompt semantics. It explicitly excludes malicious-orchestrator defense, server-owned prompts, semantic packet policing, rigid workflow transitions, general network denial, and general MCP authorization redesign (lines 60–78, 384–405, 468–480).

The design also correctly qualifies its claim as a “filesystem-complete tool boundary,” not whole-agent process isolation (lines 80–84). It distinguishes whole protected-session isolation—which requires a fixed base environment for the parent—from child-only lane isolation (lines 131–146, 437–438). That distinction is necessary and correctly prevents an overclaim.

**Design blockers found: none.** The current Hermes tree does not yet implement this contract, but those mismatches are direct implementation gates, not contradictions in a document explicitly scoped as a product/runtime contract rather than an implementation plan (lines 3–5).

## 4. Findings

### F-1 — Parent-scoped path-preserving reveals are coherent

**Classification:** Design passes; direct implementation gate remains.

Under “### 4.3 Protected session base environment” and “### 4.4 Path-preserving reveal scope,” child authority is an attenuation of the admitted parent-session ceiling, selected profile, and invocation reveal (lines 131–177). A requested reveal is not merely an arbitrary host pathname: it must be visible in the parent’s admitted namespace, backed by storage marked revealable by the base profile, and no broader than the parent’s grant (lines 159–177, 243–259). Files that exist only in the parent’s private container layer are expressly non-revealable (line 175).

That resolves the apparently difficult nested-orchestrator case. A scoped parent can reveal only a capability already represented in its trusted admitted-backing table; the child receives an attenuated mapping of that backing object. It cannot name a path outside the parent ceiling and cause trusted runtime code to rediscover it in the host namespace.

The current Hermes `delegate_task` schema has only `goal`, `context`, routing overrides, `tasks`, `role`, and the compatibility `background` field; it has no `profile`, `workdir`, or `reveal` fields (`tools/delegate_tool.py:4208–4307`). The registry handler likewise forwards no such state (`tools/delegate_tool.py:4354–4369`). Adding the contract without replacing free-form delegation is therefore a direct implementation change, exactly as the design intends.

### F-2 — The Docker namespace statement and backing-object requirement are correct

**Classification:** Design passes; high-priority implementation gate.

The design accurately states that Docker does not inherit the parent agent’s logical scope and that a parent-visible path may not be a daemon-visible source path (lines 165–173). It requires trusted runtime code to retain canonical backing paths, volume records, or another backend-specific backing identity, and keeps raw mount specifications out of model arguments (lines 171–173). This is the correct authority split.

The current Docker backend accepts host-side volume strings, host-CWD mounts, credential/skill/cache mounts, and extra Docker arguments directly when constructing `docker run` (`tools/environments/docker.py:650–705`, `709–785`, `837–855`). It does not have a parent-admitted backing-object table. Protected profiles therefore need a separate trusted mount-plan compiler rather than passing the model-visible `path` as Docker’s source or reusing the ordinary volume pipeline.

### F-3 — Revealing a leaf at the same absolute path need not expose host ancestors or siblings

**Classification:** Design passes; mount-assembly verification gate.

The design explicitly separates the child’s synthetic ancestor directory structure from host contents and states that selected leaf files/directories can be mounted without exposing unmounted siblings or ancestor contents (line 175). It also suppresses implicit mounts and inherited global volumes (lines 92–103, 271–281), requires exact path preservation (lines 159–173, 247–257), and forbids shadowing protected prefixes (line 254).

This is coherent on Linux: trusted runtime code can create destination parent directories in the fresh container filesystem and bind only the accepted leaf object at the identical destination. The host parent is not mounted merely because the destination hierarchy exists. A directory reveal intentionally includes that directory’s descendants, but not its host siblings.

Implementation must test both file and directory leaves and must reject destination-side symlink/collision tricks in the base image. Those details are implied by the exact-path, protected-prefix, canonicalization, and no-substitution requirements, but they should be explicit test cases rather than assumptions.

### F-4 — Symlink, substitution, special-file, overlap, attenuation, and workdir rules are sufficient at design level

**Classification:** Design passes; security-critical implementation gates.

“### 5.4 Reveal validation contract” requires absolute normalized existing paths, parent-namespace visibility, revealable backing storage, canonical/symlink containment, exact `ro`/`rw`, non-amplification of the parent grant, identical parent/child paths, rejection of conflicting duplicate/overlapping reveals, protected-prefix non-shadowing, rejection of devices/sockets/Docker endpoints/forbidden types, no substitutable validation-to-attachment interval, and no post-validation hidden reveal (lines 243–259). “### 5.5 Workdir contract” permits only an absolute in-container directory inside a suitable reveal or a valid container-local directory and forbids an implicit host-CWD mount (lines 261–263). RO and RW outcomes are acceptance conditions (lines 443–445).

The contract therefore covers the requested classes. The implementation test matrix must additionally make the following interpretations concrete:

1. compare backing-object identity, not only lexical path, so hard-link aliases cannot create a RO/RW bypass;
2. reject source and destination ancestor symlinks that escape the accepted object or protected destination prefixes;
3. pin the accepted object (open handle, inode-equivalent reference, immutable volume/subpath revision, or trusted snapshot as appropriate) through attachment, rather than `stat`-then-mount-by-name;
4. validate the effective workdir after mount assembly and reject `..`, symlink, mount-alias, or container-image-prefix escape;
5. reject mixed-mode overlaps before container creation, including canonical aliases, and prove that RO is enforced from every alias;
6. reject FIFOs, block/character devices, sockets, Docker/Podman/containerd controls, proc/sysfs control paths, and unsupported mount sources before any daemon call.

These are direct gates for satisfying lines 243–263, not missing authorization concepts.

### F-5 — Resume/retry persistence is correctly bound to declarative authority and backing identity

**Classification:** Design passes; persistence migration gate.

Under “### 7.2 Persistence and resume,” the durable record stores the pinned profile identity/hash, workdir, validated reveal specification, and trusted backing-object identity or manifest revision—not only a path or ephemeral container ID (lines 311–323). Resume receives a fresh container, retains the same declaration, reattaches only the originally validated backing objects under the pinned session ceiling, and fails closed on reconstruction failure or attempted widening (lines 313–323). “### 7.3 Attempt authorization state” further prevents missing/revoked/cleaned protected attempts from falling back to ordinary authority and retains a denial tombstone for late calls (lines 325–335).

Current Hermes already has durable logical/run/attempt structure with generic `spec_json` and `metadata_json` (`hermes_state.py:986–1031`) and reserves a new run and attempt on resume (`tools/delegation_repository.py:241–302`). However, initial task persistence currently captures only goal(s), context, toolsets, role, model, and batch status (`tools/delegation_repository.py:180–225`), while the resume metadata allowlist lacks profile, reveal, profile hash, and backing identity (`tools/async_delegation.py:327–345`). Current resume reconstructs from that narrower metadata and passes a workdir (`tools/async_delegation.py:417–468`, `472–560`). Persisting and validating the immutable boundary at logical-delegation level, then referencing it from every physical attempt, is therefore a direct implementation gate.

### F-6 — Atomic batch semantics and fresh-container semantics are internally consistent

**Classification:** Design passes; dispatch-order gate.

The invocation-wide profile/workdir/reveal shape and separate container per child are explicit (lines 192–219, 303–309). The entire protected plan must validate before any child starts, and invalid plans reject atomically (lines 217–219). Each physical attempt—including a resumed attempt—gets a fresh container (lines 273–280, 305–320, 439–448). Shared RW state exists only when deliberately revealed (lines 307–309).

Current Hermes validates ordinary task shape and then constructs every child before executing the fan-out (`tools/delegate_tool.py:3185–3218`, `3242–3296`); batch execution then runs those children in parallel and returns ordered consolidated results (`tools/delegate_tool.py:3298–3434`). This structure can preserve existing packet/result behavior, but protected-plan validation and backing-object pinning must occur before live transcript allocation or child construction, and each child must receive a unique physical attempt environment.

Current environment routing does the opposite of that isolation: `_resolve_container_task_id` deliberately collapses ordinary subagent IDs to `"default"` so children share one long-lived container (`tools/terminal_tool.py:1175–1207`). File tools use that collapsed key (`tools/file_tools.py:928–956`), and `execute_code` does too (`tools/code_execution_tool.py:687–707`). Replacing that collapse only for protected physical attempts—while retaining it for ordinary sessions—is a central implementation gate.

### F-7 — The cross-tool and bounded-store parity claim is complete enough, and current bypasses are explicitly anticipated

**Classification:** Design passes; broad implementation inventory and conformance gate.

The document makes the boundary apply to every local-path-capable child tool or excludes that tool from the claim (lines 80–84). It separately classifies pathless host-backed stores such as skills, history, and media caches (lines 179–190), forbids automatic credentials/source/skills/caches/home/CWD/global volumes in strict profiles (lines 271–281), and gives precise parity requirements for terminal/file/code, structured documents, vision, exact visual grants, skills/history, and caches (lines 361–369, 431–450).

Current-Hermes evidence confirms why each gate is needed:

- Terminal, file, and code currently share the ordinary collapsed environment (`tools/terminal_tool.py:1175–1207`; `tools/file_tools.py:928–1097`; `tools/code_execution_tool.py:687–787`). Protected mode needs the same *attempt-specific* routing, not the shared key.
- Structured-document extraction currently resolves a container-shaped path and invokes host-side parsers and `os.path.getsize` before the normal environment-backed read (`tools/file_tools.py:1109–1185`); those parsers directly `open(path)` (`tools/read_extract.py:42–64`). The design correctly requires bytes to come through the attempt environment or an attempt-owned temporary copy (lines 363–365, 443).
- Vision’s resolver is already task-aware and routes non-cache paths through the active environment (`tools/image_source.py:89–145`, `262–316`), but its non-local policy currently permits any resolved path under broad media-cache roots (`tools/image_source.py:210–259`). Strict mode must replace cache-directory membership with the exact attempt-scoped evidence grant required by design lines 367–369.
- Local browser automation currently launches `agent-browser` as a host subprocess with a task-named socket/session (`tools/browser_tool.py:2302–2424`, `2491–2499`) and writes snapshots/screenshots to shared host caches (`tools/browser_tool.py:2630–2665`, `4033–4065`). A container-contained Playwright claim needs the browser-ready attempt environment; a deliberately host-global browser must instead be reported as external authority.
- MCP calls currently forward model-supplied argument dictionaries directly to long-lived server sessions (`tools/mcp_tool.py:1803–1825`, `4560–4611`). The design correctly avoids claiming that the container constrains such servers and requires truthful location/authority reporting (lines 388–405).
- Host desktop control is another independent retrieval channel even though it does not take a pathname argument: current `computer_use` drives OS applications through a host `cua-driver` MCP/CLI and can capture target windows or desktop-shell surfaces (`tools/computer_use/__init__.py:1–29`; `tools/computer_use/cua_backend.py:1–17`, `145–154`, `682–694`). A strict profile must disable it or run it against an independently isolated desktop; if intentionally left host-global, the effective-access report must qualify it like a host-global browser. The generic route-or-exclude rule at design lines 80–84 is sufficient, but this must not be missed in the implementation inventory.
- Skill and session tools are independent host-backed retrieval channels: skills resolve the active profile’s host skill directory (`tools/skills_tool.py:139–158`), and session search can open other profiles’ state databases and even scan profile databases (`tools/session_search_tool.py:144–205`). Strict child profiles must disable them or bind them to specifically admitted read-only bundles, as design lines 179–188 and 440–443 require.
- Summary/live-log/tool-result/browser/hook spill paths are also host-backed stores. For example, delegation summaries are written under shared `cache/delegation` and intended for broad remote-backend mounting (`tools/delegate_tool.py:2230–2249`). The implementation inventory must cover every producer and consumer, not only model arguments named `path`.

This is a large parity surface, but the design’s acceptance criteria are appropriately exhaustive. It is an implementation conformance burden, not a reason to redesign generic delegation.

### F-8 — Playwright, vision, MCP, and lifecycle claims are accurate and qualified

**Classification:** Design passes; implementation and audit gates.

The browser-ready profile requires pinned browser/runtime dependencies, no Docker socket/privileged/host-IPC requirement, explicit persistent artifact reveals, and task-environment vision consumption (lines 283–297). The external capability section distinguishes direct container Playwright, a future task-scoped MCP, and an intentionally allowed host-global Playwright MCP (lines 388–405). It never mislabels external MCP/browser authority as container-contained.

Lifecycle is likewise strong: every physical attempt owns a full ledger; timeout/interruption revoke before teardown; late calls encounter a tombstone; cleanup covers success, exception, cancellation, timeout, partial startup, and shutdown; containers are force-removed on normal paths; orphan labels support bounded recovery without reuse (lines 325–359).

Current cleanup is keyed primarily by conversational/session task ID: `AIAgent.close()` kills processes and cleans terminal/browser state using `session_id` (`run_agent.py:3833–3864`), and child completion simply calls `child.close()` (`tools/delegate_tool.py:2989–3039`). Ordinary persistent Docker cleanup can intentionally no-op and leave a cross-process-reusable container (`tools/terminal_tool.py:1788–1846`; `tools/environments/docker.py:1328–1400`), while ordinary container reuse matches task/profile labels (`tools/environments/docker.py:885–966`). Protected attempts therefore require a distinct physical-attempt ledger, revoke-before-close ordering, `force_remove=True`, non-reusable labels, partial-start rollback, and a bounded tombstone/reaper path. These are direct implementation gates already demanded by lines 325–359 and 447–449.

### F-9 — Ordinary-session compatibility is explicitly preserved

**Classification:** Design passes; regression-test gate.

Omitting a profile outside protected sessions preserves the current shared parent/child environment (lines 9–13, 21, 221–241, 435–436). Protected policy is trusted, immutable session state; the model selects only among allowed named profiles and cannot submit raw Docker specifications (lines 107–129, 225–241). This permits adding optional fields and a protected dispatch path without changing ordinary `delegate_task` semantics.

Compatibility tests must prove that unprotected single, batch, nested orchestrator, steering, resume, completion delivery, evidence spilling, and shared-container behavior remain unchanged. Protected tests must separately prove omission/default/unknown profiles fail before child creation. No design text calls for globally replacing today’s ordinary behavior.

### F-10 — Existing orchestration authority and the stated threat model are preserved

**Classification:** Design passes.

The design’s desired guarantee is autonomous local retrieval confinement, not suppression of information the orchestrator intentionally passes (lines 29–37). It keeps packet construction, source-aware/source-blind evidence handling, steering, resume, worker reuse, contamination decisions, repair budgets, review authority, and truthful archive status under the existing methodology (lines 371–386). The acceptance conditions reiterate this and disclaim malicious orchestration, semantic leakage, model memorization, and independently broad MCPs (lines 431–451).

This is precisely the requested threat model. Expanding the design into semantic packet authorization, server-owned prompts, or a general capability framework would violate its frozen review brief (lines 468–480), not improve this contract.

## 5. Direct implementation gates

The following are required before an implementation can claim conformance. They are not design blockers:

1. **Trusted session policy:** pin the allowed profile set and, for whole-session claims, the parent base environment independently of model text; fail protected omission/default/unknown values before any child-side effect (design lines 107–146, 225–241).
2. **Schema without workflow replacement:** add invocation-wide `profile`, `workdir`, and `reveal: [{path, mode}]` while preserving existing free-form goal/context, batch, role, steering, completion, and resume rails (lines 192–263).
3. **Nested backing-capability table:** resolve reveals through the parent’s admitted backing records; prohibit private container-layer objects; make Docker sources daemon-resolvable without trusting the model-visible path as a host source (lines 148–177).
4. **Atomic mount-plan compiler:** validate and pin the whole batch plan before logs, child construction, container creation, or parallel submission; reject all items together on any error (lines 217–219, 243–259).
5. **Mount safety:** cover canonical aliases, hard links, source and destination symlinks, protected-prefix collisions, TOCTOU substitution, devices/control sockets, mixed-mode overlaps, RO enforcement, exact leaf path preservation, and post-mount workdir containment (lines 243–263, 443–445).
6. **Dedicated physical attempt routing:** use one fresh non-fallback environment key per physical protected attempt across terminal, file, code, vision, browser, process, and helper dispatch; never collapse a protected attempt to `default` (lines 303–369, 439–448).
7. **Strict Docker path:** disable host-CWD mounting, ordinary configured volumes, automatic credential/skill/cache mounts, persistent workspaces, cross-process reuse, privileged/control endpoints, unsafe extra arguments, and unapproved environment forwarding; add only validated reveal and admitted-bundle mounts (lines 271–297, 439–450).
8. **Durable authority:** persist profile name/hash, workdir, normalized reveals, modes, backing identity/manifest revision, session ceiling reference, and admitted bundles at logical delegation level; every retry/resume must reconstruct from that immutable record into a fresh container or fail closed (lines 311–335, 409–421, 446–447).
9. **Tool/store parity:** inventory every local-path-capable tool, indirect host-control surface (including `computer_use`), helper, parser, spill path, skill/history/memory store, upload/download cache, browser artifact path, vision cache, plugin/hook path, and path-capable MCP. Route, exactly grant, disable, or truthfully exclude each (lines 80–84, 179–190, 361–369, 388–405, 440–450).
10. **Structured-document repair:** fetch bytes through the attempt environment and parse an attempt-owned temporary copy; never hand a container-style path to host `open`/`stat` (lines 363–365, 443).
11. **Vision/browser correctness:** direct Playwright and local helpers execute in the browser-ready attempt; screenshots remain readable before cleanup and persist only through an RW artifact reveal or exact evidence grant; external browsers/MCPs are labeled external (lines 283–297, 367–369, 388–405, 449–450).
12. **Revocation and cleanup ledger:** revoke first, deny late calls with tombstones, force-remove normal-path ephemeral containers, clean all attempt-keyed caches/processes/browser state/locks, roll back partial starts, and reap labeled orphans without reuse (lines 325–359, 447–449).
13. **Observability:** emit the non-secret effective-access record specified in lines 407–429, including authority location and cleanup/revocation outcome.
14. **Compatibility and adversarial tests:** retain ordinary behavior and test all acceptance conditions at lines 431–451, including nested scoped parents, batch atomicity, siblings, resume, timeout races, and independently broad MCP/browser qualification.

## 6. Adjacent, non-blocking notes

1. **Make target-side mountpoint hardening explicit in implementation documentation.** The design already requires exact path identity, protected-prefix non-shadowing, canonical containment, and no substitution (lines 243–259). The implementation plan should name container-image ancestor symlinks, pre-existing mountpoint type mismatches, and hard-link aliases as mandatory adversarial tests.
2. **Bind supported deployment assumptions.** The design correctly defers host-bind versus named-volume details (lines 453–466). Deployment documentation should state which Docker daemon arrangements can resolve retained host backing objects and how nested parent scopes are represented on each supported platform.
3. **Treat the host-backed store inventory as a maintained security manifest.** The design’s generic rule is sufficient, but new tools, plugins, caches, and spill mechanisms can silently add retrieval channels. A registry-level “filesystem authority class” and conformance test would reduce drift without becoming a general authorization redesign.
4. **Keep browser wording location-specific.** Existing host-global browser automation can remain intentionally available, but an “enforced filesystem lane” report must not imply its process or download paths are contained. The design already requires this distinction (lines 392–405, 425–429).
5. **Do not broaden the threat model.** Semantic packet controls, malicious-parent defenses, general MCP authorization, and server-owned prompts remain adjacent work by explicit design (lines 60–78, 468–480).

## 7. Final rationale

The frozen design is internally coherent across scoped-parent attenuation, Docker daemon namespace reality, exact-path leaf reveals, symlink/substitution/special-file/overlap/RO-RW/workdir safety, resume identity, atomic batches, fresh containers, cross-tool and host-store parity, Playwright/vision/MCP qualification, lifecycle revocation, ordinary compatibility, and preservation of the existing orchestration skill and threat model.

Its current-Hermes mismatches are substantial but expected: today’s implementation deliberately shares the default child container, auto-mounts broad host-backed material, persists no reveal/profile authority, uses host-side paths in some helpers, runs local browser/MCP services outside an attempt environment, and cleans ordinary persistent resources by session semantics. The design directly identifies and forbids those behaviors in protected profiles. None requires changing the requested product contract or replacing generic model-driven delegation.

**Final verdict: ACCEPT WITH NON-BLOCKING NOTES.** Proceed to implementation planning only if every direct gate above is treated as release-blocking for the protected-profile claim.