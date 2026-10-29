# Native OMP Plugins and Upstream-Named Worker Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install official orchestrator plugins into distro-local OMP storage and copy selected, unmodified, upstream-named skills into each worker worktree before task creation completes.

**Architecture:** Keep OMP's official marketplace installer and registry authoritative. One small OMP runtime module supplies the distro-local environment and resolves installed plugin payloads; the existing worker-skill snapshot/journal machinery copies selected payload directories at task creation and verifies them at launch, relaunch, readiness, and cleanup. Plugin extensions run in the orchestrator, not as a consequence of worker skill provisioning.

**Tech Stack:** Existing Bash installer, Python 3.12+ CLI, OMP native plugin CLI, Treehouse 3.1.6, existing TypeScript/Bun integrations and pytest/Bun checks. No new installer, package manager, framework, or YAML dependency.

**Spec:** This document's [Approved design](#approved-design), recording the user's accepted design and explicit requirement to preserve upstream names. Source report: `/home/feddericokz/Workspace/omp-plugin-installation-report.md` (context, not an executor dependency).

**Planning baseline:** upstream `main` at `c475d4d7fabf38dbf029a28302c53010cad11357`. This is a plan only; implementation and publication are not authorized by this document.

## Global Constraints

- Preserve upstream directory names, `name:` values, skill text, supporting files, and executable modes. No `agd-` renaming, compatibility aliases, or rewriting upstream cross-references.
- Use official marketplace commands, at project scope, for `ponytail@ponytail` and `superpowers@superpowers-dev`; never substitute npm/Git user-scope installs.
- All AGD-controlled OMP persistent data must stay within the selected distro. Preserve the real `HOME` for Git, SSH, and other worker tools. This is storage isolation, not a security sandbox for plugins or tools.
- Keep provider/model configuration in OMP. No authentication/model prompts, automatic credential copying, or mutation of the user's global OMP installation.
- Store selected skills in the worktree before a new task becomes `prepared`. Worker launch must not resolve a new default selection or substitute an upgraded plugin's bytes.
- Retain lease identity, ownership, authorization, symlink/collision checks, content hashes, generated-file exclusions, and retention on uncertain failure.
- Preserve the AGD-authored `agd-scoped-pr-relay` adapter under its original name and existing grant boundary. It is not a renamed upstream skill.
- Linux x86-64 remains the installer platform. Native storage/registry behavior was investigated against OMP 18.14.0; require at least that version and verify paths/capabilities rather than assuming future versions preserve undocumented schemas.
- Fresh installs and newly created tasks use the new contract. Do not silently migrate an existing private home, rewrite retained task artifacts, grant new capabilities, or mutate an active central. Old upstream-prefixed profiles must receive an explicit incompatibility/refusal rather than alias translation. Read-only historical evidence remains retained.

## Review Focus

1. Inherited profiles, dotenv variables, and XDG directories must not redirect OMP outside the distro or alter `HOME`: Task 1, `test_local_environment_wins_over_inherited_storage`, plus native dotenv smoke.
2. Registry paths that escape the distro, stale/disabled installs, and duplicate skill names must not select arbitrary source files: Tasks 2–3, `test_plugin_sources_refuse_unsafe_or_ambiguous_registration` and `test_catalog_refuses_native_name_collisions`.
3. A crash after lease allocation but before the skill copy must not allocate twice or mark the task prepared prematurely: Task 4, `test_reconcile_finishes_same_lease_before_prepared`.
4. Plugin upgrade/removal must not change existing tasks; changed snapshots/generated artifacts must refuse execution/cleanup: Task 4, `test_task_snapshot_survives_plugin_removal` and existing integrity tests.
5. Orchestrator plugins must not expand reviewer capabilities or leak their lifecycle behavior into worker provisioning: Task 5, native bounded-reviewer regression; final native worker/orchestrator smoke.

## Approved design

### Storage and launch environment

Use `H = store.home(...)` and `R = H/data/omp`. Do not replace `HOME`. Build the same deliberate, non-empty storage environment for every AGD-owned OMP process, dependency probe, plugin command, and native reviewer host:

| Variable | Value |
|---|---|
| `PI_CONFIG_DIR` | `os.path.relpath(R / 'config', effective_home)`; verify joining to effective `HOME` resolves exactly to `R/config` |
| `PI_CODING_AGENT_DIR` | absolute `R/agent` |
| `OMP_PROFILE`, `PI_PROFILE` | `default` |
| `XDG_DATA_HOME` | absolute `R/xdg/data` |
| `XDG_STATE_HOME` | absolute `R/xdg/state` |
| `XDG_CACHE_HOME` | absolute `R/xdg/cache` |

`effective_home` is the child environment's `HOME`, or the account home if unset; do not compute against an unrelated parent process HOME. Reject unsafe/symlinked runtime roots using the repository's existing path-validation conventions. Create private runtime roots with owner-only directory permissions. Run official `omp config init-xdg` in that environment during setup. Do not run migration from global state.

The explicit agent directory deliberately differs from `R/config/agent`. In OMP 18.14 this means per-agent files use `R/agent` and plugin payloads use `R/config/plugins/cache/plugins`; XDG roots still localize other/base runtime categories. **Do not claim `XDG_DATA_HOME` alone captures all OMP paths, or that `PI_CODING_AGENT_DIR` alone moves plugins.** Native project discovery remains literal `<cwd>/.omp`, independent of `PI_CONFIG_DIR`.

OMP's explicit dotenv loader fills missing/falsy variables. Set these controls to non-empty values, including `OMP_PROFILE=default`, rather than unsetting them. Preserve explicit provider API-key environment variables without persisting or printing them. Shell/profile startup files and project dotenv inputs are part of the native smoke boundary.

Persistent layout:

```text
H/data/omp/config/                  OMP user/config namespace, plugin registry/cache
H/data/omp/agent/                   OMP agent settings/auth and per-agent data
H/data/omp/xdg/{data,state,cache}/   other native OMP runtime roots
H/.omp/plugins/                    orchestrator's project registration/runtime links
H/data/tasks/ID/worker-skills/      frozen task skill directories
WORKTREE/.omp/skills/NAME/          selected original-name skill copies
```

Existing explicit central/worker/reviewer transcript directories under `H/data/observability/` stay unchanged. OMP/Herdr/Treehouse executable installation in `~/.local/bin` is unchanged: this change concerns runtime data and plugin payloads, not host binary provisioning.

Expose `bin/agd omp [ARGS...]` as a transparent native OMP launcher in the correct local environment, resolving the executable from validated dependency receipts. This lets a later shell run `bin/agd omp login ...` or native plugin-management commands without remembering environment exports. It does not invent provider configuration or manage an existing central. Add internal `installer shell` dispatch so `install.sh` enters its final nested shell using this same environment helper rather than duplicating environment construction in Bash.

### Official installation and source authority

After compatible OMP installation/probing, run from `H`:

```sh
omp config init-xdg
omp plugin marketplace add DietrichGebert/ponytail
omp plugin install --scope project ponytail@ponytail
omp plugin marketplace add obra/superpowers
omp plugin install --scope project superpowers@superpowers-dev
```

Execute each command with the local environment. Announce the two official plugins before installation; retain the installer's dependency consent/failure cleanup and colored/plain-output behavior. Reuse a matching native registration instead of force-reinstalling it. Refuse a conflicting catalog source; do not overwrite it. Do not run automatic upgrades or enable marketplace auto-upgrades.

Read the native project registry (`H/.omp/plugins/installed_plugins.json`, currently `version: 2`, `plugins[ID]` arrays with `scope`, `installPath`, `version`, optional `enabled`). Missing `enabled` means enabled; only `enabled: false` disables an entry. Validate exactly one applicable project entry for each required plugin, expected identity, enabled status, real payload directory beneath `R`, and declared `package.json` `pi.skills` paths confined to that payload. Follow native registry paths, not hardcoded cache/version directory names. Do not create a competing installation registry. Record source plugin ID/version/relative skill path in task manifests along with existing file hashes.

Native project runtime state is in `H/.omp/plugins/omp-plugins.lock.json` (`plugins[packageName].enabled` and `enabledFeatures`), with project overrides in `H/.omp/plugin-overrides.json` (`disabled`, `features`, `settings`). A contradictory/disabled required package or excluded skill feature must refuse with native re-enable guidance, not silently install again. Read these narrow required-plugin states without recreating the general OMP plugin manager. Do not infer enabled status from summary output of `omp plugin list --json`; do not confuse `project_registries.json`, a cache-cleanup index, with enablement. The reviewer boundary is an empty run-local `.omp/` directory: OMP selects the nearest `.omp/` anchor before falling back to a Git root.

