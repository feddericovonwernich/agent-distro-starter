# Confirmed Distro Uninstall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `./bin/agd --home . uninstall` as a confirmed, recoverable full teardown of exactly one distro and its owned resources, without deleting shared resources or allowing the old home to reappear.

**Architecture:** Add a small uninstall-state/admission module and an explicit coordinator in the existing Python CLI. Resource-specific inventory and teardown stay in their current owner modules; a temporary foreground copy of the CLI and a sibling journal survive deletion of the original checkout. This is one feature with five testable tasks, not independently shipped subsystem uninstalls.

**Tech Stack:** Existing Linux x86-64/Python 3.12+ CLI, Python standard library, pytest/Bun test conventions, native Git, pinned Treehouse v3.1.6, supported tmux/Herdr, user-systemd, and Docker Compose. No new packages or runtime downloads.

**Spec:** `docs/superpowers/specs/2026-10-31-distro-uninstall-design.md` (approved by the user, including relative-home/in-directory invocation).

## Global Constraints

- Full removal after confirmation; shared tooling, unrelated resources, and remote artifacts are never uninstall targets.
- Dirty or unpublished work requires separate explicit discard approval. Unknown ownership is not a discardable data-loss warning.
- Dry-run is read-only and works without a terminal. Do not provision/chmod directories, generate secrets, restore skills, render stacks, or write locks during inventory.
- Execution requires an interactive terminal and the exact canonical home path in a typed deletion confirmation. No generic `--force`, `--yes`, environment-variable authorization, or piped default acceptance.
- Relative `--home` is resolved before staging/chdir for this command only. Do not relax `store.home()` or other commands' contracts.
- A normal in-home terminal is allowed; a coordinator in a managed process tree being stopped is not. Do not claim to change the caller shell's cwd.
- No `pkill`, PID-only kill, path-based ownership, global Treehouse/Git/Docker prune, broad external directory deletion, or removal of an unproven backend server.
- Stop producers, verify owned absence, remove external dependencies, and delete the original home last. A missing/replaced socket or unavailable tool is not proof of absence.
- Preserve the external executor/journal through deletion and delayed verification. Never delete a replacement directory on retry or silently repair corrupt recovery evidence.
- No new dependency, service manager, global resource registry, generic workflow engine, automatic backup, package uninstaller, or unattended uninstall mode.
- No real user-distro teardown, global dependency upgrade, paid model call, push, or merge is authorized by this plan.
- Existing incidental wording/argv-order/source-wiring tests that break must be removed, not re-pinned; keep their meaningful state/safety assertions.

## Review Focus

1. A parent path containing spaces, percent signs and quotes, plus an in-home caller: resolve the target once, preserve exact argv/identity, and never select the stage or sibling home for removal — Task 5.
2. A coordinator dies after unlinking the home but before delayed verification: recover from the external journal without importing/recreating the deleted tree — Tasks 1 and 5.
3. A home is recreated at the same pathname, or its recovery journal is stale/corrupt: old approval must not authorize the new inode/home ID — Tasks 1 and 5.
4. Docker context/engine changes or inspection is denied midway through cleanup: retain remaining evidence and never translate the error into successful absence — Task 3.
5. Frozen skills retain original source paths while execution moves to a stage, and a clean source checkout is itself linked to shared Git metadata: validate frozen destinations without rebasing provenance, remove only exact registrations, and preserve siblings — Tasks 4 and 5.

---

## File and interface map

New production files only:

- `bin/agd_lib/uninstall_state.py`: safe target/operation identities, private sibling journal, admission/retirement coordination, scoped approval checks and checkpoints.
- `bin/agd_lib/uninstall.py`: aggregate inventory/digests, consent, explicit teardown sequencing, foreground staging/recovery, final deletion/result.

Modify owning modules, not parallel adapters:

- `bin/agd`: relative uninstall dispatch, parser/output/TTY integration; existing init/start/exec entrypoints participate in retirement admission.
- `store.py`, `launcher.py`, `workers.py`, `supervision.py`, `relay.py`, `reviewer_runner.py`, `reviewer_timer.py`, `tasks.py`, `projects.py`, `installer.py`, `omp_runtime.py`, `observability.py`: guard actual creation/registration windows. Do not hold admission for long-lived watch/model/broker loops.
- `launcher.py`, `supervision.py`, `tmux.py`, `herdr.py`, `workers.py`, `relay.py`, `reviewer_runner.py`: runtime identities and owned shutdown.
- `reviewer_timer.py`, `observability.py`: read-only native discovery and full owned unit/Compose cleanup.
- `tasks.py`, `projects.py`, `worker_skills.py`: exact lease/worktree/Git/frozen-artifact inventory and deletion.
- Tests stay beside the owning modules; add `tests/test_uninstall_state.py`, `tests/test_uninstall.py`, and one opt-in `tests/smoke_uninstall.py`. Reuse `tests/support.py`, existing native smoke setup and existing PTY patterns; do not introduce a test framework.
- Fold README/operations/compatibility/changelog updates into Task 5 after real smoke evidence.

