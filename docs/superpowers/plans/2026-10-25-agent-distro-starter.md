# Agent Distro Starter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a central omp distro home that manages projects and task worktrees, runs omp/Cursor workers through tmux/Herdr, and supervises them in the background without losing work or exceeding landing authority.

**Architecture:** A small Python-standard-library command tool owns durable records and guarded operations; existing Git, Treehouse, tmux, and Herdr binaries do the underlying work. Two concrete terminal adapters and two concrete harness integrations share task records, not a plugin framework. One tracked omp TypeScript extension owns the monitoring process and delivers durable events into the central conversation.

**Tech Stack:** Python 3.12+, Git, Treehouse v3.1.6, tmux 3.4, Herdr 0.8.2, omp 18.3.2, Cursor Agent CLI, TypeScript through omp's extension loader; Bun for extension contract tests. No pip/npm runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-25-agent-distro-design.md`

## Global Constraints

- Central harness: omp only.
- Worker harnesses: omp and Cursor Agent CLI.
- Worker backends: tmux and Herdr, independently selectable from the harness.
- Stable project clones under projects/ with registration, listing, inspection, guarded refresh, and safe removal.
- Isolated task worktrees for central-agent and worker code changes.
- Background monitoring, durable actionable events, omp wake delivery, and restart reconciliation.
- Completed implementation tasks produce a committed task branch and a report with verification evidence and limitations.
- Local merge and PR publication each require explicit user authorization. Publication does not authorize PR merge.
- Keep work until landing is confirmed or explicit discard is authorized.
- A configured default is overridden only by an explicit task choice. Dependency, authentication, or backend failure must not silently select another harness/backend.
- No implementation claim is complete until all four real worker/backend combinations and the real central omp extension have been exercised.
- No automatic package installation, login, global configuration changes, publication, or destructive cleanup during implementation verification.

## Execution decisions and prerequisites

This is one integrated deliverable with three reviewable stages: workspace lifecycle (tasks 1–3), workers (tasks 4–6), supervision and release proof (tasks 7–9). Intermediate stages are useful independently but are not substitutes for the full spec. Execute dependent tasks in order. Backend tasks 4 and 5 may run independently after their shared contract is settled: each owns its adapter, while the integration owner alone edits the shared backend test and compatibility documentation.

Use Python for filesystem transactions, JSON, subprocess argument lists, and POSIX locks rather than reproducing these in shell. Use TypeScript only where omp requires an extension. Linux is the measured development host; do not advertise macOS or Windows support without running their checks. No Windows-specific implementation is requested.

Observed during planning (2026-10-25):

| Component | Evidence | Implementation consequence |
|---|---|---|
| omp | `omp --help` reports 18.3.2 | Target this release; live-check extension events before marking supported. |
| tmux | `tmux -V` reports 3.4 | Available for isolated socket smoke checks. |
| Herdr | `herdr --version` reports 0.8.2 | Available; use only a deliberately named lab session, never the focused/default session. |
| Treehouse | Installed v2.3.0; `get --help` lacks `--base` and `--branch` | Refuse this version for task acquisition. Seek approval for a separate pinned v3.1.6 test binary; do not silently upgrade the user's copy. |
| Cursor | Neither `cursor-agent` nor legacy `agent` resolves on PATH | Installation and account authentication need user action/approval before Cursor live checks. |
| Python / Bun / Git | 3.12.3 / 1.4.0 / 2.43.0 | Available; no new framework needed. |

Treehouse release target: https://github.com/kunchenguid/treehouse/releases/tag/v3.1.6 . Its published linux-amd64 archive SHA-256 is `e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1`; verify the downloaded release against its published checksums before execution. Pin a concrete Cursor version when installed and record the live evidence; do not pretend the upstream reference version is installed here.

Firstmate reference snapshot: `abb81944d69b49db3a9618565786700a1442cfe0`. Read relevant files at that revision when implementing; preserve its MIT notice for copied material. Do not copy its 1,000-line watcher bridge and dependencies wholesale. Herdr is AGPL/commercial; invoke an independently installed binary, and do not bundle it without a licensing decision.

### Permission posture

Default workers retain their native approval prompts. Prompts become blockers for the central agent to surface, not opportunities to send automatic confirmation keys. Permit explicit per-task `--unattended` only with a recorded user approval for tool execution; then omp receives `--auto-approve` and Cursor receives `--yolo`. Cursor's `--trust` is also an explicit project-trust choice recorded at registration/launch, not inferred from having a directory on disk. This keeps unattended execution available without granting it implicitly.

Approval records contain the exact user instruction, action, task, current head, and intended target. They are an audit and confused-agent guard, not proof against a malicious agent sharing the user's filesystem/credentials. Prompts and Git worktrees are not security sandboxes. Existing credentials, server permissions, and branch protections remain necessary.

### Publication boundary

Do not guess the company's forge. Central omp uses the company's existing forge tool to publish after explicit approval, then records the resulting URL and verifies the pushed branch with Git. The distro owns the approval and evidence workflow, not a new generic forge adapter system. `task publication-record` cannot itself claim that an unverified review URL exists; the result includes the existing tool's output/evidence file. PR merging is not automated. This fulfills approved publication while avoiding unrequested GitHub-only coupling.

## File boundaries

All paths below are relative to this repository. Existing files: the design spec only.

| File | Owns |
|---|---|
| `bin/agd` | Executable entrypoint, argparse, stable JSON output and exit mapping |
| `bin/agd_lib/__init__.py` | Package marker only |
| `bin/agd_lib/store.py` | Canonical home, identifiers, atomic JSON, locks, subprocess limits, owner/role checks |
| `bin/agd_lib/projects.py` | Project registry and guarded clone lifecycle |
| `bin/agd_lib/tasks.py` | Task records, Treehouse leases, readiness, approval/landing/publication/cleanup |
| `bin/agd_lib/tmux.py` | Exact tmux endpoint transport and identity |
| `bin/agd_lib/herdr.py` | Exact Herdr endpoint transport and identity |
| `bin/agd_lib/harnesses.py` | omp/Cursor launch arguments and execution evidence |
| `bin/agd_lib/workers.py` | Launch transaction, messages, worker runner, interrupt/stop/relaunch |
| `bin/agd_lib/supervision.py` | Durable events, reconciliation, watcher, owned session lock |
| `.omp/extensions/agd-primary.ts` | Central omp integration, generation ownership and wake delivery |
| `lib/omp-worker.ts` | Explicitly loaded worker lifecycle extension, outside auto-discovery |
| `tests/support.py` | Isolated local repo/home helper and command assertions |
| `tests/test_projects.py`, `tests/test_tasks.py`, `tests/test_delivery.py` | Persistent-state and work-preservation regressions |
| `tests/test_backends.py`, `tests/test_workers.py`, `tests/test_supervision.py` | Transport, task lifecycle and recovery regressions |
| `tests/primary.test.ts` | Central extension lifecycle contract tests using Bun's built-in test support |
| `AGENTS.md`, `.agents/skills/projects/SKILL.md`, `.agents/skills/tasks/SKILL.md`, `.agents/skills/orchestrate/SKILL.md` | Central operating rules and conditional procedures |
| `.gitignore`, `README.md`, `CHANGELOG.md`, `docs/operations.md`, `docs/compatibility.md` | Distribution boundary, setup, recovery and observed support |

