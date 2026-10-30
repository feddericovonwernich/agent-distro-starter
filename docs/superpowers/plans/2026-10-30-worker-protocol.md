# Worker Protocol Skill and Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace verbose OMP worker launch contracts with a mandatory native protocol skill, fresh lifecycle context, and bounded unfinished-assignment correction.

**Architecture:** Keep one effective frozen skill manifest containing optional and AGD-owned system skills. Extend the existing Python worker CLI with a scoped context query and the existing OMP worker extension with policy/context/stop hooks. Continue using guarded AGD commands and durable observations; neither prompts nor hooks grant authority.

**Tech Stack:** Existing Python 3.12+, TypeScript/Bun, OMP 18.14.0+ native extensions, Treehouse 3.1.6, pytest, existing tmux/Herdr adapters. No new production dependencies.

**Spec:** `docs/superpowers/specs/2026-10-30-worker-protocol-design.md`

## Global Constraints

- Target OMP 18.14.0+ native extension APIs; verify the installed version's event ordering before claiming support.
- No new production dependency, plugin installation, generic policy framework, or model-tool API.
- Preserve upstream skill names, bytes, resources, source provenance, and task-frozen optional selections.
- `--skill-mode none` means no optional task skills; every newly created managed OMP task still receives `agd-worker`.
- Ambient extension discovery stays disabled; native skills are exactly the frozen effective manifest.
- Mandatory protocol guidance is not approval, an OS sandbox, or proof the model obeyed it.
- Never infer readiness from assistant prose or a completed turn. Only the existing guarded AGD transition may mark a task ready.
- Never repeat the completed assignment when recovering a ready worker; follow-up reply capture remains extension-owned.
- No hot migration, aliases, overwrite of retained snapshots, or silent addition of permissions to existing tasks.

## Review Focus

1. A valid PR grant on a none-mode task with mandatory protocol artifacts must succeed; optional task skills, foreign grants, and interrupted/conflicting adapter appends must still refuse before endpoint creation. Covered in Task 1.
2. A worker under a live central must be able to query its context and persist its reply without the owner token, but foreign task/incarnation commands must refuse. Covered in Task 2.
3. A resolved or old-incarnation blocker must not permanently disable completion checks; an unreconciled current decision must allow an honest pause. Covered in Task 2.
4. Compaction, session switching, and readiness reached mid-turn must not lose the core, duplicate instructions, carry stale permissions, or replay the completed assignment. Covered in Tasks 3–4.
5. A corrective continuation, provider error, interruption, or active FIFO follow-up must not loop, acknowledge early, or consume another request. Covered in Tasks 3–4.

---

## File map and execution order

Work from merged baseline `b94415f` or newer in a leased development worktree. Do not edit `/home/feddericokz/Distros/*` or the stable clone. Use the repository's customization/task procedures at execution time.

| Files | Responsibility |
|---|---|
| `bundled/worker-skills/agd-worker/SKILL.md`, `references/commands.md` (new) | One mandatory protocol core and on-demand AGD command recipes. |
| `bundled/worker-skills/agd-scoped-pr-relay/SKILL.md` | Self-contained granted-PR procedure; no launch-prompt recipe dependency. |
| `bin/agd_lib/worker_skills.py`, `tasks.py`, `workers.py` | Effective selection, frozen protocol provisioning, grant append transaction, lifecycle verification and scoped context. |
| `bin/agd`, `bin/agd_lib/harnesses.py` | Context CLI authorization, worker completion classification, short OMP prompt and exact effective skill launch. |
| `lib/omp-worker.ts` | Core injection, current context, bounded stop check, existing FIFO/reply lifecycle. |
| Existing Python tests and `tests/worker.test.ts` | Consumer-visible state, authority, snapshot, and lifecycle regressions. |
| `tests/worker-native.test.ts` (new), `tests/smoke_native_plugins.py` | Real native model-input/resolution/compaction proof and disposable backend acceptance. |
| `README.md`, `docs/operations.md`, `docs/compatibility.md`, `.agents/skills/orchestrate/SKILL.md`, `.agents/skills/customize/SKILL.md`, `CHANGELOG.md` | Selection semantics, activation, workflow and exercised evidence. |