### Shared data contracts

Use plain dictionaries and existing atomic/private-file conventions, not a resource-provider class hierarchy. Keep the field names below consistent across all tasks.

- **Target identity:** `canonical_home`, `home_id`, `uid`, `device`, `inode`. These describe the original target; a later missing marker is valid only when the operation proves its planned deletion.
- **Resource:** `key` (stable kind-qualified string), `kind`, `identity` (resource-specific non-secret IDs/paths/process-start/socket/engine evidence), `presence` (`present|absent|unknown`), `ownership` (`owned|shared|foreign|unknown`), `blockers` (list of `{code, reason}`). Relevant records may also carry `work_loss` metadata. No tokens, auth values, payload text or full process environments.
- **Inventory:** `schema: 1`, `target`, `resources`, `work_loss`, `retained`, `blockers`, `warnings`, `digest`, `discard_digest`, `outcome` (`ready|blocked|already-absent`). Digests cover canonical stable identity/removal/work-loss fields, not heartbeats or presentation text. At-risk Git history can be approved; ambiguous runtime ownership cannot.
- **Operation:** `schema: 1`, `target`, `coordinator` (`pid`, `start`, `uid`), `phase`, `inventory`, `discard_digest`, `completed` (resource keys), `irreversible_started`, `executor` (original code root, outside interpreter and staged-file hashes), `failure`. Phases are `preparing|quiescing|removing|verifying|completed|cancelled|blocked`. `executor` may be absent only before staging is complete; no destructive step is admitted then.
- Resource modules export `uninstall_inventory(home: Path, *, expected: list[dict] | None = None) -> list[dict]`. `expected` is only a validated pending operation's saved subset, used after source receipts have been removed; it does not authorize discovering/adopting other resources.
- Runtime modules export `uninstall_quiesce(home: Path, resources: list[dict], operation: dict) -> None`. Removable native/data modules export `uninstall_remove(home: Path, resources: list[dict], operation: dict) -> None`. Each destructive helper checks its key against the current operation, revalidates identity, verifies absence and checkpoints that resource only afterward. Partial success must remain resumable.
- The coordinator calls these modules explicitly in the prescribed order; no dynamic provider registry. All ordinary public lifecycle/approval APIs keep their existing contracts.

## Task 1: Retirement admission and durable operation authority

**Files:**
- Create: `bin/agd_lib/uninstall_state.py`, `tests/test_uninstall_state.py`.
- Modify: `bin/agd:181-272`, `bin/agd_lib/store.py:63-85`; creation windows in `launcher.py:417-467`, `workers.py:122-273,535-575`, `supervision.py:207-261`, `relay.py:456-557`, `reviewer_runner.py:72-148`, `reviewer_timer.py:268-305`, `tasks.py:31-130`, `projects.py:25-65`, `installer.py:381-506`, `omp_runtime.py:132-150,321-359`, `observability.py:135-187,271-303,654-704`.
- Test/fixture: `tests/support.py`, relevant existing init/start/launch/reviewer/Relay tests.

**Interfaces:**
- Produces in `uninstall_state.py`: `operation_dir(home: Path) -> Path`, `target_identity(home: Path) -> dict`, `read_operation(home: Path) -> dict | None`, `fingerprint(value: object) -> str`, `require_active(home: Path) -> None`.
- Produces context managers `admission(home: Path, *, initializing: bool = False) -> Iterator[None]` and `retirement(home: Path, inventory: dict, *, discard_digest: str | None) -> Iterator[dict]`.
- Produces `require_scope(home: Path, operation: dict, key: str) -> dict` and `checkpoint(home: Path, operation: dict, *, phase: str, completed: str | None = None, irreversible: bool = False, error: str | None = None) -> dict`.
- Produces test helper `support.uninstall_scope(home: Path, resources: list[dict], *, discard: bool = False)`: context manager constructing a real target/inventory from initialized fixture metadata and entering `retirement`; no product consent bypass is exposed in the CLI.

- [ ] **Step 0: Record the unchanged baseline.** Use the final Python/Bun gate below with the already-available official SDK on test PATH and the three named known tmux exclusions. Record actual output before changing runtime code; do not download/upgrade dependencies or rerun known missing-prerequisite cases merely to confirm them.