Do not pre-create empty modules, skills, configuration directories, or placeholder docs. Add each file only in the task that makes it useful.

## Shared interfaces

### CLI behavior

All commands below return one JSON value to stdout; diagnostics go to stderr. Exit 0 means the requested operation completed, 2 invalid input, 3 refused safety/authority/identity check, 4 missing/incompatible dependency, 5 external operation failed or outcome unconfirmed. Python exceptions are translated at the entrypoint, never hidden as success. Subprocesses use argument arrays with explicit cwd, `stdin=DEVNULL` unless a deliberate transport needs bytes, and finite timeouts; no `shell=True`.

Exception: `session start` is the long-lived watcher process; it emits newline-delimited JSON records (`ready`, `events`, `failure`) and stays alive. All other commands return one JSON value. Define `store.Refusal(Exception)` for domain refusals (CLI exit 3); backend functions raise it on ownership mismatch rather than returning a success-shaped object.

`--home PATH` is accepted globally. Otherwise use `AGD_HOME`, then the resolved distro root. The home must be an absolute canonical directory; reject symlinked private state roots. `AGD_TASK_ID` plus a per-incarnation token scopes worker event/message operations; workers cannot call central lifecycle verbs through this CLI. This is an accidental-misuse guard, not an OS security boundary.

Proposed domain commands (introduced by their owning tasks):

```text
agd init
agd doctor [--harness omp|cursor] [--backend tmux|herdr]
agd project add ID --source URL_OR_PATH [--base BRANCH]
agd project register ID --path PATH [--base BRANCH]
agd project trust ID --instruction FILE
agd project list
agd project inspect ID
agd project refresh ID
agd project remove ID [--delete-clone --approval APPROVAL_ID]
agd task create ID --project ID --brief FILE [--base BRANCH] [--kind ship|scout]
agd task list
agd task status ID
agd task ready ID --report FILE
agd task approve ID --action local-merge|publish|discard|unattended --target TARGET --instruction FILE
agd task land ID --approval APPROVAL_ID
agd task publication-record ID --approval APPROVAL_ID --remote REMOTE --branch BRANCH --url URL --evidence FILE
agd task cleanup ID [--discard --approval APPROVAL_ID]
agd worker launch ID [--harness omp|cursor] [--backend tmux|herdr] [--unattended --approval APPROVAL_ID]
agd worker run ID --incarnation TOKEN
agd worker send ID --file FILE
agd worker inbox ID
agd worker acknowledge ID MESSAGE_ID
agd worker inspect ID
agd worker interrupt ID
agd worker stop ID
agd worker relaunch ID
agd worker event ID --incarnation TOKEN --kind busy|idle|blocked|decision|resolved|failed --key KEY --message FILE
agd session start --owner-pid PID --owner-start ID --generation TOKEN
agd session status
agd session reconcile
agd events list
agd events acknowledge EVENT_ID [EVENT_ID ...]
agd decision resolve TASK_ID KEY --answer FILE
```

`project remove --delete-clone` needs a project-scoped approval: extend `project approve ID --action delete-clone --instruction FILE` using the same approval-record format without fabricating a task. Ordinary unregister leaves the directory untouched and refuses while any task retains resources for that project.

### Records

Use versioned JSON files, atomically replaced under locks. Durability-critical writes use file fsync, rename, and directory fsync. Keep brief/report/message bodies in separate UTF-8 files so metadata reads stay bounded.

- `data/home.json`: schema=1, immutable random `home_id`.
- `data/projects/<id>.json`: schema=1, id, canonical path, canonical Git common directory, source, base branch, trusted flag.
- `data/tasks/<id>/task.json`: schema=1, id, project, kind, phase (`preparing|prepared|running|ready|landed|cleaned`), condition (`normal|blocked|failed|interrupted|unknown`), base branch/start commit, branch, worktree, lease_id, lease_holder, allocation attempt ID, executor (`central|worker`), worker incarnation, harness/backend, endpoint, ready_head, report path, review evidence, landing evidence.
- `data/tasks/<id>/brief.md`, `report.md`, `inbox/<uuid>.json`, `inbox-ack/<uuid>.json`, `approvals/<uuid>.json`.
- `state/events/<uuid>.json`: schema=1, id, task, incarnation, kind, key, observed_at, details; acknowledged events move to `state/events-handled/` rather than disappear before a durable receipt exists.
- `data/tasks/<id>/decisions/<key>.json`: open/resolved plus question and answer pointers. Acknowledging a wake never closes a decision.
- `state/owner.json`: central PID plus process-start evidence, generation, owner token, watcher PID/start evidence, heartbeat. `state/session.lock` is held by the watcher for its lifetime; `state/mutation.lock` serializes short updates.
- `state/tasks/<id>/`: runner PID/start evidence, incarnation, harness observations, backend capture fingerprints. These cannot override durable task/decision records.

The session lock and mutation lock are separate; the watcher does not hold a mutation lock while sleeping. Ordinary CLI operations without a live central owner are allowed for the operator; while another central owns the home, central mutations require its inherited `AGD_OWNER` token. Worker-scoped writes use their own task/incarnation instead. A second central may inspect but may not launch, steer, drain/ack, land or clean up.

An explicitly worker-scoped caller may invoke only its own inbox/acknowledge/event/ready operations, never approve, land, publish, cleanup or project mutations. `task ready` additionally requires the current incarnation token for workers. Trust records bind the canonical project identity and original user instruction; `project trust` is the only grant path, and clone replacement invalidates it.

### Backend interface

Implement the same plain functions in `tmux.py` and `herdr.py`; no abstract base class or registry:

```python
create(home: Path, task_id: str, incarnation: str, cwd: Path, argv: list[str]) -> dict
inspect(endpoint: dict) -> dict
capture(endpoint: dict, lines: int = 120) -> str
send_text(endpoint: dict, text: str) -> dict
send_key(endpoint: dict, key: str) -> dict
close(endpoint: dict) -> dict
```

`inspect` returns `presence: present|missing|unknown`, transport identity evidence, and process information; it does not label task success. `send_text` returns `submitted|unconfirmed`, not `processed`. Keys are allowlisted (`escape`, `enter`); no arbitrary lifecycle text. A dead worker may leave a live endpoint. Never kill all sessions or match labels as an ownership proof.

## Task 1: Managed project home and safe project operations

**Files:** create `bin/agd`, `bin/agd_lib/{__init__,store,projects}.py`, `.gitignore`, `tests/support.py`, `tests/test_projects.py`, `README.md`.

**Interfaces:** produce `store.home(path=None) -> Path`, `store.run(argv, cwd, timeout=30) -> CompletedProcess[str]`, `store.atomic_json(path, value) -> None`, `store.lock(home)` context manager, `store.validate_id(value) -> str`, and `projects.read(home, project_id) -> dict`. IDs match `[a-z][a-z0-9-]{0,47}`. `project add` clones beneath projects; `register` accepts only an existing canonical clone beneath this home's projects/ and never moves an external directory.

- [ ] Write the isolated helper and project preservation regression. Helper code for `tests/support.py`:

```python
import json, os, subprocess, tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin' / 'agd'

def run(argv, cwd=None, env=None, expected=0):
    p = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=90)
    assert p.returncode == expected, (p.returncode, p.stdout, p.stderr)
    return p

def command(home, *args, expected=0):
    env = dict(os.environ, AGD_HOME=str(home),
               TREEHOUSE_ROOT=str(home.parent / 'pool'))
    for key in ('AGD_OWNER', 'AGD_TASK_ID', 'AGD_INCARNATION'):
        env.pop(key, None)
    p = run([CLI, '--home', home, *args], env=env, expected=expected)
    return json.loads(p.stdout) if expected == 0 else p

@contextmanager
def lab():
    with tempfile.TemporaryDirectory(prefix='agd-test-') as tmp:
        root = Path(tmp)
        seed, origin, home = root/'seed', root/'origin.git', root/'home'
        run(['git', 'init', '-b', 'main', seed])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=seed)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=seed)
        (seed/'value.txt').write_text('base\n')
        run(['git', 'add', 'value.txt'], cwd=seed)
        run(['git', 'commit', '-m', 'initial'], cwd=seed)
        run(['git', 'clone', '--bare', seed, origin])
        command(home, 'init')
        command(home, 'project', 'add', 'sample', '--source', str(origin), '--base', 'main')
        project = home/'projects'/'sample'
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=project)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=project)
        yield home, project
```

`tests/test_projects.py`:

```python
from support import lab, command

def test_refresh_preserves_dirty_clone():
    with lab() as (home, project):
        (project/'value.txt').write_text('personal edit\n')
        command(home, 'project', 'refresh', 'sample', expected=3)
        assert (project/'value.txt').read_text() == 'personal edit\n'
        command(home, 'project', 'remove', 'sample')
        assert (project/'.git').exists()
        assert command(home, 'project', 'list') == []

if __name__ == '__main__':
    test_refresh_preserves_dirty_clone()
```

- [ ] Run `python3 tests/test_projects.py`; it must fail because the command does not exist, not because Git identity/network is required.
- [ ] Implement init and project operations with argparse. Add ignores `/config/`, `/projects/`, `/data/`, `/state/`, `.env`, `__pycache__/`, `*.pyc`. New home/private directories use 0700, state/approval files 0600. Never follow a registry path outside the canonical project root for deletion. Reject traversal IDs, symlink roots, bare repos, duplicate IDs and existing clone destinations. Add persists registration only after successful clone verification; a failed clone is reported and not deleted blindly.

Use this atomic-write shape; create a unique temporary file in the same directory and unlink it only on failure:

```python
fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
```

`refresh`: verify exact registered clone, clean tracked/untracked state, selected base checked out, then fetch the named remote and `git merge --ff-only` its base; refuse divergence, detached HEAD or dirty state. Do not stash/reset/force. Bound external commands.
- [ ] Add regressions for traversal ID, registration outside home, dirty/untracked clone deletion, and a project with active task records. Implement `project approve`/`remove --delete-clone` so approval names the project identity and removal refuses registered task resources or unpushed/unverifiable local refs. Ambiguous deletion stays refused; unregister is available without deleting bytes.
- [ ] Implement `project trust ID --instruction FILE`: record the exact user grant against canonical path/common-dir identity; leave new registrations untrusted. This is an explicit user operation, never an automatic diagnostic repair.
- [ ] Run `python3 tests/test_projects.py`. Run the actual init/add/list/inspect/refresh/remove commands in a temporary lab and inspect the preserved clone; use the same Python helper as a throwaway driver, not a source-text test.
- [ ] Document central-home launch intent and private surfaces; commit `feat: manage private project homes` after successful checks.

## Task 2: Durable task allocation and recoverable Treehouse leases

**Files:** create `bin/agd_lib/tasks.py`, `tests/test_tasks.py`; modify `bin/agd`, `bin/agd_lib/store.py`, `README.md`; create `docs/compatibility.md`.

**Interfaces:** produce `tasks.read(home, task_id) -> dict`, `tasks.save(home, task) -> None`, `tasks.reconcile_allocation(home, task_id) -> dict`. CLI create returns the full task record, including `worktree` and `lease_id`. `doctor` reports installed versions and required capabilities without installing anything.

- [ ] Obtain approval for a separate Treehouse v3.1.6 binary, then verify its checksum/version and help. If approval/tool is unavailable, complete reachable implementation work but report workspace runtime verification as blocked; never adapt by dropping base/lease checks. Capture evidence in compatibility docs. Use its exact path via PATH in test environments.
- [ ] Add a failing lease/isolation test:

```python
from pathlib import Path
from support import lab, command

def test_distinct_leases_and_no_duplicate_create():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt and report checks.\n')
        a = command(home, 'task', 'create', 'one', '--project', 'sample', '--brief', str(brief))
        b = command(home, 'task', 'create', 'two', '--project', 'sample', '--brief', str(brief))
        assert a['lease_id'] != b['lease_id']
        assert Path(a['worktree']) != Path(b['worktree']) != project
        (Path(a['worktree'])/'value.txt').write_text('one only\n')
        assert (Path(b['worktree'])/'value.txt').read_text() == 'base\n'
        assert (project/'value.txt').read_text() == 'base\n'
        command(home, 'task', 'create', 'one', '--project', 'sample', '--brief', str(brief), expected=3)
        assert command(home, 'task', 'status', 'one')['lease_id'] == a['lease_id']

if __name__ == '__main__':
    test_distinct_leases_and_no_duplicate_create()
```

