# Background Skill Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, bounded background reviewer that turns selected completed-worker evidence into durable skill-improvement or new-skill proposals without ever applying them.

**Architecture:** Reuse the merged observability module's private-home/native OMP session conventions, but add a reviewer-owned state namespace and a separate evidence broker/reader. The broker freezes a bounded packet before an isolated OMP reviewer starts; an explicit reviewer extension exposes only excerpt read, packet search, and report submission. A one-shot runner is invoked manually or by an explicitly enabled systemd user timer, while existing supervision events notify an available central session.

**Tech Stack:** Python 3.12 stdlib; existing `bin/agd`, `agd_lib.store`, `agd_lib.supervision`, merged `agd_lib.observability`; TypeScript/Bun OMP extensions; installed OMP 18.3.2 native `SessionManager`/`AgentSession`; systemd user timers; pytest and Bun test.

**Spec:** `docs/superpowers/specs/2026-10-27-background-skill-review-design.md`

## Global Constraints

- Before source edits, reconcile the isolated implementation worktree with the current approved source history: fetch `origin/main`, verify the approved spec/plan commits and merged observability commit `68c3a8cba5007419a467516b792d0df6716ace9a` are ancestors of the reconciled `HEAD`, and merge normally. Never reset, rebase, force-checkout, clean, drop documentation, or modify the stable checkout.
- Reuse `store.home`, `store.lock`, `store.atomic_json`, `store.require_owner`, `supervision.emit`, `supervision.pending`, and `supervision.acknowledge`; do not change their existing task semantics. Reviewer mutations must pass the same live-central owner guard, so a timer/foreign process cannot take over a live central home. A reviewer has its own per-home lock and never holds `store.lock(home)` across OMP/model/network/systemd waits.
- Native OMP JSONL is authoritative. Langfuse is optional span discovery only. The existing OpenTelemetry Collector remains an observability transport and never reads JSONL or runs review logic; do not deploy a new Collector, index, provider, SDK, or generic query API.
- The reviewer never changes reviewed skills/sources/profiles/task lifecycle records. It writes only its private config, reviewer native output/artifacts, immutable evidence/review/proposal revisions, mutable human triage, scheduler checkpoints/receipts, and existing event receipts.
- Default limits are user-approved: 20 distinct episode candidates/run, 262144 UTF-8 broker-input bytes/run, 20 native reviewer model invocations including tool continuations/run, 65536 UTF-8 persisted report bytes/run, and a 1800-second wall-clock runner deadline. They do not promise a USD ceiling or bound hidden provider transport retries.
- Freeze validated configuration, resolved model, instruction-bundle hash, packet ID, source versions, and cutoff in each run record before the first model call. Later model/schedule/configuration changes apply only to later runs.
- The model receives no provider/Langfuse credential, shell, generic filesystem/network/SQL/Git/MCP/package/task/publish tool, or arbitrary path/task/Langfuse-key query. Tool restriction is a model-tool boundary, not an OS sandbox.
- Reviewer native JSONL/artifacts are always persisted under `AGD_HOME` through the native OMP lifecycle even with telemetry disabled. Existing observability transport, when enabled, adds only `purpose=background-skill-review` and `review_run_id`; it must not create ordinary task IDs or change native conversation IDs.
- A timer is inert until explicitly enabled with separate approval. Use a bounded systemd **user** service/timer, never cron, linger, a resident daemon, or automatic installation/start at `agd init`. Periodic triggers may coalesce only into one compatible private pending-intent record; explicit retrospective run-ID selections are separate immutable scoped intents and are never dropped, widened, or silently folded into periodic/default work.
- No proposal creates a task or edits a skill. Event acknowledgement, proposal acceptance, and authorization to implement are distinct actions. Reviewer runs are excluded from default selection; explicit retrospective self-review selects only closed prior run IDs and excludes current/later IDs.

---

## Code map and interfaces

