# Metadata-first Behavioral Reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reflect on other agents' execution and propose evidence-backed improvements to any behavior-affecting surface, with explicit project/distro destinations, bounded context and honest coverage.

**Architecture:** Keep native completion authority, normal managed OMP and existing evidence/proposal transactions. Replace eager packets with schema-2 selection manifests, add one scoped inspection module, and introduce general schema-2 behavior-change reports. Default review connects intent, actions and outcome; skills are inspected only when implicated. No generic broker, automatic remediation or new background service.

**Tech Stack:** Existing Python 3.12+, standalone OMP, Herdr/tmux, Treehouse and optional verified official `@langfuse/cli@1.2.4`; existing pytest/native OMP fixtures. No dependency addition.

**Spec:** [Metadata-first behavioral reviewer design](../specs/2026-11-04-metadata-first-reviewer-design.md), including exact new report/target contracts and the full proposed reviewer skill. This plan supersedes earlier skill-only/schema-1-new-report and metadata-only-default completion assumptions.

## Global Constraints

- Planning revision only until implementation is requested. No live distro update, service/model/API call, push/PR, merge or lease disposal is authorized here. Keep prior ready worktrees unchanged.
- Any behavior-affecting surface can be recommended: instructions/AGENTS.md/skills, prompts/task briefs, context/memory/retrieval/compaction, hooks/extensions/tools/schema/CLI, configuration/permissions/model routing, orchestration/delegation/supervision/lifecycle, validation/feedback. Categories are not a closed target allowlist. This is execution improvement, not a general code audit.
- Project-dependent rules stay in their registered project. Distro recommendations require a transfer rationale and no project-specific normative assumptions. Retain project-linked private citations; do not publish them as global guidance. Human triage/apply boundaries remain unchanged.
- Hypothesis-gate all surface investigation; no skill hypothesis means zero skill/catalog lookup. Tool defects can yield tool fixes, not mandatory instruction workarounds. Do not force findings or infer causality from assignment/loading alone.
- Native final-response/confirmed-exit proof owns eligibility, including living worker/central processes. Preserve source/branch/cutoff/dedup/retrospective/self-exclusion and failed-run retryability; no idle/task-ready/Langfuse substitute.
- New request/receipt/selection/report/proposal writes use schema 2; target records use schema 1; config remains schema 2. Historical schema-1 packet/reports/proposals keep their bytes/meaning. No new old-engine fallback.
- New selections use `paths()['selections']` at data/reviewer/selections; existing manifests path remains config/instruction snapshots. Runtime gets only request/selection manifest and dedicated reviewer skill, not eager packet/catalog bodies.
- Response/manifest cap 16 KiB serialized UTF-8, metadata query max20 rows, CLI stdout max262144 bytes. Existing defaults: candidates20, admitted input262144, output65536, deadline1800s. These are not total model-input/cost/shell caps. No hidden polling/retries.
- Official local Langfuse remains optional and owned-loopback guarded; native fallback must work. Never implicitly install/enable services, expose keys or use cloud/ambient endpoints.
- Normal tools remain; recommendation-only/query-only are policy, not an OS sandbox. Preserve existing probe execution/cleanup restrictions verbatim.
- Before exported-symbol changes use available LSP references, otherwise scoped caller searches with the capability limit stated. One integration owner coordinates shared reviewer.py/bin/agd edits. Delegated editors skip builds/tests/lint/formatters; owner handles red checks before edits and integrated verification after them.

## Review Focus

- Clean tool metadata may hide a missed requirement or unsupported completion: bounded intent/action/outcome reflection and its semantic evaluation (Tasks 1/3/5/6).
- A tooling problem must be representable without fabricating a skill; project versus distro targets must be structurally separate (Tasks 2/3).
- Scope/version/cursor confusion, secret configuration and arbitrary artifact labels must not forge target ownership or native coverage (Task 2).
- Changed target guards must revise one supported proposal, while same prose in two projects cannot collide; old artifacts stay immutable (Task 3).
- Partial/held evidence, failure/crash and budget boundaries must neither claim reflection nor cause endless paid no-progress loops (Tasks 1/3/4).

## Ownership and sequencing