- [ ] Run `python3 tests/test_tasks.py` and observe missing task behavior before implementation.
- [ ] Implement create, list and status. Under the mutation lock, persist `phase=preparing`, the copied brief, branch `agd/<id>`, and unique lease holder `<home-id>:<task-id>:<allocation-attempt>` before acquiring. Release the short lock while the bounded external acquire runs; the preparing record prohibits another allocation for that task. Use:

```python
argv = ['treehouse', 'get', '--lease', '--json',
        '--lease-holder', lease_holder, '--base', base_branch,
        '--branch', 'agd/' + task_id]
result = store.run(argv, cwd=project_path, timeout=120)
allocation = json.loads(result.stdout)
```

Verify canonical Git common-dir equals the registered clone, worktree differs from stable clone, branch is exact, base is resolvable, and returned lease identity matches `treehouse status --json`. Persist base start commit and prepared state. Do not copy .env/secrets by default; use an empty explicit include manifest for acquisition unless the user deliberately configured seeding.
- [ ] Implement recovery of a preparing record by matching the unique holder in Treehouse's authoritative status: exactly one matching lease with correct clone/branch may be adopted; zero matches after an interrupted command is an unknown failed allocation requiring inspection, not automatic reallocation; multiple/unreadable matches refuse. Preserve quarantined failed acquisitions. Retrying create on an existing record always refuses; reconciliation is explicit.
- [ ] Add a real child-process interruption scenario around acquire and record publication; recover the existing lease without creating another. Verify an idle leased worktree is not reused when another task is acquired. Use a temporary executable wrapper to pause only the external command boundary; do not add production fault flags.
- [ ] Run project/task checks plus the actual acquire/status/reconcile scenario. Record that return can reset worktrees and terminate lingering processes: it is never the first cleanup action. Commit `feat: allocate durable isolated task workspaces`.

## Task 3: Readiness, explicit landing, and work-preserving cleanup

**Files:** modify `bin/agd_lib/tasks.py`, `bin/agd`, `tests/support.py`; create `tests/test_delivery.py`, `docs/operations.md`; update `README.md`.

**Interfaces:** `tasks.verify_ready(home, task_id) -> dict`, `tasks.verify_workspace(home, task) -> dict`, `tasks.authorize(home, task, action, target, approval_id) -> dict`. Approval records bind action, task ID, exact head, exact target and user instruction; repeat completion reads evidence instead of applying an action again.

- [ ] Add the failing unlanded-preservation test:

```python
from pathlib import Path
from support import lab, command, run

def test_ready_is_not_cleanup_authority():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Update value.txt.\n')
        task = command(home, 'task', 'create', 'change', '--project', 'sample', '--brief', str(brief))
        work = Path(task['worktree'])
        (work/'value.txt').write_text('changed\n')
        run(['git', 'add', 'value.txt'], cwd=work)
        run(['git', 'commit', '-m', 'change'], cwd=work)
        report = home/'result.md'; report.write_text('Updated value.txt. Check: value is changed. Limit: no product tests.\n')
        ready = command(home, 'task', 'ready', 'change', '--report', str(report))
        command(home, 'task', 'cleanup', 'change', expected=3)
        assert (work/'value.txt').read_text() == 'changed\n'
        assert run(['git', 'rev-parse', 'HEAD'], cwd=work).stdout.strip() == ready['ready_head']
        instruction = home/'approval.txt'; instruction.write_text('Merge task change into sample main locally.\n')
        approval = command(home, 'task', 'approve', 'change', '--action', 'local-merge', '--target', 'sample:main', '--instruction', str(instruction))
        command(home, 'task', 'land', 'change', '--approval', approval['id'])
        assert (project/'value.txt').read_text() == 'changed\n'
        command(home, 'task', 'cleanup', 'change')
        assert command(home, 'task', 'status', 'change')['phase'] == 'cleaned'

if __name__ == '__main__':
    test_ready_is_not_cleanup_authority()
```

- [ ] Run `python3 tests/test_delivery.py` before adding readiness/landing.
- [ ] Implement ready: copy report into task data, require clean tracked/untracked state, verify branch and head, require a real implementation commit beyond the starting commit for ships. Scouts need a preserved report and no unexpected changes; never invent a commit. For workers, native idle alone is insufficient: the worker must have declared its report, and the workspace must pass verification.
- [ ] Implement approval and local landing. Refresh readiness inside the per-project operation lock; refuse an approval bound to an older head, wrong target, expired task identity, dirty clone, or live worker. Use argument arrays:

```python
store.run(['git', 'merge-base', '--is-ancestor', target_head, ready_head], cwd=project_path)
store.run(['git', 'merge', '--ff-only', ready_head], cwd=project_path)
landed = store.run(['git', 'rev-parse', 'HEAD'], cwd=project_path).stdout.strip()
if landed != ready_head:
    raise RuntimeError('landing unconfirmed; retain task workspace')
```

Persist action intent before merge. If interrupted after merge, reconcile the exact approved head against the target instead of merging again. Never force-update refs or silently rebase.
- [ ] Implement publication-record: require publish approval binding remote/base/head, canonical HTTP(S) URL with no embedded credentials, copied forge-output evidence, and `git ls-remote --heads REMOTE refs/heads/BRANCH` exactly matching `ready_head`. Output `published`, not `landed`. For local file remotes use them only in lab checks; no fabricated review URL may be reported as an actual PR. Central's workflow runs the existing forge tool and records real returned URL. Document a concrete `gh pr create --repo ... --base ... --head ...` example as an example, not the only supported forge or an automatic command.
- [ ] Implement cleanup after exact lease and clone identity recheck, clean working state, no active/unverifiable processes in the worktree, and ancestry of ready_head into the recorded destination. Where a squash/rebase lacks ancestry proof, refuse automatic cleanup; do not equate patch resemblance with landing. For a clean scout, its report and unchanged starting head are its completion proof. Explicit discard approval binds current task/branch/head and inventory of dirty paths; any changed inventory requires a new approval. Conditional release:

```python
argv = ['treehouse', 'return', task['worktree'],
        '--if-lease-id', task['lease_id'],
        '--if-lease-holder', task['lease_holder']]
```

