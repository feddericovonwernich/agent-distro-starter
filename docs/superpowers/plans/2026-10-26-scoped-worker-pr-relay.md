# Scoped Worker PR Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a task-target-granted PR repair loop using centrally owned Open Relay feedback, with GitHub as the final authority before the existing task becomes ready.

**Architecture:** The existing task stays `running`; a private task-local PR receipt records publication, Relay lease, and verification evidence. A new `pr-publish` grant limits the existing worker to its leased branch and exact remote target. `agd` owns the non-force push, PR reconciliation/creation, Relay process lifecycle, and GitHub query; the worker receives a grant-only skill that invokes guarded commands.

**Tech Stack:** Python standard library; existing AGD CLI and task store; Git; authenticated GitHub CLI (`gh`); Node >= 22.19/npm; Open Relay 0.1.0 pinned at `dac2865837bcab9444a8636053f4f1406a3f9291`; pytest; disposable homes/local bare Git.

**Spec:** `docs/superpowers/specs/2026-10-26-scoped-worker-pr-relay-design.md`

## Global Constraints

- Execute only in an isolated AGD task worktree; do not change any installed/running AGD home.
- Normal worker launch/default skills must remain unchanged. Relay install/service/skills activate only after a valid `pr-publish` grant.
- `pr-publish` is a new action, distinct from HEAD-bound `publish`, and is restricted to one ship task, `REMOTE:TASK_BRANCH`, and descendants of its recorded grant head.
- Never force-push, merge, tag, alter the base branch, run `sudo`, alter user-global npm/global skills, or include tokens/server/admin configuration in worker instructions.
- Private installation only: `npm install --global --prefix "$AGD_HOME/state/relay-stage-$UUID" --ignore-scripts --no-audit --no-fund https://github.com/feddericovonwernich/open-relay/archive/dac2865837bcab9444a8636053f4f1406a3f9291.tar.gz`.
- Relay compatibility comes from package metadata (`open-relay` 0.1.0 plus committed `dist/src/cli.js`) and Node >= 22.19. `relay --help` or `relay help` MAY provide a non-mutating usage smoke; neither is version/provenance evidence. Never use unsupported `relay --version` or `relay version` for detection.
- The package declares `ajv: ^8.17.1`; its transitive npm resolution is not lock-pinned and must be documented as a supply-chain limitation.
- A worker delivery uses `relay agent poll AGD_WORKER_ID --project-root CENTRAL_RELAY_ROOT --definitions pr.automation.settled@1 --structured-output --context-tokens 5000 --correlation-id github:LOWERCASE_OWNER/LOWERCASE_REPOSITORY:pull-request:PR_NUMBER`. Its default ten-minute park is intentional; do not wrap it in a 30-second process timeout or busy-loop `relay get`.
- For every delivery: `reply start`, validate untrusted payload identity/current head and result against the delivered `outputSchema`, then exactly one terminal `complete`, `fail`, or `cancelled` reply before another poll. `relay notify` is automatic acknowledgement and is not used for the worker repair lease.
- GitHub final evidence must show an OPEN, unmerged exact repository/head/base PR; equal local/remote/PR SHA; green effective required checks; `mergeStateStatus == CLEAN`; and `reviewDecision == APPROVED` when effective reviews are required. Missing/ambiguous policy data blocks.
- GitHub and Git are not one transaction. Re-read remote and PR heads immediately before `ready`; if either moved, reject the evidence and reconcile/verify again.
- No credentialed GitHub publication/connector acceptance run is allowed without a separate user instruction naming the target. Committed tests use fakes at GitHub/Relay/npm boundaries and disposable local Git only.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `bin/agd` | New grant, guarded worker, and inspection command parsing/dispatch. |
| `bin/agd_lib/tasks.py` | `pr-publish` grant records, immutable receipt path, and final-ready gate. |
| `bin/agd_lib/pr.py` | Guarded non-force publication, exact PR recovery, and verification orchestration. |
| `bin/agd_lib/github.py` | Small authenticated `gh` boundary for PR discovery, creation, and policy/evidence reads. |
| `bin/agd_lib/relay.py` | Metadata discovery, private staging, central runtime/connector ownership, poll/reply protocol. |
| `bin/agd_lib/workers.py`, `bin/agd_lib/harnesses.py`, `bin/agd_lib/worker_skills.py` | Grant-only worker capability, conditional prompt replacement, and frozen scoped skill. |
| `bundled/worker-skills/agd-scoped-pr-relay/` | AGD-owned worker instructions, never a default skill. |
| `.agents/skills/{using-open-relay,setting-up-open-relay,waiting-for-open-relay-events,automating-github-prs-with-open-relay,recovering-open-relay}/` | Pinned canonical Relay central skills. |
| `bundled/worker-skills/` | Worker bundles needed from those five upstream skill trees, copied with their licenses. |
| `third_party/PROVENANCE.md` | Pinned source, Apache-2.0, copied file hashes, and `ajv` resolution caveat. |
| `tests/test_relay.py`, `tests/test_github.py`, `tests/test_pr.py` | Behavior tests for runtime safety, protocol settlement, GitHub policy, and PR boundaries. |
| `tests/test_tasks.py`, `tests/test_workers.py`, `tests/test_worker_skills.py` | Grant/ready/launch regressions. |
| `docs/operations.md`, `.agents/skills/customize/SKILL.md`, `README.md` | Operating/security/scope contract. |