New central skill instructions come from plugins. Remove the copied central Ponytail/Superpowers trees, adapted `ponytail-help`, obsolete upstream worker bundles, and unused related third-party files after their replacements are wired. Keep AGD orchestration/customization skills, Open Relay skills/provenance, and AGD-owned worker adapters. Do not remove a license still needed by retained payloads; carry upstream license evidence into task provenance without rewriting skill files.

### Worker selection, names, and source snapshots

Keep the existing default selection's scope, but use the original names:

- Ponytail: `ponytail`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`, `ponytail-help`, `ponytail-review`.
- Superpowers: `systematic-debugging`, `test-driven-development`, `verification-before-completion`.

The catalog merges installed plugin skills with explicitly configured private custom skills in `config/worker-skills`. A name collision is a refusal, not an override. The general `agd-` prefix restriction is removed; AGD-owned names remain their actual names. `seed_defaults` writes the native-name default profile but no longer copies upstream content into a private catalog. `init` stays offline; attempting a default-profile task before plugins are installed refuses before lease allocation. Explicit `--skill-mode none` permits central-only/empty-skill tasks.

Move `--harness`, `--profile`, `--skill-mode`, and repeated `--skill` selection to `task create`. Harness defaults to `omp`; modes are `profile`, `curated`, and `none`. `none` rejects profile/skill arguments; `curated` requires a nonempty unique set. Remove those mutable selection options from `worker launch` and migrate all callers/docs/tests. Unsupported nonempty Cursor skill provisioning still refuses before allocation; do not implement Cursor discovery here.

Reuse existing task `preparing`/`prepared` phases and fields `harness`, `worker_skill_selection`, `worker_skills`. Before allocation, persist `harness` and a schema-2 selection with `mode`, `profile`, `skills`, and `sources` keyed by selected name; each source records its original absolute path, origin, and existing-style file/mode/hash inventory. Resolve and hash only selected payloads. Allocate/validate the lease, persist worktree/lease identity while still `preparing`, snapshot and materialize the recorded bytes, then attach the completed `worker_skills` manifest and publish `prepared`. Reconcile adopts the same lease and completes this selection without looking up new defaults or versions. An uncertain or changed source leaves the task and lease retained, never silently ready or reallocated.

New skill journals use schema 2. Each record retains `id`, `source`, `snapshot`, `destination`, and the existing file inventory; add `origin` identifying either `{kind: 'plugin', plugin_id, version, relative_path}`, `{kind: 'custom'}`, or `{kind: 'agd'}`. A completed snapshot's validation/replay must not require the original cache path or current plugin registry to exist. Task copies are exact bytes/modes of complete selected skill directories; no plugin package root, hooks, extensions, registry, or node_modules is installed in the worktree by AGD.

Do not expand the selected set by rewriting instructions. Selected Superpowers skills contain `superpowers:<name>` references; provide source-qualified provenance plus explicit native `skill://NAME`/file paths in the existing worker launch prompt. This is lookup guidance, not a renamed skill or registered alias. Verify the actual three selected cross-skill relationships and local supporting-file links in the native smoke.

PR-granted workers preserve the existing adapter-only policy: create those tasks with `--skill-mode none`. A validated `--pr-grant` may then add the original AGD-owned `agd-scoped-pr-relay` snapshot at launch. Reject PR-grant launch of a task with a nonempty frozen ordinary selection; never silently replace its skills. Authorization must be checked before changing the journal or starting Relay.

### Existing installations and bounded reviewer

This is a clean new-install contract, not an automatic migration service. Existing homes with old prefixed default profiles need explicit operator action; setup must identify that state and refuse without modifying profiles, snapshots, credentials, or active sessions. No legacy aliases/default fallback. Historical reports and old task artifacts are not rewritten or deleted. Document the stop/archive/recreate boundary rather than pretending retained old live tasks use the new schema.