Task 1 defines shared manifest/project/progress contracts. Task 2 owns the new inspection module and its CLI branches; Task 3 owns report/proposal schema and finalization in reviewer.py. Task 4 integrates their sole public runtime path. Task 5 owns skill/reference/bootstrap/docs and can draft after Task 1, but uses finalized Task 2/3 interfaces. Task 6 owns integrated verification and release-readiness. Sequence shared core edits; no competing plan variants or half-deployed public cutover. The plan is one complete feature, not six independent shipped modes.

### Task 1: Native selection, trusted project scope and bounded reflection progress

**Files:** Modify `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_episodes.py`, `tests/test_reviewer.py`, `tests/test_reviewer_episodes.py`. Reuse `projects.read` and task/native records without adding a project-registry migration or alternate parser.

**Interfaces:**
- `select_manifest(home, *, cutoff, config_snapshot, retrospective_review_run_ids=(), detail_episode_ids=(), deadline_epoch_ms=None) -> dict`.
- `read_manifest(home, manifest_id) -> dict`.
- Extend `reviewer_episodes.read_native(..., metadata_only=False)` using its current transient index and branch proof; metadata mode returns source/boundary/semantic projection and supported metadata, not bodies.
- Add paths selections/catalogs/inspections/progress; do not reuse manifests for selections. Immutable metadata/captures live in data, mutable progress/journals in state.
- Selected episode fields and project binding follow the spec. Bind home/project ID, registry identity digest and selected task revision; central uses only explicit proved links and no invented task. Freeze intent/terminal/feedback locators and hashes, not contents. Project registry identity derives from current id/path/common_dir/source, not mutable trust/base settings; never confuse it with Langfuse project ID.
- Progress per episode+basis records successful screening, claimed grounded reflection, partial ranges and limited/no-progress holds. Basis includes native semantic projection, trusted project binding and explicitly linked intent/feedback versions. Selection rotates pending bounded reflection with new work, not just unseen metadata; explicit detail passes retain remaining material access.

- [ ] **Step 1: Add failing regressions.** `test_manifest_keeps_live_final_episode_and_excludes_live_tail`, `test_manifest_freezes_project_identity_not_langfuse_project`, `test_central_unknown_project_does_not_gain_target_scope`, `test_selection_has_locators_not_body_sentinels`, `test_semantic_rewrite_changes_basis_but_title_append_does_not`, `test_screened_episode_remains_pending_reflection`, `test_unfinished_old_episode_does_not_starve_new_episode`, `test_unchanged_limited_basis_holds_without_paid_redispatch`, `test_new_feedback_or_explicit_detail_reopens_hold`. Assert exact scope/progress/outcomes, not serialization wording.
- [ ] **Step 2: Owner observes red.** Run `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_episodes.py -k 'manifest or reflection or limited_basis or semantic_rewrite or project_scope'`; new contracts must fail for missing behavior before implementation.
- [ ] **Step 3: Implement metadata-only proof and deterministic bounded admission.** Reuse canonical hashes, source identity, native final/window proof and current cutoff/legacy/retrospective rules. Prefix-fit max_candidates/16KiB/config budget with visible omissions; inability to fit one candidate is not no-work. Store coordinator-only catalog IDs/hashes without prefixes or eager model reads. No target-category restriction follows from that catalog.
- [ ] **Step 4: Implement scope/progress consumers.** Bind registered project identity to the manifest; return no project target authorization for unproved central links. Reuse successful range/message IDs without consuming unread text. Rotation is scheduling state, not coverage; limits/proof holds remain visible and reopen only on changed basis/relevant limits or explicit detail.
- [ ] **Step 5: Owner runs the targeted regressions and commits the internal contract.** Keep the public runner on its existing path until Task 4 integrates Tasks 2/3. No partial rollout or live process restart.

### Task 2: Scoped evidence and hypothesis-driven inspection of any surface

**Files:** Create `bin/agd_lib/reviewer_inspection.py`, `tests/test_reviewer_inspection.py`; modify `bin/agd` inspection/target/skills dispatch and `reviewer.py` trusted capture helper. Reuse existing langfuse_cli/observability/projects/worker_skills guards, evidence storage and native parser; no generic backend adapter registry.

