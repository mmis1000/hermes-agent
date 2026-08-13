# Strict Independent Generic-Hermes Design Review

**Verdict: ACCEPT WITH NON-BLOCKING NOTES**

## 1. Exact reviewed identity

Review object:

`/home/prod/llm-uiux-investigation/docs/research/subagent-knowledge-boundary-enforcement/delegation-profile-filesystem-isolation-design.md`

I computed the identity before reading the design substantively. All three frozen measures match exactly:

| Measure | Required | Observed | Result |
|---|---:|---:|---|
| SHA-256 | `d6049d25fbd3a9d55aa6e05ea2b14b40a756e442d11f046fc73d701522607da2` | `d6049d25fbd3a9d55aa6e05ea2b14b40a756e442d11f046fc73d701522607da2` | PASS |
| Bytes | 31,144 | 31,144 | PASS |
| Physical lines | 485 | 485 | PASS |

I also independently verified the preserved predecessor at `revisions/delegation-profile-filesystem-isolation-design-ui-use-case-reviewed.md` as SHA-256 `5d35f95edaeff82de1572cb992cdd2c5993365593b269ce3262c63bb337e049b` and compared the complete unified diff.

Current-Hermes evidence was inspected at `/home/prod/.hermes/hermes-agent`, exact revision `6b83b37056049432d864198bbab3e2ed5d1fd43f`; `git status --short` was empty at the review checkpoint.

No design or Hermes source file was edited.

## 2. Auditable coverage of the complete design

The complete frozen object was read. Its reviewed top-level headings and exact ranges are:

| Quoted heading | Design lines |
|---|---:|
| “## 1. Decision summary” | 7–24 |
| “## 2. Objective” | 25–42 |
| “## 3. Authority and threat model” | 43–89 |
| “## 4. Core concepts” | 90–195 |
| “## 5. `delegate_task` contract” | 196–268 |
| “## 6. Profile semantics” | 269–306 |
| “## 7. Runtime and lifecycle behavior” | 307–374 |
| “## 8. Preservation of existing orchestration and domain workflows” | 375–391 |
| “## 9. MCP, browser, and external capability boundary” | 392–410 |
| “## 10. Effective configuration and observability” | 411–434 |
| “## 11. Design acceptance conditions” | 435–457 |
| “## 12. Deferred decisions” | 458–472 |
| “## 13. Review brief” | 473–485 |

## 3. Scope and genericization assessment

### 3.1 Generic Hermes architecture and ownership — PASS

The owner is now unambiguously Hermes runtime/delegation infrastructure: the decision summary calls it a generic Hermes capability usable by software development, research, data processing, document review, UI investigation, and other workflows (lines 9–15); the objective requires selection by any trusted Hermes session initiator and use by any domain workflow (lines 27–35); and the execution profile is server/operator-owned while the orchestrator merely selects a name (lines 92–109).

The architecture is not placed in a UI skill. Session admission, profile resolution, reveal validation, governed-tool routing, and container lifecycle belong to the trusted Hermes runtime (lines 45–51, 111–150). UI methodology remains with its consuming orchestrator rather than becoming runtime behavior (lines 31–35, 375–390).

### 3.2 UI-independent contract surfaces — PASS

The core surfaces are generic throughout:

- session policy and profile resolution use generic protected-session handles (lines 111–150);
- reveal examples use `/work/hermes-runs/.../inputs/...` and `/work/hermes-runs/.../workspaces/...`, not an archive or UI lane (lines 152–179, 196–223);
- profile semantics describe a general filesystem-isolated workload plus a generally useful browser-ready variant (lines 269–305);
- lifecycle, durable authority, attempt state, cleanup, and all-tool consistency are domain-neutral (lines 307–374);
- acceptance expressly rejects a UI role, packet type, directory layout, or skill dependency in the core schema/enforcement (lines 435–456);
- the review brief makes generic Hermes ownership itself part of the frozen object (lines 473–485).