The reviewer shares local OMP authentication/model settings through `R/agent`, not `<checkout>/.omp`. Preserve its exact three broker tools, read-only settings, empty/nonreloadable skills, disabled extension discovery, and spawn policy. Give it a dedicated runtime cwd/project boundary under its run directory so it cannot discover the orchestrator's project registry. Verify native project discovery stops at that boundary; flags alone are not evidence of isolation. Reviewer evidence catalog readers must use the same resolved source descriptors as worker provisioning, not assume `config/worker-skills/ID/SKILL.md`.

## File map and dependency order

| Unit | Files and responsibility |
|---|---|
| Local OMP environment/official plugin resolution | New `bin/agd_lib/omp_runtime.py`; extend `store.run` with optional `env` only if needed; `bin/agd` native passthrough/internal installer shell |
| Installer and central cutover | `install.sh`, `bin/agd_lib/installer.py`, `observability.py`, `launcher.py`; remove obsolete central upstream skill copies |
| Native catalog and snapshots | `worker_skills.py`, default profiles, bundled payload removal; retain AGD PR adapter |
| Creation/launch/recovery | `tasks.py`, `workers.py`, `harnesses.py`, CLI parser/dispatch and all direct callers |
| Bounded reviewer | `reviewer.py`, `reviewer_runner.py`, existing reviewer TypeScript input/environment boundaries |
| Evidence/docs | Existing Python/Bun tests; new focused runtime tests and opt-in real plugin smoke; README, operations, compatibility, AGD skills, provenance, changelog |

Tasks 1 → 2 → 3 → 4 establish the shared contracts. Task 5 consumes Tasks 1 and 3; integrate it after Task 4 for one-owner review. Task 6 verifies the combined behavior. Do not parallel-edit these shared lifecycle files. Use semantic references before changing exported APIs where an LSP is available, otherwise inventory callers with repository search.

## Task 1: Establish a local native OMP environment and entrypoint

**Files:** Create `bin/agd_lib/omp_runtime.py`, `tests/test_omp_runtime.py`; modify `bin/agd`, `bin/agd_lib/store.py` if required, `observability.py`, `reviewer_runner.py`, `install.sh`, relevant environment/installer tests and README.

**Interfaces:** `omp_runtime.environment(home: Path, inherited: dict[str, str]) -> dict[str, str]` is pure; `agent_dir(home: Path) -> Path`; `initialize(home: Path, executable: str) -> None` validates/creates owned roots and invokes native `config init-xdg`. CLI `omp` transparently execs the validated executable with forwarded args and `cwd=home`; internal `installer shell` execs the selected interactive shell in that environment. No native profile selection escapes the distro.

- [ ] Write `test_local_environment_wins_over_inherited_storage` with foreign profile/agent/XDG inputs and a HOME different from the parent interpreter's home. Assertions include:
  ```python
  env = omp_runtime.environment(home, inherited)
  assert env['HOME'] == inherited['HOME']
  assert env['OMP_PROFILE'] == env['PI_PROFILE'] == 'default'
  assert Path(env['HOME'], env['PI_CONFIG_DIR']).resolve() == home / 'data/omp/config'
  assert env['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')
  assert env['XDG_DATA_HOME'] == str(home / 'data/omp/xdg/data')
  assert inherited == before
  ```
  Cover symlinked roots and independent homes without changing either caller's environment; assert an unrelated PATH/provider-env sentinel survives.
- [ ] Run `python3 -m pytest tests/test_omp_runtime.py -q`; confirm the missing environment implementation fails the behavioral assertions.
- [ ] Implement the interfaces and CLI dispatch. Apply the environment to OMP-specific probes, managed central/worker construction, reviewer probe/host environment, and installer shell. Do not apply a changed HOME to other tools or make OMP version checks read the user's global agent store. Central forwarded `--profile`/agent-dir controls that defeat the fixed runtime must refuse before reserving a launch.
- [ ] Add native-command forwarding/error-exit checks and update the existing PTY/environment tests; preserve installer rollback and plain/colored output. Use the helper at actual process launch, not just at setup.
- [ ] Run `python3 -m pytest tests/test_omp_runtime.py tests/test_observability.py tests/test_install.py tests/test_launcher.py -q` and `bash -n install.sh`. Run the native disposable path probe described in Evidence below, with hostile dotenv values; observe local config, agent DB, plugin path, default profile and unchanged HOME. No plugin install yet.
- [ ] Document `bin/agd omp login ...` as an optional user-owned native command, no secret copying. Commit as `feat: isolate native OMP state within each distro`.

