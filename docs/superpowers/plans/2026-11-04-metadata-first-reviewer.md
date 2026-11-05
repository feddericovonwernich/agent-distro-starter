# Metadata-first Reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the execution reviewer a small immutable native episode manifest and bounded Langfuse-first investigation instead of eager transcript/catalog content, with honest screening versus detailed coverage.

**Architecture:** Reuse native completion proofs, existing evidence/report/proposal transactions and the normal managed OMP runtime. Replace new-run packet production with schema-2 manifests and add one purpose-built inspection module that captures exactly scoped metadata/content. Keep normal tools and existing official CLI; do not introduce another agent framework, generic query broker, SDK, MCP service or automatic remediation.

**Tech Stack:** Existing Python 3.12+, standalone OMP, current Herdr/tmux/Treehouse integrations and verified optional `@langfuse/cli@1.2.4`. No new dependency or exporter prerequisite.

**Spec:** [2026-11-04-metadata-first-reviewer-design.md](../specs/2026-11-04-metadata-first-reviewer-design.md), including the complete proposed reviewer skill.

## Approval boundary

This document is a planning artifact, not implementation authorization. Obtain approval of the spec's decisions and full skill draft before changing runtime/skills. No live distro update, service/reviewer start, provider call, push/PR, merge or lease disposal is authorized by writing this plan. Explicit operational approval is required for the final real-provider quality/usage comparison.

## Global Constraints

- Native final-response/confirmed-exit proof owns eligibility; living central/worker processes stay alive. No idle/ready/Langfuse heuristic and no fabricated central task.
- Preserve cutoff/branch/source guards, deterministic bounds, reviewer-self exclusion, explicit retrospectives, immutable history, scoped message/content dedup and successful-finalization-only transitions.
- Metadata-screened does not mean native content reviewed. Clean metadata is not queued repeatedly; explicit detail passes retain access to unread native material. Manifest presence alone advances nothing.
- Keep report schema 1, exact existing-skill/new-skill fields, inert proposal/triage semantics, separate optional telemetry/CLI/timer settings, relative `--home .`, and managed one-shot runtime/absence proof.
- New request/receipt/manifest writes use schema 2 with `manifest_id`; historical packets remain read-only. No new eager packet/catalog runtime path or old producer fallback.
- Inspection responses/manifests are at most 16 KiB serialized UTF-8; metadata provider pages contain at most 20 rows; raw CLI stdout is capped at 262144 bytes. Existing configured cumulative admitted-byte, report-byte and deadline limits remain. These are not total token/cost/shell limits.
- Normal shell/account access remains trusted-model policy, not filesystem/secret/descendant containment. Preserve current probe execution restrictions and operator-controlled cleanup exactly.
- Before exported-symbol changes use LSP references when available; otherwise record unavailable capability and use scoped caller search. Update every real consumer, not just runner prompt text.
- Delegated editors skip build/lint/test/formatter runs. The integration owner runs listed failing regressions before implementation and one integrated verification after edits; do not repeat complete suites per worker. Red-green boundaries remain required, with the owner handling commands.

## Review Focus

- A selected-but-uninspected episode or metadata-only capture cannot consume native material: Tasks 1/4.
- Langfuse foreign/missing/ambiguous/late records, CLI output truncation and native rewrites cannot become exact closed-episode evidence: Task 2.
- Large successful tool outputs can waste context without error; expected red tests/recovery must not become automatic negative findings: Task 5.
- Old prepared submissions recover once, failure/no-work advance nothing, and modified journals cannot change committed coverage: Tasks 3/4.
- No bulk evidence is exposed before a scoped request, and offline fallback/native runtime really work without paid-provider or live-distro mutation: Task 6.

## File ownership and sequencing

Task 1 establishes shared schema/selection contracts. Task 2 owns the new inspection module and its parser branches; Task 3 owns runner/receipt/runtime cutover; Task 4 owns final report/evidence admission and coverage commit in `reviewer.py`. Integrate those in dependency order, not competing edits to the same core file. Task 5's skill/reference/bootstrap/docs drafting can run independently after Task 1 fixes interfaces, with Task 5 as sole instruction-file owner. Task 6 is the single integrated verification owner. Keep the feature within one complete cutover; intermediate internal commits are not independently deployable public runtime modes.