### Foundation task: Add disposable PR/Relay test support

**Files:**
- Create: `tests/pr_support.py`
- Modify: `tests/conftest.py`

**Interfaces:**

```text
create_ship_commit(home: Path, task_id: str) -> dict
write_report(home: Path) -> Path
make_relay_runtime(root: Path, version: str, executable: bool) -> Path
fake_npm_success(home: Path) -> Callable
FakeGitHub(open_prs: list[dict])
exact_open_pr(task: dict, number: int) -> dict
pr_payload(state="OPEN", head="abc", checks=None, merge="CLEAN", review=None) -> dict
required_policy(checks=(), reviews=0) -> dict
empty_rules() -> dict
make_remote_branch_not_ancestor_of(worktree: str, branch: str) -> None
remote_branch_sha(task: dict) -> str
git_sha(worktree: str) -> str
recording_relay_delivery(task: dict) -> Callable
seeded_delivery_receipt(home: Path, task: dict, grant: dict, lease_id: str) -> dict
rejecting_reply(argv, cwd=None, timeout=30, check=True) -> subprocess.CompletedProcess
launch_with_pr_grant(home: Path, task: dict, grant: dict) -> dict
published_exact_receipt(task: dict, grant: dict) -> dict
```

- [ ] **Step 1: Create helpers backed only by the existing disposable lab**

`create_ship_commit` writes a brief, calls existing `command(home, "task", "create", ...)`, changes `value.txt`, and commits with the local test Git identity. `write_report` creates a nonempty report. `make_relay_runtime` and `fake_npm_success` create only a validated fake private staging tree and return `subprocess.CompletedProcess`. The Git helpers create local bare-remote divergence and read its SHA. The Relay helpers return one schema-bearing `pr.automation.settled@1` delivery or a completion rejection while retaining the receipt. `FakeGitHub` returns only supplied provider payloads and records actual create calls; it never reproduces AGD receipt logic.

- [ ] **Step 2: Commit the reusable disposable support**

```bash
git add tests/pr_support.py
git commit -m "test: add disposable PR workflow support"
```

### Task 1: Vendor the known Relay skill set and provenance

**Files:**
- Create: `.agents/skills/using-open-relay/`, `.agents/skills/setting-up-open-relay/`, `.agents/skills/waiting-for-open-relay-events/`, `.agents/skills/automating-github-prs-with-open-relay/`, `.agents/skills/recovering-open-relay/`
- Create: matching approved worker bundles under `bundled/worker-skills/`, preserving upstream directory names/resources/LICENSEs
- Modify: `third_party/PROVENANCE.md`

**Interfaces:**
- Produces `RELAY_SKILL_IDS = ("using-open-relay", "setting-up-open-relay", "waiting-for-open-relay-events", "automating-github-prs-with-open-relay", "recovering-open-relay")` for `agd_lib.relay`.

- [ ] **Step 1: Obtain the pinned archive without executing it**

```bash
curl --fail --location --output /tmp/open-relay-dac2865.tar.gz https://github.com/feddericovonwernich/open-relay/archive/dac2865837bcab9444a8636053f4f1406a3f9291.tar.gz
tar -tzf /tmp/open-relay-dac2865.tar.gz | sed -n '/\/skills\//p'
```


- [ ] **Step 2: Copy only the five named canonical skill trees**

Copy their `SKILL.md`, referenced local resources, and Apache-2.0 license files without running package lifecycle scripts or modifying user skills. Keep upstream bodies verbatim; the only AGD-authored skill is Task 5's concise scoped worker bundle.

- [ ] **Step 3: Record provenance and run a throwaway integrity check**

Record source URL, SHA, package version, Apache-2.0, every copied file SHA-256, and the `ajv: ^8.17.1` transitive-resolution limitation. Run once, without committing a wording/copy test:

```bash
sha256sum .agents/skills/{using-open-relay,setting-up-open-relay,waiting-for-open-relay-events,automating-github-prs-with-open-relay,recovering-open-relay}/SKILL.md
```

Compare those five hashes with the provenance entries.

- [ ] **Step 4: Commit**

```bash
git add .agents/skills bundled/worker-skills third_party/PROVENANCE.md
git commit -m "feat: vendor pinned Open Relay skills"
```

### Task 2: Build safe, on-demand private Relay provisioning

**Files:**
- Create: `bin/agd_lib/relay.py`
- Test: `tests/test_relay.py`

**Interfaces:**

```text
find_compatible_system() -> Path | None
ensure_runtime(home: Path, run=store.run, which=shutil.which) -> Path
runtime_root(home: Path) -> Path
```

`ensure_runtime` returns either a metadata-proven compatible system runtime or `home/relay`; it raises `DependencyError` for Node/npm absence or Node < 22.19 and `Refusal` for unsafe, partial, or failed private state.

- [ ] **Step 1: Write failing behavior tests**

```python
def test_stale_system_candidate_results_in_usable_private_runtime(tmp_path):
    home = tmp_path / "home"
    stale = make_relay_runtime(tmp_path / "stale", version="0.0.9", executable=True)
    runtime = relay.ensure_runtime(home, which=lambda name: str(stale / "bin" / name), run=fake_npm_success(home))
    assert runtime == home / "relay"
    assert runtime_has_open_relay_010(runtime)


def test_partial_private_runtime_is_preserved_and_refused(tmp_path):
    home = tmp_path / "home"; (home / "relay").mkdir(parents=True)
    with pytest.raises(store.Refusal, match="partial"):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))
    assert (home / "relay").is_dir()
```

`fake_npm_success(home)` has the same callable shape as `store.run(argv, cwd=None, timeout=30, check=True)`: it creates a complete private staging tree containing `node_modules/open-relay/package.json` (`name: open-relay`, `version: 0.1.0`) and `dist/src/cli.js`, then returns `subprocess.CompletedProcess(argv, 0, "", "")`. It does not assert forwarded arguments or echo implementation internals.

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_relay.py -k 'stale_system or partial_private' -v`

Expected: FAIL because no runtime implementation exists.

- [ ] **Step 3: Implement discovery, locking, and atomic publish**

Use `lstat` on each private path, a private `state/relay-install.lock`, unique `state/relay-stage-$UUID` directories, and `os.replace` only after package metadata and compiled entry-point validation. Inspect the system executable's adjacent installation metadata; executable existence alone never suffices. Invoke Node only as `node -p process.versions.node`; invoke npm only after that gate using the exact global/private/pinned/archive/ignore-scripts/no-audit/no-fund command. Store an atomic marker with archive SHA, package version, Node version, and metadata digest. Refuse, rather than delete/overwrite, unsafe or partial stages/final directories.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest tests/test_relay.py -k 'stale_system or partial_private' -v`

Expected: PASS; the stale candidate cannot be selected and an unsafe existing private directory survives unchanged.

```bash
git add bin/agd_lib/relay.py tests/test_relay.py
git commit -m "feat: provision Open Relay privately"
```

### Task 3: Record the exact PR grant and receipt without adding a task phase

**Files:**
- Modify: `bin/agd`, `bin/agd_lib/tasks.py`, `tests/pr_support.py`
- Test: `tests/test_tasks.py`

**Interfaces:**

```text
grant_pr_publish(home: Path, task_id: str, target: str, instruction: Path) -> dict
authorize_pr_publish(home: Path, task: dict, grant_id: str) -> dict
pr_receipt_file(home: Path, task_id: str) -> Path
read_pr_receipt(home: Path, task_id: str) -> dict | None
write_pr_receipt(home: Path, task_id: str, receipt: dict) -> None
approve_pr(home: Path, task: dict) -> dict
commit_descendant(worktree: str, message: str) -> str
home_task fixture -> tuple[Path, dict, dict]
```

CLI: `task approve ID --action pr-publish --target REMOTE:BRANCH --instruction FILE`. Receipt fields are evidence (`grant_id`, remote/repository/base/branch, local/remote/PR heads, PR identity, Relay correlation/lease, verification, blocked reason); they are not a new task state machine.

- [ ] **Step 1: Write failing permission and ancestry tests**