| Path | Change and responsibility |
|---|---|
| `bin/agd` | Add complete `reviewer` command group only after backing operations exist; keep read-only operations outside current mutation gate and route reviewer mutators through normal owner protection. |
| `bin/agd_lib/reviewer.py` | Private-path safety, strict config, deterministic episode selection/packet records, immutable evidence/review/proposal revision persistence, triage, dedicated lock/checkpoint, and durable batch-event emission. |
| `bin/agd_lib/reviewer_runner.py` | Owns one bounded reviewer run: freezes the run receipt, resolves model, creates native OMP reviewer session binding, invokes the verified native reviewer bootstrap, and records retryable receipts outside the home lock. |
| `lib/agd-reviewer.ts` | Trusted native OMP bootstrap/extension module. It constructs the restricted `Agent`/`AgentSession` from the installed OMP package, registers only broker tools, wraps native `streamSimple` through `streamFn` for run-wide call/deadline enforcement, and writes only via trusted runner IPC. |
| `bin/agd_lib/reviewer_timer.py` | Renders/installs/removes the private systemd user unit files, parses native `systemd-analyze calendar` output for calendar validation, and computes checkpointed catch-up/no-drift eligibility. |
| `tests/test_reviewer.py` | Core packet, provenance, proposal/triage/deduplication, lock/checkpoint/recovery, event, and CLI behavior. |
| `tests/test_reviewer_timer.py` | Pure schedule/checkpoint semantics and disposable user-unit rendering/explicit enable guards. |
| `tests/reviewer.test.ts` | Actual installed OMP 18.3.2 `Agent`/`AgentSession`/`SessionManager` offline-stream integration: supported-bootstrap capability gate, native reviewer persistence, exact registry inventory, denied extra tools, call/deadline caps, broker packet behavior, and report IPC. |
| `.omp/extensions/agd-primary.ts`, `tests/primary.test.ts` | Render reviewer batch notifications as existing durable events only; no change to acknowledgement semantics. |
| `docs/operations.md`, `docs/compatibility.md`, `README.md`, `CHANGELOG.md` | Configuration, explicit user-timer activation, limitations, OMP-version acceptance evidence, and no-auto-apply contract. |

### Cross-task contracts

```python
# reviewer.py
DEFAULT_LIMITS = {'max_candidates': 20, 'max_input_bytes': 262144,
                  'max_model_calls': 20, 'max_output_bytes': 65536,
                  'max_runtime_seconds': 1800}

def paths(home: Path) -> dict[str, Path]: ...
def read_config(home: Path) -> dict: ...
def save_config(home: Path, config: dict) -> dict: ...
def select_packet(home: Path, *, cutoff: float, retrospective_run_ids: tuple[str, ...] = ()) -> dict: ...
def enqueue_intent(home: Path, *, kind: str, retrospective_run_ids: tuple[str, ...] = ()) -> dict: ...
def begin_run(home: Path, packet: dict, intent: dict) -> dict: ...
def dispose(home: Path, proposal_id: str, status: str, reason: str | None,
            replacement: str | None, operator: dict) -> dict: ...
def recover(home: Path) -> dict: ...

# reviewer_runner.py
def assert_runtime_capability() -> dict: ...
def resolve_model(config: dict, omp_binary: str) -> str: ...
def launch(home: Path, run_id: str, packet: dict, model: str) -> dict: ...

# reviewer_timer.py
def validate_schedule(schedule: dict) -> dict: ...
def due(checkpoint: dict, now: float, schedule: dict) -> bool: ...
def unit_text(home: Path, schedule: dict) -> tuple[str, str]: ...
```
`select_packet` returns `{'schema': 1, 'packet_id': <sha256>, 'cutoff': <float>, 'episodes': [...], 'sources': [...], 'coverage': {...}, 'limits': DEFAULT_LIMITS, 'skill_inventory': [...]}`. Every `sources[]` item has immutable `source_id`, `kind`, `version`, `sha256`, `span`, and `relation`; only selected candidate episodes, explicit prior proposals, and explicit historical/counterexample episodes may occur. `enqueue_intent` writes the smallest private record under `state/reviewer/pending/`, freezing `kind`, exact normalized retrospective IDs (empty only for default/periodic), requested time, config/instruction digest, and schedule anchor. Only equal default/periodic fingerprints deduplicate; historical selections retain a distinct record. `begin_run` consumes one intent, creates `review_run_id` UUID and a checkpoint containing that immutable intent, packet ID/cutoff, resolved model, and frozen digests before the first model call. `submit_report` accepts only packet citation IDs and persists finalized immutable review/evidence/revision bytes; it returns proposal IDs and never edits skill files.

### Dependency order

| Task | Depends on | Independently reviewable result |
|---|---|---|
| 1. Reconcile and reviewer state/selection | merged observability | Safe private records and bounded authoritative packets, no OMP call |
| 2. Proposal lifecycle and notifications | 1 | Immutable proposal revisions, human-only triage, durable event batch |
| 3. Restricted native OMP runner | 1, 2 | Real OMP session/tool inventory and native reviewer persistence |
| 4. Explicit systemd user timer | 1, 3 | Inert configuration, bounded one-shot scheduling/catch-up |
| 5. CLI and central presentation | 1–4 | No stub commands; complete operator interface and event display |
| 6. Documentation and end-to-end acceptance | 1–5 | User-facing contract and disposable no-paid-model smoke |