### Task 1: Native metadata manifests and separate screening identity

**Files:** Modify `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_episodes.py`, `tests/test_reviewer.py`, `tests/test_reviewer_episodes.py`; add no new parser/index service.

**Interfaces and algorithm:**

- Add `select_manifest(home, *, cutoff, config_snapshot, retrospective_review_run_ids=(), detail_episode_ids=(), deadline_epoch_ms=None) -> dict` and `read_manifest(home, manifest_id) -> dict`.
- Add `manifests`, `catalogs`, `inspections` and `screened` paths under existing reviewer data/state roots. All mutable state stays in state; immutable manifests/catalog metadata/evidence stay in data.
- Extend `read_native(..., metadata_only=False)` through its existing transient `_MetadataIndex`/branch proof, not a second JSONL parser. Metadata mode returns proof/summary and a canonical semantic projection digest of scoped message IDs/text hashes without retaining message bodies. Role/tool status/timing/usage metadata is extracted only when the native record supports it; unknown remains unknown.
- Manifest episode IDs reuse native source/interaction/boundary identity and legacy exit identity. Metadata basis additionally incorporates semantic projection and relevant linked-decision versions; never include current review cutoff, title, line number, PID or fetch time as a changing basis by itself. A legacy exit's fixed observed time is identity, not every later review clock.
- Freeze catalog ID/description/full-SKILL SHA metadata once, remove snapshot text/prefixes for new runs, and reference it by digest. No full catalog in the model-facing manifest. Keep worker assignment provenance distinct from observed loading.
- Prefix-fit candidates into the 16 KiB pretty-printed manifest and configured candidate/input limits; record precise omissions. Keep no-work distinct from budget/proof failure. Preserve legacy `_reviewed_material` only as detailed material authority.
- Default selection filters previously finalized `episode_id + metadata_basis` screening, not unread content; detail selection explicitly filters existing reviewed material IDs. Historical fully finalized episodes may initialize screening only from provable complete retained coverage; partial old packets stay partial. Do not rewrite old files.

- [ ] **Step 1: Write failing behavioral cases.** Cover living-worker/central final episodes; later live-tail exclusion; semantic rewrite versus title/append stability; no body or unrelated-skill sentinel exposure in manifest; candidate byte-cap omissions remaining selectable; metadata selection creating zero reviewed material IDs; same completed episode selected for explicit unread detail after screening; distinct follow-ups in one session.
- [ ] **Step 2: Observe red.** Owner runs `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_episodes.py -k 'manifest or metadata_screen or detail_pass or semantic_projection'`. Existing real truncation is established evidence; do not rerun a paid reviewer to reproduce it.
- [ ] **Step 3: Implement selection and history readers.** Reuse guarded atomic storage, native parser, cutoff validation, config snapshot/digest and retrospective scope. Metadata-only records cannot enter `_reviewed_material`. Retain `read_packet` solely for historical interpretation/recovery; remove new-run `select_packet` consumers in Task 3's complete cutover.
- [ ] **Step 4: Define immutable schema contracts for Tasks 2–5.** Specify episode/source/window/projection fields, optional Langfuse hints, catalog metadata reference, coverage limitations and selection mode. IDs are content digests of canonical data excluding their own ID fields. A metadata basis change creates a new manifest; never mutate one.
- [ ] **Step 5: Commit this internal contract change.** Document the no-live-rollout boundary in the commit/handoff. Final passing integration proof belongs to Task 6; do not claim a public feature before the runner/admission cutover.

### Task 2: Scoped metadata, targeted I/O, native fallback and lazy skill capture

**Files:** Create `bin/agd_lib/reviewer_inspection.py`, `tests/test_reviewer_inspection.py`; modify `bin/agd` and only the shared evidence-capture helper in `reviewer.py` needed for trusted origin metadata. Reuse `langfuse_cli.py`, `observability.py`, `reviewer_episodes.py` and existing capture/integrity primitives; do not duplicate their authorization.

**Interfaces:**