Only the explicitly approved discard path may add `--force`. Never call return while a foreign/unknown process remains. Keep branch refs after ordinary cleanup; deleting a branch is not needed for pool reuse.
- [ ] Add regressions for stale approvals, dirty/untracked work, wrong lease, active/unknown process, a published-but-unmerged branch, changed discard inventory, clean scout completion, and merge interruption. Run all workspace checks and the actual local merge/return scenario. Commit `feat: guard task delivery and cleanup`.

## Task 4: tmux endpoint transport

**Files:** create `bin/agd_lib/tmux.py`, `tests/test_backends.py`; modify `docs/compatibility.md`.

**Interfaces:** implement the six backend functions above. Endpoint records include socket path, server identity, pane ID, task/incarnation and a random endpoint token. Use a distro-owned socket beneath `state/` (short enough for Unix socket paths); never the user's default tmux server.

- [ ] Add a real backend regression in `tests/test_backends.py`, starting with tmux. Import modules with `sys.path.insert(0, str(ROOT/'bin'))`. Create an endpoint running `['/bin/sh', '-c', 'printf "agd-live-check\\n"; sleep 30']` in a temporary cwd; wait with a bounded deadline for capture to contain the marker. Assert endpoint presence, then attempt close on a copy with the wrong token and require refusal. Close only the genuine endpoint in `finally`. The child is a transport probe, never a fake agent integration test.

Use this executable starting point for `tests/test_backends.py`; the backend is selected by its explicit command-line argument. Both adapters include a `token` field and verify it against their persisted endpoint ownership receipt before mutating:

```python
import importlib, sys, tempfile, time
from pathlib import Path
from support import ROOT, command
sys.path.insert(0, str(ROOT/'bin'))
from agd_lib.store import Refusal

def test_owned_endpoint(backend_name):
    assert backend_name in ('tmux', 'herdr')
    backend = importlib.import_module('agd_lib.' + backend_name)
    with tempfile.TemporaryDirectory(prefix='agd-pane-') as tmp:
        home = Path(tmp)/'home'
        command(home, 'init')
        endpoint = backend.create(
            home, 'probe', 'incarnation-one', home,
            ['/bin/sh', '-c', 'printf "agd-live-check\\n"; sleep 30'])
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if 'agd-live-check' in backend.capture(endpoint):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('probe output never appeared')
            assert backend.inspect(endpoint)['presence'] == 'present'
            foreign = dict(endpoint, token='not-the-owner')
            try:
                backend.close(foreign)
            except Refusal:
                pass
            else:
                raise AssertionError('foreign endpoint close was accepted')
            assert backend.inspect(endpoint)['presence'] == 'present'
        finally:
            backend.close(endpoint)
        assert backend.inspect(endpoint)['presence'] == 'missing'

if __name__ == '__main__':
    test_owned_endpoint(sys.argv[1])
```

Keep the owned server alive through the final absence read, then retire the test-created server only with recorded identity. For Herdr, prepare its explicit named lab server before invoking this check; absence of that setup is a failed prerequisite, not permission to attach to the default server.
- [ ] Run `python3 tests/test_backends.py tmux` and observe the missing adapter failure.
- [ ] Implement explicit socket-scoped calls with bounded timeouts. `create` starts a named distro session when absent, then one window per task, using `-c` for cwd and `-P -F '#{pane_id}'` for exact identity. Build the unavoidable shell command with `shlex.join(argv)`, never interpolate raw task text. Record task/incarnation token as a pane option and verify it on every mutation. Capture uses `capture-pane -p -S -120 -t PANE`.

```python
prefix = ['tmux', '-S', str(socket_path)]
command = shlex.join(argv)
# On an existing owned server/session:
launch = prefix + ['new-window', '-d', '-P', '-F', '#{pane_id}',
                   '-t', session_name, '-c', str(cwd), command]
```

Keep creation identity durable before sending subsequent instructions. If connection fails, presence is unknown unless the recorded server identity is positively absent. Pane `%1` on a replacement server is not the same endpoint. Send text via a uniquely named tmux buffer and bracketed paste; send Enter separately. Dispose only this buffer. Submission does not prove processing.
- [ ] Add shell-metacharacter/path-space cases and verify literal bytes, not command execution. Exercise lost server access and mismatched incarnation; both must refuse control. Close only task windows; retain any uncertain resources for inspection.
- [ ] Run the real socket probe and tests once after the adapter is complete. Commit `feat: add isolated tmux worker transport`.

## Task 5: Herdr endpoint transport

**Files:** create `bin/agd_lib/herdr.py`; extend `tests/test_backends.py`; update `docs/compatibility.md`.

**Interfaces:** implement the same six backend functions. Endpoint includes explicit named session/server identity, workspace ID, tab ID, pane ID and task/incarnation. No focused-session fallback.

- [ ] Inspect `herdr --help`, `herdr --skill`, and safe command-group help. Start an explicitly owned lab session for verification only; use the documented headless-server help to determine exact invocation before execution. Never issue `server stop`, `session delete`, or inspect/control the default/focused session. If outside Herdr, do not use implicit context; all calls target the explicitly provisioned lab session.
- [ ] Extend the backend regression to Herdr with the same marker and wrong-identity refusal. Run `python3 tests/test_backends.py herdr` before implementation.
- [ ] Use explicit session selection on every operation. Create a home-owned workspace and task tabs; preserve caller focus. Parse `result.workspace`, `result.tab`, and `result.root_pane` from successful JSON, never derive IDs from labels. The installed help exposes:

```text
herdr --session NAME tab create --workspace WORKSPACE --cwd PATH --label LABEL --no-focus
herdr --session NAME pane get PANE
herdr --session NAME pane process-info --pane PANE
herdr --session NAME pane run PANE COMMAND
herdr --session NAME pane read PANE --source recent-unwrapped --lines 120
herdr --session NAME pane send-text PANE TEXT
herdr --session NAME pane send-keys PANE enter
herdr --session NAME pane close PANE
```

`pane run` receives only a shell-quoted runner argv, never a brief body. Validate CLI exit and JSON success/error fields. Capture process identity rather than trusting the label. A session/server error is unknown, not missing. Only an authoritative query to the recorded live server proving pane absence yields missing.
- [ ] Confirm parsed endpoint/process-response fields against actual lab output before finalizing types. Record protocol version and any unsupported control operation in compatibility docs; unsupported operations must refuse, never fake success. Do not use Herdr native Cursor blocked state as definitive busy/idle evidence.
- [ ] Run both backend probes and inspect actual panes/captured marker. Stop only recorded lab processes/resources in `finally`; if verification cannot prove ownership, leave them and report exact IDs. Commit `feat: add explicit Herdr worker transport`.