- [ ] **Step 1: Write state/admission regressions.** Use a fresh CLI-initialized fixture home. Add these tests with real files/flocks/subprocess coordination:
  - `test_retirement_blocks_creation_without_recreating_home`: after retirement is admitted and a disposable home is removed, `require_active(home)` raises `store.Refusal`; `assert not home.exists()` after a stale producer attempts init.
  - `test_admission_drains_before_retirement`: a subprocess holds shared admission and signals readiness through a pipe; retirement refuses busy without starting removal. After release, retirement succeeds; a subsequent producer cannot enter. No sleep-based race.
  - `test_admission_does_not_deadlock_nested_store_mutation`: an admitted creation path acquires `store.lock(home)`, writes a real receipt and exits within a subprocess timeout.
  - `test_replaced_home_never_inherits_old_scope`: replace the original target with a different inode/home ID; `require_scope` raises and the new marker/sentinel bytes remain unchanged.
  - `test_corrupt_or_conflicting_operation_retains_evidence`: malformed/hash/identity-mismatched journal is refused; `assert journal.read_bytes() == original_bytes`; no new grant/fence repair occurs.
  - `test_cancelled_and_irreversible_operations_have_distinct_admission`: cancelled-before-removal allows a new attempt; blocked-after-irreversible remains fenced. Assert actual start/receipt creation succeeds only in the former case.
- [ ] **Step 2: Run RED.** `python3 -m pytest tests/test_uninstall_state.py -q`. Expected failures: missing new module/API, not external-tool failures.
- [ ] **Step 3: Implement the state and admission contract.** Use a deterministic sibling `.agd-uninstall-<sha256(canonical_path)[:32]>` directory, current-UID mode 0700, atomic mode-0600 JSON and a coordinator flock. Reject symlinks/mount-root/identity conflicts; no secret data enters the record. New normal creation uses a separate `state/activity.lock` shared flock; retirement obtains exclusive admission nonblockingly, publishes the external receipt/in-home pointer and releases the short admission barrier. Do not extend `store.lock` across external commands or nested lifecycle functions. Check the external fence before creating any parent/lock, including init on an absent home. Init's freshness check may admit only its two generated lock files, not arbitrary existing data.

Admission is always acquired before ordinary mutation locks; never enter it from an already-held `store.lock` section. Use function-local imports at the `store`/`uninstall_state` boundary to avoid an import cycle. A valid `preparing` journal permits completion of interrupted staging while the original source still exists; it never permits quiescing/removal with an incomplete executor. Tests' `uninstall_scope` binds real outside interpreter/module hashes and an initialized fixture identity before yielding a quiescing grant; it is not a canned approval echo.
- [ ] **Step 4: Integrate every actual creation window.** Wrap reservation → native create/exec preparation → durable process/endpoint registration, then release admission. Watcher loops release after owner publication; workers release after runner/model identity publication; reviewers retain `run_lease` but release admission after coordinator registration and re-admit for child spawn; Relay releases after identities are recorded. Do not hold admission throughout model/broker/watch lifetimes. Existing loops can finish observations but may not start/rearm work after the fence. CLI mutation checks must distinguish retirement from central-owner authorization: reviewer management remains central-independent. All normal init/OMP/start/installer and optional-feature producers are covered; do not make read-only commands write a lock.
- [ ] **Step 5: Verify GREEN and normal behavior.** Run `python3 -m pytest tests/test_uninstall_state.py tests/test_launcher.py tests/test_installer.py tests/test_omp_runtime.py tests/test_supervision.py tests/test_reviewer_lifecycle.py -q`. Add actual initializer refusal, watcher startup, worker/Relay registration and reviewer dispatch regressions to their existing suites where the admission window is owned. Expected: new retirement tests and existing normal lifecycle tests pass; no permanent test pins the new lock filename.
- [ ] **Step 6: Smoke and commit.** Run the actual CLI init/read-only status in a disposable home; exercise a fenced init from a second subprocess and observe no directory recreation. Commit exact changed files with `feat: fence distro retirement and retain uninstall authority`.

## Task 2: Owned process, central and backend shutdown

**Files:**
- Modify: `bin/agd_lib/supervision.py:21-26,203-280`, `launcher.py:75-168,178-414`, `workers.py:122-273,507-575`, `relay.py:392-490`, `reviewer_runner.py:72-148`, `tmux.py`, `herdr.py`.
- Test: `tests/test_supervision.py`, `test_launcher.py`, `test_central_backends.py`, `test_workers.py`, `test_relay.py`, `test_reviewer_lifecycle.py`.

**Interfaces:**
- Consumes Task 1 operation/admission/scope/checkpoint functions.
- Produces common `supervision.stop_owned_processes(identities: list[dict], *, timeout: float = 15.0) -> None` for already-authorized PID/start/UID identities and their proven descendants. This helper is not an ownership classifier and must not accept bare PIDs.
- Produces shared-contract `uninstall_inventory` / `uninstall_quiesce` in `launcher`, `workers`, `relay`, `reviewer_runner`; `launcher.uninstall_remove` performs only late central-workspace/owned-server cleanup after worker endpoints are gone.
- Reviewer inventory additionally accepts `service: dict | None = None` for Task 3's positively owned service/run-lock proof; default `None` never guesses a legacy manual owner's PID.
- Produces backend `tmux.uninstall_close(home: Path, resource: dict, operation: dict) -> None` and the same signature in `herdr.py` for exact owned endpoint/server records.