- `reviewer_inspection.inspect(home, run_id, episode_id, *, source='auto', record_id=None, part=None, cursor=None) -> dict`.
- `reviewer_inspection.list_skills(home, run_id, *, cursor=None) -> dict` and `show_skill(home, run_id, skill_id, *, cursor=None) -> dict`.
- Commands exactly as in the spec: `reviewer inspect RUN --episode ID [--source auto|langfuse|native] [--record ID --part input|output|content] [--cursor TOKEN]`, `reviewer skills list RUN [--cursor TOKEN]`, `reviewer skills show RUN ID [--cursor TOKEN]`.
- All operations persist captures/journal state and therefore enter existing uninstall/mutation admission, even metadata list operations. Validate active run/manifest/state and selectors before any source query or disclosure; do not require central ownership or forge HERDR_ENV.
- Internal trusted capture origin has typed native/Langfuse/skill fields. Public arbitrary `evidence add` cannot set those fields, material IDs, verified correlation or screening receipts.

- [ ] **Step 1: Write failing source-boundary cases.** A fixed local CLI fixture serves different observations: selected actor, foreign project, same session/different interaction window, overlapping/unknown identity, unavailable/late data and oversized I/O. Assert which bytes can be captured/cited and which coverage remains absent—not argv forwarding or mock echoes. Native cases use real JSONL/proof fixtures, including mutated same-size message, appended unfinished tail, symlink/special file and UTF-8 boundary splits.
- [ ] **Step 2: Observe red.** Owner runs `python3 -m pytest -q tests/test_reviewer_inspection.py`. Add assertions for page-chain completeness, foreign/stale cursor refusal, duplicate capture byte accounting, exact budget boundary, raw stdout overflow, sanitized subprocess failure/timeout and no implicit network/install/Docker activity in native-only mode.
- [ ] **Step 3: Implement fixed Langfuse reads.** Build only verified `langfuse --api-version auto api observations list` argv. Overview uses bounded non-I/O fields and trusted actor/window filters. Details require a previously scoped observation; request `io` only for that record. Reuse live local guard and verified private launcher. Bound subprocess stdout/stderr/deadline without shell strings or hidden retry, and stop/prove the child on limit/timeout. Missing/ambiguous mapping is a native fallback limitation; time/session alone must not be labeled exact interaction proof.
- [ ] **Step 4: Implement native overview/detail.** Recheck semantic projection, source/header/branch/final identity and selected cutoff. Derive metadata pages from existing reader/index and capture only requested text ranges. Record full material identity, exposed range and digest separately. Resume previously successful partial ranges rather than replaying the first page; partial coverage does not imply whole-message review. Do not include later live rows.
- [ ] **Step 5: Implement skill lookup.** Make lookup available, not mandatory: the reviewer must first observe behavior and form a plausible skill-related hypothesis before browsing relevant catalog metadata or reading a skill. With no such hypothesis it performs no skill lookup. Capture only the relevant requested matching SKILL bytes on demand. Resolve validated catalog paths or exact matching immutable task snapshots; changed/missing bodies are unavailable, never silently replaced. Preserve the distinction between current target contract and instructions actually loaded during execution.
- [ ] **Step 6: Persist inspection receipts.** Use immutable capture IDs and a guarded per-run journal binding manifest/query/CLI version/source identity/capture time/hash/range/page chain. Return at most 16 KiB serialized UTF-8 with explicit complete/next-cursor fields; do not save unused raw prompts/responses for convenience. Record retrieved/exposed bytes, not an unverifiable claim of model attention. Count unique admitted bytes once.
- [ ] **Step 7: Commit focused retrieval behavior.** No generic command execution API, provider client, write proxy, new background process or tool allowlist.

### Task 3: Sole-runtime manifest cutover, detail mode and immutable request recovery

**Files:** Modify `bin/agd_lib/reviewer_runner.py`, `reviewer.py` intent/reservation/request helpers, `bin/agd` run/dispatch flags, `tests/test_reviewer_lifecycle.py`, `tests/test_reviewer_managed.py`, `tests/test_reviewer_timer.py`. Task 5 owns bootstrap/skill files.

**Interfaces:**