```python
def test_pr_grant_rejects_other_target_and_divergent_head():
    with lab() as (home, _):
        task = create_ship_commit(home, "prgrant")
        grant = approve_pr(home, task)
        assert tasks.authorize_pr_publish(home, tasks.read(home, "prgrant"), grant["id"])["id"] == grant["id"]
        commit_descendant(task["worktree"], "repair")
        assert tasks.authorize_pr_publish(home, tasks.read(home, "prgrant"), grant["id"])["grant_head"] == grant["grant_head"]
        run(["git", "reset", "--hard", task["start"]], cwd=task["worktree"])
        with pytest.raises(store.Refusal, match="descendant"):
            tasks.authorize_pr_publish(home, tasks.read(home, "prgrant"), grant["id"])


def test_receipt_write_keeps_task_phase_unchanged():
    with lab() as (home, _):
        task = create_ship_commit(home, "receipt")
        before = tasks.read(home, "receipt")["phase"]
        tasks.write_pr_receipt(home, "receipt", {"schema": 1, "grant_id": "grant", "blocked_reason": "auth"})
        assert tasks.read(home, "receipt")["phase"] == before
```

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_tasks.py -k 'pr_grant or receipt_write' -v`

Expected: FAIL because the action and receipt APIs are absent.

- [ ] **Step 3: Implement the minimal records**

Accept `pr-publish` only for `ship`, a clean verified lease, and exact `REMOTE:task.branch`. Record task ID, action, target, instruction, grant head, branch, base, and UUID in the existing approval directory. Re-authorize task/incarnation/workspace/cleanliness/branch and `git merge-base --is-ancestor grant_head HEAD` before every guarded operation. Use atomic JSON for a regular non-symlink receipt file under the existing task directory. Add `approve_pr`, `commit_descendant`, and `home_task` to `tests/pr_support.py`; the fixture creates the committed task, grant, and current worker incarnation. Keep task phase `running` until Task 8 calls the existing ready path.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest tests/test_tasks.py -k 'pr_grant or receipt_write' -v`

Expected: PASS.

```bash
git add bin/agd bin/agd_lib/tasks.py tests/pr_support.py tests/test_tasks.py
git commit -m "feat: add scoped PR publication grants"
```

### Task 4: Implement the narrow GitHub boundary and authoritative final evidence

**Files:**
- Create: `bin/agd_lib/github.py`
- Test: `tests/test_github.py`

**Interfaces:**

```text
graphql(query: str, variables: dict, run=store.run) -> dict
find_open_pr(repository: str, head: str, base: str, run=store.run) -> list[dict]
create_pr(repository: str, head: str, base: str, title: str, body: str, run=store.run) -> dict
finalize(pr: dict, classic_protection: dict | None, rules: dict, expected_sha: str) -> dict
```

`graphql` calls `gh api graphql --method POST -f query=QUERY -f owner=OWNER -f name=REPOSITORY`; all variables are string raw fields. Credential/no-`gh`/nonzero/invalid JSON/GraphQL errors become `DependencyError` or `Refusal` with no token output.

- [ ] **Step 1: Write failing policy truth-table tests**

```python
@pytest.mark.parametrize("pr, classic_protection, rules, message", [
    (pr_payload(state="CLOSED"), required_policy(), empty_rules(), "open"),
    (pr_payload(head="other"), required_policy(), empty_rules(), "head"),
    (pr_payload(checks={"ci": "PENDING"}), required_policy(checks=["ci"]), empty_rules(), "checks"),
    (pr_payload(merge="BEHIND"), required_policy(), empty_rules(), "CLEAN"),
    (pr_payload(review="CHANGES_REQUESTED"), required_policy(reviews=1), empty_rules(), "APPROVED"),
    (pr_payload(review=None), required_policy(reviews=1), empty_rules(), "APPROVED"),
])
def test_finalize_rejects_nonfinal_or_missing_required_evidence(pr, classic_protection, rules, message):
    with pytest.raises(store.Refusal, match=message):
        github.finalize(pr, classic_protection, rules, expected_sha="abc")


def test_finalize_accepts_unprotected_branch_when_rules_report_no_requirements():
    result = github.finalize(pr_payload(review=None), None, empty_rules(), expected_sha="abc")
    assert result["head_sha"] == "abc"
```

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_github.py -v`

Expected: FAIL because the adapter does not exist.

- [ ] **Step 3: Implement exact `gh` calls**

Use this GraphQL PR query, bound by repository owner/name and PR number, to fetch PR identity/head/review/merge/check evidence:

```graphql
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id number url state isDraft mergedAt headRefName headRefOid baseRefName
      mergeStateStatus reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { nodes {
        ... on CheckRun { name status conclusion }
        ... on StatusContext { context state }
      } } } } } }
    }
  }
}
```

Read classic branch protection with `gh api --method GET repos/OWNER/REPOSITORY/branches/BASE/protection` and effective branch rules with `gh api --method GET repos/OWNER/REPOSITORY/rules/branches/BASE`. A 404 from classic protection means no classic protection and supplies zero classic requirements; it is not a failure when the rules endpoint is accessible and reports no requirements. Normalize accessible classic/rules responses into required check names and approval count, rejecting only an unavailable/unrecognized rules response, conflicting requirements, or incomplete policy data. `mergeStateStatus == CLEAN` remains mandatory and is the conservative final signal for merge-queue/ruleset cases.

Create only after a matching-PR search returns none, with:

```bash
gh pr create --repo OWNER/REPOSITORY --head TASK_BRANCH --base BASE --title "$TITLE" --body "$BODY" --no-maintainer-edit
```

The explicit `--head`, `--base`, `--title`, and `--body` prevent interactive selection, implicit base choice, and push/fork behavior. Parse the returned URL, then re-query it; never rely on stdout alone.

- [ ] **Step 4: Implement and test final evidence behavior**

Normalize only a same-repository, OPEN, unmerged PR with exact head/base and equal `headRefOid`. Check all required names from accessible policy against completed-success CheckRun/StatusContext states; require `mergeStateStatus == CLEAN`; require `APPROVED` only when policy requires approvals; fail closed on absent check contexts, missing review decision when approvals are required, unavailable rules data, or unrecognized policy fields. Run: `pytest tests/test_github.py -v` — expected PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/agd_lib/github.py tests/test_github.py
git commit -m "feat: verify GitHub PR policy directly"
```