## Task 6: Real worker execution, steering, and guarded relaunch

**Files:** create `bin/agd_lib/harnesses.py`, `bin/agd_lib/workers.py`, `lib/omp-worker.ts`, `tests/test_workers.py`; modify `bin/agd`, `bin/agd_lib/tasks.py`, `docs/operations.md`, `docs/compatibility.md`.

**Interfaces:** produce `harnesses.launch_argv(task, brief_path, unattended) -> list[str]`, `harnesses.observe(task) -> dict`, `workers.launch(home, task_id, harness, backend, approval_id=None) -> dict`, `workers.inspect(home, task_id) -> dict`. Harness observation returns `busy|idle|unknown` with source and observed incarnation. Liveness (`alive|dead|missing|unknown`) is separate.

- [ ] Add a failing worker-state regression: a terminal done event for incarnation A must not mark incarnation B ready; Escape delivery must not label the process dead; a bad endpoint prevents relaunch without allocating another worktree. Use captured real protocol inputs only for the state classifier; final verification must use actual agents.
- [ ] Implement defaults (`omp`, `tmux`) in a private `config/settings.json` initialized only on request. Unknown values refuse. Worker launch records its chosen values permanently; relaunch reuses them and the same workspace. Validate tool capabilities and trust/approval before creating an endpoint. Sanitize inherited foreign primary markers and central owner token; pass only task/home/incarnation markers and normal required environment. Do not strip credentials arbitrarily or print them.
- [ ] Implement a durable launch transaction: persist starting incarnation before endpoint creation, record created endpoint, then start the task runner. The runner verifies cwd/common-dir/branch/lease and publishes PID plus start evidence before launching the harness. It waits for the harness and records exit without treating exit 0 as task completion. Any partially created resource remains recorded for recovery, not blindly deleted or recreated.

```python
omp_argv = ['omp', '--cwd', task['worktree'],
            '-e', str(code_root/'lib'/'omp-worker.ts'), prompt]
cursor_argv = [cursor_binary, '--workspace', task['worktree'], '--trust', prompt]
if unattended:
    omp_argv.insert(1, '--auto-approve')
    cursor_argv.insert(1, '--yolo')
```

The prompt names the absolute brief/inbox paths and task role. It instructs the worker to operate only in the recorded worktree, report decisions and verification, commit authorized changes, and never publish or land. Read the task brief as data; do not overwrite project AGENTS.md. Cursor trust must already have been explicitly granted. No `cursor --worktree`, no second positional prompt, no auto-loading the worker extension twice.
- [ ] omp worker extension writes native `agent_start` busy and `agent_end` idle only when `willContinue !== true`, using task/incarnation-scoped CLI events. Cursor busy evidence comes from its exact conversation transcript, not arbitrary files or pane spinner text. Read the pinned Firstmate Cursor adapter and transcript reader, then bind the actual installed Cursor transcript path/session to runner/workspace identity. User-start opens a turn; typed turn-end closes it. Missing, malformed, stale or ambiguous transcript gives unknown. Record actual format/version evidence without committing private transcripts.

Implement `worker event` in this task as a task-incarnation-scoped append to `data/tasks/<id>/observations/<uuid>.json`, not a dependency on the later watcher. It records durable blocked/decision/progress observations and native omp busy transitions. Task 7 consumes those observations into wake events and durable decisions; callers do not change. The runner must wait for the exact endpoint/incarnation receipt in task metadata before starting the harness, closing the create-before-record race without inventing a second endpoint.
- [ ] Implement durable steering: inbox message UUID with body on disk before doorbell. `inbox` lists unacknowledged messages; `acknowledge` accepts only that task's current incarnation and records a receipt. Only send the constant doorbell when the adapter proves a safe composer/idle state; otherwise retain pending delivery and surface uncertainty. An interrupted enqueue can be re-read without resubmitting the whole message. `decision resolve` persists answer, enqueues it, and closes only the named durable decision after the answer is durable.
- [ ] Implement interrupt/stop/relaunch. Interrupt sends the harness key and then reports observed state, including unconfirmed. Stop sends `/quit` for omp or `/exit` for Cursor only to a verified idle composer; if busy, request interrupt and wait for confirmed idle before exit. Do not automatically escalate to SIGKILL. Refuse when state is unknown. Relaunch requires positive previous-process absence and the same task workspace; re-read brief and pending inbox. Native conversation resume is not promised.
- [ ] Run live workers in all four combinations after approved prerequisites. Use a disposable repository and a brief to change `value.txt`, commit, and report; send a second request before completion and confirm its durable acknowledgement. Record process state, committed content, report, and clean stop. A transport marker probe is not evidence of agent success. Commit `feat: run and steer omp and Cursor task workers` only with honest evidence of completed vs blocked matrix entries.

## Task 7: Durable events and background reconciliation

**Files:** create `bin/agd_lib/supervision.py`, `tests/test_supervision.py`; modify `bin/agd`, `bin/agd_lib/workers.py`, `bin/agd_lib/store.py`, `docs/operations.md`.

**Interfaces:** `supervision.emit(home, task, incarnation, kind, key, details) -> str`, `supervision.pending(home) -> list[dict]`, `supervision.reconcile(home) -> dict`, `supervision.watch(home, owner_pid, owner_start, generation) -> None`. CLI events acknowledge only explicit IDs, not an unbounded cursor that can swallow unseen events.

- [ ] Add a failing persistence regression using the real CLI:

```python
from support import lab, command

def test_acknowledging_wake_does_not_answer_decision():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Investigate value.txt.\n')
        command(home, 'task', 'create', 'question', '--project', 'sample', '--brief', str(brief), '--kind', 'scout')
        # The central executor may publish an observed task decision through the
        # same CLI; a worker caller must additionally match its incarnation.
        message = home/'question.md'; message.write_text('Which value is intended?\n')
        command(home, 'worker', 'event', 'question', '--incarnation', 'central', '--kind', 'decision', '--key', 'value', '--message', str(message))
        command(home, 'session', 'reconcile')
        event = command(home, 'events', 'list')[0]
        command(home, 'events', 'acknowledge', event['id'])
        state = command(home, 'task', 'status', 'question')
        assert state['open_decisions'][0]['key'] == 'value'
        assert command(home, 'events', 'list') == []

if __name__ == '__main__':
    test_acknowledging_wake_does_not_answer_decision()
```