Order: 1 → 2 → 3 → 4. These share task manifests and lifecycle state; implement sequentially. No central plugin or reviewer capability redesign.

## Preflight: establish native contracts and baseline

- [ ] Read the spec, relevant repository instructions, current hook types, `worker_skills.prepare/ensure/verify/remove`, task creation/reconciliation, PR grant launch, and CLI ownership checks. For exported function changes, use language-server references when available before changing callers.
- [ ] Run the existing Python and Bun suites once, recording genuine baseline/environment failures separately. Do not rerun known missing-dependency failures solely to confirm them. Locate an official importable OMP package with `reviewer_runner.assert_runtime_capability`, not by assuming the standalone release executable contains SDK modules.
- [ ] In a throwaway native SDK probe, exercise `before_agent_start.systemPrompt`, `context` custom-message conversion, `session_stop` continuation and `agent_end.willContinue`, compaction, and explicit interruption using a scripted provider. Observe real callback ordering and outgoing provider input, not manually invoked mock handlers. No paid model call is needed. If the installed runtime contradicts the proposed hook design, update the design/plan before implementation rather than substituting unverified behavior.

### Task 1: Freeze mandatory protocol skills with ordinary selections

**Files:** Create the two `agd-worker` Markdown files; modify `worker_skills.py`, `tasks.py`, `workers.py`, and `harnesses.py`; extend `tests/test_worker_skills.py`, `tests/test_tasks.py`, `tests/test_workers.py`, and `tests/test_delivery.py`.

**Interfaces:**
- `worker_skills.with_protocol(selection: dict, harness: str) -> dict`: returns the persisted schema-2 selection. Preserve `skills` as optional IDs; add `system_skills=["agd-worker"]` for OMP; `sources` covers their union. Cursor selection is unchanged. Validate the exact bundled AGD source, not arbitrary paths labelled `origin.kind=agd`.
- `worker_skills.add_scoped_pr(home: Path, task: dict, grant_id: str) -> dict`: called under the existing launch mutation lock after grant validation; returns a complete effective manifest. Appends only the scoped adapter, binds the transaction to the grant, and preserves existing records/snapshots. Must not accept arbitrary additions.
- Existing `prepare/ensure/verify/remove` continue operating on one `worker-skills.json` journal and snapshot directory; manifest `skills` is the effective union.

- [ ] **Step 1: Add failing selection/lifecycle tests.** Use real temporary Git/Treehouse worktrees through `tests/support.py:lab`. Add these assertions to existing behavior scenarios rather than pinning prose or file copies:

```python
# test_none_task_freezes_mandatory_protocol_without_plugins
assert task['worker_skill_selection']['skills'] == []
assert task['worker_skill_selection']['system_skills'] == ['agd-worker']
assert {r['id'] for r in task['worker_skills']['skills']} == {'agd-worker'}
# Remove source after completion, then restore missing destination via ensure:
assert restored_core_bytes == frozen_core_bytes

# test_protocol_and_optional_selection_remain_distinct
assert task['worker_skill_selection']['skills'] == ['systematic-debugging']
assert {r['id'] for r in task['worker_skills']['skills']} == {
    'agd-worker', 'systematic-debugging'}

# test_granted_launch_appends_without_replacing_protocol
assert granted_ids == {'agd-worker', 'agd-scoped-pr-relay'}
assert base_record_after == base_record_before
assert base_snapshot_hash_after == base_snapshot_hash_before
```