## Task 2: Install official orchestrator plugins and remove central copies

**Files:** `omp_runtime.py`, `installer.py`, `bin/agd`, `install.sh`, `tests/test_omp_runtime.py`, `tests/test_installer.py`, `tests/test_install.py`, `.gitignore`, README/provenance/changelog; remove copied central Ponytail/Superpowers directories and unused related third-party payloads after dependency inventory.

**Interfaces:** `omp_runtime.install_plugins(home: Path, executable: str) -> dict[str, dict]`; `installed_plugins(home: Path) -> dict[str, dict]` returns only validated required project installs, each with `id`, `version`, `path`, and `skill_roots` (resolved Paths). Native registry and enablement files remain authoritative; no AGD replacement registry. Installer calls installation after binary setup and before success/shell handoff.

- [ ] Write `test_plugin_sources_refuse_unsafe_or_ambiguous_registration`: native-shaped registry fixtures with outside-distro install paths, symlink escapes, duplicate project entries, missing payloads, `enabled: false`, malformed schema, escaped `pi.skills` roots, and contradictory runtime/override enablement must refuse. Missing `enabled` and explicit `enabled: true` are both valid. Matching official project records return original payload paths/versions; differently named catalog sources refuse rather than silently replacing registration.
- [ ] Run `python3 -m pytest tests/test_omp_runtime.py -q`; confirm missing resolver/installer behavior fails.
- [ ] Implement the official command sequence and strict registry resolution. Match the recorded marketplace publisher before reusing a catalog. Re-running setup on a matching completed native install must not force-upgrade it. Preserve subprocess errors and do not announce success after a partial install. Keep generated `/.omp/plugins/` out of Git while retaining `.omp/extensions/agd-primary.ts`.
- [ ] Make installation compatibility checks require OMP 18.14.0+ and the actual native plugin capabilities used here. Keep models/login probes read-only and inside the local environment. Leave host-binary receipts in their existing format; plugin versions come from native records.
- [ ] Remove vendored central upstream skills instead of loading both copies and plugins. Keep AGD and Open Relay skills. Remove obsolete help/provenance assertions and tests that only pin copied source text; retain actual central-supervision regressions.
- [ ] Run `python3 -m pytest tests/test_omp_runtime.py tests/test_installer.py tests/test_install.py tests/test_central_backends.py -q` and `bun test tests/primary.test.ts`. Exercise one disposable official marketplace install using real network/native tooling, verify local payload files and project scope, and capture versions without credentials. Verify a simulated second-plugin failure still triggers owned-clone cleanup without altering global OMP state.
- [ ] Update README/provenance/changelog with native installation, trusted executable-plugin boundary, restart requirement, and explicit nonmigration. Commit as `feat: install official project-scoped orchestrator plugins`.

## Task 3: Resolve native-name skill sources without transforming upstream files

**Files:** `worker_skills.py`, `tests/test_worker_skills.py`, shared test fixtures in `tests/support.py`, `bin/agd` init behavior, `bundled/worker-skills/`, `third_party/PROVENANCE.md`, `.agents/skills/customize/SKILL.md`, README/operations.

**Interfaces:** Extend `worker_skills.catalog(home)` entries to `{id, description, source, origin}`; `source` is a JSON-safe absolute directory string, `origin` uses the spec above. Keep `resolve(home, *, profile=None, skill_mode='profile', skills=())`, adding explicit `none` and the schema-2 source inventories described above. `seed_defaults(home)` writes the nine canonical-name selections but does not duplicate upstream payloads into `config/worker-skills`.