- [ ] **Step 1: Add failing runtime-boundary tests.** Extend existing fixtures, not a new mock framework:
  - `test_uninstall_stops_owned_process_tree_not_pid_replacement`: real subprocess with a child heartbeat; matching evidence stops both and the heartbeat ceases; wrong start identity leaves a separate sentinel subprocess alive.
  - `test_central_quit_does_not_claim_worker_or_relay_absence`: central exit does not mark worker/Relay keys completed until their separate PID/endpoint probes are absent.
  - `test_uninstall_refuses_replaced_backend_or_foreign_pane`: altered socket inode/server start/token or an unrelated pane causes refusal and leaves the unrelated endpoint untouched.
  - `test_uninstall_quiesce_preserves_process_receipts_on_timeout`: an owned non-exiting fixture retains its identity and lease/worktree; no completed key is written.
  - `test_legacy_busy_reviewer_without_safe_identity_blocks`: busy real run lock without coordinator/service evidence is unknown, not a guessed kill target.
  - `test_manual_reviewer_records_identity_before_child_spawn`: invoke reviewer code in a separate process, then fence between collection and spawn; no late host starts and the coordinator exits or is safely quiesced.
- [ ] **Step 2: Run RED.** Run the added selectors in their existing files. Expected: missing uninstall helpers/identity behavior, not unsupported backend setup.
- [ ] **Step 3: Implement runtime inventory and safe signaling.** Use existing launcher/token/socket/argv/PID-start correlation, plus current UID and kernel-bound pidfds before new direct signals. Capture/revalidate descendant identities while the root is alive; retain original handles through graceful/TERM/KILL escalation and verify them gone. Record needed server process-start/socket evidence for new tmux endpoints, and explicit server/connector group/session identity for new non-TTY Relay launches. Do not blindly put interactive OMP children in a new session and break controlling-terminal behavior. Legacy receipts may close positively owned native panes; missing server/descendant proof blocks unsafe server signaling rather than inventing it. No kill by cwd/name or broad session sweep.

The common process evidence fields are `pid`, `start`, `uid`, `ppid`, `pgid`, and `sid`; resource owners additionally bind role/endpoint/launch identity and expected argv digest without exposing prompt/key-bearing argv. Proven isolated groups/sessions can cover ordinary descendants; do not treat an inherited interactive TTY group as exclusively owned or pretend legacy detached descendants were tracked.
- [ ] **Step 4: Implement quiescing.** Central native quit is first; verify central and watcher exit, but leave servers containing worker endpoints. Then stop exact workers/Relay/reviewer coordinators and children. Prefer existing interrupt/idle quit; bounded fallback uses only scoped proven identities. Adapt the native pattern in `tests/smoke_native_plugins.py:203-259,277-338` into production owner modules rather than importing test code. Do not let a stale heartbeat alone make an otherwise proven exit unknown, or treat absent backend transport as an absent process. Late backend removal preserves external/shared Herdr servers; if a foreign endpoint prevents retiring a home-dependent server, return a blocker.
- [ ] **Step 5: Verify and smoke.** Run affected Python suites and `bun test tests/primary.test.ts`. Exercise the common signal path on a disposable real process tree and observe PID-start/heartbeat absence; use the actual central backend at final native acceptance. Preserve existing watcher rearm/worker recovery behavior when not retiring.
- [ ] **Step 6: Commit.** `git commit -m "feat: retire owned distro processes and backend endpoints"` after staging only this task's files.

## Task 3: Reviewer units and full owned Docker teardown

**Files:**
- Modify: `bin/agd_lib/reviewer_timer.py:198-337`, `observability.py:305-389,654-778,829-912`.
- Test: `tests/test_reviewer_timer.py`, `tests/test_observability.py`, relevant partial-enable fixtures in `tests/test_install.py` only if needed.

**Interfaces:**
- Consumes Task 1 scope/checkpoint and Task 2 process evidence.
- Produces shared-contract `uninstall_inventory`, `uninstall_quiesce`, `uninstall_remove` in `reviewer_timer.py` and `observability.py`.
- An owned active reviewer service resource includes native unit ID/fragment path/hash, canonical-home/ExecStart correlation, MainPID/start, UID, cgroup, and positive `run_lease_owner` `{pid,start,device,inode}` when its main process holds that exact lock. Task 5 passes this service record to reviewer-runner inventory; unrelated busy manual locks remain blockers.
- Docker resource identities include exact container/network/volume IDs/names, home/project labels, configured roots and approved context/engine ID. No credentials or rendered secret values enter the returned data.