**Interfaces:**
- `inspect(home, run_id, episode_id, *, source='auto', record_id=None, part=None, cursor=None) -> dict`.
- `list_skills(home, run_id, *, cursor=None) -> dict`; `show_skill(home, run_id, skill_id, *, cursor=None) -> dict`.
- `inspect_target(home, run_id, *, scope, project_id=None, path=None, component=None, operation='modify', surface, basis_source_ids=(), cursor=None) -> dict`.
- `read_target(home, run_id, target_ref) -> dict` validates the immutable record and referenced capture integrity.
- CLI exact forms are in the spec. Add target inspect with --scope project|distro, --project, exactly one --path|--component, --operation create|modify|remove, --surface and repeatable --basis-source, plus --cursor. Surface is printable 1–80 characters, descriptive and not an enum. All capture/list operations enter existing mutation/uninstall admission and require an active run; no forged central/HERDR_ENV authority.
- Returned target record and guard have the spec's exact keys; identity/source/absence proof is native or explicitly evidence-described. Public `evidence add` cannot supply trusted origin/ownership/material-ID fields. A component can be outside AGD's edit authority and still be a legitimate proposed destination; record that uncertainty instead of inventing a local file.

- [ ] **Step 1: Add failing evidence/target tests.** Verify real JSONL/Git fixtures for selected-versus-foreign projects, same-session wrong interaction, native rewrites, title/append stability, private file/symlink/traversal rejection, UTF-8 range boundaries, creating an absent target and refusing false absence. Cover project AGENTS.md, project script, distro hook, non-secret setting and evidence-described external tool contract without new adapters; zero skill calls are necessary for the latter targets. Refuse missing component evidence and forged file/version ownership.
- [ ] **Step 2: Owner observes red.** Run `python3 -m pytest -q tests/test_reviewer_inspection.py`; test outputs/citation admission and scope boundaries, not mock echo/argv wiring.
- [ ] **Step 3: Implement bounded Langfuse/native inspection.** Fixed verified `langfuse --api-version auto api observations list`; metadata first, specific observation I/O only after scope validation. Enforce 20-row/16KiB response/262144-byte raw-output limits, safe diagnostics and remaining deadline with subprocess termination proof. Missing/late/ambiguous correlation falls back to native; no cloud or automatic restart/retry. Native details recheck source/window/projection and retrieve only requested ranges, including explicitly bound brief/intent/feedback artifacts.
- [ ] **Step 4: Implement file/component target inspection.** Resolve only admitted owner roots. Project files use the frozen Git revision and safe relative path; distro files record current observed bytes/version. Capture bounded requested content and full source hash without eager repository scans. Create intent proves absence; remove remains non-executing. Component locators require admitted basis sources and described guards; never infer ownership from model labels. Secret-bearing settings use sanitized field observations, not whole auth/env/config files or secret hashes.
- [ ] **Step 5: Implement lazy skill lookup through the same target mechanism.** Reuse validated catalog and matching assignment snapshots; distinguish current target baseline from observed historical loading. No automatic skill/catalog read is inserted into reflection. Missing or changed historical skill bytes stay a limitation rather than silent version replacement.
- [ ] **Step 6: Record immutable exposure/target receipts.** Bind source/range/version/query/capture time and page chains, not model attention. Cursor cannot cross run/episode/target/view/version. Repeated same-run capture is idempotent; input accounting includes targets and exposed ranges once. Re-enumerate Langfuse metadata across runs using accepted record/version IDs; do not persist provider cursor as stable authority.
- [ ] **Step 7: Owner verifies targeted tests and commits.** Include CLI failure/overflow/deadline, exact budget boundary, partial response, foreign target/cursor, secret refusal and no implicit service/tool installation. Normal tools remain; scoped inspection is not a sandbox.

### Task 3: General recommendation schema, destination-aware proposals and honest coverage

**Files:** Modify `bin/agd_lib/reviewer.py`, `tests/test_reviewer.py`, `tests/test_reviewer_inspection.py`; update report/proposal list/show/dispose presentation in `bin/agd` only where actual schema consumers require it. No automatic apply operation.