- [ ] Run `python3 tests/test_supervision.py` before implementing event handling.
- [ ] Implement event files with UUID IDs and dedupe keyed by task incarnation + event kind + evidence identity. Persist before notifying. Acknowledge by atomic rename into handled receipts under the mutation lock; repeated acknowledgement succeeds only for the same existing receipt. Unknown IDs refuse. Retain unresolved decisions separately. Ready wake acceptance rechecks the workspace; old report text cannot overrule current dirty state.
- [ ] Implement singleton watcher with `fcntl.flock` session lock, central PID/start evidence, random generation and owner token, heartbeat and bounded stdout notifications. On each tick reconcile only this home's recorded tasks. Poll default every 2 seconds, heartbeat every 5 seconds, stale suspicion after 120 seconds without new attributable progress; these are configurable timing knobs, not semantic proof. Emit one suspicion per unchanged evidence episode and allow a changed state to re-arm it. No automatic restart/kill on a timer.

```python
with open(home/'state'/'session.lock', 'a+') as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Publish verified owner identity before accepting mutable fleet commands.
    # Keep this file descriptor open for the lifetime of the watch loop.
```

Owner identity includes process start evidence so recycled PIDs cannot inherit ownership. If owner disappears, stop monitoring without stopping workers or releasing leases. If another watcher holds the lock, fail read-only before event acknowledgement or resource mutations. If a prior owner remains live but its watcher is dead, only that same owner's token may restore the watcher.
- [ ] Recovery reconciles preparing allocations, starting workers, endpoints, leases, pending messages/events and ready heads. For a launch interrupted between endpoint creation and publication, use exact task/incarnation evidence in the owned server, not label matching; adopt exactly one proven endpoint or report ambiguous without launch. Backend outage, unknown busy state and missing transcript stay unknown. Durable completion survives a closed pane. Failed readiness verification does not erase the worker's report.
- [ ] Add subprocess scenarios for competing owners, owner death, crashed watcher, event produced before restart, acknowledgement interrupted, unresolved decision buried by later progress, and a lost backend. Confirm records and worktree bytes survive each scenario. Run the actual watcher against a live lab worker and observe a durable blocked/completed event. Commit `feat: supervise durable task events and recover sessions`.

## Task 8: Central omp lifecycle and no-blind-stop integration

**Files:** create `.omp/extensions/agd-primary.ts`, `tests/primary.test.ts`; modify `bin/agd_lib/supervision.py`, `docs/operations.md`, `docs/compatibility.md`.

**Interfaces:** extension default export accepts omp's extension API. Use `session_start`, `before_agent_start`, `session_stop`, `session_shutdown` plus `sendUserMessage`. Keep event/notification shape local; do not import Pi's types. Extension path is auto-discovered, never also supplied with `-e`.

Expose a small pure `stopDecision` function from the same extension file so the loop bound is tested independently of a credentialed model:

```typescript
type Health = { delegatedWork: boolean; watcherHealthy: boolean };
export function stopDecision(stopHookActive: boolean, health: Health) {
  if (stopHookActive || !health.delegatedWork || health.watcherHealthy) return;
  return {
    continue: true as const,
    additionalContext: 'Delegated work has no healthy watcher. Restore supervision or report the concrete failure; do not claim it is monitored.'
  };
}
```

Starting regression in `tests/primary.test.ts`:

```typescript
import { test, expect } from 'bun:test';
import { stopDecision } from '../.omp/extensions/agd-primary';

test('blind stop continues once; healthy or empty fleet can stop', () => {
  const broken = { delegatedWork: true, watcherHealthy: false };
  expect(stopDecision(false, broken)?.continue).toBe(true);
  expect(stopDecision(true, broken)).toBeUndefined();
  expect(stopDecision(false, { ...broken, watcherHealthy: true })).toBeUndefined();
  expect(stopDecision(false, { ...broken, delegatedWork: false })).toBeUndefined();
});
```

- [ ] Write a Bun contract test for stale generation callbacks and shutdown. A fake extension host is legitimate only for extension lifecycle logic: callbacks from a retired session must not kill the replacement watcher or enqueue an old follow-up. Use `bun:test` and a real temporary event store; do not test copied prompt text. Run `bun test tests/primary.test.ts` and observe the missing extension failure.
- [ ] Implement startup: generate a session ownership token, export it to child commands, spawn `bin/agd session start` with actual central PID/start evidence, and wait for its verified ready record. In `before_agent_start`, inject a bounded startup digest with project/task pointers, open decisions, pending events and any failures. On lock refusal, inject read-only status and disable mutation/watcher startup for that generation. Do not run a second digest or watcher on every turn.
- [ ] Implement notification: stdout is a doorbell only, durable event files are authoritative. When actionable IDs arrive, enqueue one follow-up for the current unacknowledged set, then continue monitoring. `sendUserMessage` returning means queued, not handled. While streaming use `deliverAs: 'followUp'`; verify the actual omp 18.3.2 API value during the live check rather than inferring Pi behavior. Do not acknowledge events in the extension; central handles them and invokes the CLI acknowledgement. Session replacement re-presents pending events even if a prior follow-up was accepted but not consumed.

The upstream API shape to verify:

```typescript
api.on('session_stop', async (event: { stop_hook_active?: boolean }) => {
  const health = await readSessionHealth();
  return stopDecision(event.stop_hook_active === true, health);
});
```

`readSessionHealth(): Promise<{delegatedWork: boolean; watcherHealthy: boolean}>` executes `agd session status`, rejects invalid JSON/nonzero output, and treats read failure as unhealthy. It belongs in this extension; no other task must invent it. Bound continuation to one per turn; failure is surfaced instead of spinning forever.
- [ ] Own watcher exit/re-arm. For an unexpected watcher exit with a live owner, attempt one verified restart, then report failure and keep the guard unhealthy; no unbounded retry loop. Session generation checks protect every callback. `session_shutdown` stops only this generation's watcher, leaving workers, leases and durable events intact. `/new` or session replacement starts a fresh generation after retiring the predecessor. A second central cannot stop the first's watcher.
- [ ] Run actual omp inside a disposable distro checkout with the extension auto-loaded. Confirm startup digest reaches model context, launch a real worker, wait for completion without another user message, observe the central wake, and confirm acknowledgement. Kill only the owned lab watcher to exercise the stop guard. Replace/restart the central session with an unacknowledged decision and observe re-presentation. Record real transcript excerpts free of secrets. Contract tests alone do not pass this gate.
- [ ] Run Bun lifecycle checks and all Python checks after integration. Commit `feat: integrate background supervision with central omp`.