### Task 5: Guard non-force publication and exact recovery

**Files:**
- Create: `bin/agd_lib/pr.py`
- Modify: `bin/agd`, `bin/agd_lib/tasks.py`
- Test: `tests/test_pr.py`

**Interfaces:**

```text
publish(home: Path, task_id: str, incarnation: str, grant_id: str, title: str, body: str, run=store.run, github_client=github) -> dict
verify_and_ready(home: Path, task_id: str, incarnation: str, grant_id: str, report: Path, run=store.run, github_client=github) -> dict
reconcile(home: Path, task_id: str, incarnation: str, grant_id: str, github_client=github) -> dict
```

CLI: `worker publish-pr ID --incarnation UUID --grant UUID --title FILE --body FILE`; `worker verify-pr ID --incarnation UUID --grant UUID --report FILE`. There is no raw remote/base/repository CLI argument.

- [ ] **Step 1: Write failing observable-boundary tests**

```python
def test_publish_adopts_one_exact_open_pr_without_creating_another(home_task):
    home, task, grant = home_task
    fake = FakeGitHub(open_prs=[exact_open_pr(task, number=9)])
    receipt = pr.publish(home, task["id"], "inc", grant["id"], "Title", "Body", github_client=fake)
    assert receipt["pr"]["number"] == 9
    assert fake.created_prs == []


def test_publish_refuses_remote_divergence_without_rewriting_history(home_task):
    home, task, grant = home_task
    make_remote_branch_not_ancestor_of(task["worktree"], task["branch"])
    with pytest.raises(store.Refusal, match="fast-forward"):
        pr.publish(home, task["id"], "inc", grant["id"], "Title", "Body")
    assert remote_branch_sha(task) != git_sha(task["worktree"])
```

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_pr.py -k 'adopts_one_exact or remote_divergence' -v`

Expected: FAIL because publication does not exist.

- [ ] **Step 3: Implement guarded publication**

Recheck current task/incarnation/grant/lease/clean workspace/ancestry/remote/base/branch before each mutation. Push only `git push REMOTE HEAD:refs/heads/TASK_BRANCH`; first reject a remote branch head that is not an ancestor of local HEAD. Record remote SHA from `git ls-remote`. Find matching open PRs before create and after ambiguous failures. Adopt exactly one PR whose repository/head branch/base branch/head SHA match; create only when none match; block on closed recorded PR, multiple matches, mismatch, failed/unknown create, or changed remote head. Persist sanitized raw provider evidence and receipt atomically. No merge/force/tag command belongs in this module.

- [ ] **Step 4: Add crash/head-race tests and run**

Add a test simulating a persisted receipt followed by a relaunch: `reconcile` adopts the exact matching PR and `FakeGitHub.created_prs` remains empty. Add a verifier test where initial evidence is green then the final remote or PR-head re-read changes; assert `tasks.ready` is not called and receipt remains non-final. Run: `pytest tests/test_pr.py -v` — expected PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/agd bin/agd_lib/pr.py bin/agd_lib/tasks.py tests/test_pr.py
git commit -m "feat: guard PR publication and recovery"
```

### Task 6: Add the actual central Relay lifecycle and delivery protocol