- [ ] **Step 1: Write native-resource failure/state regressions.** Reuse timer `home()`/`interval_config()`, observability `lab()`, and the partial-enable fixtures:
  - `test_uninstall_inventory_unconfigured_features_is_read_only`: before/after filesystem metadata agree, no secrets/units/directories are created, and no unavailable optional runtime is demanded when there is no feature evidence.
  - `test_active_reviewer_service_is_stopped_not_only_timer_disabled`: a real owned user service keeps a marker/lock active; uninstall quiesce stops its cgroup, releases the lock and removes only its timer/service/wants/stamp. A second home unit survives.
  - `test_uninstall_refuses_foreign_fragment_or_service_home`: unit marker/path/ExecStart mismatch or a symlink yields a blocker without issuing destructive control.
  - `test_docker_inspection_error_is_not_absence`: permission/daemon/malformed failures leave fixture resources and recovery records intact; only positively classified not-found is absent.
  - `test_context_switch_prevents_deletion_of_other_engine`: switch the fixture's engine ID between inspection and execution; `assert foreign_objects == original_foreign_objects`; no completed resource keys appear.
  - `test_partial_render_can_be_purged_without_published_config`: real retained Compose/lock/secrets state is sufficient for owned inventory and cleanup, without calling enable/render or generating replacement credentials.
- [ ] **Step 2: Run RED.** Run the new selectors in `tests/test_reviewer_timer.py` and `tests/test_observability.py`; expected missing owned-purge behavior. Native user-manager tests must name/skip an unavailable manager rather than produce a vacuous pass.
- [ ] **Step 3: Implement pure discovery.** For systemd, walk existing XDG/fragment paths with `lstat`; never call `_safe_directory()` / `_unit_directory()` in dry-run. Inspect only exact candidate unit IDs and loaded fragments. For observability, reuse read-only `_existing_private_directory`, `_read_json`, `_read_secrets`, `_load_lock` and ownership parsing; never call `_lifecycle`, `_runtime_directories`, `_secrets`, `render_stack` or `_save_state` just to inventory. Classify inspect errors explicitly. Show legacy unknown-engine evidence; confirmation is scoped to the displayed engine, not a global engine search.
- [ ] **Step 4: Implement owned quiesce/removal.** Disable activation, stop both timer and active service/cgroup, verify inactive/absent, clean persistent timer state, then remove exact owned unit artifacts and reload. Manual busy leases must be discharged by their proven owner, never unlinked. For Docker, stop/remove every approved owned project container (including proven orphans), then networks and the four declared owned data-volume classes plus any explicitly inventoried owned resource. Verify labels/IDs/context again before each destructive boundary and reconcile actual absence afterward. No image deletion, system prune, arbitrary drop-in deletion, user-manager stop, or credential loss before the last resource using it is resolved. Keep normal `disable` retention semantics unchanged.
- [ ] **Step 5: Verify GREEN and native APIs.** `python3 -m pytest tests/test_reviewer_timer.py tests/test_observability.py tests/test_install.py -q`. Add a disposable real systemd service/timer cleanup run; in Task 5's native smoke, create uniquely labelled Docker resources and observe their actual disappearance while shared sentinels remain.
- [ ] **Step 6: Commit.** `git commit -m "feat: remove owned reviewer units and observability resources"` with exact files staged.

## Task 4: Work-loss inventory and narrow Treehouse/Git removal

**Files:**
- Modify: `bin/agd_lib/tasks.py:12-27,133-159,437-490`, `projects.py:8-22,84-119`, `worker_skills.py:438-513,551-574,737-790`.
- Test: `tests/test_tasks.py`, `tests/test_projects.py`, `tests/test_worker_skills.py`.

**Interfaces:**
- Consumes Task 1 scoped grants and Task 2 actual process/endpoint absence.
- Produces shared-contract `uninstall_inventory` / `uninstall_remove` in `tasks.py` and `worker_skills.py`.
- Produces `projects.checkout_inventory(path: Path) -> dict`, `projects.uninstall_inventory(home: Path, *, expected: list[dict] | None = None) -> list[dict]`, and `projects.remove_checkout(home: Path, resource: dict, operation: dict) -> None`.
- Checkout identity records exact path/device/inode, common-dir identity, source/project role, linked registration and sibling registrations. Work-loss records include changed tracked files, non-generated untracked/ignored data, local-only refs and commits/reflog history that would be lost or cannot be evidenced in existing upstream refs.

- [ ] **Step 1: Add destructive-boundary tests using real local Git and existing lab/skill fixtures.**
  - `test_clean_published_clone_needs_only_home_confirmation`: clean upstream-evidenced history has no work-loss entries; custom dirty/ignored files and local-only commits do, including at the distro source root.
  - `test_foreign_or_reassigned_lease_is_preserved`: change the exact lease/holder before conditional return, or reacquire it between return/destroy; cleanup refuses/skips and the replacement owner's bytes survive.
  - `test_return_is_not_destroy_and_skipped_slot_is_not_success`: an exit-zero bulk/skip result does not mark the exact slot absent; a verified native single-slot removal does.
  - `test_staged_skill_teardown_uses_frozen_proof_not_stage_source_path`: remove the original skill catalog, execute teardown from staged module paths, and assert only proven generated files/owner entries disappear; custom `.env` and another exclusion owner remain.
  - `test_linked_source_removal_preserves_common_repo_and_sibling`: real `git worktree add` source checkout; after removal its registration is absent, sibling bytes/refs/common directory survive.
  - `test_external_worktree_blocks_backing_clone_deletion`: an unowned external registration preserves the backing clone. Separately, an owned exact external worktree on another filesystem is allowed, while nested/bind mounts or mount-root deletion are refused.
