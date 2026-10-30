# Worker Skill Isolation and Retained Replies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each OMP worker expose only its frozen task skills and turn every `worker send` into a durable, correlated reply from the same retained worker session.

**Architecture:** OMP's native `--no-extensions`, `--no-skills`, and `--skills` flags enforce the task manifest at process launch while preserving the explicit AGD worker extension. The existing disk inbox becomes a FIFO request queue; the extension submits one native OMP follow-up at a time and records terminal `agent_end` text through an idempotent worker-scoped CLI completion command, which reuses supervision's durable event wake path.

**Tech Stack:** Python 3.12 standard library, OMP 18.14 extension API, TypeScript/Bun, existing JSON task store and supervision watcher, pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-worker-skill-isolation-and-replies-design.md`

## Global Constraints

- Keep central OMP project plugins installed and enabled; isolate only worker processes.
- Every OMP worker passes `--no-extensions`; explicit `-e lib/omp-worker.ts` must still load.
- Worker skills come only from `task.worker_skills.skills`; empty or absent manifests mean `--no-skills`.
- `worker send` remains asynchronous and is the only central request API; do not add blocking waits or `worker ask`.
- Process requests FIFO, one active request per worker session.
- Persist reply/observation before acknowledgement; accepted requests may redeliver after a crash but must not disappear.
- Ready workers may answer follow-ups; existing lease, Git cleanliness, and ready-head checks remain unchanged.
- No new dependency, plugin installer, compatibility alias, or migration of already-running workers.

## Review Focus

- Shared XDG plugin cache with a `none` task: native worker-visible skill discovery must be empty, not merely `plugin list`.
- A frozen skill name also present in an installed plugin: the worktree snapshot must be the only exposed source.
- Concurrent `worker send` calls: both requests persist with unique increasing sequence numbers and are consumed FIFO.
- Completion interrupted after reply or observation write: retry repairs missing later artifacts and emits one central event.
- Worker becomes missing after an accepted send: the request remains unacknowledged for relaunch; new sends to missing/stopped/unknown workers refuse.

---

### Task 1: Enforce the exact native OMP worker capability surface

**Files:**
- Modify: `bin/agd_lib/harnesses.py:13-64`
- Modify: `tests/test_harnesses.py:10-18`
- Modify: `tests/test_workers.py:392-453`
- Modify: `tests/smoke_native_plugins.py:369-405`

**Interfaces:**
- Consumes: existing schema-2 `task.worker_skills.skills` records with canonical `id` fields.
- Produces: `harnesses.worker_skill_args(task: dict) -> list[str]`; `launch_argv()` includes its result before the explicit `-e` worker extension.

- [ ] **Step 1: Add failing launch-argument tests**

Add `test_omp_argv_disables_ambient_extensions_and_empty_skills` and `test_omp_argv_allows_only_manifest_skills` in `tests/test_harnesses.py`.

```python
assert '--no-extensions' in empty_argv
assert '--no-skills' in empty_argv
assert '--skills' not in empty_argv
assert empty_argv[empty_argv.index('-e') + 1].endswith('lib/omp-worker.ts')