Also test reserved names and ancestor collisions before publication of `prepared`; invalid grant/optional selection refusal before an endpoint; same-grant append retry after interruption; different-grant refusal; snapshot tampering; cleanup preserving unrelated files; and old-task read/cleanup compatibility with explicit OMP launch/relaunch refusal. A Cursor none-mode task must still be creatable without new OMP artifacts.
Add a runner smoke with hostile inherited `AGD_CLI`/`AGD_PR_GRANT`: the spawned OMP process must execute the checkout CLI from `AGD_CLI`; an ordinary worker must receive no grant, and a granted worker must receive only its bound grant ID. Cover spaces in the home path by running a recipe, not merely inspecting environment dictionaries.

- [ ] **Step 2: Run RED.** `python3 -m pytest tests/test_worker_skills.py tests/test_tasks.py tests/test_workers.py tests/test_delivery.py -q` with only previously documented unavailable integrations deselected. New cases must fail at the missing protocol contract, not test setup.
- [ ] **Step 3: Implement the effective selection and protocol bundle.** Call `with_protocol` before lease allocation and persist its descriptors in `worker_skill_selection`. Provision when either optional or system IDs exist; task preparation/reconciliation must retain the same allocation. Reuse current hashing, collision and Git-exclusion machinery. Reject custom/plugin reuse of AGD-owned names without requiring plugin registries in none mode.
- [ ] **Step 3b: Bind executable command recipes.** In `workers.run_worker`, override `AGD_CLI` with the absolute checkout `bin/agd` path for OMP children. Set `AGD_PR_GRANT` to the recorded grant only for granted workers, otherwise remove any inherited value. Preserve the existing complete managed environment and owner-token stripping.
- [ ] **Step 4: Implement the narrowly scoped grant append.** Replace the blanket `task.get('worker_skills')` PR refusal with checks on optional IDs and the exact permitted system manifest. Validate the grant before any adapter materialization. Journal intent with `grant_id`, append only the adapter record/source/exclusion, copy and verify it, then atomically publish complete state. A retry validates existing records and the same grant; absent/partial/changed frozen bytes refuse rather than being resnapshotted. Teach `ensure` to finish only a verifiably complete appended snapshot. Endpoint creation remains after completion. Persist updated selection and manifest on the task under the same launch guards. Preserve the mandatory base during granted retries and relaunches.
- [ ] **Step 5: Write the concise core and command reference.** Root core must say it is automatically loaded, optional skills load only when relevant, and full recipes are at `skill://agd-worker/references/commands.md`. Recipes invoke `"$AGD_CLI" --home "$AGD_HOME"` and quote `$AGD_TASK_ID`, `$AGD_INCARNATION`, paths, and `$AGD_PR_GRANT` where applicable. Include actual status, ready, blocked/decision/resolved commands and explain that FIFO replies are extension-owned. Core must direct granted workers to the scoped procedure instead of ordinary `task ready`. Keep upstream skills untouched.
- [ ] **Step 6: Run GREEN and lifecycle smoke.** Rerun affected suites. In a disposable home, use the actual CLI to create a none task, inspect/materialize its native protocol, restore a deleted generated copy from the snapshot, and clean its unchanged scout workspace using normal guarded cleanup. Record the restored hash and returned lease, then remove only this disposable home.
- [ ] **Step 7: Commit.** `git commit -m "feat: provision mandatory worker protocol skills"` after staging only Task 1 files.

### Task 2: Supply scoped authoritative context and executable recipes

**Files:** Modify `bin/agd`, `bin/agd_lib/workers.py`, `bundled/worker-skills/agd-scoped-pr-relay/SKILL.md`; extend `tests/test_workers.py`, `tests/test_supervision.py`, and existing PR tests where command recipes expose a contract.

**Interfaces:**
- `workers.context(home: Path, task_id: str, incarnation: str) -> dict` backs read-only `worker context TASK --incarnation TOKEN`.
- Return schema 1 with `task_id`, `incarnation`, `phase`, `kind`, `worktree`, `brief`, `report`, `agd_argv: list[str]`, `pr_grant: str | None`, `task_skills: list[str]`, `system_skills: list[str]`, `protocol: {path: str, sha256: str}`, and `outcome: unfinished|ready|blocked|decision|closed`.
- This response is the only dynamic task-state interface consumed by Task 3. It does not emit events or invoke backend status.