- New request/receipt schema 2 contains `manifest_id`; no new receipt `packet_id`. Preserve configuration schema 2 and report schema 1.
- Extend `enqueue_intent`/coordinator selection with immutable `detail_episode_ids=()`; `reviewer run --detail-episode ID` repeats for manual runs only. Do not mix timer mode or reviewer-self/retrospective bypasses; validate known completed episodes before dispatch.
- Runtime `request.json` names `selection-manifest.json`, allowed episode IDs, catalog reference, bounds, evidence/artifact root and report destination. `allowed_scope` is not a list of pre-citable text source IDs anymore.
- Historical artifact selection is a read-only schema branch. Never redispatch an old packet engine, fabricate source/cost fields or mutate old receipts.

- [ ] **Step 1: Write failing lifecycle contracts.** A real native child fixture must first receive metadata only, then retrieve an explicitly chosen sentinel-containing message through inspection and submit a valid cited report. Check no unrelated body is exposed; failed/nonzero/missing-report/timeouts commit nothing; no eligible metadata starts no model/backend; explicit pending-detail work does start; timer remains independent of central/HERDR_ENV/Langfuse availability.
- [ ] **Step 2: Observe red.** Owner runs `python3 -m pytest -q tests/test_reviewer_lifecycle.py tests/test_reviewer_managed.py tests/test_reviewer_timer.py -k 'manifest or inspection or detail or no_work or recovery'`.
- [ ] **Step 3: Cut over every producer/caller.** Reserve/freeze a manifest and its request, stop writing packet/catalog runtime content, and pass only a short bootstrap plus native skill discovery. Remove dead new-run excerpt/catalog builders once references migrate; retain only historical read/recovery support. No alias preserving an eager dispatch path.
- [ ] **Step 4: Preserve real runtime admission.** Keep native session binding, supervisor identity/lease, managed endpoint, OMP tools/model selection, deadlines, exit0/absence proof, report-file limit and sanitized failure diagnostics. No paid model or live restart in unit/integration fixtures.
- [ ] **Step 5: Recover exact frozen runs.** A schema-2 pre-admitted nonterminal run with proved exit0/absence/binding/report resumes immutable submission once; missing proof stays failed/unknown and retryable. A historical pre-upgrade run uses its existing frozen artifact/report validator only for recovery, never another dispatch. Terminal failed/unknown stays terminal. Retain old packets/reports/receipts unchanged.
- [ ] **Step 6: Commit the sole new runtime path.** Complete core and CLI callsite migration in this task, with Task 4 admission/coverage integrated before exposing a deployable cutover.

### Task 4: Citation admission and honest successful-only coverage

**Files:** Modify `bin/agd_lib/reviewer.py`, `tests/test_reviewer.py`, `tests/test_reviewer_inspection.py`, lifecycle/managed tests where finalization is exercised. Reuse existing prepared-submit transaction and candidate material ledgers.

**Algorithm:**

- New-run report source inventory comes from immutable current-run captures, not manifest locators. Historical packets retain old citation semantics. Metadata sources may support metadata claims, not unseen content.
- An existing-skill target must match the trusted frozen catalog SHA and admitted requested skill capture; arbitrary artifacts claiming a catalog hash do not qualify. Keep wrong-kind fields, missing version, null/empty new_skill and invalid draft/triggers rejected exactly as today.
- On report commit, freeze inspection-journal digest plus proposed screening/material sets under mutation lock. Close inspection admission at that state. Whole metadata page chains qualify screening; whole native messages qualify existing material IDs. Partial ranges stay partial; Langfuse/shell artifacts cannot create native material coverage.
- Finalization idempotently writes the separate screened-basis ledger and existing detailed material ledgers only for those sets. Recover interrupted coverage commit from the same immutable transaction; never iterate all selected manifest entries as reviewed.
- Persist successful partial metadata record/version IDs and native material byte ranges alongside their full source identity/hash. Union unchanged-version ranges; promote a material ID only when all bytes are covered. New runs re-enumerate bounded Langfuse metadata and skip previously successful record/version pairs rather than trusting an old provider cursor. Keep rotation/no-progress barriers separate from coverage: rotate unfinished attempts, and surface unchanged budget/proof barriers instead of endlessly dispatching paid no-progress runs.