- [ ] Add a reusable deterministic plugin fixture writer to `tests/support.py` using native registry/enablement shapes and local payloads. No network or user-global plugin state in permanent tests. Write `test_catalog_accepts_original_upstream_metadata` using representative exact Ponytail folded descriptions/extra scalar fields and Superpowers plain fields. Assert `row['id'] == 'ponytail'`, the source `SKILL.md` bytes are unchanged, and supporting resources remain inside the selected directory.
- [ ] Add `test_catalog_refuses_native_name_collisions` for two plugins, plugin/custom catalog duplicates, directory/frontmatter mismatch, traversal, symlinks and special files. Remove only the blanket upstream `agd-` namespace restriction; retain structural validation.
- [ ] Run `python3 -m pytest tests/test_worker_skills.py -q`; confirm the current parser/prefix logic rejects the real upstream forms.
- [ ] Implement metadata extraction for the observed safe subset: required plain ID name; plain/quoted or folded/literal block description; preserve and ignore unrelated scalar metadata such as `argument-hint`/`license`; reject duplicate/malformed required fields or unsupported structures clearly. Do not claim full YAML support or execute YAML tags. Never rewrite source bytes to make validation pass.
- [ ] Resolve plugin skill roots from Task 2, combine with user-authored custom sources without precedence tricks, and update default profiles. Remove the nine transformed upstream bundles and their seeding path; retain `agd-scoped-pr-relay`. Existing incompatible upstream-prefixed profiles must fail explicitly, not silently select aliases or no skills.
- [ ] Update existing snapshot/provenance code to accept these source descriptors while preserving file inventory checks. Run worker-skill tests and a native `omp read skill://NAME` check against copied real installed skill directories in a disposable worktree, including `test-driven-development/writing-good-tests.md` and `systematic-debugging/root-cause-tracing.md`.
- [ ] Update catalog/customization docs and commit as `refactor: source worker skills from native plugin installations`.

## Task 4: Prepare frozen worktree skills during task creation

**Files:** `tasks.py`, `worker_skills.py`, `workers.py`, `harnesses.py`, `bin/agd`; all callers in tests (`test_tasks`, `test_workers`, `test_worker_skills`, `test_delivery`, `test_backends`, PR/Relay and reviewer fixtures); AGD tasks/orchestrate/customize skills and operations examples.

**Interfaces:**
- `tasks.create(home, task_id, project_id, brief, base=None, kind='ship', *, harness='omp', profile=None, skill_mode='profile', skills=()) -> dict`.
- `tasks.reconcile_allocation(home, task_id)` finishes the same lease and frozen selection, never fresh defaults.
- `workers.launch(home, task_id, selected_backend=None, unattended=False, approval_id=None, pr_grant=None)` reads harness and skill selection from the task.
- Keep `worker_skills.prepare/ensure/verify/remove`; change journals to schema 2 and provenance validation, using snapshots rather than mutable sources for completed manifests. `prepare` both freezes and materializes during creation; it is not a delayed launch step.

- [ ] Write `test_create_materializes_native_skills_before_prepared` using a real temporary Git/Treehouse lab and synthetic native plugin payloads. After `create`, assert phase `prepared`, original-name paths already exist under `.omp/skills`, each file byte/mode equals its snapshot, no `agd-` aliases exist, and generated files do not dirty the worktree.
- [ ] Write `test_reconcile_finishes_same_lease_before_prepared`: interrupt after allocation and before snapshot completion; assert the record retains the same attempt/holder/lease, remains `preparing`, cannot launch/report ready, and reconcile completes that allocation without calling `treehouse get` again. If a source changes before the first copy or during a partial copy, refuse against the persisted source inventory and retain evidence instead of adopting the changed bytes.
- [ ] Write `test_task_snapshot_survives_plugin_removal`: create task A, change/remove source registration and payload, then launch/relaunch or restore a missing generated copy from A's snapshot; original hashes must remain. Task B uses a new validated install or refuses if unavailable. Modified snapshots/worktree copies and source collisions must fail before endpoint creation.
- [ ] Run `python3 -m pytest tests/test_tasks.py tests/test_worker_skills.py tests/test_workers.py -q`; observe failures at creation-time materialization/frozen replay contracts.
- [ ] Move CLI selection to task creation, persist selection/harness before allocation, and publish `prepared` only after successful skill journal/copy completion. Recover owned pending copies only when their recorded source/inventory still proves the expected bytes; preserve ambiguous files. Update all direct API and CLI consumers; remove old launch selection flags rather than forwarding them as shims.
- [ ] Keep launch/relaunch source-independent after snapshot completion, with existing endpoint/task concurrency guards. For PR workers, require creation with `--skill-mode none`; validate the grant before adding `agd-scoped-pr-relay` or starting Relay. Cover valid adapter-only launch, nonempty-selection refusal, invalid grants, retries and relaunch without replacing ordinary snapshots.
- [ ] Reuse cleanup/exclusion machinery with canonical paths and schema-2 records. Ready/land/publication still reject modified or committed generated skill files; cleanup only removes exact owned copies and preserves unrelated names. Retain task snapshots/provenance after lease return.
- [ ] Run `python3 -m pytest tests/test_tasks.py tests/test_worker_skills.py tests/test_workers.py tests/test_delivery.py tests/test_relay.py tests/test_relay_reviews.py -q`. Exercise actual task create → native skill read → stopped-worker proof → task cleanup in a disposable project; verify one returned lease and no orphaned worktree/process. Commit as `feat: snapshot native worker skills at task creation`.