- [ ] **Step 1: Add failing boundary/outcome tests.** Create valid frozen tasks from Task 1. Assert exact identity/path bindings, separate system/optional IDs, and protocol hash. Exercise missing/tampered protocol, foreign task environment, stale incarnation, non-worker and non-OMP records; each refuses. Test current raw decisions before watcher reconciliation, resolved receipts, latest blocked→resolved by key, old-incarnation observations, and runtime guard diagnostics excluded from `outcome`.

```python
# test_context_uses_current_durable_outcome_not_watcher_timing
assert context_before_decision['outcome'] == 'unfinished'
assert context_after_raw_decision['outcome'] == 'decision'
assert context_after_decision_resolution['outcome'] == 'unfinished'
assert context_after_ready['outcome'] == 'ready'

# test_worker_complete_with_live_owner_is_scoped
assert completion_process.returncode == 0   # no AGD_OWNER in child env
assert persisted_reply['body'] == 'Verified answer.'
assert foreign_task_process.returncode == 3
assert stale_incarnation_process.returncode == 3
```

Use a real live owner process/start identity and CLI subprocess for the completion case; direct function calls alone would miss the dispatch bug.

- [ ] **Step 2: Run RED.** `python3 -m pytest tests/test_workers.py tests/test_supervision.py -q` with the recorded environment exclusions. Confirm failures include missing context dispatch and worker completion refusal under a live owner.
- [ ] **Step 3: Implement the scoped context query.** Register `context` as read-only in the CLI and enforce identity inside `workers.context`. Read a coherent task/observation view under the existing lock; fold outcomes exactly as the spec defines. Validate the core's recorded destination/snapshot identity and SHA-256. Return no central token, credential, unrelated task, or raw observation body. Use argument arrays; do not introduce an executable shell prefix supplied by model text.
- [ ] **Step 4: Fix completion dispatch and remove dead scope naming.** Replace obsolete `acknowledge` with `complete` in worker-scoped mutators. Preserve `workers.complete` current-incarnation/request/environment checks and all other central-only restrictions. No owner token propagation.
- [ ] **Step 5: Make the scoped skill self-contained.** Move the existing guarded publish/verify/relay recipes out of their prompt dependency into the scoped skill using runtime bindings. Preserve lease start/terminal acknowledgement order, exact payload schemas, non-force publication, independent feedback inspection, and forbidden actions. Never grant permissions just because the skill exists.
- [ ] **Step 6: Run GREEN and CLI smoke.** Rerun the affected tests. In a disposable task with a live central owner, invoke real `worker context`, enqueue a request centrally, complete it from the exact worker environment, and reconcile to one correlated event. Confirm stale/foreign calls refuse and an ordinary worker cannot invoke granted PR operations.
- [ ] **Step 7: Commit.** `git commit -m "feat: expose scoped worker protocol context"` after staging only Task 2 files.

### Task 3: Inject protocol guidance and bound unfinished-assignment correction

**Files:** Modify `lib/omp-worker.ts` and `bin/agd_lib/harnesses.py`; extend `tests/worker.test.ts`, `tests/test_harnesses.py`, and relevant recovery tests in `tests/test_workers.py`.

**Interfaces:**
- Retain the existing worker-extension factory and injected `WorkerSpawn` test boundary; extend its event/result types to the verified native API. Do not globally mock `node:child_process`.
- Consume Task 2's context response. Validate its schema/identity before using it. Cache only the validated frozen core; current phase/outcome must be refreshed on each `context` and `session_stop` callback.
- `before_agent_start` returns `{systemPrompt: string[]}` preserving native segments plus one marked AGD core segment. `context` returns `{messages}` with exactly one hidden `customType: "agd:worker-context"` message. `session_stop` may return `{continue: true, additionalContext: string}` once per unfinished assignment episode.