**Interfaces and contract:**
- Add `_validate_report_v2(home, receipt, manifest, report, evidence, targets, journal) -> dict`; historical `_validate_report` logic remains an explicit schema-1 read/recovery branch, never a new-run fallback. Preserve public submit/recover/finalize entrypoints and existing locking/prepared submission mechanism.
- New report exact root keys: schema2, summary, episode_reviews, findings. Episode row keys: episode_id, assessment(screened/reflected/limited), intent_sources, action_sources, outcome_sources, limitations. Reflected requires admitted content sources for all three roles, selected scope and journal provenance, not metadata alone. This is evidence availability, not a proof the model reasoned correctly.
- Finding exact keys: episode_ids, target_ref, finding, proposed_change, expected_benefit, applicability, scope_rationale, citations, counterexamples, alternatives, uncertainty, validation, validation_limits, risks, draft. Use exact types/invariants in spec. All recommendation episodes must have reflected rows; no mandatory skill-shaped object/version field. Draft is nullable inert text.
- `_finding_fingerprint` schema2 includes stable target identity+operation+normalized finding/applicability, not target_ref/version/capture time. Revision hashes include guards/change/evidence/risks/validation; keep evidence-overlap versus uncertain-distinct policy. Never merge across project/distro or distinct registered owner identities. Old namespace fingerprints stay intact.
- Validate target_ref by native immutable target record, not free-text applicability. Project owner must occur in the manifest's trusted binding; distro recommendations require a nonempty transfer rationale. Semantic generality/causality remain reviewed by model/human evaluations, not claimed as structural guarantees.

- [ ] **Step 1: Add failing behavioral contract tests.** Accept valid cited project-script, project-AGENTS, distro-hook, model-routing-component and new-file proposals without skills. Reject unknown/foreign target_ref, wrong project, missing guards, secret-artifact origin, unsupported schema keys, bare manifest IDs as content citations, and reflected rows using metadata only. Same wording in two projects yields different proposals; same target/evidence with changed guard yields a guarded revision; old report/proposal bytes remain unchanged.
- [ ] **Step 2: Owner observes red.** Run `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_inspection.py -k 'report_v2 or destination or target_guard or reflection or coverage or historical'`.
- [ ] **Step 3: Implement strict new report and revision records.** Resolve target identity/guard from admitted current-run targets, freeze copies/references in report/proposal revisions, and store manifest_id/inspection digest instead of packet/catalog assumptions. Report/proposal/event displays identify scope/target/guard/limitations without printing private captured bodies by default. Triage remains accept/reject/defer/supersede; acceptance authorizes no action.
- [ ] **Step 4: Freeze successful coverage transaction.** At report commit freeze inspection journal, target refs and proposed screening/reflection/material sets under the existing lock; refuse later mutation. Commit only supplied assessments supported by exposed sources; selected-only entries consume nothing. Metadata screening cannot retire pending reflection. Full native material IDs require complete unchanged-version range coverage across successful runs; Langfuse/manual sources cannot create them.
- [ ] **Step 5: Prove crash/partial/no-progress behavior.** Inject crashes before/after report commit and between coverage/event writes, then recover exactly once with no redispatch. Failed/unknown/timeout/invalid/no-work outcomes consume no successful coverage. A successful limited result records a visible held basis rather than false reflection; fresh evidence/limits or explicit detail reopen it. Partial progress resumes remaining ranges and preserves fairness for newer episodes.
- [ ] **Step 6: Owner verifies targeted tests and commits.** Preserve old schemas/IDs, legacy prepared submission recovery and default reviewer-self exclusion. Do not backfill guessed project destinations, promote old failures or apply the new schema to an already-frozen old run.

### Task 4: Sole-runtime integration, bounded reflection and compatibility cutover

**Files:** Modify `bin/agd_lib/reviewer_runner.py`, `reviewer.py` intent/reservation helpers, `bin/agd` run options, `tests/test_reviewer_lifecycle.py`, `tests/test_reviewer_managed.py`, `tests/test_reviewer_timer.py`, and workspace test setup helpers if they construct old packets. Task 5 owns instruction files.

**Interfaces:**
- New `_native_request`/receipt schema2 names manifest_id and report_schema2, allowed episodes/project bindings, target/inspection bounds, artifact/report paths and sanitized Langfuse availability.
- Extend existing intent/coordinator flow with `detail_episode_ids=()`; manual repeated --detail-episode is explicit remaining-content access, not a timer or retrospective bypass. Default selection resumes pending bounded reflection without repeatedly screening unchanged metadata.
- Keep `run_native_reviewer`, execute/bind/session/endpoint APIs and existing model/deadline behavior. No SDK fallback, persistent reviewer, exporter changes, fake task or central-owner requirement.

