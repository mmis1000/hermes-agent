# Independent Final Review — Delegation Profile Filesystem Isolation

**Verdict: ACCEPT**

## 1. Frozen-object integrity gate

Reviewed object:

`/home/prod/llm-uiux-investigation/docs/research/subagent-knowledge-boundary-enforcement/delegation-profile-filesystem-isolation-design.md`

| Property | Expected | Observed | Result |
| --- | ---: | ---: | --- |
| SHA-256 | `58d267ceefde68da9387146af14c86912baec1fa040df45ad82b98e3f08d001f` | `58d267ceefde68da9387146af14c86912baec1fa040df45ad82b98e3f08d001f` | Match |
| Byte count | 26,693 | 26,693 | Match |
| Line count | 466 | 466 | Match |

The integrity gate passed. I read all 466 lines of the frozen object. I did not read or rely on any earlier review or reconciliation artifact.

## 2. Review basis

I reviewed the frozen design against:

- current Hermes source at `/home/prod/.hermes/hermes-agent`, Git commit `6b83b37056049432d864198bbab3e2ed5d1fd43f`;
- `/home/prod/llm-uiux-investigation/docs/research/subagent-knowledge-boundary-enforcement/SKILL.md` in full;
- `/home/prod/llm-uiux-investigation/docs/research/theme-transfer-subagent-knowledge-boundary-rule.md` in full; and
- the frozen scope stated in the assignment: retain generic `delegate_task`, preserve ordinary delegation and orchestration, add the technical local-filesystem boundary for child lanes, qualify whole-session claims by the parent's admitted base, and keep malicious-orchestrator and semantic-packet policing outside scope.

The current source was inspected at the relevant seams: delegation schema and dispatch, child construction and tool inheritance, synchronous/background batch execution, durable attempt identity and resume reconstruction, terminal/file environment routing, environment persistence and cleanup, structured-document extraction, vision source resolution and host-cache exceptions, process ownership, and toolset/MCP inheritance.

## 3. Overall assessment

The revised design is internally coherent and technically truthful as a design contract.

It makes a narrow claim: a **filesystem-complete governed-tool boundary**, not whole-child-process isolation and not semantic noninterference. It preserves the existing UI investigation methodology rather than trying to encode that methodology in a server state machine. It keeps ordinary delegation unchanged outside protected sessions, while making profile selection mandatory and fail-closed inside protected sessions. It addresses batch validation, resume reconstruction, late calls from timed-out workers, physical-attempt ownership, deterministic cleanup, structured-document parsing, visual-byte grants, and external MCP/browser qualifications.

No blocking delta defect remains. The substantial work still required is implementation work that the document already states as acceptance conditions. Those pre-existing implementation gates do not justify `REVISE`.

## 4. Contract evaluation

### 4.1 Internal coherence and authority model

The trust model is consistent from objective through acceptance:

- The archive server admits and pins policy.
- The orchestrator remains trusted to construct prompts and packets.
- The delegated child is technically bounded in local filesystem exploration.
- The runtime and container runtime enforce the technical boundary.
- A malicious orchestrator deliberately disclosing information remains out of scope.

The parent-base qualification prevents the document from overstating a child-only mechanism as whole-session isolation. The authority equation at lines 139-144 also makes clear that a child invocation attenuates the admitted session ceiling rather than creating new host authority.

### 4.2 Preservation of the UI investigation skill

The methodology sources require default-deny evidence lanes, smallest-complete packets, fresh contexts, irreversible contamination, preserved originals, controlled unblinding, and truthful distinction between instruction-blind and enforced-blind operation. The design preserves those methodological obligations and changes only local retrieval mechanics.

The design does **not** claim that a filesystem profile alone establishes complete enforced blindness where network, browser, prompt, packet, or MCP channels remain broad. That is correct. The original skill's network-denial, packet-sanitization, contamination, and fresh-worker rules remain necessary where the experiment requires them.

### 4.3 Workflow preservation and scope discipline