## Task 5: Keep the bounded reviewer local and plugin-independent

**Files:** `reviewer.py`, `reviewer_runner.py`, `bin/agd`, `lib/agd-reviewer.ts`/host only where input contracts require; `tests/test_reviewer.py`, `tests/test_reviewer_lifecycle.py`, `tests/reviewer_bridge.py`, `tests/reviewer.test.ts`, operations/compatibility.

**Interfaces:** Reviewer `_catalog` consumes Task 3 source descriptors. Capability probing/host spawn consume Task 1 environment. Reviewer payload `agentDir` is `omp_runtime.agent_dir(home)`; SDK/process cwd is a private run-local runtime directory with an explicit empty native project boundary, not the distro project. Keep the existing broker and report schemas.

- [ ] Update catalog tests to hash/read the installed plugin's original `SKILL.md` and frozen task provenance. Assert reviewer operations leave native registries, plugin payloads, worker profile/snapshot bytes and owner state unchanged; missing/unsafe sources produce the existing explicit coverage-unavailable record.
- [ ] Extend native reviewer regression with an orchestrator plugin discovery sentinel. Assert exactly `reviewer_excerpt`, `reviewer_search`, `reviewer_submit`; no plugin tools, injected bootstrap, command discovery or outside-packet reads. Check native project resolution of the private boundary, not merely a mocked SDK flag. The reviewer must still use the local OMP settings/auth store, not a fresh unrelated database.
- [ ] Run `python3 -m pytest tests/test_reviewer.py tests/test_reviewer_lifecycle.py -q` and `bun test tests/reviewer.test.ts` to observe failures caused by old catalog paths/agentDir/cwd.
- [ ] Update only those source/environment/cwd paths. Preserve read-only Settings, exact tool registry, `skills: []`, `skillsReloadable: false`, `disableExtensionDiscovery: true`, spawn prohibition, model limits and explicit session directories. Ensure timer entrypoints derive the same environment without relying on interactive shell exports; use Bun's `--no-env-file` for direct host/probe launches where supported, without mistaking it for disabling OMP's own dotenv loader.
- [ ] Run Python reviewer/observability tests and both native reviewer tests (`bun test tests/reviewer.test.ts tests/reviewer-telemetry.test.ts`). Missing importable native OMP modules are a prerequisite failure, not permission to substitute mock proof or claim isolation passed. Do not repair unrelated reviewer/provider behavior here.
- [ ] Update reviewer operating instructions and commit as `fix: preserve bounded reviewer isolation with native plugins`.

## Task 6: Prove the installed experience and finish the cutover

**Files:** Add opt-in `tests/smoke_native_plugins.py` following existing stdlib/PTy smoke conventions; update `tests/test_backends.py` live entrypoints, README, operations/compatibility, AGD procedure skills, provenance, changelog. No new test framework or background service.

**Interfaces:** `python3 tests/smoke_native_plugins.py --backend herdr|tmux` uses a disposable distro/project, official marketplace installs, explicit owned backend identity, and guaranteed teardown. Downloads are opt-in via this explicit smoke command, not part of pytest collection. Record actual versions/paths and bounded evidence, never credentials.