- [ ] **Step 2: Run RED.** Run these selectors in their owning files. Use a named prerequisite skip for an unavailable native mount setup; do not use privileged mounts or global worktree pools as a fixture.
- [ ] **Step 3: Implement read-only Git/lease/skill inventory.** Enumerate task receipts and their recorded `treehouse_root`, native exact slot status, and all project/source Git registrations. Do not reconcile/rewrite task phase or restore generated skills during dry-run. Inspect original/frozen descriptors and destination hashes without comparing their recorded AGD source path to the stage's `__file__`. No catalog/bootstrap/model validation is needed for removal. Clean upstream-evidenced history is covered by main consent; unknown publication is a discardable owned-data warning, not a forced network fetch. Foreign leases/registrations remain non-overridable blockers.
- [ ] **Step 4: Implement narrow removal.** Remove only frozen artifacts and the existing `_exclude` ownership key, leaving other owners and user exclusion lines intact. Verify worker/runner/endpoint absence; conditionally return a slot with `--if-lease-id` and `--if-lease-holder`, using native force only when the exact changed-work scope was approved. Reconcile the returned slot; invoke single-path native destroy without `--all`, `--include-leased` or `--include-in-use`. Include unlanded removal only under the verified discard scope. Native reacquisition/skip is a blocker, not permission to widen flags. Verify exact slot/registration absence before completion. Use exact Git worktree removal for linked checkouts; ordinary clone deletion first revalidates all external dependencies. Preserve external common directories/refs. Keep ordinary `task cleanup`, approvals and `project remove` contracts intact; do not forge lifecycle states to get past them.
- [ ] **Step 5: Verify and smoke.** `python3 -m pytest tests/test_tasks.py tests/test_projects.py tests/test_worker_skills.py -q`. Exercise a disposable native Treehouse exact lease return/destroy, and a real local Git linked-source removal. Compare sibling worktree files and exclusion ownership before/after. Record any native tool limitation instead of bypassing its lease protections.
- [ ] **Step 6: Commit.** `git commit -m "feat: remove approved distro worktrees and Git registrations"` with exact files staged.

## Task 5: Guided CLI, foreground recovery and full native acceptance

**Files:**
- Create: `bin/agd_lib/uninstall.py`, `tests/test_uninstall.py`, `tests/smoke_uninstall.py`.
- Modify: `bin/agd:12,15-160,181-232,418-425`, owning helpers above only where integration proves a contract gap; `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`.

**Interfaces:**
- Consumes all shared contracts and source-local helpers from Tasks 1–4.
- Produces `resolve_home(value: str | Path, *, cwd: Path | None = None) -> Path`, `inventory(home: Path) -> dict`, `execute(home: Path, preview: dict, *, discard_digest: str | None, ask: Callable[[str], str | None], report: Callable[[str], None]) -> dict`, and `command(home: Path, *, dry_run: bool, ask: Callable[[str], str | None], report: Callable[[str], None]) -> tuple[dict, int]` in `uninstall.py`. `command` passes its callbacks through so changed-work approval and progress remain available after quiescing; the staged CLI binds fresh terminal callbacks rather than attempting to serialize Python closures.
- Produces `stage_executor(home: Path, operation: dict) -> Path` and `handoff(home: Path, operation: dict) -> NoReturn`. The staged CLI resumes the same operation using validated coordinator PID/start continuity; a different process always goes through fresh preview/consent. No user-visible hidden authorization flag or environment bypass.
- Produces final outcomes `uninstalled|cancelled|already-absent|blocked`; stdout is always pretty JSON for this command, stage/prompts/warnings go to stderr. Exit 0 for ready dry-run/handled cancellation/verified uninstall/already-absent, 3 for unsafe state, 4 for missing essential inspection dependency, 2 for malformed arguments.