The design retains free-form goal/context, packet construction, evidence routing, steering, resume, role-specific mounts, and ordinary delegation defaults. It avoids server-owned prompts, semantic leakage scanners, workflow-transition state machines, model/provider authorization theater, a general MCP policy system, and heterogeneous per-item profiles in the initial contract.

Requiring separate calls for mixed-profile batches is a narrow invocation-level contract, not an unnecessary workflow restriction. It matches the existing invocation-wide model/provider/reasoning override pattern and allows atomic pre-fan-out validation.

### 4.4 Fail-closed behavior and lifecycle

Protected omission/`none`/`default`/unknown-profile cases fail before child creation. Batch validation is atomic. Resume retains the same declarative profile and mounts in a fresh container. Authorization state is independent of the presence of a mutable routing entry. Revocation precedes timeout/interruption teardown, and a tombstone remains while late calls can arrive. Cleanup is physical-attempt-owned, idempotent, and explicitly covers partial startup and parent shutdown.

This is the correct shape for the current Hermes implementation, where a timed-out daemon worker can outlive the returned timeout and where logical subagent identity is stable across resumed physical attempts.

### 4.5 Playwright, vision, MCP, and browser truthfulness

The design correctly distinguishes:

- direct Playwright in the child container;
- visual byte resolution through the task environment or an exact attempt-owned grant;
- durable screenshots written through an explicit artifact mount;
- an MCP/browser process running outside the container; and
- the independent authority of an MCP that can expose host files.

It does not represent a host-global Playwright MCP as container-contained, does not claim containers constrain arbitrary MCP authority, and requires effective records to report browser/MCP location. Those are honest boundaries.

## 5. Classified findings

### 5.1 Blocking delta defects

**None.**

### 5.2 Direct pre-existing implementation gates

These are real gates in current Hermes, but the frozen design already states each one correctly as a required contract or acceptance condition. They therefore do not change the verdict.

#### G1 — Public schema, trusted session policy, and runtime enforcement must be added together

**Classification: direct pre-existing implementation gate**

Exact design authority — **“## 5. `delegate_task` contract” (lines 179-230)**:

> “The existing tool remains. Its protected-session extension is conceptually:” (lines 181-184)  
> “When `profile` is omitted and the session does not require one, Hermes preserves the current shared parent/child delegation environment without behavioral change.” (lines 210-212)  
> “`none`, `default`, an empty value, omission, and unknown profiles fail closed before child creation” (line 220)  
> “The displayed schema is guidance; the runtime enforces the same rule independently.” (line 230)

Current Hermes has no `profile`, `workdir`, or `mounts` fields in the callable or model schema (`tools/delegate_tool.py` lines 3065-3077 and 4208-4307), and its registry handler forwards only the existing fields (lines 4354-4369). The dynamic schema machinery already exists, but model-facing guidance cannot be the enforcement point. The implementation must carry immutable trusted session policy into both initial and nested/resumed dispatch paths, validate independently of schema display, and preserve the current no-profile behavior outside protected sessions.

This is not a design omission: lines 210-230 state the required compatibility and enforcement behavior explicitly.

#### G2 — Attempt-keyed environment routing must replace current child-to-default collapse only for profiled attempts

**Classification: direct pre-existing implementation gate**

Exact design authority — **“### 5.4 Mount validation contract” (lines 232-246)** and **“### 6.1 `ui-isolated`” (lines 254-268)**:

> “Before a child container exists, Hermes validates that…” (line 234), including canonical permitted roots, symlink containment, RO/RW ceilings, target conflicts, protected targets, forbidden source types, and no hidden mounts (lines 236-244).  
> “fresh container per child attempt” (line 260)  
> “no automatic Hermes credential, source, skill, cache, home, current-directory, persistent-workspace, or global-volume mounts” (line 262)  
> “container-routed terminal, file, and code-execution paths” (line 264)

Current terminal routing deliberately collapses ordinary child task IDs to `"default"` (`tools/terminal_tool.py` lines 1175-1207 and 2160-2164), and file operations use the same collapse (`tools/file_tools.py` lines 928-955). Current Docker configuration can inherit global volumes, forwarded environment values, persistent filesystems, and cross-process container reuse (`tools/terminal_tool.py` lines 1349-1472 and 1484-1541).