The browser-ready profile does not make the feature UI-specific. Browser execution is a workload capability, and its location/authority is separately qualified (lines 287–301, 392–409).

### 3.3 Remaining UI/archive/source-blind wording — PASS, bounded compatibility example

Every remaining occurrence is scoped away from the runtime contract:

- lines 31–35 explicitly title UI investigation as “one motivating use case” and state that UI roles, archive objects, source-blind packets, and UI layout are absent from the Hermes contract;
- line 47 names the UI archive server only as one example of a trusted session initiator;
- line 66 leaves source-blind feedback under caller/domain-owned integrity rules in the explicit non-goals;
- line 388 says the source-aware/source-blind behavior is preserved for the UI consumer “without making those concepts part of the generic Hermes API”;
- lines 456 and 478 make UI-independence and compatibility-only status acceptance/review requirements.

This wording motivates and tests compatibility; it does not define profile resolution, reveal authority, lifecycle, or tool routing.

### 3.4 Illustrative profile names — PASS, not mandatory built-ins

The YAML, public-shape example, failure text, and profile-semantics headings use `filesystem-isolated` and `filesystem-isolated-browser` (lines 115–125, 202–218, 239–243, 271–301). Line 125 expressly says these are illustrative server/session-defined handles and not mandatory Hermes built-ins. Lines 460–464 separately defer the exact initial profile names and registry serialization. The labels therefore describe two policy classes without specifying immutable built-in identifiers.

### 3.5 Previously accepted technical contract — PASS, not weakened

The full predecessor/current diff changes ownership, terminology, examples, and compatibility framing; it does not remove or relax the accepted technical behavior. The current frozen design retains:

| Preserved technical property | Current design evidence |
|---|---|
| Path-preserving reveals | lines 152–181, 247–263 |
| Scoped-parent attenuation | lines 135–150, 181, 251–256 |
| Daemon-side backing resolution rather than trusting the visible path as a Docker source | lines 167–177 |
| Filesystem-complete governed-tool/store parity | lines 84–88, 183–194, 365–373 |
| Fresh physical container per child attempt, including resume | lines 277–285, 309–325 |
| Durable retry/resume authority and backing identity | lines 315–339 |
| Attempt-owned cleanup, force removal, tombstones, and orphan ownership | lines 329–363 |
| MCP/browser location and authority qualification | lines 392–409 |
| Ordinary unprofiled compatibility | lines 13–15, 225–245, 439–455 |

No genericization edit contradicts these properties.

### 3.6 Free-form model orchestration and domain workflows — PASS

Protected sessions still expose normal free-form goals, context, packet construction/routing, profile choice within the admitted set, feedback, steering, and resume (lines 15, 41, 225–245, 375–390). The public shape remains an extension of `delegate_task`, not a replacement gateway (lines 196–237). Domain workflows retain their own integrity rules, repair budgets, review authority, publication rules, and acceptance criteria (lines 375–388). Acceptance conditions repeat this preservation requirement (lines 439–456).

Current Hermes confirms that these are real generic seams worth preserving: `delegate_task` accepts free-form `goal`, `context`, and batch tasks (`tools/delegate_tool.py:3065–3089`, `3185–3218`); its public schema exposes free-form goal/context and per-task goal/context (`tools/delegate_tool.py:4208–4286`); and the single dispatch adapter forwards the same generic fields across invocation paths (`run_agent.py:6451–6484`).

### 3.7 Narrow threat model — PASS, not broadened

The guarantee is autonomous child local-filesystem retrieval confinement, not semantic noninterference (lines 37–41). The design explicitly excludes malicious-orchestrator defense, server-owned child prompts, workflow-transition state machines, restrictions on free-form goal/context or packets, semantic leakage detection, general MCP authorization redesign, and whole-agent isolation (lines 64–82). It repeats that host-store closure does not inspect prompt semantics (lines 183–194), leaves packet mistakes to consuming workflow rules (lines 375–390), and makes broader semantic/general-authorization proposals non-blocking adjacent work (lines 455, 473–485).