### Task 1: Reconcile source history and add reviewer state/evidence selection

**Files:**
- Create: `bin/agd_lib/reviewer.py`
- Create: `bin/agd_lib/reviewer_runner.py`
- Create: `tests/test_reviewer.py`
- Create: `tests/reviewer.test.ts`
- Modify: `bin/agd_lib/observability.py` only to add a `reviewer_binding(home, run_id)` private native-session directory helper after reconciliation proves the module is present.

**Consumes:** merged `observability.paths`, `store.atomic_json`, `store.lock`, `tasks.read`, task observation/decision files, and the installed `omp` executable.

**Produces:** `reviewer.paths`, `read_config`, `save_config`, `select_packet`, `begin_run`, `recover`, `reviewer_runner.assert_runtime_capability`, and `observability.reviewer_binding`; Tasks 2–5 consume their exact records.

- [ ] **Step 1: Reconcile the isolated implementation worktree without rewriting approved work**

```sh
PLAN_BASE=$(git rev-parse HEAD)
git fetch origin main
git merge-base --is-ancestor f9c7be9274966bf16700af5d3dc62f423d946387 "$PLAN_BASE"
git merge-base --is-ancestor 68c3a8cba5007419a467516b792d0df6716ace9a origin/main
git merge --no-ff origin/main -m "merge: reconcile approved runtime source"
git merge-base --is-ancestor "$PLAN_BASE" HEAD
git merge-base --is-ancestor origin/main HEAD
git diff --check "$PLAN_BASE"..HEAD
```

Expected: every command exits 0; the normal merge retains approved documentation and makes merged observability sources available. If either ancestry check fails, stop and report the exact missing approved object—do not reset/rebase/overwrite documentation.

- [ ] **Step 2: Write failing core tests**

```python
def test_packet_is_authoritative_without_langfuse_and_bounded_across_episodes():
    with lab() as (home, _):
        first = _ended_worker(home, 'first', '00000000-0000-4000-8000-000000000001', 'failed')
        second = _ended_worker(home, 'second', '00000000-0000-4000-8000-000000000002', 'idle')
        _native_jsonl(home, first, 'tool failed\n')
        _native_jsonl(home, second, 'counterexample succeeded\n')
        packet = reviewer.select_packet(home, cutoff=10.0,
                                        historical=(('second', second['incarnation']),))
        assert {row['task_id'] for row in packet['episodes']} == {'first', 'second'}
        assert packet['coverage']['langfuse'] == 'unavailable'
        assert all(row['kind'] == 'native-jsonl' for row in packet['sources'])
        assert sum(row['span']['bytes'] for row in packet['sources']) <= 262144


def test_active_and_reviewer_sessions_are_not_default_candidates():
    with lab() as (home, _):
        _running_worker(home, 'active', '00000000-0000-4000-8000-000000000003')
        reviewer._write_run_fixture(home, 'closed-review', finished=True)
        packet = reviewer.select_packet(home, cutoff=10.0)
        assert packet['episodes'] == []
        assert packet['coverage']['excluded'] == ['active worker incarnation', 'reviewer run']
```

- [ ] **Step 3: Run the focused failing tests, including the native runtime capability gate**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/reviewer.test.ts`

Expected: FAIL because `agd_lib.reviewer` and the reviewer native bootstrap do not exist.

- [ ] **Step 4: Establish the exact installed-OMP capability gate before any review is admissible**

`assert_runtime_capability()` resolves `omp` with `shutil.which`, takes its real path, walks only its real parent chain to the package `package.json`, and accepts exactly package name `@oh-my-pi/pi-coding-agent`, version `18.3.2`, and readable installed `dist/sdk.js`/`src/sdk.ts`. It returns the immutable executable, package root, SDK URL, and version; it never writes or imports a hard-coded `/home/...` path and never adds an SDK dependency. It rejects another version or a package lacking the verified public `createAgentSession`, `SessionManager`, `discoverAuthStorage`, and direct native `Agent` bootstrap source before creating a run.

The accompanying offline Bun test imports the resolved SDK URL rather than a repository dependency and asserts the same version/capabilities. It must prove that the supported runtime provides `SessionManager`, `AgentSession`, and `Agent`'s `streamFn` constructor path; an unsupported runtime fails closed. This gate is a prerequisite, not a fallback to CLI `--tools`, `setActiveToolNames`, ambient extension discovery, or a provider/event hook.

- [ ] **Step 5: Implement private records and deterministic packet construction**

```python
# reviewer.py
ROOT = ('data', 'reviewer')
STATE = ('state', 'reviewer')