The protected implementation must use the physical attempt ID as the non-collapsing environment key, suppress every implicit/global mount source, and bind only the validated structured mount plan. Ordinary no-profile calls must continue using today's shared `default` route. Canonicalization and bind creation must be one race-safe authorization operation; a check followed by a substitutable source path would not satisfy the quoted “symlink resolution cannot escape” contract.

The design already states this boundary and its compatibility split correctly.

#### G3 — Every host-backed local retrieval seam must be routed, exactly granted, or excluded

**Classification: direct pre-existing implementation gate**

Exact design authority — **“### 4.5 Protected host-backed data sources” (lines 166-177)** and **“### 7.5 Tool consistency” (lines 347-355)**:

> “Container mounts do not constrain those tools, so protected profiles must classify them explicitly rather than treating them as ordinary container-routed tools.” (line 168)  
> “unrestricted session-history search and host-side skill mutation are not part of the child filesystem lane” (line 172)  
> “All governed local-path tools for a child must resolve the same effective environment, workdir, and mounts regardless of which tool creates the environment first.” (line 349)  
> “Notebook, Word, and spreadsheet extraction must consume bytes obtained through the attempt's environment or an attempt-owned temporary copy” (line 351)  
> “General membership in a shared Hermes media-cache directory is not authorization.” (line 355)

Current child toolsets can include session search, skill read/mutation, browser/computer-use, web extraction, vision, and plugin/MCP tools (`toolsets.py` lines 29-81; child inheritance in `tools/delegate_tool.py` lines 1657-1723). Session resume/search and skills operate on server-local profile stores. Structured-document extraction currently calls the host parser on the resolved path before creating file operations (`tools/file_tools.py` lines 1128-1148). Vision currently permits broad host reads under shared media-cache roots on non-local backends (`tools/image_source.py` lines 210-259), although non-cache paths already use the active task environment and fail closed when none exists (lines 262-316).

Implementation must inventory static, dynamic registry, plugin, and MCP-derived tools; route arbitrary local-path readers through the attempt environment; admit only exact read-only methodology bundles; disable unrestricted history and host-side skill mutation; change structured-document extraction to operate on attempt-obtained bytes; and replace cache-wide vision authority with exact attempt grants in protected attempts. Network-only tools need not be disabled merely because they are unrelated to local host retrieval.

The design already makes this whole-tool-surface gate explicit and does not mistake mount containment for containment of named host stores.

#### G4 — Durable resume metadata and authorization tombstones must be keyed to the physical attempt

**Classification: direct pre-existing implementation gate**

Exact design authority — **“### 7.2 Persistence and resume” (lines 298-309)** and **“### 7.3 Attempt authorization state” (lines 311-321)**:

> “The durable record stores the resolved declarative profile identity/hash, workdir, and validated mount specification—not an ephemeral container ID.” (line 300)  
> A resumed child “retains the same profile and mount declaration,” “receives a fresh container,” and “cannot switch profile or add authority as part of resume.” (lines 302-307)  
> “A protected physical attempt has an authorization state independent of whether its container routing entry currently exists” (line 313)  
> “A revoked, cleaned, unknown-protected, or profile-missing attempt fails closed; it never falls back to the ordinary shared/default environment.” (line 319)  
> “the runtime retains a denial tombstone after cleanup for as long as a late call can still arrive.” (line 321)

Current Hermes already distinguishes stable logical subagents from physical attempt IDs and reserves a new attempt on resume (`tools/delegation_repository.py` lines 180-302). However, current resume metadata does not contain profile or mount declarations (`tools/async_delegation.py` lines 340-414), and reconstruction currently restores model/tool/workdir metadata only (`tools/delegate_tool.py` lines 1398-1537). Current timeout handling can abandon a daemon worker without waiting for it to unwind (`tools/delegate_tool.py` lines 2611-2652 and 2746-2749), making the independent denial state essential.