- [ ] **Step 1: Write failing coverage transitions.** Three selected episodes, one overview retrieved: only that complete metadata chain screens. One metadata-only success: zero native material IDs consumed, next default scan skips that basis, explicit detail still retrieves unread bytes. A partial native body remains partial; a later successful pass resumes the remaining range and only then completes its material ID. Test successful partial metadata progress, failed fragments not reusable, changed versions not mergeable, fair selection past a blocked episode and no endless no-progress dispatch. New interactions/semantic rewrites become eligible without replaying unchanged scope.
- [ ] **Step 2: Observe red.** Owner runs `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_inspection.py -k 'coverage or screening or citation or version or recovery'`.
- [ ] **Step 3: Implement strict admission and frozen coverage proposal.** Keep exact report envelope/variants; freeze captures and prohibit post-commit journal mutation. A metadata source ID is a citation only to returned metadata, never a wildcard to an entire session. Arbitrary `evidence add` remains useful supplemental evidence without fabricated scope.
- [ ] **Step 4: Implement/recover commits once.** Exercise crash before/after report commit and between ledger/event writes; repeat recovery yields the same report/proposals/coverage and no dispatch. Failed/unknown/timeout/invalid/no-work outcomes leave all new screening/material coverage untouched. A successful empty report still commits only actually retrieved eligible scope.
- [ ] **Step 5: Preserve historical and retrospective behavior.** Keep old source IDs/hashes/report bytes readable, conservative legacy coverage reconstruction and reviewer-self/default exclusions. Explicit detail requests cannot reopen a final failed reviewer outcome or claim historical full-content coverage from partial excerpts.
- [ ] **Step 6: Commit finalization and report admission with the runtime cutover.** No report-schema expansion, automatic proposal acceptance or standalone second coverage engine.

### Task 5: Canonical reviewer skill, command contract and operator docs

**Files:** Modify `bundled/reviewer-skills/agd-reviewer/SKILL.md`, `bundled/reviewer-skills/agd-reviewer/references/commands.md`, `lib/agd-reviewer-instructions.md`, `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`. Apply only the user-approved draft from the spec; do not rename skills or broaden probes.

- [ ] **Step 1: Retain the established behavioral baseline.** Record the actual overbroad packet/catalog read and non-error truncation as the failing-before scenario. Include pressure cases where no behavior implicates a skill (zero catalog/skill lookups), observed behavior plausibly implicates one skill (targeted inspection only), and inspection disproves that hypothesis (no unsupported attribution). Also cover expected failing tests/recovery, missing/ambiguous Langfuse data, token aggregation, unsupported completion, changed skill version and untrusted trace instructions. Do not add permanent tests comparing skill wording/source text or incidental examples.
- [ ] **Step 2: Apply approved skill procedure.** Start with request/manifest and bounded metadata; investigate observed behavior using targeted I/O and counterexamples. Skill inspection follows a stated, behavior-backed hypothesis, never routine catalog review or a requirement to inspect some skill. Inspect evaluated skills as evidence rather than activating them. Preserve the current probe paragraph verbatim.
- [ ] **Step 3: Establish one command/report reference.** Update commands.md with exact inspect/skills/detail/evidence/workspace commands, returned scope/limit meanings, valid existing/new-skill JSON examples and no wrong-kind fields. Make lib/agd-reviewer-instructions.md a concise trusted bootstrap pointing to the native `agd-reviewer` procedure/reference, not a second long conflicting policy/schema document. Keep policy/guard reminders and no secrets/path lists in prompt.
- [ ] **Step 4: Document honest user-visible behavior.** Explain metadata-screened versus content-pending/detail passes, precise `no-work` meaning, partial provider/native pages, local CLI correlation limitations, optional telemetry/CLI independence, live-agent eligibility, immutable historical reports and unchanged report locations. Explain byte/runtime bounds versus actual model tokens/cost.
- [ ] **Step 5: Validate behavior, not declarations.** Deterministic source/coverage/report tests establish mechanical guarantees. A real model pressure run after operational approval checks it actually chooses metadata then targeted details and treats counterexamples correctly; fixed scripted-provider tools cannot prove skill compliance. Do not claim semantic success from merely installing the draft.
- [ ] **Step 6: Update changelog after smoke proof and commit.** State changed behavior and backward-read compatibility, not unmeasured savings. The planning spec remains a proposal until the user approves it.