**Files:**
- Modify: `bin/agd_lib/relay.py`, `bin/agd_lib/workers.py`, `bin/agd_lib/supervision.py`
- Test: `tests/test_relay.py`, `tests/test_workers.py`

**Interfaces:**

```text
ensure_project_service(home: Path, project_id: str, repository: str) -> dict
poll(home: Path, task: dict, grant: dict, incarnation: str, run=store.run) -> dict
reply(home: Path, task: dict, grant: dict, incarnation: str, lease_id: str, action: str, payload: dict | None, run=store.run) -> dict
reconcile_task(home: Path, task: dict) -> dict | None
```

Service record: `home/state/relay/projects/PROJECT_ID.json`. Correlation ID: `github:owner/repository:pull-request:NUMBER`, where owner/repository is lowercased after canonical GitHub remote parsing. Stable project root: `home/relay/projects/PROJECT_ID`; never the task worktree.

- [ ] **Step 1: Write failing delivery/lease behavior tests**

```python
def test_poll_uses_real_protocol_shape_and_central_root(home_task):
    home, task, grant = home_task
    result = relay.poll(home, task, grant, "worker-inc", run=recording_relay_delivery(task))
    assert result["lease_id"] == "lease-1"
    assert result["correlation_id"] == "github:owner/repo:pull-request:9"
    assert result["definition"] == "pr.automation.settled@1"


def test_completion_rejection_keeps_lease_unsettled(home_task):
    home, task, grant = home_task
    receipt = seeded_delivery_receipt(home, task, grant, lease_id="lease-1")
    with pytest.raises(store.Refusal, match="schema"):
        relay.reply(home, task, grant, "worker-inc", "lease-1", "complete", {"result": {}}, run=rejecting_reply)
    assert tasks.read_pr_receipt(home, task["id"])["relay"]["lease"]["status"] == "active"
```

The `run` fakes accept `argv`, `cwd`, `timeout`, and `check`, return `subprocess.CompletedProcess`, and simulate only Relay subprocess JSON. Assertions examine receipt state and delivery contract, not forwarded command text.

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_relay.py tests/test_workers.py -k 'protocol_shape or lease_unsettled' -v`

Expected: FAIL because no service/poll/reply implementation exists.

- [ ] **Step 3: Implement service ownership and exact worker protocol**

At first valid grant, call `ensure_runtime`, create/inspect the central project root, and run `relay init --github OWNER/REPOSITORY` only when its generated `.relay` content is absent; preserve/refuse a modified existing root. Configure connector `tokenEnv` only in the central root and enforce read-only GitHub permissions (Checks, Pull requests, Metadata). Run discovery once with `relay connect github --config .relay/connectors/github.json --once --discover`; inspect and pin returned provider app IDs/slugs before the continuing connector. Start only one owned loopback Relay process and one `relay connect github --config .relay/connectors/github.json` process per project, retaining PID/start identity and configuration digest. The connector observes every configured project PR; workers select their assigned PR solely with the exact correlation filter. A second connector for the same project is refused. Stop owned processes on terminal success or durable block after lease settlement; never start them from a worker.

For a worker poll, use the exact one-shot command with `timeout=610` in `store.run` so the Relay default 600-second park plus process overhead can complete:

```bash
relay agent poll AGD_WORKER_ID --project-root CENTRAL_RELAY_ROOT --definitions pr.automation.settled@1 --structured-output --context-tokens 5000 --correlation-id github:owner/repository:pull-request:PR_NUMBER
```

On `{"type":"timeout"}`, record an idle observation and permit one immediate later poll; never loop inside AGD and never poll with an active lease. On delivery, persist lease ID, exact definition/correlation, event/PR/head identity, and delivered `outputSchema`, then require:

```bash
relay agent reply LEASE_ID start --project-root CENTRAL_RELAY_ROOT
relay agent reply LEASE_ID complete --project-root CENTRAL_RELAY_ROOT --json '{"result":{"summary":"verified current-head feedback handled"},"effects":[]}'
```

For this pinned `pr.automation.settled@1`, generated output schema is an object with exactly required string `summary`; validate the proposed result against that delivered schema before `complete`. A Relay HTTP 400/schema rejection leaves the lease active; preserve it for correction. For invalid, stale, or untrusted feedback, record why and settle with `relay agent reply LEASE_ID cancelled --project-root CENTRAL_RELAY_ROOT --json '{"effects":[]}'`; do not re-poll until Relay accepts the terminal reply. Relay restart/401/409 invalidates local lease authority: mark receipt blocked and use recovery rather than guessing.

- [ ] **Step 4: Run protocol tests and commit**

Run: `pytest tests/test_relay.py tests/test_workers.py -k 'protocol_shape or lease_unsettled' -v`

Expected: PASS; a poll receipt is correlated/leased and a rejected completion does not erase it.

```bash
git add bin/agd_lib/relay.py bin/agd_lib/workers.py bin/agd_lib/supervision.py tests/test_relay.py tests/test_workers.py
git commit -m "feat: supervise Relay PR feedback leases"
```

### Task 7: Provision the scoped worker skill and remove conflicting default prompt text

**Files:**
- Create: `bundled/worker-skills/agd-scoped-pr-relay/SKILL.md`
- Modify: `bin/agd`, `bin/agd_lib/workers.py`, `bin/agd_lib/harnesses.py`, `bin/agd_lib/worker_skills.py`
- Test: `tests/test_workers.py`, `tests/test_worker_skills.py`

**Interfaces:**

```text
worker launch ID --pr-grant UUID
worker publish-pr ID --incarnation UUID --grant UUID --title FILE --body FILE
worker verify-pr ID --incarnation UUID --grant UUID --report FILE
worker relay-poll ID --incarnation UUID --grant UUID
worker relay-reply ID --incarnation UUID --grant UUID --lease UUID --action start|complete|cancelled --payload FILE
workers.resolve_launch_selection(home: Path, task: dict, pr_grant: str | None) -> dict