The authorization state should remain distinct from conversational/delivery statuses such as completed, interrupted, or abandoned. Every governed tool lookup must consult it before environment lookup, and missing routing cannot mean ordinary authority for a protected attempt.

The design states this correctly; implementation must now attach the declared boundary to existing durable attempt identity.

#### G5 — Cleanup must cover the entire attempt-owned resource ledger and force-remove strict ephemeral containers

**Classification: direct pre-existing implementation gate**

Exact design authority — **“### 7.4 Cleanup and resource ownership” (lines 323-345)**:

> “One physical attempt ID owns its complete resource ledger” (line 325), including environment routing, background processes, browser sessions, file cache/cwd, grants, and creation locks (lines 327-332).  
> Cleanup applies on success, exception, cancellation/interruption, timeout, partial startup failure, and parent shutdown (lines 334-341).  
> “Cleanup is idempotent, keyed by the physical attempt ID used for tool routing rather than the child's conversational/session identity” (line 343).  
> Orphans carry attempt ownership labels for bounded recovery and are never reusable (line 345).

Current cleanup is fragmented. Terminal per-turn cleanup deliberately preserves persistent environments (`agent/chat_completion_helpers.py` lines 2155-2180); `cleanup_vm` defaults to honoring persistent reuse, and its source notes that no current caller force-removes (`tools/terminal_tool.py` lines 1788-1853; `tools/environments/docker.py` lines 1355-1359). Background processes and browser resources have separate registries. Child construction and batch startup can also fail after partial allocations.

Protected ephemeral attempts therefore need an explicit attempt-ledger owner that revokes first, then idempotently clears file-operation caches, cwd and overrides, process/browser resources, visual grants, creation locks, and environment routing, and force-removes the attempt container without touching the ordinary shared environment.

This is a direct implementation gate already spelled out by the design, not a reason to revise the contract.

#### G6 — The admitted parent base is required for whole-session claims

**Classification: direct pre-existing implementation gate**

Exact design authority — **“### 4.3 Protected session base environment” (lines 131-146)**:

> “The child-profile allowlist constrains delegation; it does not by itself constrain local-path tools used directly by the parent orchestrator.” (line 133)  
> A whole-session claim therefore requires “a fixed base execution profile at admission” (lines 133-135).  
> “If a deployment constrains only delegated children and does not assign a base environment to the parent, it may claim child-lane isolation only—not whole inspection-session host isolation.” (line 146)

Current Hermes has no archive-server admission seam that pins this base and child attenuation policy. Implementers must bind the parent governed-tool environment before the protected session begins and ensure child mount authority intersects that admitted ceiling. A child-only deployment remains valid if it reports only the narrower child-lane claim.

The design is technically honest precisely because it does not infer whole-session isolation from child profiles.

#### G7 — Direct Playwright, visual analysis, built-in browser tools, and MCPs require location-aware capability auditing

**Classification: direct pre-existing implementation gate**

Exact design authority — **“### 6.2 `ui-isolated-playwright`” (lines 270-284)** and **“## 9. MCP, browser, and external capability boundary” (lines 374-391)**:

> “Playwright may run directly through the containerized terminal.” (line 284)  
> “If an MCP-based Playwright service runs outside the container, its authority is reported separately and is not misrepresented as container-contained.” (line 284)  
> The effective-access report distinguishes container filesystem authority, host/network MCP authority, browser authority and location, and network access (lines 378-383).  
> “An MCP that exposes broad host filesystem reads is incompatible with a claim that the child can access only mounted files unless the MCP is independently constrained.” (line 385)  
> “an existing host-global Playwright MCP remains usable if intentionally allowed, but its browser process is outside this filesystem boundary and must be labeled as such.” (line 391)