- [ ] **Step 1: Write CLI/authority regressions before implementation.** Add an actual PTY driver local to `tests/test_uninstall.py` following `tests/test_install.py`; no product headless flag. Cover:
  - `test_relative_home_resolved_before_handoff`: invoke the target's own script with `--home .` from a path containing spaces/percent/quotes; assert only that original home disappears, a sibling sentinel survives, stage cwd is outside, and the parent-shell test explicitly performs `cd ..` rather than expecting Python to move it.
  - `test_dry_run_and_decline_write_nothing`: target paths/content/modes/modification times and external fixture resources remain identical (not filesystem read-access timestamps); JSON has the correct planned data classes/blockers without secret values. Include a fresh own-source checkout with no application bytecode cache so imports cannot silently create one during preview.
  - `test_staged_python_environment_cannot_recreate_target`: point inherited Python cache/import controls inside the disposable target, execute the real foreground handoff, and assert the deleted home/cache stay absent after verification; stage imports must come from the captured bundle, not another cwd/user package.
  - `test_owned_work_loss_requires_second_bound_confirmation`: wrong/no discard response preserves work; change a head/file after approval and require refreshed consent before deletion. Clean published clone/approved volumes do not add extra prompts.
  - `test_managed_ancestor_is_refused_before_quiescing`: a coordinator inside a recorded central/worker process tree leaves that process and home intact; a normal in-home caller is accepted.
  - `test_crash_during_home_removal_resumes_without_original_cli`: terminate a subprocess after partial real deletion; run the printed staged command and assert verified completion from external state.
  - `test_crash_after_home_unlink_repeats_delayed_verification`: exit before the final check, resume with no original home/marker/modules, and verify no recreation before removing the stage.
  - `test_recreated_home_and_corrupt_journal_are_never_adopted`: replace path/inode/home ID or corrupt the sibling record; assert the replacement/sibling/evidence bytes are unchanged and exit 3.
  - `test_optional_features_never_enabled_need_no_optional_runtime`: absence of Docker/Bun/OMP SDK/systemd/Treehouse runtime evidence must not provision those features or demand them just to remove ordinary owned files. Git remains required when Git identity/dependency inspection is needed.
- [ ] **Step 2: Run RED.** `python3 -m pytest tests/test_uninstall.py -q`. Expected: parser/module/required behavior absent. Do not turn these into source-text or mock-forwarding assertions.
- [ ] **Step 3: Implement aggregate inventory and consent.** Merge the explicit module inventories; probe reviewer unit ownership first and pass its positive run-lock/service proof to reviewer-runner inventory. Catch resource discovery refusals into structured blockers so dry-run still emits useful JSON. Normalize relative target only on uninstall dispatch before `store.home`, and keep lifecycle module calls absolute. Validate target and caller lineage, derive stable digests, disclose all private data/volume loss, and collect the exact path plus any distinct discard grant. Unknown ownership is never converted into an extra force prompt. Reuse current TERM/NO_COLOR/TTY styling conventions without a new UI library or snapshot tests. Cancel before consent writes no stage/journal.
- [ ] **Step 4: Implement staging and resume.** After consent, copy the exact `bin/agd` and explicit `bin/agd_lib/*.py` module allowlist (including the two new modules) plus `bundled/observability/release-lock.json` into the private sibling stage. Preserve package layout and file hashes. Do not copy `.git`, OMP/plugin/auth/session/project/config state, Relay bundles or worker-skill catalogs: teardown uses frozen provenance, not provisioning/content loaders. Resolve an outside interpreter, move coordinator cwd outside every removal root, print the recovery invocation, and foreground exec. Reacquire/validate the operation lock and actor continuity without trusting an environment flag. Handle corrupt/conflicting records as blockers; do not overwrite them or attach old approval to a replacement directory.

The production module allowlist is exactly `github.py`, `harnesses.py`, `herdr.py`, `installer.py`, `launcher.py`, `observability.py`, `omp_runtime.py`, `pr.py`, `projects.py`, `relay.py`, `relay_reviews.py`, `review_feedback.py`, `reviewer.py`, `reviewer_runner.py`, `reviewer_timer.py`, `store.py`, `supervision.py`, `tasks.py`, `tmux.py`, `worker_skills.py`, `workers.py`, `uninstall_state.py`, and `uninstall.py`, all under staged `bin/agd_lib/`, plus `bin/agd` and the one release-lock asset. Do not glob arbitrary extra files from a user's `bin` directory. Publish the complete staged bundle/hash manifest before handoff; interrupted staging can resume only from valid matching source evidence, never by mixing unverified versions.