- [ ] **Step 1: Add failing lifecycle tests.** Build on the existing extension harness, using a real temporary core file and scripted CLI responses only at the process boundary. Assert preservation of unrelated policy, a single core segment across repeated preparation, one fresh dynamic message after compaction/session switch, and phase changes reflected within a turn. Verify missing/corrupt state yields a diagnostic rather than a fabricated ready state.

```typescript
// unfinished assignment, then correction also unfinished
expect(firstStop.continue).toBe(true);
expect(secondStop?.continue).not.toBe(true);
expect(contractBlockedObservations).toHaveLength(1);
expect(taskPhase).toBe('running');

// continuation cannot settle/advance a FIFO request
handlers.agent_end({ willContinue: true, messages: assistantMessages });
expect(completions).toEqual([]);
expect(deliveredRequestIds).toEqual([firstRequestId]);
// terminal event later completes first, then delivers second as existing tests prove
```

Cover ready/landed/cleaned, genuine blocked/decision, active follow-up while running, ready follow-up, failed query, aborted signal, provider-error terminal output, explicit interruption and shutdown. Resetting `agent_start` during correction must not replenish its budget. Ready relaunch must still deliver a queued request without any startup assignment prompt.

- [ ] **Step 2: Run RED.** `bun test tests/worker.test.ts tests/primary.test.ts` and the focused harness/recovery tests. Failures must demonstrate missing policy/context/guard behavior, not only changed strings.
- [ ] **Step 3: Implement core and dynamic context injection.** Load the core from the verified frozen skill, strip only its frontmatter, and supply it through native policy. Replace only the extension's own marked segment/message; preserve all others and avoid mutating stored history. Use the verified native custom-message shape with `display: false`. Include bindings and phase-specific next action, not repeated skill lists. Revalidate on switch, keep the core after compaction, and surface startup/runtime failures as specified.
- [ ] **Step 4: Implement the bounded stop guard.** Reread state at stop. Apply correction only to unfinished initial assignment work, never active FIFO follow-ups or ready/closed/blocked/decision outcomes. Respect `stop_hook_active`, abort/error/interruption/teardown state, and a local correction latch that survives continuation `agent_start`. After an unsuccessful correction, write the fixed diagnostic to the task data directory, emit `worker event ... --kind blocked --key agd-contract-incomplete`, latch successful emission, and allow idle. Do not call readiness commands or spawn a second model session. Show persistence failures; never silently drop them or loop the model to retry them.
- [ ] **Step 5: Remove OMP launch-contract duplication.** Refactor `launch_argv` so ordinary and PR OMP workers use the short prompt from the spec and the manifest's exact skill IDs. No absolute skill paths, mandatory loading of every optional skill, or command recipes in the prompt. Keep explicit AGD extension loading, ambient-extension isolation, session directory and unattended handling, Cursor's existing instructions, and ready relaunch without a prompt. Delete tests that only pin old wording rather than re-pinning new wording; keep native policy/permission boundary checks.
- [ ] **Step 6: Run GREEN and exercise the extension.** `bun test tests/worker.test.ts tests/primary.test.ts`; run focused Python tests; then repeat the provider-free native probe with the actual modified extension and CLI. Observe real provider input, one correction, no premature reply persistence, and no startup-prompt replay. Mock-handler tests alone are not smoke proof.
- [ ] **Step 7: Commit.** `git commit -m "feat: reinforce worker protocol through lifecycle hooks"` after staging only Task 3 files.

### Task 4: Prove native discovery, recovery, and operator workflow

**Files:** Create `tests/worker-native.test.ts`; update `tests/smoke_native_plugins.py`, relevant fixture assumptions in existing tests, `README.md`, `docs/operations.md`, `docs/compatibility.md`, `.agents/skills/orchestrate/SKILL.md`, `.agents/skills/customize/SKILL.md`, and `CHANGELOG.md`.