- [ ] **Step 1: Add failing runtime cases.** Actual standalone native child initially sees only metadata/locators, then retrieves task constraint/action/final content and an implicated non-skill target through CLI, writes a schema2 report and exits. Verify unrelated sentinel content is never delivered, zero forced skill calls, report validity/absence proof before finalization, no backend/model allocation for no-work, and a screened-but-unreflected episode remains actionable.
- [ ] **Step 2: Owner observes red.** Run `python3 -m pytest -q tests/test_reviewer_lifecycle.py tests/test_reviewer_managed.py tests/test_reviewer_timer.py -k 'manifest or reflection or report or recovery or no_work'`.
- [ ] **Step 3: Cut over all new-run producers and consumers.** Runtime writes request.json and selection-manifest.json plus skill/provenance, never packet.json/catalog.json. Remove dead eager builders when their consumers are migrated; keep only immutable historical reads/recovery. Report/proposal revisions/events use manifest/target references; no implicit catalog-only target universe remains.
- [ ] **Step 4: Preserve lifecycle evidence.** Keep native OMP tool availability, private skill/plugin isolation, auth/settings, binding, managed Herdr/tmux identities, deadline, safe diagnostics, exit0/endpoint/process absence and report-file size limits. A completion sentence or Herdr idle is not success. Recover valid pre-admitted exit/report proof once; unknown/failed remains conservatively retained and retryable.
- [ ] **Step 5: Owner verifies lifecycle tests and commits.** Optional CLI/telemetry absence and scheduled minimal environment still work. No new install prompt, schedule change, model/auth modification or live reload is required by this cutover. Do not deploy intermediate commits before all schema/reference consumers agree.

### Task 5: Reviewer skill, exact report examples and behavioral evaluations

**Files:** Modify `bundled/reviewer-skills/agd-reviewer/SKILL.md`, `bundled/reviewer-skills/agd-reviewer/references/commands.md`, `lib/agd-reviewer-instructions.md`, `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`. Add `tests/reviewer-behavior-scenarios.md` as the user-requested behavioral acceptance fixture, not runtime prompts or source-wording tests.

- [ ] **Step 1: Preserve baseline and define pressure scenarios.** Keep the observed overbroad/truncated packet read as the existing failing-before evidence; do not run another paid model just to reproduce it. For new semantic requirements evaluate the old skill first in an authorized disposable replay before asserting improvement. Store short synthetic episode fixtures/expected observable choices in the scenario document, with explicit provider approval needed for semantic runs.
- [ ] **Step 2: Apply the complete proposed skill in the spec.** Objective is reflection and any behavior-affecting surface. Require intent/action/outcome grounding, then hypothesis-driven inspection and smallest effective change; zero skill lookup absent suspicion. Tools/configuration/runtime are legitimate targets. Compare useful successful patterns; do not optimize only error/token counts. Preserve name/upstream naming and existing probe paragraph verbatim.
- [ ] **Step 3: Replace duplicated instruction contracts.** Keep lib bootstrap short and trusted, canonical procedure in native skill and exact CLI/schema2 examples in references/commands.md. Include complete examples for project AGENTS.md, project validation script, distro hook/CLI and described model-routing component, plus empty findings/limited episode rows. Remove old new-run existing-skill/new-skill requirements rather than retaining contradictory examples. Use actual fixture IDs/guards for runnable checks, not text snapshots.
- [ ] **Step 4: Exercise these behavioral scenarios after operational approval:**
  1. Healthy expected red test followed by recovery/validation: no invented finding, no skill lookup.
  2. All tools succeed but one task constraint/final claim is wrong: detect via bounded grounding, not bulk transcript.
  3. Specific skill hypothesis: inspect only relevant version, then reject an unsupported attribution when counterevidence disproves it.
  4. Tool-schema/CLI defect: recommend the tool/schema fix, with zero skill reads and no compensating instruction by default.
  5. Project-specific validation/configuration improvement: exact project/target/guard, no distro leakage.
  6. Transferable context/supervision/hook improvement: generic distro prescription with private project evidence retained, no local command promoted globally.
  7. Mixed/uncertain scope and external ownership: split only when justified; otherwise local/deferred, no invented writable source.
  8. Missing telemetry/intent/outcome or injected trace instructions: native fallback/limited result and no speculative cause/action.
  Record actual reads, hypotheses, target selection, recommendation quality, alternatives/validation, and coverage. A scripted provider proves mechanics only; it cannot prove learned behavior.
