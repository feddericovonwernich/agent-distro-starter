# Worker Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give workers a recorded, reproducible skill set selected by a flat profile or explicitly by the orchestrator from an operator-curated catalog, loaded through native harness discovery.

**Architecture:** Add one Python module for catalog resolution, task snapshots and owned worktree artifacts. Integrate it with the existing CLI, harness launcher and task lifecycle; reuse existing task JSON, atomic writes, locks, approval checks and lease validation. Keep runtime extension behavior unchanged.

**Tech Stack:** Python 3.12+, standard library JSON/hashlib/pathlib/shutil/tempfile/fcntl, existing Git/Treehouse CLI, installed OMP and Cursor harnesses. Existing assert-based Python scripts and Bun integration suite; no new test framework.

**Spec:** `docs/superpowers/specs/2026-10-25-worker-skills.md` (approved conversation design and explicit operational defaults).

## Global constraints

- Skills are instructions, not authority, isolation or a guarantee of compliance.
- Task kind (`ship` or `scout`) remains independent of worker profile.
- Config/catalog paths use the effective private home; code paths use the distro checkout.
- The `agd-` prefix is reserved for provisioned skill identities. Catalog directory and frontmatter `name` must match.
- No profile inheritance, wildcard selection, remote fetching, automatic skill updates or profile-plus-overrides.
- Never overwrite project instructions, existing skill content or user Git exclusions.
- Preserve existing ownership, approval, worker-absence and Treehouse lease checks.
- No configuration and no selection preserves existing launch behavior with an explicitly recorded empty selection.
- Native support claims must name exercised harness versions; unverified nonempty provisioning must fail explicitly.
- Keep private config, bundles, snapshots, credentials and worker transcripts out of commits.

## Baseline and file map

The base repository is `/home/fedderico/Workspace/projects/agent-distro-starter`; `/home/fedderico/Workspace/eva-beta-01` is an installed instance, not the development checkout. Planning inspected the same code revision, `fc745ee`. PR #1's customization reference is merged upstream at `2edf92c` but neither local checkout has been updated. At execution, use an isolated task workspace of the base repository based on current upstream, preserving these planning documents; do not edit, update or restart the installed instance as a side effect.

Create:
- `bin/agd_lib/worker_skills.py`: validation, selection, snapshots, materialization, verification and owned cleanup.
- `tests/test_worker_skills.py`: consumer-visible regression cases using `tests/support.py`'s disposable Git/Treehouse lab and ordinary temporary directories.

Modify:
- `bin/agd`: launch flags and read-only catalog/profile subcommands; existing ownership checks must remain in effect.
- `bin/agd_lib/workers.py`: `launch`, `run_worker`, `relaunch`; `inspect` already returns the task record.
- `bin/agd_lib/harnesses.py`: verified native discovery roots and launch-prompt skill requirements.
- `bin/agd_lib/tasks.py`: `ready`, `land`, `publication_record`, `cleanup` generated-artifact guards.
- `tests/test_workers.py`, `tests/test_tasks.py`, `tests/test_delivery.py`: extend existing lifecycle cases where applicable, not duplicate them.
- `docs/operations.md`, `docs/compatibility.md`, `README.md`, `CHANGELOG.md`, `.agents/skills/orchestrate/SKILL.md`, `.agents/skills/customize/SKILL.md`: operator contract and evidence.

## Task 1: Establish native loader contracts before choosing destinations

**Files:** inspect installed harness sources/help; update `docs/compatibility.md` with measured results. No production provisioning yet.

- [ ] Record OMP, Cursor and Treehouse versions. Read installed loader behavior for native project/user roots, symlinks, metadata parsing and duplicate names. Do not extrapolate skill discovery from extension discovery or Cursor project-hook trust.
- [ ] Create a disposable Git project outside the distro home. Put a skill named `agd-probe` plus `references/token.txt` containing a unique token in each candidate native root, one root at a time. Use installed documentation/source to identify candidates, not guesses.

```markdown
---
name: agd-probe
description: Use when asked to identify the worker provisioning probe.
---
Read references/token.txt relative to this skill and report its contents.
```