Before any application imports on the uninstall path, suppress Python bytecode writes; an early reserved-command hint may do only that, while actual dispatch/authorization still uses parsed arguments. Foreground handoff and its printed recovery command use the outside interpreter with `-I -S -B`; bootstrap only the trusted stage `bin` onto the isolated import path before loading `agd_lib`. This ignores inherited Python path/cache controls, disables site hooks, and prevents post-delete bytecode cache recreation without changing normal commands' caching behavior. Confirm the chosen interpreter/stdlib are outside every deletion root.
- [ ] **Step 5: Implement explicit execution order.** Under the retirement grant: disable future reviewer activation; stop central/watcher; stop owned workers, scheduled/manual reviewers and Relay; finish exact backend removal; re-inventory and refresh changed-work consent. Remove approved native units/Compose resources, frozen artifacts/Treehouse slots and Git registrations. Revalidate all absence and foreign dependencies before deleting project/source data and the original home. Use Git-native final removal for a linked source checkout; never import target code after deletion. Persist external `verifying` state before home unlink, verify delayed absence for **at least eight seconds** (four existing two-second watcher heartbeat intervals), and persist the result before stage cleanup. A surviving/recreated path is a failure, not another blind rmtree. Cancellation before irreversible removal clears retirement safely but does not restart services; after irreversible removal preserve stage/evidence and return the appropriate blocked outcome. A missing target without a pending receipt is only `already-absent`.
- [ ] **Step 6: Run the focused suite and actual PTY smoke.** `python3 -m pytest tests/test_uninstall_state.py tests/test_uninstall.py -q`. Observe colored/NO_COLOR normal-terminal output, pretty JSON, exact path/discard questions, EOF/decline, blocked inspection, and the original caller's explicit parent-directory transition. No style-copy assertions.
- [ ] **Step 7: Leave one retained opt-in native check and run it.** `tests/smoke_uninstall.py` accepts `--backend tmux|herdr` and `--with-docker`, but no arbitrary user-home target. It creates a unique temporary checkout-home and second sentinel home, actual central/worker/watcher endpoints using the existing native setup pattern, an owned reviewer service/timer with no paid work, exact Treehouse/Git registrations, and optionally labelled local Docker resources from already-available pinned images. Drive the real self-binary invocation in a PTY, not the coordinator library. Observe all targeted processes, units/stamps, slots/registrations, containers/networks/volumes and the original home absent; shared tools/second-home resources survive; delay verifies no old heartbeat recreates paths. Exercise a linked source and partial teardown/resume. Do not copy the old smoke's broad `--include-leased --include-in-use --include-unlanded` cleanup fallback. On a failed proof preserve scoped evidence and use only independently proven fixture-owned cleanup. Run the available native backend; do not fake `HERDR_ENV=1`, upgrade tools, pull images implicitly or claim unavailable cases passed.
- [ ] **Step 8: Update user docs from observed behavior.** Document both `./bin/agd --home . uninstall` and the `&& cd ..` shell caveat, dry-run/result codes, confirmation/data-loss rules, exact owned/shared boundaries, outside recovery command, cancellation/partial failure, linked/external worktree handling and safe legacy blockers. Record exercised native versions/scenarios and explicit limitations in compatibility; do not claim paid-model/provider/Langfuse ingestion acceptance.
- [ ] **Step 9: Run the integrated gate once after changes settle.** Use the official importable OMP SDK already available in test infrastructure on PATH; never a production fallback. Run Python and all affected native Bun suites, Bash syntax and Python compilation. On this host the three previously known missing-tmux recovery cases are not rerun just to reconfirm the prerequisite:

```sh
python3 -m pytest tests -q \
  --deselect=tests/test_workers.py::test_crashed_runner_recovered_only_after_process_and_pane_absent \
  --deselect=tests/test_workers.py::test_reconcile_adopts_exact_owned_endpoint_after_publication_gap \
  --deselect=tests/test_workers.py::test_proved_absent_unpublished_start_returns_to_prepared
bun test tests/primary.test.ts tests/worker.test.ts tests/worker-native.test.ts tests/reviewer.test.ts tests/reviewer-telemetry.test.ts
bash -n install.sh
python3 -m py_compile bin/agd bin/agd_lib/*.py
```

Expected: all exercised checks pass; report exact counts/skips/warnings and native smoke results. If a prerequisite actually changes, include its formerly excluded cases. Fix regressions without re-pinning incidental tests. Native smoke is required in addition to unit suites; name any unreachable prerequisite instead of narrowing acceptance silently.

- [ ] **Step 10: Commit and hand off for integration.** Commit exact product/test/doc files with `feat: add confirmed recoverable distro uninstall`. Obtain fresh review according to the user's chosen execution method, fix findings with behavior proof, and ask before push/PR/merge. Do not uninstall a real distro or remove this task's lease as part of verification.

## Self-review checklist (planning, not implementation evidence)

- [x] Every approved spec section maps to the five tasks, including in-home invocation, aggregate versus discard approval, no-write preview, shared exclusions, corrupt/cancelled/replaced recovery states, mount boundaries, linked source, active reviewer, partial Docker, admission races, external executor, and delayed non-recreation.
- [x] Every cross-task API matches the signatures/data fields above; no undefined helper/type or promised future stub is needed for another task to proceed.
- [x] Every Review Focus item has named behavioral tests in its owning task. Fixtures and native controls are scoped; no caller/provider-secret/source-text/argv-order tests are added.
- [x] The plan is decisions/tests/interfaces, not a transcript of implementation bodies. Setup and documentation are folded into the deliverable that owns them; no separate scaffolding or cleanup task.
- [x] Planning has changed only approved design/plan documents; execution still requires the user's plan review and method selection.