- [ ] Implement a runnable acceptance check for the following actual native flow, reusing the project's existing PTY/process cleanup helpers rather than writing another installer:
  1. Install OMP dependencies and both official plugins into a fresh temporary distro with conflicting caller profile/XDG values and dotenv sentinels. Verify all OMP-owned artifacts stay local and global plugin/auth/config sentinel bytes stay unchanged.
  2. Start an actual orchestrator with `agd start`; verify the AGD watcher and both upstream extension lifecycles, including Ponytail command/mode and Superpowers bootstrap, using provider-free RPC/native fixtures where possible. Discovery/listing alone is insufficient for hook execution evidence.
  3. Create a temporary task, verify nine canonical skill copies immediately, then launch a real worker. Read original-name skills and supporting resources through native OMP discovery; verify no AGD-installed plugin runtime hooks were inherited into that worktree.
  4. Verify selected Superpowers cross-skill references resolve using the supplied native names/file paths without editing or aliasing upstream files.
  5. Remove/change an installed source in the disposable environment and prove the existing task still uses its snapshot; a new task must observe the new validated source or refuse.
  6. Complete the bounded reviewer check from Task 5; no autonomous AI task-completion claim unless such a task was actually executed.
  7. Stop exact owned workers/central/daemon children, return/destroy disposable leases through AGD/Treehouse, delete only the owned backend session, remove the temporary distro, and check no process recreates it across at least four former two-second heartbeat intervals.
- [ ] Run the new smoke for both supported backends. Do not touch a user's live session or stop a default Herdr/tmux server. Missing tmux/native reviewer modules must be reported and resolved as prerequisites before claiming the relevant backend/role passes.
- [ ] Run `python3 -m pytest tests -q`, `bun test tests/primary.test.ts tests/reviewer.test.ts tests/reviewer-telemetry.test.ts`, and `bash -n install.sh` once on the integrated branch. Treat previously recorded environment failures separately and accurately; fix regressions introduced by this change, never weaken assertions to get green.
- [ ] Audit affected callers/docs for old launch selection flags, transformed upstream IDs, hardcoded worker catalog paths, and assumptions that `HOME/.omp` is authoritative. Historical design/plan documents stay historical; active instructions must describe the new contract. Confirm no generated plugin registrations, auth data, cache payloads or smoke homes are tracked.
- [ ] Update the compatibility evidence with exercised versions/scenarios and any explicit untested boundary. Remove throwaway investigation scaffolds; retain the runnable native smoke. Run `git diff --check` and commit as `test: verify distro-local plugins and native worker skill lifecycle`.

## Evidence and acceptance boundaries

Planning investigation read OMP 18.14.0 `dirs.ts`, `env.ts`, marketplace manager/cache/loader and official docs. A disposable native probe ran `omp config init-xdg`; with the environment above and an empty local plugin directory, `omp plugin doctor --json` reported that exact `R/config/plugins` directory, and `omp plugin list --json` returned no inherited global plugins. A first probe incorrectly expected doctor to print the path of a missing directory; it only reported `Not created yet`. The corrected probe verified resolution without installing anything. Temporary directories were removed.

This proves directory selection, not a complete plugin installation or lifecycle execution. Task 6 is the required end-to-end proof. No production runtime code or existing OMP installation changed during planning.

References:
- [Official OMP plugin installation/scope/restart docs](https://omp.sh/docs/plugins)
- [Official marketplace catalogs and source pinning](https://omp.sh/docs/marketplace)
- [OMP 18.14 directory resolver](https://github.com/can1357/oh-my-pi/blob/v18.14.0/packages/utils/src/dirs.ts)
- [OMP 18.14 environment loading](https://github.com/can1357/oh-my-pi/blob/v18.14.0/packages/utils/src/env.ts)
- [Native marketplace manager](https://github.com/can1357/oh-my-pi/blob/v18.14.0/packages/coding-agent/src/extensibility/plugins/marketplace/manager.ts)
- [Ponytail official OMP installation](https://github.com/DietrichGebert/ponytail/blob/main/INSTALL.md#oh-my-pi-omp)
- [Superpowers marketplace](https://github.com/obra/superpowers/blob/main/.claude-plugin/marketplace.json)

## Completion and execution handoff

Acceptance is all six tasks plus actual installed orchestrator/worker/reviewer evidence, not just a clean test run or files copied into a worktree. Every native skill retains its upstream name and bytes; all relevant callers/tests/docs use the new creation-time contract; no duplicate central copies or upstream-name aliases remain. Publish/merge only with separate user authorization.

Recommended execution: **Native**, one integration owner executing this plan sequentially, with one independent whole-branch review before publication. Environment, catalog, task journals, worker launch and reviewer evidence are tightly coupled; parallel implementation would create unnecessary shared-file/interface conflicts. Subagent-driven execution remains an option if the user prefers a fresh implementer/reviewer gate per task.