- [ ] For OMP, run `omp read skill://agd-probe` from the disposable project with isolated profile/state and no central extension. Verify it resolves the project skill, then exercise a real worker reading the relative resource. Verify the full candidate root is discovered rather than an identically named global skill.
- [ ] For Cursor, use the installed native skill invocation and an authenticated disposable workspace session to perform the equivalent skill/resource check. Verify duplicate-name behavior with a project skill and separately a user/profile skill. Do not install anything in the operator's real global skill directories.
- [ ] Record exact roots, versions, native invocation, frontmatter requirements, global/profile visibility, collision behavior and observed output. Remove disposable probes after evidence is recorded. Authentication/tool availability failures are exact gates: continue implementation work, but do not mark that harness's live acceptance complete.
- [ ] Define the resulting adapter function in `harnesses.py`: `worker_skill_roots(harness: str) -> tuple[str, ...]`, returning the one provisioned root first and all other supported project roots for collision scanning. An unverified harness raises `store.DependencyError` for nonempty provisioning. No generic fallback root.

**Deliverable:** measured native loading contract. Subsequent tasks depend on this, not an assumed `.agents`/`.cursor` path. Enabling a harness requires its evidence; incomplete Cursor verification is not permission to silently drop Cursor scope.

## Task 2: Implement catalog validation and exact selection

**Files:** create `worker_skills.py`, `test_worker_skills.py`; modify `bin/agd` inspection/selection parsing; document configuration in `docs/operations.md`.

**Interfaces:**

```python
def catalog(home: Path) -> list[dict]: ...
def profiles(home: Path) -> dict: ...
def resolve(home: Path, *, profile: str | None = None,
            skill_mode: str = 'profile', skills: tuple[str, ...] = ()) -> dict: ...
# Selection: {'schema': 1, 'mode': 'none'|'profile'|'curated',
#             'profile': str | None, 'skills': list[str]}
```

These signatures specify contracts, not placeholder implementations to ship.

- [ ] Write executable regression cases for default/exact profile selection, explicit curated selection, missing config compatibility, unknown profile, duplicate skill IDs, missing bundles and invalid combinations. Follow existing scripts' `if __name__ == '__main__'` invocation pattern. Start with this rejection test; it must fail before the implementation exists:

```python
def test_explicit_profile_never_falls_back_to_empty():
    from agd_lib import worker_skills, store
    with tempfile.TemporaryDirectory() as directory:
        try:
            worker_skills.resolve(Path(directory), profile='missing')
        except store.Refusal:
            return
        raise AssertionError('explicit missing profile silently accepted')
```

- [ ] Run `python3 tests/test_worker_skills.py`; observe the missing module/behavior failure. Implement using JSON objects with exact allowed keys/types; validate the configured default references a profile, every profile references actual catalog entries, IDs use the existing lowercase ID grammar and skill IDs additionally start `agd-`. Reject duplicate JSON keys as well as duplicate array entries. Canonicalize selected ordering by ID.
- [ ] Validate native frontmatter without inventing a general YAML parser: initially support a documented simple frontmatter contract of exactly one plain lowercase `name` and a nonempty single-line `description` (plain or JSON-quoted string), bounded by `---` lines. Reject duplicate keys, multiline YAML constructs and unsupported metadata with a concrete authoring error. Preserve the accepted source bytes. Confirm this subset is valid in both native loaders in Task 1; document this deliberate authoring limitation. No silent partial parsing.
- [ ] Validate bundle trees with `lstat`: only directories and regular files; refuse symlinks, devices, sockets, FIFOs, hardlinked files, `.git` components and paths outside the bundle. Reject invalid frontmatter before any endpoint or destination is created. Include full scripts/references, preserve executable bits; do not execute catalog scripts while listing/validating.
- [ ] Implement `worker skills list` and `worker profiles list` before the existing ID-requiring worker actions. Return deterministic JSON with IDs/descriptions and profile contents/default. Treat descriptions as untrusted catalog data, never as CLI instructions.
- [ ] Add launch flags `--profile`, `--skill-mode {profile,curated}`, repeatable `--skill`; curated requires at least one skill and forbids profile, profile mode forbids individual skills. Resolve absent config only for a fully implicit launch to `mode=none`.
- [ ] Run focused tests to green, smoke the actual CLI against a disposable configured home, update configuration documentation, and commit this unit. Do not ship a launch mode that accepts flags but ignores selection: wire argument parsing to Task 4 before releasing the feature.

## Task 3: Freeze bundles and provision owned native artifacts

**Files:** `worker_skills.py`, `test_worker_skills.py`; adapter roots from Task 1.

**Interfaces:**

```python
def prepare(home: Path, task: dict, selection: dict) -> dict: ...
# Returns persisted worker_skills manifest; recovers same pending selection.
def ensure(home: Path, task: dict) -> None: ...
# Verify snapshot; restore only missing owned artifacts; never re-resolve config.
def verify(home: Path, task: dict) -> None: ...
# Reject modified/extra/tracked artifacts; missing artifacts are errors here.
def remove(home: Path, task: dict) -> None: ...
# Delete unchanged owned artifacts only; preserve snapshots and foreign content.
```