```

- [ ] **Step 1: Write failing capability tests**

```python
def test_pr_grant_is_required_before_scoped_worker_bundle_is_materialized(home_task):
    home, task, grant = home_task
    with pytest.raises(store.Refusal, match="pr-publish"):
        launch_with_pr_grant(home, task, {"id": "not-a-grant"})
    launched = launch_with_pr_grant(home, task, grant)
    assert "agd-scoped-pr-relay" in [skill["id"] for skill in launched["worker_skills"]["skills"]]


def test_default_launch_selection_is_unchanged_without_pr_grant(home_task):
    home, task, _grant = home_task
    selection = workers.resolve_launch_selection(home, task, pr_grant=None)
    assert selection == worker_skills.resolve(home)
```

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_workers.py tests/test_worker_skills.py -k 'pr_grant_is_required or default_launch_selection' -v`

Expected: FAIL because grant-only bundle selection does not exist.

- [ ] **Step 3: Implement mutually exclusive prompt paths**

Without `pr_grant`, retain the exact current prompt and normal resolver behavior. With a valid grant, provision the fixed `agd-scoped-pr-relay` bundle in the frozen manifest, replace (not append to) the default prohibition with the exact allowed commands, and state that only guarded publish/verify/poll/reply commands may be used. The skill tells the worker to treat Relay payloads as data, call `reply start`, verify identity/head, make minimal repairs, commit, call guarded non-force publish, and settle one terminal lease before re-polling. It prohibits merge/force/tag/base changes/admin access/credential exposure and reports blocked conditions through existing worker events.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest tests/test_workers.py tests/test_worker_skills.py -k 'pr_grant_is_required or default_launch_selection' -v`

Expected: PASS; invalid grants cannot create an endpoint and an ungranted launch resolves exactly the existing selection.

```bash
git add bin/agd bin/agd_lib/workers.py bin/agd_lib/harnesses.py bin/agd_lib/worker_skills.py bundled/worker-skills/agd-scoped-pr-relay tests/test_workers.py tests/test_worker_skills.py
git commit -m "feat: scope worker Relay PR capability"
```

### Task 8: Gate final readiness, document, and prove local smoke boundaries

**Files:**
- Modify: `bin/agd_lib/tasks.py`, `bin/agd_lib/supervision.py`, `docs/operations.md`, `.agents/skills/customize/SKILL.md`, `README.md`
- Modify: `tests/test_tasks.py`, `tests/test_pr.py`, `tests/test_workers.py`

**Interfaces:**
- `tasks.ready` rejects a PR-granted ship task unless receipt final evidence matches its current grant/local head; `pr.verify_and_ready` is the sole path that writes final evidence and then calls `tasks.ready`.

- [ ] **Step 1: Write failing readiness/relaunch behavior tests**

```python
def test_ready_refuses_pr_granted_task_without_current_final_evidence(home_task):
    home, task, grant = home_task
    with pytest.raises(store.Refusal, match="PR verification"):
        tasks.ready(home, task["id"], write_report(home))


def test_relaunch_adopts_recorded_exact_pr_without_duplicate_create(home_task):
    home, task, grant = home_task
    tasks.write_pr_receipt(home, task["id"], published_exact_receipt(task, grant))
    fake = FakeGitHub(open_prs=[exact_open_pr(task, number=9)])
    assert pr.reconcile(home, task["id"], "new-inc", grant["id"], fake)["pr"]["number"] == 9
    assert fake.created_prs == []