assert '--no-extensions' in selected_argv
assert selected_argv[selected_argv.index('--skills') + 1] == 'ponytail,systematic-debugging'
assert '--no-skills' not in selected_argv
```

Construct `selected_argv` from a manifest whose input records are deliberately reversed; the expected allowlist remains sorted. Extend `test_granted_launch_uses_frozen_private_scoped_skill_and_prompt` in `tests/test_workers.py` to require `--skills agd-scoped-pr-relay` and `--no-extensions`.

- [ ] **Step 2: Run the launch tests and verify RED**

Run: `python3 -m pytest tests/test_harnesses.py tests/test_workers.py::test_granted_launch_uses_frozen_private_scoped_skill_and_prompt -q`

Expected: FAIL because current launch arguments contain neither discovery opt-out nor an exact allowlist.

- [ ] **Step 3: Implement native launch isolation**

Add:

```python
def worker_skill_args(task: dict) -> list[str]: ...
```

Read IDs from `(task.get('worker_skills') or {}).get('skills', ())`. Return `['--no-skills']` when empty; otherwise return `['--skills', ','.join(sorted(ids))]`. In `launch_argv`, construct OMP argv with `--no-extensions`, these skill arguments, `--cwd`, optional `--session-dir`, and the existing explicit `-e` extension. Do not change central or Cursor launch paths.

- [ ] **Step 4: Run focused launch tests and verify GREEN**

Run: `python3 -m pytest tests/test_harnesses.py tests/test_workers.py::test_granted_launch_uses_frozen_private_scoped_skill_and_prompt -q`

Expected: PASS.

- [ ] **Step 5: Replace the false plugin-isolation smoke assertion**

In `tests/smoke_native_plugins.py`, invoke `omp skill list WORKTREE --json` with the same `--no-extensions` plus `--no-skills` or exact `--skills` launch controls. Assert that a `--skill-mode none` worktree exposes no skills and that the default-profile worktree exposes exactly the frozen `SKILLS` names. Assert each selected record's resolved source path is under the worktree's `.omp/skills`, never `data/omp/.../plugins/cache`; use the field defined by the installed OMP JSON schema rather than assuming plugin-list output. Retain the real explicit-extension worker launch already exercised by the smoke.

- [ ] **Step 6: Run the native smoke's non-networked checks**

Run: `python3 -m py_compile tests/smoke_native_plugins.py bin/agd_lib/harnesses.py && python3 -m pytest tests/test_harnesses.py -q`

Expected: syntax succeeds and focused tests pass. The full opt-in backend smoke runs in Task 3.

- [ ] **Step 7: Commit exact worker capability isolation**

```bash
git add bin/agd_lib/harnesses.py tests/test_harnesses.py tests/test_workers.py tests/smoke_native_plugins.py
git commit -m "fix: isolate worker OMP capabilities"
```

### Task 2: Add durable FIFO request completion and supervision correlation

**Files:**
- Modify: `bin/agd:91-109,343-360`
- Modify: `bin/agd_lib/workers.py:22-38,197-230`
- Modify: `tests/test_workers.py:299-311`
- Modify: `tests/test_supervision.py:1-160`

**Interfaces:**
- Consumes: current task ownership, backend `inspect`, inbox records, and `supervision.reconcile` handling of `done`/`blocked` observations.
- Produces:
  - `workers.complete(home: Path, task_id: str, request_id: str, incarnation: str, status: str, response: str) -> dict`
  - `worker complete TASK REQUEST_ID --incarnation UUID --status done|failed`, reading response text from stdin.
  - Inbox schema 1 fields: `schema`, `id`, `body`, `incarnation`, `sequence`, `queued_at`.
  - Reply schema 1 fields: `schema`, `id`, `enqueueing_incarnation`, `completed_incarnation`, `status`, `body`, `completed_at`, `observation_id`.

- [ ] **Step 1: Add failing FIFO and send-eligibility tests**

Replace the old inbox-only test with tests that construct a live worker and patch `workers.inspect` to return the existing shape `{'task': task, 'endpoint': {'presence': 'present'}}`.

Add:

```python
def test_send_requires_verified_live_worker_but_allows_ready(): ...
def test_concurrent_sends_receive_unique_fifo_sequences(): ...
def test_pending_request_survives_worker_crash_for_relaunch(): ...
```

Assert ready/live accepts; `worker_alive=False`, missing, and unknown endpoint states refuse without an inbox file. Submit concurrent sends with `ThreadPoolExecutor`, then assert `worker inbox` returns both bodies ordered by integer sequences `[1, 2]`. After an accepted send, set `worker_alive=False` and assert the inbox record remains.

- [ ] **Step 2: Run send tests and verify RED**

Run: `python3 -m pytest tests/test_workers.py -k 'send or inbox or pending_request' -q`

Expected: FAIL because current messages have no sequence and send does not prove a live endpoint.

- [ ] **Step 3: Implement sequenced send and inbox reads**

Read and inspect the worker before taking the global store lock; backend inspection may execute an external command. Under `store.lock(home)`, make `workers.send` re-read the task and require the inspected task's incarnation/endpoint to still match. Accept only `phase in ('running', 'ready')`, `executor == 'worker'`, `worker_alive is True`, and inspected endpoint presence `present`. Increment `data/tasks/TASK/inbox-sequence.json` atomically and write the schema-1 inbox record. Keep the external return shape `{id, delivery: 'queued', inbox}`.

Make `workers.inbox` validate every record and return pending records ordered by `(sequence, id)`. Before listing, repair any reply journal missing its deterministic observation or final acknowledgement, then filter by acknowledgement. This restart path finishes an interrupted completion without rerunning the model.

- [ ] **Step 4: Run send tests and verify GREEN**

Run: `python3 -m pytest tests/test_workers.py -k 'send or inbox or pending_request' -q`

Expected: PASS.

- [ ] **Step 5: Add failing completion, recovery, and notification tests**

Add:

```python
def test_complete_is_scoped_idempotent_and_acknowledges_last(): ...
def test_complete_repairs_reply_without_observation_or_ack(): ...
def test_inbox_repairs_interrupted_completion_after_process_restart(): ...
def test_prior_incarnation_request_completes_after_relaunch(): ...
def test_failed_completion_emits_one_blocked_event(): ...
```

Assert foreign environment/incarnation and unknown request refuse. A successful completion writes reply, deterministic observation, then acknowledgement; the same immutable inputs return the existing record. Simulate interruption by preserving reply while removing observation and acknowledgement, then assert both direct retry and a later `worker inbox` repair recreate the missing artifacts without exposing the request for another model turn. A request enqueued under `old` may complete from the current `new` incarnation. After repeated `session reconcile`, `events list` contains exactly one correlated `done` or `blocked` event whose details equal the stored body.

- [ ] **Step 6: Run completion tests and verify RED**

Run: `python3 -m pytest tests/test_workers.py tests/test_supervision.py -k 'complete or completion' -q`

Expected: FAIL because `worker complete` and deterministic reply observations do not exist.

- [ ] **Step 7: Implement idempotent completion**

Refactor observation persistence behind:

```python
def _observation(home: Path, task_id: str, incarnation: str, kind: str,
                 key: str, text: str, observation_id: str | None = None) -> dict: ...