def paths(home):
    home = store.home(home)
    return {'config': home.joinpath(*ROOT, 'config.json'),
            'runs': home.joinpath(*ROOT, 'runs'),
            'evidence': home.joinpath(*ROOT, 'evidence'),
            'proposals': home.joinpath(*ROOT, 'proposals'),
            'triage': home.joinpath(*ROOT, 'triage'),
            'lock': home.joinpath(*STATE, 'run.lock'),
            'checkpoint': home.joinpath(*STATE, 'checkpoint.json')}

def _source_id(kind, version, digest, span):
    payload = json.dumps([kind, version, digest, span], sort_keys=True,
                         separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()
```

Validate exact config keys `schema`, `schedule`, `limits`, `review_model`; require the approved limits exactly on an absent config, validate positive integers when supplied, and require `review_model` to be null or a nonempty string. Walk native session JSONL only through recorded observability task/incarnation bindings; hash source bytes and use byte spans. Select only ended incarnations (including failure), never infer central links, and record unknown/missing/redacted sources in coverage. Persist packet before a reviewer can be launched. `reviewer_binding` creates `data/observability/reviewer/<run_uuid>/omp` with `binding.json` containing only `review_run_id`, `purpose`, `session_dir`, and `state`; it must reject paths/symlinks/changed receipts exactly as `worker_binding` does.

- [ ] **Step 6: Verify packet/runtime invariants and commit**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/reviewer.test.ts`

Expected: PASS; the capability gate only inspects the installed runtime; no OMP, Langfuse, Docker, or paid model call occurs.

```sh
git add bin/agd_lib/reviewer.py bin/agd_lib/reviewer_runner.py bin/agd_lib/observability.py tests/test_reviewer.py tests/reviewer.test.ts
git commit -m "feat: add reviewer evidence packets"
```

### Task 2: Add immutable proposal lifecycle, human triage, and durable batch notification

**Files:**
- Modify: `bin/agd_lib/reviewer.py`
- Modify: `tests/test_reviewer.py`

**Consumes:** Task 1 packet/run records and `supervision.emit`.

**Produces:** `submit_report`, `dispose`, `proposal_revision`, and `emit_batch`; Task 3 submits only through `submit_report`, Task 5 exposes them through CLI.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_report_revision_preserves_rejected_history_and_never_edits_skill():
    with lab() as (home, _):
        run = _begun_run(home, _packet_with_problem(home))
        first = reviewer.submit_report(home, run['review_run_id'], _report(
            'agd-review', ['e-1'], 'missing boundary check', 'existing-skill'))
        proposal = first['proposals'][0]
        triage = reviewer.dispose(home, proposal['proposal_id'], 'rejected', None, None,
                                  {'uid': os.getuid(), 'identity': str(os.getuid())})
        second = reviewer.submit_report(home, run['review_run_id'], _report(
            'agd-review', ['e-1', 'e-2'], 'missing boundary check', 'existing-skill'))
        assert second['proposals'][0]['proposal_id'] == proposal['proposal_id']
        assert reviewer.read_triage(home, proposal['proposal_id'])['current'] == 'rejected'
        assert triage['history'][0]['reason'] is None
        assert not list((home / 'config' / 'worker-skills').glob('**/SKILL.md'))


def test_uncertain_fingerprint_match_creates_distinct_proposal_and_ack_is_not_triage():
    with lab() as (home, _):
        batch = _persist_two_uncertain_findings(home)
        event = next(row for row in supervision.pending(home) if row['kind'] == 'reviewer-batch')
        supervision.acknowledge(home, [event['id']])
        assert reviewer.list_proposals(home)[0]['current'] == 'pending'
        assert len(reviewer.list_proposals(home)) == 2
```

- [ ] **Step 2: Run failing lifecycle tests**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q`

Expected: FAIL because report submission/triage functions are absent.

- [ ] **Step 3: Implement report validation, revisions, and human-only triage**

```python
def _revision_id(proposal_id, evidence_ids, change):
    value = [proposal_id, sorted(evidence_ids), change]
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()

def dispose(home, proposal_id, status, reason, replacement, operator):
    if status not in {'accepted', 'rejected', 'deferred', 'superseded'}:
        raise store.Refusal('invalid reviewer disposition')
    if replacement is not None and status != 'superseded':
        raise store.Refusal('replacement requires superseded disposition')
    if not isinstance(reason, (str, type(None))):
        raise store.Refusal('reviewer disposition reason is invalid')
    if not isinstance(operator.get('uid'), int):
        raise store.Refusal('local operator identity is required')
```

Validate every cited evidence ID belongs to the frozen packet, every proposal has counterexamples/uncertainty/resolution/validation, and new-skill proposals include draft/triggers/scope/prerequisites/validation limits. Compute a deduplication fingerprint from schema, kind, target, normalized finding/problem, and applicability; assign a persistent UUID when a match is certain. On uncertain collision, persist a distinct UUID and a `deduplication='uncertain-distinct'` marker. Finalized evidence/review/revision files are create-only and content-hash checked; triage is a separate append-only history record. `accepted` writes no source/config/profile/task record and invokes no task command. Emit one `supervision.emit(home, 'reviewer', run_id, 'reviewer-batch', batch_id, summary)` only after all records exist; batch ID hashes sorted persisted revision IDs.

- [ ] **Step 4: Verify lifecycle behavior and commit**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_supervision.py -q`

Expected: PASS; an acknowledged event leaves proposal state unchanged and no skill/task is created.

```sh
git add bin/agd_lib/reviewer.py tests/test_reviewer.py
git commit -m "feat: persist reviewer proposals"
```

### Task 3: Implement the restricted native OMP reviewer adapter and offline integration proof

**Files:**
- Create: `bin/agd_lib/reviewer_runner.py`
- Create: `lib/agd-reviewer.ts`
- Create: `tests/reviewer.test.ts`
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/observability.py`

**Consumes:** Task 1 packet/native binding and Task 2 `submit_report`.

**Produces:** `resolve_model`, `launch`, extension tools `reviewer_excerpt`, `reviewer_search`, `reviewer_submit`, native reviewer JSONL/artifact receipt, and a completed-or-retryable runner record.

- [ ] **Step 1: Write an offline real-native-loop integration test**

The test obtains the package root only through `assert_runtime_capability`, dynamically imports its SDK/core URLs, and creates a disposable `SessionManager`. A deterministic offline native `StreamFn` emits `reviewer_search`, then `reviewer_submit`; it does not mock the agent loop, call the broker directly, or use a handwritten report path. Assert native JSONL persistence, exactly `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit` in the `toolRegistry`/agent tools, and broker-visible calls in order. A second run requests `bash`, `write`, and an unknown source ID and proves no denied tool exists, the broker refuses the ID, source bytes remain unchanged, and no provider/Langfuse credential reaches the offline stream. A third deterministic stream makes 21 continuations and proves the wrapper admits 20 native stream calls then rejects the 21st; an expired deadline is rejected before the first call.

- [ ] **Step 2: Run the integration test to prove absence**

Run: `bun test tests/reviewer.test.ts`

Expected: FAIL because reviewer runner/bootstrap does not exist.

- [ ] **Step 3: Implement the verified direct native bootstrap and broker**

`lib/agd-reviewer.ts` receives only the capability-gated package root, private session directory, frozen run receipt path, and private broker file descriptors. It dynamically imports the actual installed package—never a machine-specific path or repository SDK dependency—and uses `Settings.init({ cwd, agentDir })`, `discoverAuthStorage(agentDir, { cwd, settings })`, `new ModelRegistry(authStorage, undefined, { settings })`, `SessionManager.create({ sessionDir })`, `new Agent({ initialState: { model, tools }, getApiKey: model => authStorage.getApiKey(model), streamFn })`, and `new AgentSession({ agent, sessionManager, settings, modelRegistry, toolRegistry })`.

Tool exclusion is direct: the initial `Agent` tools and `AgentSession` `toolRegistry` are the same three-entry map; no `ExtensionRunner`, extension-path discovery, MCP/LSP/IRC, skills, rules, context files, slash commands, spawn policy, telemetry, or ambient tool registry is constructed. The known `createAgentSession` options (`disableExtensionDiscovery`, `enableMCP`, `enableLsp`, `enableIrc`, `toolNames`, `restrictToolNames`, and `allowRestrictedCustomTools`) are deliberately not treated as access control because this bounded direct bootstrap does not invoke that factory. `setActiveToolNames` is status presentation only, never access control.

`cappedStreamFn(native, deadline, limit)` is the only provider boundary: before forwarding each request to native `streamSimple`, it checks absolute deadline and increments a run-local counter; call 21/expiry is rejected without calling the provider. It includes continuations and uses no extension event, custom tracing/span, prompt alteration, or provider hook. `session.prompt`, `sessionManager.flush`, and `session.dispose` are the native lifecycle.

Python creates the binding/frozen receipt first, sanitizes the child environment, and runs the Bun host. Private JSONL broker requests are `{id, op, args}` and responses `{id, ok, value|error}`: excerpt accepts only cited source ID; search scans packeted UTF-8 excerpts within remaining byte budget; submit accepts bounded structured report then calls `submit_report`. No fallback tool map, shell, generic file/network/MCP, or CLI `--tools` capability claim exists.

- [ ] **Step 4: Verify real OMP boundaries and commit**

Run: `bun test tests/reviewer.test.ts && PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q`

Expected: PASS without paid model calls, service startup, Langfuse key, or mutation outside a disposable reviewer namespace.

```sh
git add bin/agd_lib/reviewer_runner.py bin/agd_lib/reviewer.py bin/agd_lib/observability.py lib/agd-reviewer.ts tests/reviewer.test.ts
git commit -m "feat: run restricted native reviewers"
```


### Task 4: Add explicit systemd user-timer scheduling and crash-safe catch-up

**Files:**
- Create: `bin/agd_lib/reviewer_timer.py`
- Create: `tests/test_reviewer_timer.py`
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_runner.py`

**Consumes:** Task 1 config/checkpoint and Task 3 bounded `launch`.

**Produces:** `validate_schedule`, `due`, `unit_text`, `enable`, `disable`, `status`; Task 5 calls these only after their behavior exists.

- [ ] **Step 1: Write failing timer tests**

```python
def test_calendar_unit_uses_iana_zone_and_bounded_oneshot_runner(tmp_path):
    schedule = {'mode': 'calendar', 'timezone': 'Europe/Berlin', 'calendar': '02:00', 'interval_hours': None}
    service, timer = reviewer_timer.unit_text(tmp_path / 'home', schedule)
    assert 'ExecStart=' in service and 'reviewer run --timer' in service
    assert 'RuntimeMaxSec=1800' in service
    assert 'OnCalendar=*-*-* 02:00:00 Europe/Berlin' in timer
    assert 'Persistent=true' in timer


def test_due_uses_checkpoint_anchor_without_claiming_monotonic_persistence():
    schedule = {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 12}
    checkpoint = {'anchor': 0.0, 'last_started': 0.0, 'active_run': None}
    assert reviewer_timer.due(checkpoint, 36 * 3600 + 1, schedule) is True
    assert reviewer_timer.next_anchor(checkpoint, 36 * 3600 + 1, schedule) == 48 * 3600
    _, timer = reviewer_timer.unit_text(Path('/tmp/home'), schedule)
    assert 'OnUnitActiveSec=' in timer and 'Persistent=true' not in timer
```

- [ ] **Step 2: Run timer tests to prove absence**

Run: `uv run --python 3.12 --with pytest pytest tests/test_reviewer_timer.py -q`

Expected: FAIL because `agd_lib.reviewer_timer` does not exist.

- [ ] **Step 3: Implement strict schedule and explicit installation**

```python
def validate_schedule(value):
    if set(value) != {'mode', 'timezone', 'calendar', 'interval_hours'}:
        raise store.Refusal('reviewer schedule has unsupported fields')
    if value['mode'] == 'calendar' and isinstance(value['timezone'], str) and re.fullmatch(r'\d\d:\d\d', value['calendar']) and value['interval_hours'] is None:
        return value
    if value['mode'] == 'interval' and value['timezone'] is None and value['calendar'] is None and isinstance(value['interval_hours'], int) and value['interval_hours'] > 0:
        return value
    if value['mode'] == 'manual-only' and value['timezone'] is None and value['calendar'] is None and value['interval_hours'] is None:
        return value
    raise store.Refusal('reviewer schedule is invalid')
```

Use `systemd-analyze calendar "*-*-* HH:MM:00 Zone"` before writing a calendar timer; reject enablement if it does not accept the exact selected IANA zone expression. Calendar units use supported `Persistent=true`; interval units use `OnUnitActiveSec` but make no Persistent/catch-up claim. The runner itself compares the durable anchor and records the next integral anchor, so an offline/missed interval is caught up at the next service invocation without execution-duration drift. Write units only under the private home reviewer namespace; call `systemctl --user daemon-reload` and `systemctl --user enable --now <owned timer>` only from explicit `reviewer timer enable --approval APPROVAL_ID`. The approval record is a reviewer-owned explicit `timer-enable` action containing home ID/unit name, not a task approval. `disable` calls `systemctl --user disable --now` only for the recorded owned unit. The runner lock uses `fcntl.LOCK_EX | LOCK_NB`; a timer/manual collision persists only a compatible private pending intent and exits success. Compatibility is the immutable scope (`default` or the exact normalized retrospective run-ID tuple) plus frozen config/instruction digest: periodic/default triggers may deduplicate that record, while each explicit retrospective selection remains its own scoped record. The active run is never widened or mutated. After completion the runner drains one pending intent by creating its separate frozen run record.

- [ ] **Step 4: Verify pure and native-timer boundaries, then commit**

Run: `uv run --python 3.12 --with pytest pytest tests/test_reviewer_timer.py tests/test_reviewer.py -q`

Expected: PASS without enabling a real user timer. In a separately approved disposable user-session acceptance run, execute `systemd-analyze calendar '*-*-* 02:00:00 Europe/Berlin'`, install only the generated disposable owned unit, inspect it with `systemctl --user list-timers`, then disable/remove it; if user-systemd is unavailable, report that external prerequisite and do not claim timer integration passed.

```sh
git add bin/agd_lib/reviewer_timer.py bin/agd_lib/reviewer.py bin/agd_lib/reviewer_runner.py tests/test_reviewer_timer.py tests/test_reviewer.py
git commit -m "feat: schedule bounded reviewer runs"
```

### Task 5: Expose complete reviewer commands and central notification presentation

**Files:**
- Modify: `bin/agd`
- Modify: `.omp/extensions/agd-primary.ts`
- Modify: `tests/test_reviewer.py`, `tests/primary.test.ts`

**Consumes:** Tasks 1–4 complete functions.

**Produces:** backed CLI operations `reviewer run|status|reports list|show|proposals list|show|proposals dispose|timer enable|disable|status` and existing durable-event wake presentation.

- [ ] **Step 1: Write failing CLI/notification tests**

```python
def test_cli_disposition_and_event_ack_never_apply_or_create_task():
    with lab() as (home, _):
        proposal = _persist_proposal(home)
        command(home, 'reviewer', 'proposals', 'dispose', proposal['proposal_id'],
                '--status', 'accepted')
        assert command(home, 'reviewer', 'proposals', 'show', proposal['proposal_id'])['current'] == 'accepted'
        assert command(home, 'task', 'list') == []
        assert not (home / 'config' / 'worker-skills' / 'agd-new-gap').exists()
```

```python
def test_reviewer_mutator_refuses_foreign_live_central_owner():
    with lab() as (home, _):
        _live_foreign_central_owner(home)
        result = run(home, 'reviewer', 'run')
        assert result.returncode != 0
        assert 'central owner' in result.stderr
        assert reviewer.list_reports(home) == []
```

```ts
test('reviewer batch wake remains an ordinary durable acknowledgement', async () => {
  // Reuse the primary extension's real watcher fixture and write one reviewer-batch event.
  // The observed follow-up names the event ID; it does not submit a disposition command.
  expect(deliveries.at(-1)).toEqual({ deliverAs: 'followUp' });
  expect(readFileSync(triagePath, 'utf8')).toContain('"current":"pending"');
});
```

- [ ] **Step 2: Run the focused CLI/extension tests to prove absence**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/primary.test.ts`

Expected: FAIL because `reviewer` parser/dispatch does not exist.

- [ ] **Step 3: Add parser/dispatch only for complete functions**

```python
reviewer_command = sub.add_parser('reviewer').add_subparsers(dest='operation', required=True)
reviewer_command.add_parser('run').add_argument('--retrospective-run', action='append', default=[])
reviewer_command.add_parser('status')
reports = reviewer_command.add_parser('reports').add_subparsers(dest='report_operation', required=True)
reports.add_parser('list'); reports.add_parser('show').add_argument('id')
proposals = reviewer_command.add_parser('proposals').add_subparsers(dest='proposal_operation', required=True)
proposals.add_parser('list'); proposals.add_parser('show').add_argument('id')
dispose = proposals.add_parser('dispose'); dispose.add_argument('id'); dispose.add_argument('--status', required=True, choices=('accepted','rejected','deferred','superseded')); dispose.add_argument('--reason'); dispose.add_argument('--replacement')
```

Read-only list/show/status use no mutation owner gate. Every reviewer mutation (`run`, disposition, timer enable/disable, recovery, and timer invocation) first calls the existing `store.require_owner` against the central home; it refuses a missing/foreign live central owner and never creates/replaces an owner record. `--reason` reads a file only when supplied; the dispatch derives operator UID/identity locally and never trusts the reviewer report. The primary extension keeps its existing `events list`/exact acknowledgement message; it may append a concise reviewer-batch count from event details, but must not render accept/reject controls or mutate triage.

- [ ] **Step 4: Verify backed commands and commit**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_supervision.py -q && bun test tests/primary.test.ts`

Expected: PASS; all exposed commands have backing behavior and accepted proposals remain inert.

```sh
git add bin/agd .omp/extensions/agd-primary.ts tests/test_reviewer.py tests/primary.test.ts
git commit -m "feat: expose reviewer proposal inbox"
```

### Task 6: Document operation and run the disposable end-to-end no-paid-model smoke

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/compatibility.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_reviewer.py`, `tests/reviewer.test.ts`

**Consumes:** Tasks 1–5.

**Produces:** operator-facing activation/limitation documentation and an exercised disposable path from selected packets through native OMP tool calls/report persistence/event/triage.

- [ ] **Step 1: Add a failing end-to-end invariant test**

```python
def test_offline_reviewer_smoke_persists_revision_notifies_and_keeps_new_skill_inert():
    with lab() as (home, _):
        _ended_episode_with_problem_and_counterexample(home)
        result = reviewer_runner.launch(home, *_prepared_run(home), model='offline-test-model')
        report = reviewer.read_report(home, result['review_id'])
        assert report['coverage']['episode_denominator'] == 2
        assert report['proposals'][0]['citations'] == ['e-problem', 'e-counterexample']
        assert any(e['kind'] == 'reviewer-batch' for e in supervision.pending(home))
        assert not (home / 'config' / 'worker-skills' / report['proposals'][0]['new_skill']['id']).exists()
```

- [ ] **Step 2: Run it to prove it fails before final wiring**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/reviewer.test.ts`

Expected: FAIL until the Python runner test hook invokes the same offline native OMP model/bootstrap path as Task 3.

- [ ] **Step 3: Finish docs and smoke wiring**

Document: default calendar/manual behavior; all five limits and their units; explicit timer approval/enablement and no automatic installation; native JSONL authority/Langfuse optionality; sensitive excerpts/no perfect redaction; packet coverage/counterexamples; proposal dispositions and separate implementation-task authority; new-skill inertness; self-review opt-in restrictions; and the exact OMP/systemd evidence still required for a production claim. `compatibility.md` must name the exercised OMP version and state whether user-systemd acceptance was actually available.

Use the Task 3 offline native OMP model test hook for the smoke—never a handwritten report bypass—so it exercises packet tool calls, native session write/flush, broker validation, immutable revision persistence, pending event, and human triage without provider credentials or paid model calls.

- [ ] **Step 4: Run focused and suite verification, then commit**

Run:

```sh
PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_reviewer_timer.py tests/test_observability.py tests/test_workers.py tests/test_supervision.py -q
bun test tests/primary.test.ts tests/reviewer.test.ts
PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests -q
```

Expected: PASS. If Docker/user-systemd or another external prerequisite is unavailable, report that exact unexercised integration while retaining all deterministic/offline proof; do not claim it passed.

```sh
git add README.md docs/operations.md docs/compatibility.md CHANGELOG.md tests/test_reviewer.py tests/reviewer.test.ts
git commit -m "docs: describe bounded skill review"
```

## Plan self-review

- **Spec coverage:** Task 1 covers authoritative JSONL, ended/failure candidates, bounded cross-episode packets, source/version/counterexample provenance, excluded active/reviewer runs, optional Langfuse, frozen provenance, and fail-closed OMP 18.3.2 capability discovery. Task 2 covers immutable evidence/revisions, UUID/fingerprint deduplication, uncertain-distinct proposals, human triage, optional reasons, and acknowledgement separation. Task 3 covers the actual native `Agent`/`AgentSession` constructor APIs, installed-package resolution, `streamFn` call/deadline cap, native persistence, isolated tools, credential boundary, model resolution, and no fallback inventory. Task 4 covers private timer state, explicit approval/activation, calendar/interval limitations, checkpoint catch-up/no drift, scoped coalescing, and recovery. Task 5 covers central-owner protection and durable central-offline notification presentation. Task 6 covers explicit user documentation and a no-paid-model end-to-end path.
- **Capability audit:** CLI `--tools`, `--no-extensions`, extension tool events, and `setActiveToolNames` are never represented as enforcement. The plan uses the installed direct `Agent` `streamFn` boundary and three-entry tool map, and fails unsupported runtime versions rather than loosening constraints.
- **Placeholder scan:** Every declared interface has a producing task and focused behavioral test/command. Pending intents are deliberately minimal private records, not a generic queue; configuration/model/instruction/cutoff are frozen before a run begins.

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-10-27-background-skill-review.md`. Parent review is required before implementation; the parent controls any subsequent execution handoff.