The parent base profile at lines 135–150 does not broaden the attacker model; it merely qualifies when a deployment may claim whole-session rather than child-only isolation.

### 3.8 Design plan rather than implementation checklist — PASS

The object identifies itself as a product/runtime contract and explicitly not an implementation plan (lines 1–5). It states behavioral invariants, lifecycle semantics, acceptance conditions, and deferred deployment bindings, while deferring registry location/serialization, exact handles, backend mount mechanism, resource tuning, Playwright MCP choice, approval UX, whole-agent execution, and heterogeneous batches (lines 458–471). It contains no file-by-file change list, commit sequence, command list, or TDD checklist. Its implementation-relevant precision is appropriate for a security-sensitive design contract and does not turn it into an implementation plan.

## 4. Findings and classification

### Design blockers

**None.** The genericization delta is coherent and complete enough to make the capability genuinely generic Hermes infrastructure. No direct pre-existing source condition makes that generic product contract false or unusable; the current source gaps are implementation work already required by the design.

### Direct later implementation gates

These gates are release-blocking for a future implementation’s protected-profile claim, but they are not blockers to accepting this design.

1. **Public contract and trusted session policy propagation.** Add invocation-wide `profile`, `workdir`, and `reveal` to the static/dynamic schema, direct function, registry fallback, single dispatch adapter, and batch preflight without narrowing goal/context/packet behavior. Current source has no such fields in the function (`tools/delegate_tool.py:3065–3077`), schema (`tools/delegate_tool.py:4208–4308`), registry handler (`tools/delegate_tool.py:4354–4369`), or central adapter (`run_agent.py:6451–6484`). Enforce the pinned session policy before any child/log/container side effect as required by design lines 111–150 and 221–263.

2. **Trusted backing-object and mount-plan resolution.** Implement the parent-admitted backing table and a daemon-resolvable mount-plan compiler; never reinterpret a model-visible parent path as arbitrary host authority. Validate attenuation, canonical/backing identity, overlaps, protected prefixes, object types, TOCTOU attachment, and exact destination identity before child creation (design lines 135–181, 247–263).

3. **Attempt-keyed, all-tool routing with fail-closed protected state.** Current terminal routing deliberately collapses ordinary child IDs to `default` (`tools/terminal_tool.py:1175–1207`, `2160–2172`); file tools use the same collapsed key (`tools/file_tools.py:928–955`); code execution independently creates/reuses that effective environment (`tools/code_execution_tool.py:687–748`). Protected attempts need one fresh physical-attempt key and the same resolved environment/reveals across terminal, file, process, code, image, browser, and helper paths, while ordinary sessions retain the current collapse. Missing/revoked/cleaned protected state must deny rather than fall through (design lines 307–373).

4. **Durable authority across retry/resume.** Current initial persistence captures only goal(s), context, toolsets, role, model, and batch status (`tools/delegation_repository.py:180–225`), and the resume metadata allowlist lacks profile hash, reveals, session ceiling, and backing identity (`tools/async_delegation.py:327–345`). Hermes already reserves a new physical run/attempt on resume (`tools/delegation_repository.py:241–302`) and reconstructs a logical child (`tools/async_delegation.py:417–560`); the implementation must persist the immutable profile/reveal/backing declaration and reconstruct it into that fresh attempt or fail closed, exactly as required by design lines 315–339.

5. **Attempt-owned revocation and cleanup ledger.** Current child cleanup unregisters the subagent and releases a credential lease but does not own a protected environment/browser/process/cache ledger (`tools/delegate_tool.py:2989–3008`). Current `cleanup_vm` defaults to persistence-respecting cleanup and requires explicit `force_remove=True` for destructive teardown (`tools/terminal_tool.py:1788–1846`). Implement revoke-before-teardown, late-call tombstones, idempotent physical-attempt cleanup, force removal, partial-start rollback, and bounded orphan recovery for all outcomes in design lines 329–363.