Current vision resolution already accepts a task ID and reads non-cache local paths through the active environment (`tools/vision_tools.py` lines 932-1058 and 1063-1352; `tools/image_source.py` lines 89-145 and 262-316), which is a sound reuse seam. Current children may also inherit built-in browser automation and MCP toolsets. The protected implementation must ensure that any built-in browser/computer-use or MCP capability capable of `file:` navigation, host uploads/downloads, host browser-state inspection, or other local retrieval is either attempt-contained, independently constrained, excluded from the mounted-only claim, or explicitly reported as external authority.

The document neither disables unrelated MCPs nor overclaims their containment. That is the correct contract.

### 5.3 Adjacent improvements

These are non-blocking clarifications suitable for implementation planning or operator documentation. They do not indicate a defect in the frozen design.

#### A1 — Make transitive policy propagation explicit for nested orchestrator-role children

**Classification: adjacent improvement**

Exact design authority — **“### 5.3 Protected-session behavior” (lines 214-230)** and **“## 11. Design acceptance conditions” (lines 417-437)**:

> In a protected session, `profile` is mandatory and omission/default/unknown values fail closed before child creation (lines 216-220).  
> “A protected session cannot use unprofiled/default delegation or a profile outside its pinned allowlist.” (line 422)  
> “The orchestrator retains ordinary goals, context, packet construction, steering, resume, and evidence-routing behavior.” (line 424)

Current Hermes supports `role='orchestrator'` nested delegation. The acceptance language already applies to all delegation in a protected session, so a nested child must not escape by starting an unprofiled grandchild. An implementation plan should say explicitly how the immutable protected policy and admitted ceiling propagate into orchestrator-role children and how a nested invocation names host-side sources or derives mounts from already admitted roots. This is clarification of an existing acceptance condition, not a new contract and not a blocker.

#### A2 — State the registry-drift failure mode chosen for resume

**Classification: adjacent improvement**

Exact design authority — **“### 4.2 Session delegation policy” (lines 107-129)** and **“### 7.2 Persistence and resume” (lines 298-309)**:

> “It is pinned for the admitted session so later profile-registry changes do not silently widen or alter that run.” (line 121)  
> “The durable record stores the resolved declarative profile identity/hash, workdir, and validated mount specification” (line 300)  
> Resume retains the same declaration and cannot add authority (lines 302-307).

The contract is sufficient: registry drift may neither silently alter nor widen a resumed run. Implementation planning should choose and document whether the admitted resolved profile is durably snapshotted or resume rejects a hash mismatch until the original definition is available. Either satisfies the frozen contract if it fails closed; the exact serialization is intentionally deferred at lines 439-452.

## 6. Acceptance-condition cross-check

All seventeen design acceptance conditions are coherent and necessary:

1. ordinary omission remains unchanged;
2. protected profile omission/default/out-of-allowlist fails closed;
3. whole-session claims require a parent base;
4. free-form orchestration remains;
5. containers are per physical attempt;
6. every governed local retrieval path is bounded;
7. implicit Hermes/home/cache/workspace/global mounts are absent;
8. session history, broad skills, and cache-wide reads cannot bypass the lane;
9. structured documents read attempt-obtained bytes;
10. mount modes are enforced;
11. siblings are isolated except intentional shared mounts;
12. resume reconstructs the same declaration in a fresh container;
13. late/revoked/cleaned attempts never fall back;
14. all terminal and partial-startup outcomes clean the ledger;
15. Playwright screenshots can reach vision and durable artifacts intentionally;
16. effective records distinguish container, host-backed, and external authority; and
17. the design does not overclaim semantic, malicious-orchestrator, memorization, or broad-MCP protection.

## 7. Final verdict

**ACCEPT.**

The frozen design satisfies the requested product scope. It preserves the original UI investigation workflow and ordinary delegation, adds only the necessary local-filesystem enforcement boundary, qualifies parent/whole-session claims honestly, fails closed across protected initial/batch/resume/late-worker paths, gives cleanup coherent physical-attempt ownership, and accurately states Playwright, vision, browser, and MCP boundaries.

The current Hermes source does not yet implement the contract, but those direct pre-existing implementation gates are already represented as explicit design requirements and acceptance conditions. No blocking delta defect requires another design revision.