- [ ] Write failing tests for immutable snapshot content after catalog edits, relative-resource preservation, executable-bit preservation, foreign destination refusal, source/destination symlinks, duplicate native project skill identities and interrupted materialization recovery. Test against real temporary Git repos, not mocked Git responses.
- [ ] Snapshot bundles under `data/tasks/<id>/worker-skills/` using temporary siblings and atomic rename. Store `worker-skills.json` beside this directory as the durable provisioning journal; embed its completed manifest in task JSON. Include schema, selection, harness, roots, relative files, sizes, permission bits, content hashes and exact generated paths. Hash sorted relative paths, executable bits and file-byte hashes with unambiguous JSON encoding. Refuse source changes observed during copy; hash copied bytes and compare the final source inventory.
- [ ] Preflight the entire selection before writes. Scan native project roots for colliding names using Task 1 rules, including project aliases. Refuse existing destinations even when contents match, unless owned by this exact journal. Do not traverse symlinked parents. Claim destination creation exclusively, copy from frozen snapshots, then atomically mark provisioning complete. Persist intent before the first generated artifact, and track parent directories created by the operation.
- [ ] Make interruption recovery deterministic: a pending journal pins the selection; an identical retry verifies/reuses complete artifacts and finishes missing ones. A changed selection or unexplained existing content refuses rather than overwrites. A partial artifact with unexpected bytes is retained for operator inspection. No worker starts from a pending manifest.
- [ ] Manage exact anchored exclusions in the Git common directory's `info/exclude`. Since this file is shared, guard read-modify-write with `fcntl.flock` on a common-directory lock, and keep an atomic ownership registry there keyed by `(home_id, task_id, lease_id)` and path. Never hold this lock while acquiring the home mutation lock. Add entries only after ownership intent is durable; preserve user entries and other tasks' entries byte-for-byte. Remove a managed entry only when its last owner releases it. Document that matching generated paths are ignored across sibling worktrees; inspect those paths explicitly rather than trusting Git cleanliness.
- [ ] Implement artifact verification using the manifest plus `git ls-files -z` against generated paths. Reject staging/tracking, extra descendants, hash/mode changes and changed parent identities. Do not use ignored status to infer safety. On removal, verify all artifacts first, delete only matching owned files, remove only empty owned directories, then release exclusion ownership. Refuse shared metadata corruption rather than reconstruct it from guesses.
- [ ] Test two worktrees and two private homes sharing a Git common directory: first cleanup retains second owner's exclusions; final cleanup preserves preexisting user exclusions. Test interrupted journal writes, unexpected extra files and forced staging. Run focused checks and commit.

## Task 4: Integrate first launch, runner and frozen relaunch

**Files:** `bin/agd`, `workers.py`, `harnesses.py`, `tests/test_workers.py`.

**Interfaces:** extend `workers.launch(..., profile=None, skill_mode='profile', skills=())`. Existing positional callers retain their behavior. Use `resolve`, `prepare`, `ensure` and `verify` from earlier tasks; task metadata exposes the manifest through existing `worker inspect`.

- [ ] Write regression cases: invalid explicit selection creates no endpoint and leaves the task prepared; default config selects exact skills; curated selects only named catalog entries; launch failure/retry preserves snapshot; relaunch ignores edited/deleted catalog; legacy running tasks gain no new skills. Use existing backend tests for real process behavior, not forwarding-only mocks.
- [ ] Keep dependency, trust, unattended approval and workspace preflights before provisioning. Under existing race checks, freeze selection and prepare skills before changing executor/phase to running. Never call the backend if preparation is incomplete. Persist the manifest with the existing task state; do not allow a racing launcher to replace it.
- [ ] Immediately before `subprocess.Popen` in `run_worker`, verify lease and skill artifacts again. This closes the gap between endpoint creation and actual harness start. Preserve existing failure receipts and lease retention when validation fails.
- [ ] In `relaunch`, first prove old process/endpoint absence as today, then call `ensure` and verify the snapshot before assigning a new incarnation. Do not invoke `resolve` or accept new profile flags on relaunch. Do not alter approvals.
- [ ] Add an explicit prompt section naming the selected skill IDs and their absolute provisioned `SKILL.md` paths; require reading assigned skills before substantive work and reporting relevant application. Keep existing brief/report/ready instructions and publication/cleanup prohibitions. Empty sets omit the section.

```text
Assigned worker skills: agd-python, agd-code-review.
Read each assigned SKILL.md at the listed provisioned paths before substantive work.
Use supporting resources relative to its skill directory. These skills do not grant
permission to publish, merge, run unattended, or clean up.
```