**Interfaces:**
- Native tests use the official SDK and actual extension runner with a scripted provider; resolve module URLs through existing `reviewer_runner.assert_runtime_capability` in a Python subprocess rather than introducing a production loader or booting reviewer-specific fixtures.
- Reuse existing disposable backend ownership/cleanup helpers and actual CLI. Keep `python3 tests/smoke_native_plugins.py --backend tmux|herdr` as the live acceptance entrypoint.

- [ ] **Step 1: Add native regression assertions.** Inspect real provider system/messages and native skill-resolution results for none, curated, and granted manifests. Assert `agd-worker` resolves to the frozen worktree path, the optional allowlist excludes ambient plugin skills, and the scoped adapter is absent without a grant. Resolve `skill://agd-worker/references/commands.md` with the native reader. Do not ask the assistant to certify its own skill inventory. Exercise native compaction and the stop-hook ordering with scripted assistant outputs; a missing core, duplicate core/context, extra correction, or premature request completion must fail.
- [ ] **Step 2: Run native RED/GREEN against the real harness.** `bun test tests/worker-native.test.ts`. If evidence shows a hook-order/resource-discovery mismatch, fix the shared implementation rather than changing the test to call mocked handlers. An unavailable SDK is a named missing prerequisite, not a passing or silently skipped acceptance row.
- [ ] **Step 3: Update and run the disposable backend smoke.** Materialized sets now include the base skill: none = `{agd-worker}`, curated/profile = selected plus base, granted = base plus scoped adapter. Replace model-generated skill-surface assertions with native evidence from the new check. Give the smoke worker a real small scout assignment, not an instruction to invent its skill list. Observe short visible startup, protocol loading, guarded readiness, two same-incarnation replies, and ready-worker crash/relaunch without replaying initial work. Exercise one unfinished assignment that produces exactly one corrective continuation and then central blocked evidence. Check actual central-owner completion authorization. Run tmux and Herdr only with their verified prerequisites and explicitly owned disposable sessions; never set `HERDR_ENV=1` to bypass pane ownership.
- [ ] **Step 4: Update active documentation after smoke.** Explain optional versus system skills, no automatic upstream renaming/loading, native skill/reference usage, core injection and state reminders, bounded correction rather than guaranteed obedience, ordinary versus granted completion, unsupported Cursor hooks, and recreate-task activation for old snapshots. Update `worker inspect` examples and none-mode smoke evidence names. Document exact exercised OMP/backend versions and any unavailable matrix rows. Do not rewrite historical plans/specs or claim acceptance from unrun tests. Add a concise changelog entry.
- [ ] **Step 5: Run the integrated verification once.** `python3 -m pytest tests -q`; `bun test tests/primary.test.ts tests/worker.test.ts tests/worker-native.test.ts tests/reviewer.test.ts tests/reviewer-telemetry.test.ts`; `bash -n install.sh`; Python compile checks for changed modules and smoke. Report the full results and exact environment exclusions separately. Remove throwaway probes after retaining their evidence; leave only behavior-focused regressions and the maintained smoke.
- [ ] **Step 6: Commit.** `git commit -m "test: verify native worker protocol lifecycle"` after staging native tests, smoke updates and active docs.

## Self-review and completion criteria

Coverage: native discoverability and skill ownership → Task 1; commands/state/authority → Task 2; runtime reinforcement and bounded correction → Task 3; compaction, native resolution, backend recovery and active docs → Task 4. All five Review Focus items have owning regression scenarios. The same context schema and selection semantics are used throughout.

No change is complete on mocked extension callbacks alone. The native probe and disposable lifecycle are required acceptance evidence; a missing backend/SDK is reported as a blocker, never replaced by a weaker assertion. Obtain one whole-branch review after the integrated work, resolve important findings, and keep the user's separate authorization boundary for publishing or merging.