## Task 9: Distro procedures, end-to-end matrix, and delivery evidence

**Files:** create `AGENTS.md`, `.agents/skills/projects/SKILL.md`, `.agents/skills/tasks/SKILL.md`, `.agents/skills/orchestrate/SKILL.md`, `CHANGELOG.md`; finish `README.md`, `docs/operations.md`, `docs/compatibility.md`. Modify code only for observed defects; keep each correction in its owning module.

**Interfaces:** skills invoke the already implemented commands, never reproduce their parsing, lock, or cleanup logic. No new runtime API.

- [ ] Write a short central role contract: choose the project from explicit request/context; ask only if genuinely ambiguous; use task worktrees for code changes; central can execute directly; delegate through recorded workers; preserve user intent; load relevant procedure; keep supervision live; request specific approval for publication/landing/discard; report verified outcomes and limitations. Do not import nautical language, supervisor-only editing prohibitions, or Firstmate's unrelated skill suite.
- [ ] Write the three procedures with concrete command sequences from the CLI contract. Projects procedure covers add/registration/trust/removal. Tasks procedure covers direct central work, readiness, approval binding, local landing, company-forge publication recording, and retained work. Orchestration covers defaults/overrides, launch, steering/acknowledgement, decisions, stop/relaunch and supervised recovery. Include tool permission limits, not promises of sandboxing.
- [ ] Document setup using independently installed tools. `doctor` must report exact missing/incompatible binaries, approval/trust prerequisites and remediation; it must not install or authenticate automatically. Record copied-source license notices where applicable. Document the default home launch `omp`, which auto-loads `.omp/extensions/agd-primary.ts`, and explain that workers use a different explicitly loaded extension.
- [ ] Execute the acceptance matrix below with disposable projects, separate backend sessions and explicit approval for credentialed model calls/tool autonomy. Keep evidence in `docs/compatibility.md` with actual versions, commands, outcomes and limitations. Keep raw private logs under ignored state, not tracked docs. Never fill a matrix row with an upstream claim or a fake worker.

| Scenario | Required observable result |
|---|---|
| omp worker / tmux | Real edit, commit, steering acknowledgement, report, completion wake, safe stop |
| Cursor worker / tmux | Same, including actual transcript-bound status |
| omp worker / Herdr | Same on a recorded named server/pane |
| Cursor worker / Herdr | Same without trusting native Herdr blocked as completion |
| Direct central execution | Isolated task worktree, committed result, no worker required |
| Two tasks on one project | Distinct leases/branches; stable clone and sibling unchanged |
| Central restart | Existing workers/leases adopted; pending event and decision survive |
| Watcher failure | Real omp guard surfaces/restores monitoring; no silent stop |
| Backend unavailable | Unknown reported, no duplicate worker or cleanup |
| Permission prompt | Blocker surfaced; no automatic approval keystroke |
| Local landing | Exact-head approval, clean fast-forward, conditional lease return |
| PR publication | Explicitly authorized disposable remote, actual forge-created URL, verified branch head; no merge/cleanup merely because published |
| Dirty/unlanded/stale identity | Refusal with bytes/commits/lease intact |
| Competing central | Read-only refusal, first owner's monitor remains alive |
| Private material | No config, state, credentials, project clones, or raw transcripts tracked |

- [ ] Run `python3 tests/test_projects.py`, `python3 tests/test_tasks.py`, `python3 tests/test_delivery.py`, `python3 tests/test_backends.py tmux`, `python3 tests/test_backends.py herdr`, `python3 tests/test_workers.py`, `python3 tests/test_supervision.py`, and `bun test tests/primary.test.ts` once after integration. Permanent tests must be deterministic, scoped to temporary homes, and must not use real credentials. The explicit backend tests may use real installed terminal binaries; real model runs remain separate live smoke scenarios, not part of default tests.
- [ ] Verify an authorized PR-publication smoke only after the user provides/approves the disposable remote. Absence of that authorization is a documented verification blocker, not permission to publish elsewhere. Finish all reachable checks first.
- [ ] Remove temporary probes and lab resources only when their identity is proven and work-preservation checks pass. Do not remove unfinished task branches just to leave a clean demo. Update changelog and delivery evidence, then commit `docs: document and verify the starter workflow`.

## Review checklist and handoff

- [ ] Project registry/private-home behavior maps to task 1; task/worktree isolation and lease recovery to task 2.
- [ ] Ready versus landed, explicit publication/merge/discard authority and safe cleanup map to task 3.
- [ ] tmux and Herdr endpoint identity/control map to tasks 4–5.
- [ ] Both actual worker harnesses, direct-central parity, durable steering and relaunch map to task 6 and task 9's matrix.
- [ ] Durable events, open decisions, ownership refusal and restart recovery map to task 7.
- [ ] Real omp startup, background wake, generation replacement and stop guard map to task 8.
- [ ] Skills, installation diagnostics, company-tool boundary, privacy, licensing and full runtime evidence map to task 9.
- [ ] No unsupported platform/version claim, fake runtime row, silent fallback, secret in tracked files, or automatic publication.

This document plans implementation; none of its example checks are reported as already passing. Planning inspected installed binary help/version output and upstream source contracts only. Dependency installation, live agent runs, remote publication and implementation commits remain execution work. Start with task 1 after the user chooses execution; no production implementation has been added by this planning task.

## Worker-generation regression starting point

Add this code to `tests/test_workers.py` in task 6 before implementing worker observations. It proves that an obsolete worker incarnation cannot change the current task record; it is a state-transition regression, not a replacement for real worker smoke runs.

```python
import sys
from support import ROOT, lab, command
sys.path.insert(0, str(ROOT/'bin'))
from agd_lib import tasks

def test_obsolete_worker_cannot_report():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'worker', '--project', 'sample', '--brief', str(brief))
        task.update(executor='worker', incarnation='new-worker', phase='running')
        tasks.save(home, task)
        before = tasks.read(home, 'worker')
        message = home/'message.md'; message.write_text('old worker failed\n')
        command(home, 'worker', 'event', 'worker', '--incarnation', 'old-worker',
                '--kind', 'failed', '--key', 'exit', '--message', str(message), expected=3)
        assert tasks.read(home, 'worker') == before
        assert tasks.read(home, 'worker')['lease_id'] == task['lease_id']

if __name__ == '__main__':
    test_obsolete_worker_cannot_report()
```