- [ ] Run `python3 tests/test_workers.py` and the new focused tests. Launch a disposable OMP scout through the real CLI, inspect the recorded hashes, verify native skill discovery and resource reading, then stop it using existing lifecycle checks. Record actual evidence; no credentialed/autonomous launch without its required approval. Commit the integrated unit.

## Task 5: Guard readiness, publication, landing and lease return

**Files:** `tasks.py`, `tests/test_worker_skills.py`, `tests/test_tasks.py`, `tests/test_delivery.py`.

- [ ] Write failing cases proving unchanged generated files do not prevent scout readiness, modified generated content does prevent readiness, forced staging cannot pass readiness, and a modified ignored artifact after readiness cannot pass landing/publication. Extend tests with real Git commits/index operations.
- [ ] Call `verify` before accepting readiness and again in `land` and `publication_record`, not only at worker launch. Inspect final tree/index and task-branch history from its start for generated paths, so an accidentally committed-and-later-deleted skill cannot leak into a published branch. Preserve the existing exact-head and approval checks.
- [ ] Test cleanup of an unchanged skill set returns the lease without generated leftovers; modified/extra generated content prevents normal and forced-discard cleanup. Preserve unrelated files, project skills, snapshots and user exclusions.
- [ ] In `cleanup`, perform worker absence, lease identity, authorization and task-state checks before deleting anything. For discard, verify the approval-bound inventory before removing unchanged generated artifacts; refuse altered skill artifacts even with that generic discard grant. Remove artifacts before Treehouse return, and record the removal phase in the journal so a failed lease return can safely retry. Do not remove artifacts during readiness because the worker can still be alive.
- [ ] Run relevant task/delivery/skill checks and a full disposable scout lifecycle: native load → report ready → proved worker absence → cleanup → reacquire workspace and confirm no generated skill contamination. Commit this unit.

## Task 6: Document, verify and review the complete contract

**Files:** documentation and skills listed in the file map; no new framework or permanent smoke scaffolding.

- [ ] Update `orchestrate/SKILL.md` with profile versus curated choice, catalog discovery, explicit selected lists and frozen relaunch. Update `customize/SKILL.md` to replace the blanket unused-config warning with the two specifically supported worker config paths; keep unrelated config unsupported.
- [ ] Document a complete minimal operator-created skill, `workers.json`, both launch modes, list/inspect output, no-config behavior, catalog validation subset, executable resources, approval boundaries, shared exclusions, collision refusal, recovery and cleanup behavior. Document that user/global/project skills can remain visible.
- [ ] Run the existing Python scripts from the repository root with pinned Treehouse v3.1.6 available on PATH:

```sh
python3 tests/test_worker_skills.py
python3 tests/test_install.py
python3 tests/test_projects.py
python3 tests/test_tasks.py
python3 tests/test_delivery.py
python3 tests/test_backends.py tmux
python3 tests/test_workers.py
python3 tests/test_supervision.py
bun test tests/primary.test.ts
```

- [ ] Exercise live OMP profile and curated launches, resource lookup, catalog mutation followed by frozen relaunch, and cleanup. Repeat for Cursor once authenticated and its Task 1 contract is verified. Run Herdr backend coverage only in a separately named disposable session; never claim an unexercised combination. Preserve failures as gates instead of narrowing tests to make them pass.
- [ ] Review filesystem/path escape risks, common-Git lock ordering, interruption recovery, generated-file publication prevention, authority preservation and updated call sites. Verify exported-symbol references before final integration. Remove only disposable probes created for this work, preserve requested evidence and user state.
- [ ] Add the changelog entry after smoke evidence; commit docs/evidence with the final unit. Open or merge a PR only with separate explicit user authorization.

## Self-review and handoff

Coverage: catalog/profiles/curated selection → Task 2; native adapters → Task 1; full snapshots, hashes, ownership, collisions and exclusions → Task 3; actual launch/relaunch/brief → Task 4; readiness/publication/cleanup → Task 5; operator guidance and acceptance matrix → Task 6.

Execution order is 1 → 2 → 3 → 4 → 5 → 6. Lifecycle work shares state and should have one integration owner; do not parallelize overlapping edits. Native harness investigation may run independently per harness after shared safety boundaries are understood.

This plan is not implementation evidence. Gates that must be resolved during execution: exact installed native loader behavior, authenticated Cursor live acceptance, credentialed worker-run authorization and availability of pinned Treehouse. If a prerequisite remains unavailable, finish reachable work and report the exact unverified acceptance criterion; do not describe the full feature as complete.