```

- [ ] **Step 2: Run to prove failure**

Run: `pytest tests/test_tasks.py tests/test_pr.py -k 'current_final_evidence or duplicate_create' -v`

Expected: FAIL because final receipt gating/reconciliation is absent.

- [ ] **Step 3: Implement gate, race rejection, and durable block mapping**

`verify_and_ready` obtains Task 4 evidence, compares exact task repo/head/base and local SHA, reads `git ls-remote` and GitHub PR head again under the home lock, records both comparisons, and only then calls `tasks.ready`. Any changed head clears final evidence and returns to reconciliation; auth loss, closed/mismatched PR, missing policy, install failure, expired lease, or push divergence writes an actionable `blocked_reason`. Extend `supervision.reconcile` to emit the existing durable blocked event once per receipt reason/incarnation, retaining the worktree. Relay's `pr.verification.settled@1` is never a ready signal.

- [ ] **Step 4: Add the disposable direct CLI smoke and execute it**

Use fake Relay/GitHub subprocess boundaries plus the existing disposable `lab()` bare remote to launch a granted worker identity, publish/adopt one fake exact PR, complete a fake current-head verification, and assert the task becomes ready. The fake boundary must return a `CompletedProcess` and provider payloads, not assert AGD argument forwarding. Confirm the test never receives a credential environment variable and never opens a network socket.

Separately, record the already exercised isolated Relay CLI smoke as an implementation acceptance command, not a claim that polling or GitHub automation passed:

```bash
npm install --global --prefix "$TMP/prefix" --ignore-scripts --no-audit --no-fund https://github.com/feddericovonwernich/open-relay/archive/dac2865837bcab9444a8636053f4f1406a3f9291.tar.gz
"$TMP/prefix/bin/relay" init
"$TMP/prefix/bin/relay" start --port 0
"$TMP/prefix/bin/relay" emit example.requested 1 '{"message":"smoke"}'
"$TMP/prefix/bin/relay" get EVENT_ID
"$TMP/prefix/bin/relay" stop
```

Observed isolated facts: Node v24.14.1/npm 11.11.0; install returned 0 with `open-relay@0.1.0`; init/start/emit/get/stop returned successfully, start used loopback/assigned port, get returned the emitted event, and the temporary directory/owned process were cleaned. This did **not** exercise `agent poll`, lease settlement, connector feedback, or GitHub publication.

- [ ] **Step 5: Update docs, verify, and commit**

Document the exact grant/CLI commands, normal-worker non-impact, private runtime paths, same-Unix-identity non-sandbox limitation, required GitHub `gh` authentication/access, no merge/force/tag guarantee, one central read-only connector, ten-minute lease behavior, blocked/relaunch outcomes, and requirement for explicit target-scoped approval before any credentialed smoke.

Run: `pytest -q`

Expected: PASS. Then run the fake-boundary disposable CLI smoke from Step 4. Do not run a credentialed GitHub publication, install in a user home, or start/change a user service.

```bash
git add bin/agd_lib/tasks.py bin/agd_lib/supervision.py docs/operations.md .agents/skills/customize/SKILL.md README.md tests/test_tasks.py tests/test_pr.py tests/test_workers.py
git commit -m "feat: gate ready on verified Relay PR workflow"
```

## Plan self-review

- **Spec coverage:** Tasks 1–2 cover pinned Relay skills/provenance and private provisioning. Tasks 3, 5, and 8 cover exact authority, recovery, final-ready/head-race, and durable blocks. Task 4 provides GitHub final authority and credentials failures. Tasks 6–7 provide a single central read-only Relay connector, actual poll/reply lease settlement, same-worker scope, and the corrected non-conflicting prompt. Task 8 covers docs and disposable smoke.
- **No permanent incidental tests:** no test asserts documentation/provenance wording, copied-file presence, raw command forwarding, or mock echo. Tests exercise observable authority, lease, PR identity, head race, and final-ready behavior. Provenance integrity uses a throwaway check.
- **Known external boundary:** GitHub rules endpoint access and output vary by repository plan/ruleset. A classic-protection 404 is explicitly the unprotected case only when accessible rules report no requirements; unavailable or unrecognized rules remain blocked. No completion claim is valid without an explicitly approved credentialed target acceptance run. The pinned Relay commands/definitions/output schema in Task 6 are sourced from its pinned `src/cli.ts`, `src/agent-session.ts`, and `src/init.ts`, not guessed.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-26-scoped-worker-pr-relay.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks.
2. **Inline Execution** — execute tasks in this session using `executing-plans`, with review checkpoints.

Choose an approach before implementation; this planning task does not start implementation.