- [ ] **Step 5: Document new scope/schema/coverage.** Explain screened versus reflected versus full material coverage, limited holds/detail requests, target guards/current-vs-historical versions, project/distro distinction, evidence privacy, old-history reads and unchanged human approval. State cumulative byte/runtime bounds honestly, not as token/cost caps.
- [ ] **Step 6: After native smoke proof, update changelog and commit.** No claimed behavioral success or token-saving percentage without actual observed evaluation. Leave no old skill-only report contract governing new runs.

### Task 6: Integrated native smoke, recovery/uninstall and release-readiness

**Files:** Modify `bin/agd_lib/uninstall.py` module allowlist, `tests/test_uninstall.py`, `tests/test_uninstall_state.py` as required. Reuse `tests/native_reviewer_fixture.ts`, existing managed fixture and the scenario file; do not add a product SDK or paid-provider dependency to ordinary tests.

- [ ] **Step 1: Prove uninstall/recovery bundle support.** Add reviewer_inspection to the explicit source bundle. Disposable-home removal handles selections/targets/progress/captures under the home, stops only proved owned reviewer resources and preserves shared external tools, other projects, dirty/exposed probes and unknown processes under existing refusal rules. Old packet-only homes still work; no unrelated cleanup redesign.
- [ ] **Step 2: Run integrated regressions once after edits.** `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_episodes.py tests/test_reviewer_inspection.py tests/test_reviewer_lifecycle.py tests/test_reviewer_managed.py tests/test_reviewer_timer.py tests/test_reviewer_workspaces.py tests/test_langfuse_cli.py tests/test_observability.py tests/test_uninstall.py tests/test_uninstall_state.py`. Use the repo's documented native extension suite if its code changed. Known unavailable tmux is a reported limit, not a rerun target or suppressed failure.
- [ ] **Step 3: Exercise actual CLI/OMP without a paid model.** In a disposable home with real Git/native episode fixtures, use the existing standalone scripted-provider integration to retrieve bounded reflection evidence and a project script/distro hook target, submit general recommendations, inspect report/proposal scope/guards, and observe true exit/absence/finalization. Verify separate project proposal identities and guarded revisions; unchanged scope produces no duplicate model work. Repeat offline/no-CLI and failed/partial/held cases. Remove throwaway scaffolds after proof.
- [ ] **Step 4: With separate approval, run bounded real reviewer evaluation.** Use fresh completed worker/central episodes while producers remain alive, actual local Langfuse/native trace reads, and Task 5 scenarios. Verify intent/actions/outcome grounding, hypothesis/surface choice, useful recommendations and no project-global leakage. Inspect model peak/cumulative input/cache/output by generation without root double counting. Compare honestly, not as a controlled benchmark when task/model differs. Do not update jarvis-16 or change a live timer under planning approval.
- [ ] **Step 5: Self-review and whole-change review.** Verify exact target/report/CLI signatures, all current callers, historical reads/prepared recovery, event/triage output, fairness/holds and uninstall source bundle. Delete dead new-run packet/catalog/skill-only code and incidental-wording tests rather than shims. Commit verified implementation and mark its native task ready only at final head. Publication/merge/live rollout remain separate approvals.

## Completion criteria and requirement mapping

| Requirement | Owning tasks / evidence |
|---|---|
| Any behavior-affecting target, not an instruction-only product | 2/3/5; tool/script/hook/config/component proposals without skill lookup |
| Project-specific versus genuinely reusable distro guidance | 1/2/3/5; trusted owner/target guards, separate fingerprints, mixed/private-citation scenarios |
| Reflection connects intent/actions/outcome, even with clean metadata | 1/3/4/5; bounded source chain, explicit coverage and hidden-defect scenario |
| No gratuitous skill or other surface inspection | 2/5; hypothesis-first, zero-skill-read and disproven-hypothesis cases |
| Bounded metadata-first Langfuse with native fallback | 1/2/4/6; no eager evidence/catalog, source/range/cursor budgets and actual offline runtime |
| Honest coverage, failure/recovery, historical compatibility | 1/3/4/6; successful-only commits, partial/held work, old bytes unchanged |
| Inert human-triaged proposals and no safety regression | 3/5/6; no apply path, unchanged probes, owned uninstall/recovery |

This planning update changes only the spec/plan and embedded skill draft. Implementation and behavioral verification remain future work requiring the stated execution/operational approvals; no scaffold is represented as a completed feature.