```

Keep `workers.event` behavior by reading its message file and delegating to the helper with a random ID. Implement `workers.complete` with deterministic `uuid.uuid5(uuid.NAMESPACE_URL, f'{task_id}:{request_id}:reply')`. Validate current `AGD_TASK_ID`/`AGD_INCARNATION`, current task incarnation, request UUID, status, and nonempty response. Under the task lock, validate an existing reply against the immutable request/status/body/incarnation inputs while preserving its original `completed_at`; write or repair reply, observation, and acknowledgement in that order. Share that repair routine with `workers.inbox`. Map `done` to observation kind `done`; map `failed` to `blocked`.

- [ ] **Step 8: Wire the completion CLI and delete obsolete acknowledgement**

Replace `acknowledge` in the worker parser with `complete`; add `message_id`, required `--incarnation`, and required `--status` choices. Dispatch with `sys.stdin.read()` to `workers.complete`. Remove `workers.acknowledge` and all direct tests/callers; there is no compatibility alias.

- [ ] **Step 9: Run worker and supervision tests and verify GREEN**

Run: `python3 -m pytest tests/test_workers.py tests/test_supervision.py -q`

Expected: PASS.

- [ ] **Step 10: Commit the durable reply protocol**

```bash
git add bin/agd bin/agd_lib/workers.py tests/test_workers.py tests/test_supervision.py
git commit -m "feat: persist correlated worker replies"
```

### Task 3: Complete follow-up turns in the retained OMP session

**Files:**
- Modify: `lib/omp-worker.ts:1-53`
- Create: `tests/worker.test.ts`
- Modify: `tests/smoke_native_plugins.py`
- Modify: `README.md:37-41`
- Modify: `docs/operations.md:21-31,53-63`
- Modify: `CHANGELOG.md:3-6`
- Include: `docs/superpowers/specs/2026-10-30-worker-skill-isolation-and-replies-design.md`
- Include: `docs/superpowers/plans/2026-10-30-worker-skill-isolation-and-replies.md`

**Interfaces:**
- Consumes: Task 2's ordered `worker inbox` output and `worker complete ... --status` stdin contract; OMP 18.14 `AgentEndEvent.messages` and `sendUserMessage` extension APIs.
- Produces: `assistantReply(messages: unknown[]) -> string | undefined` and a one-active-request extension lifecycle that durably completes each terminal follow-up.

- [ ] **Step 1: Add failing Bun extension tests**

Create `tests/worker.test.ts` using `mock.module('node:child_process', ...)` before a query-string import of `lib/omp-worker.ts`. Capture extension handlers and `sendUserMessage` calls.

Add tests asserting:

- after the initial terminal `agent_end` establishes idle, two inbox records are read but only the first body is delivered as `{deliverAs: 'followUp'}`;
- `agent_end` with `willContinue: true` does not complete;
- terminal `agent_end` concatenates only final assistant text blocks and invokes `worker complete` with matching request ID, current incarnation, `--status done`, and response on stdin;
- a nonzero completion retains the active response and the timer retries completion without a second model turn;
- no assistant text completes with `--status failed` and exact stdin `steering turn ended without assistant text`;
- after successful completion, the next queued body is delivered; shutdown clears polling.

- [ ] **Step 2: Run the Bun test and verify RED**

Run: `bun test tests/worker.test.ts`

Expected: FAIL because the current extension sends a meta-instruction, exposes no message extractor, and records only idle.

- [ ] **Step 3: Implement the retained request/reply state machine**

Export:

```typescript
export function assistantReply(messages: unknown[]): string | undefined
```

Return the last assistant message's string content or concatenated `type: 'text'` block text; ignore thinking/tool blocks and return `undefined` for empty text.

Replace the `rung` set with one active request containing ID and optional captured completion. Preserve the initial `unknown` turn state so queued follow-ups cannot preempt the task's original turn. `checkInbox` first retries an unsaved completion, otherwise delivers only the first queued body when idle. A terminal `agent_end` with no active request only establishes idle; with an active request it captures the response/status and calls `worker complete` with `spawnSync(..., {input: body})`. Clear active state only on status 0, then check the next inbox item. Keep busy/idle observations and session timer cleanup.

- [ ] **Step 4: Run Bun and Python focused tests and verify GREEN**

Run: `bun test tests/worker.test.ts && python3 -m pytest tests/test_workers.py tests/test_supervision.py tests/test_harnesses.py -q`

Expected: PASS.

- [ ] **Step 5: Extend the opt-in native smoke through two retained turns**

After launching the real OMP worker, send two distinct files sequentially. For each request, wait for the correlated `done` event, assert its details contain that turn's assistant response, acknowledge the event, and assert task incarnation and endpoint identity did not change. For the `none` task, assert the worker session reports no skills; for the selected task, assert exact manifest names and worktree paths. Stop through the existing guarded idle path.

- [ ] **Step 6: Run the actual changed surface**

Run from a disposable backend-owned environment:

```bash
python3 tests/smoke_native_plugins.py --backend herdr
```

Expected: the script reports exact worker skill isolation and two correlated replies from one incarnation, then proves worker/process cleanup. If no managed Herdr pane is available, run the tmux variant when tmux is available; otherwise report this acceptance prerequisite as unexercised rather than weakening the smoke.

- [ ] **Step 7: Update operator documentation**

Document in README and operations:

- workers are retained task executors, not one-shot disposable agents;
- `worker send NAME --file FILE` queues an asynchronous request whose terminal reply becomes a durable central event;
- only one request runs at a time and queued requests survive crash/relaunch at least once;
- stopped/missing/unknown workers reject new sends;
- exact task skills are enforced with native OMP discovery controls;
- ready workers may answer read-only follow-ups, while post-ready workspace changes trigger existing integrity checks;
- `worker acknowledge` is removed.

Add one Unreleased changelog bullet covering both corrected boundaries.

- [ ] **Step 8: Run full verification**

Run:

```bash
python3 -m pytest tests -q
bun test tests/primary.test.ts tests/worker.test.ts tests/reviewer.test.ts tests/reviewer-telemetry.test.ts
python3 -m py_compile bin/agd bin/agd_lib/harnesses.py bin/agd_lib/workers.py tests/smoke_native_plugins.py
git diff --check
```

Expected: all available tests pass. Environment-only backend tests may be reported as unavailable only when their executable/server prerequisite is absent; do not convert that absence into passing evidence.

- [ ] **Step 9: Request whole-branch review and fix findings**

Review against the spec, focusing on skill-source leakage, message loss/duplication, foreign-incarnation completion, ready-worker semantics, and cleanup retention. Fix every Critical/Important finding and rerun the affected focused tests.

- [ ] **Step 10: Commit integration, smoke, and docs**

```bash
git add lib/omp-worker.ts tests/worker.test.ts tests/smoke_native_plugins.py README.md docs/operations.md CHANGELOG.md docs/superpowers/specs/2026-10-30-worker-skill-isolation-and-replies-design.md docs/superpowers/plans/2026-10-30-worker-skill-isolation-and-replies.md
git commit -m "feat: complete retained worker follow-ups"
```