6. **Host-side reader/store and structured-document closure.** Current structured-document handling resolves a task path and then invokes host-side extraction plus `os.path.getsize` (`tools/file_tools.py:1109–1148`); the parser directly uses host `open`/`ZipFile` (`tools/read_extract.py:42–49`, `61–64`, `107–114`, `133–159`). Skills resolve profile-owned host directories (`tools/skill_manager_tool.py:150–168`), while session search opens current or other profile state databases (`tools/session_search_tool.py:144–164`, `619–648`). Route these through attempt-owned bytes/exact admitted bundles or exclude them under strict profiles, as design lines 183–194, 365–373, and 444–447 require.

7. **Vision, browser, MCP, and indirect host-control qualification.** Vision is task-aware and can read through the active environment, but current non-local handling also admits broad Hermes media-cache roots (`tools/image_source.py:210–259`, `262–316`); strict mode must use exact live evidence grants. Browser sessions are task-keyed but may be local/cloud/external (`tools/browser_tool.py:2808–2851`), so process location and artifact authority must match the effective-access report. `computer_use` drives host applications through a cua-driver MCP/stdio process and can target desktop/shell windows (`tools/computer_use/__init__.py:1–29`; `tools/computer_use/cua_backend.py:1–17`, `145–154`, `675–703`); a strict profile must exclude it or bind it to an independently isolated desktop if the revealed-files-only claim is made. This is the narrow autonomous-retrieval closure required by design lines 84–88, 392–409—not a general authorization redesign.

8. **Compatibility and acceptance validation.** Exercise every condition in design lines 435–456, including ordinary single/batch/nested/shared behavior, protected omission/default/unknown rejection, scoped-parent RO/RW attenuation, daemon backing identity, terminal-first/file-first parity, structured documents, exact visual grants, sibling isolation, fresh resume, late timeout calls, all cleanup outcomes, and truthful external MCP/browser qualification.

### Adjacent non-blocking improvements

1. **Keep `computer_use` explicit in the later implementation inventory/effective-access documentation.** The generic route-or-exclude contract is sufficient at design level, and this was not introduced by the genericization delta. Naming the host-desktop channel in the implementation plan will reduce the chance that a pathname-free but filesystem-retrieving surface is overlooked. This must remain a protected-profile qualification, not grow into general desktop authorization policy.
2. **Keep example-handle wording next to generated schemas and operator docs.** The frozen design already makes the handles illustrative (line 125) and defers exact names (line 463). Future docs should preserve that distinction so examples are not accidentally shipped or tested as universal built-ins.
3. **Do not reopen adjacent policy.** Malicious-orchestrator defenses, semantic packet policing, server-owned prompts, general MCP authorization, and rigid domain workflows remain explicitly out of scope (lines 64–82, 473–485).

## 5. Concise rationale

The exact generic-Hermes revision succeeds at its authorized delta. Generic Hermes runtime/session infrastructure owns admission, profile resolution, reveal validation, attempt routing, durable authority, and lifecycle; consuming workflows retain free-form orchestration and domain semantics. UI/archive/source-blind language is confined to a motivating compatibility example and explicit non-dependency statements. Example profile handles are plainly illustrative. The complete accepted technical boundary remains intact, including scoped path-preserving reveals, daemon-side backing resolution, all-tool parity, fresh attempts, durable resume authority, fail-closed revocation, cleanup, external-capability qualification, and ordinary shared compatibility.

The current Hermes source does not yet implement the contract, but its existing generic delegation, task/environment, durable attempt, browser, vision, and cleanup seams make the design implementable. Those source mismatches are direct later implementation gates already demanded by the design, not reasons to reject the generic product claim.

**Final verdict: ACCEPT WITH NON-BLOCKING NOTES.**