### Task 6: Integrated smoke, history/uninstall proof and measured acceptance

**Files:** Modify `bin/agd_lib/uninstall.py` module allowlist, `tests/test_uninstall.py`, `tests/test_uninstall_state.py` only as required for new source/artifact behavior. Reuse the existing `tests/native_reviewer_fixture.ts` standalone OMP scripted-provider harness and managed integration fixtures; retire eager-packet-only fixtures/wording tests rather than preserving dead wiring.

- [ ] **Step 1: Prove recovery/uninstall completeness.** Include `reviewer_inspection` in the recovery source bundle. A disposable-home uninstall inventories/stops only owned reviewer resources and removes home-owned manifests/catalog metadata/journals/captures; foreign/shared software, dirty/exposed probes and process/lease uncertainty keep existing refusal/retention behavior. Historical packet-only homes still work. No unrelated uninstall redesign.
- [ ] **Step 2: Run the affected integrated suite once.** `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_episodes.py tests/test_reviewer_inspection.py tests/test_reviewer_lifecycle.py tests/test_reviewer_managed.py tests/test_reviewer_timer.py tests/test_reviewer_workspaces.py tests/test_langfuse_cli.py tests/test_observability.py tests/test_uninstall.py tests/test_uninstall_state.py`. Run the existing native extension suite using the repository's documented development command when extension/shared episode code changed. Do not add a runtime SDK requirement or rerun known unavailable tmux merely to confirm absence.
- [ ] **Step 3: Exercise the actual CLI/native child.** In a disposable home, use a proved completed episode and the installed standalone OMP scripted-provider fixture to run the real managed coordinator/inspection/report path. Observe bounded overview then one targeted body, source hashes, a valid persisted report, exit/endpoint/process absence, screening/material ledger distinction, retry after failure and unchanged default no-work. Run with telemetry/CLI absent too. This is runtime smoke, not proof of real-model quality or Langfuse delivery.
- [ ] **Step 4: After separate approval, run one bounded real quality/usage scenario.** Generate a small fresh completed worker/central interaction while their agents remain alive, then one reviewer on the owned local stack. Use actual Langfuse observations and native transcript to identify every read/query, how the hypothesis changed, targeted I/O, expected-error counterexamples, final report and consumed scope. Capture peak context and cumulative generation input/cache/output without summing root aggregates. Compare to the observed eager-packet failure mode without pretending different tasks/models are a controlled benchmark or promising a token percentage.
- [ ] **Step 5: Review the whole cutover.** Check all callers/docs/history/recovery paths, no leftover new-run packet/catalog producer, no source-wording tests, no secret/policy overclaims and no unused scaffolding. Obtain whole-change review, commit tested feature and mark its native development task ready. Publication/merge/live jarvis-16 rollout remain separate approvals.

## Completion checklist

- [ ] User approved both design decisions and the actual skill draft before runtime implementation.
- [ ] New runs expose metadata by default, not eager body/catalog content; retrieval and citations are bounded and versioned.
- [ ] Completed living-worker/central episodes remain eligible; scope/cutoff/retrospective/self guards hold.
- [ ] Metadata-only success cannot consume unread native text; detail remains available without clean-metadata replay/starvation.
- [ ] Failure/no-work/recovery and historical immutable reads preserve their real semantics.
- [ ] Reviewer still proposes skill/process improvements only; strict report variants and inert human triage hold.
- [ ] Native/offline/optional Langfuse paths and actual managed runtime are exercised; paid quality/usage evidence is reported only if operationally approved and observed.
- [ ] New module is in uninstall recovery; no shared resources or uncertain probe work is silently destroyed.

## Requirement mapping

The user's metadata-led investigation direction maps to Tasks 1–3. Episode selection without eager evidence, honest dedup and success-only coverage map to Tasks 1/4. Revised reviewer skill and explicit user review map to the approval gate/Task 5 and full skill in the spec. Scope/fallback/security/history/real behavioral proof map to Tasks 2/3/6. No requested scope is deferred to an unimplemented scaffold.
