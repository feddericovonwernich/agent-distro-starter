# Background Skill Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, bounded background reviewer that turns selected completed-worker evidence into durable skill-improvement or new-skill proposals without ever applying them.

**Architecture:** Reuse the merged observability module's private-home/native OMP session conventions, but add a reviewer-owned state namespace and a separate evidence broker/reader. The broker freezes a bounded packet before an isolated OMP reviewer starts; an explicit reviewer extension exposes only excerpt read, packet search, and report submission. A one-shot runner is invoked manually or by an explicitly enabled systemd user timer, while existing supervision events notify an available central session.

**Tech Stack:** Python 3.12 stdlib; existing `bin/agd`, `agd_lib.store`, `agd_lib.supervision`, merged `agd_lib.observability`; TypeScript/Bun OMP extensions; installed OMP 18.3.2 native `SessionManager`/`AgentSession`; systemd user timers; pytest and Bun test.

**Spec:** `docs/superpowers/specs/2026-10-27-background-skill-review-design.md`

## Global Constraints

- Before source edits, reconcile the isolated implementation worktree with current `origin/main` using a normal merge; never reset, rebase, force-checkout, clean, drop documentation, or modify the stable checkout. `PLAN_BASE` must remain an ancestor of the resulting `HEAD`. Treat user-confirmed merged PR 5 as ground truth even when squash merge makes `68c3a8cba5007419a467516b792d0df6716ace9a` non-ancestral: verify its merged status/merge SHA through the forge, then verify the expected `bin/agd_lib/observability.py` tree paths and patch behavior in the reconciled tree rather than requiring that feature object as an ancestor.
- Reuse `store.home`, `store.lock`, `store.atomic_json`, `supervision.pending`, and `supervision.acknowledge`; do not change ordinary-task semantics. Reviewer code never claims/replaces `state/owner.json` or sets worker identity, but uses `store.lock(home)` only for short reviewer publication/reservation/triage/intent transactions and a separate nonblocking reviewer `run.lock` for one live runner. It never holds either while waiting on OMP, provider, systemd, or network.
- Native OMP JSONL is authoritative. Design choice: do not add a Langfuse query client, credentials, index, or API. If the reconciled repository already exposes an indexed span-discovery reader, call it only as optional discovery and record its exact source/version; otherwise record `langfuse_index: "unused"` and collect identically from native bindings whether Langfuse is online or offline. A consumed late external discovery is a separately bounded stable-source-version follow-up keyed by its source hash, never a heuristic task match. The existing OpenTelemetry Collector remains transport only and never reads JSONL or runs review logic.
- The reviewer never changes reviewed skills/sources/profiles/task lifecycle records. It writes only its private config, reviewer native output/artifacts, immutable evidence/review/proposal revisions, mutable human triage, scheduler checkpoints/receipts, and existing event receipts.
- Default limits are user-approved: 20 distinct episode candidates/run, 262144 UTF-8 broker-input bytes/run, 20 native reviewer model invocations including tool continuations/run, 65536 UTF-8 persisted report bytes/run, and a 1800-second wall-clock runner deadline. They do not promise a USD ceiling or bound hidden provider transport retries.
- Freeze validated configuration, resolved model, instruction-bundle hash, packet ID, source versions, and cutoff in each run record before the first model call. Later model/schedule/configuration changes apply only to later runs.
- Provider credentials may reach the trusted native transport through `ModelRegistry.getApiKey`; they never enter reviewer prompt/packet, broker protocol, tool arguments/results, report, or Langfuse configuration. The model receives no shell, generic filesystem/network/SQL/Git/MCP/package/task/publish tool, or arbitrary path/task/key query. Tool restriction is a model-tool boundary, not an OS sandbox.
- Reviewer native JSONL/artifacts are always persisted under `AGD_HOME` through the native OMP lifecycle even with telemetry disabled. Existing observability transport, when enabled, adds only `purpose=background-skill-review` and `review_run_id`; it must not create ordinary task IDs or change native conversation IDs.
- A timer is inert until an operator explicitly invokes `reviewer timer enable`; that invocation is the separate approval. Use a bounded systemd **user** service/timer, never cron, linger, a resident daemon, or automatic installation/start at `agd init`. Periodic triggers may coalesce only into one compatible private pending-intent record; explicit retrospective run-ID selections are separate immutable scoped intents and are never dropped, widened, or silently folded into periodic/default work.
- No proposal creates a task or edits a skill. Event acknowledgement, proposal acceptance, and authorization to implement are distinct actions. Reviewer runs are excluded from default selection; explicit retrospective self-review selects only closed prior run IDs and excludes current/later IDs.

- Reviewer notification envelopes retain the existing event pipeline but use `purpose: "background-skill-review"` and `review_run_id`, never a fabricated ordinary `task`/incarnation identity. The primary consumer recognizes that purpose separately from task events.
- A run receipt persists consumed UTF-8 input bytes, produced UTF-8 report bytes, and native stream-call count before each irreversible boundary. Recovery resumes only when the receipt has no dispatched-but-unsettled stream request; otherwise it marks `unknown-provider-outcome` and requires an explicit new run/intent rather than claiming exactly-once provider execution.
- Packet evidence is selected by recorded immutable source/version/hash and cutoff, including explicitly linked feedback/history only; it never infers association from task naming or heuristics. Configured limits are validated bounded overrides of the approved defaults and frozen into the receipt, including the runtime deadline used both by `TimeoutStartSec` and the host wrapper.

---

## Code map and interfaces

| Path | Change and responsibility |
|---|---|
| `bin/agd` | Add complete `reviewer` command group only after backing operations exist; reviewer mutators use scoped reviewer authority, never ordinary task-owner takeover. |
| `bin/agd_lib/reviewer.py` | Private-path safety, strict config, deterministic packet/intent records, short home-lock publication transactions, immutable evidence/review/proposal revisions, triage, and durable reviewer-purpose events. |
| `bin/agd_lib/reviewer_runner.py` | Owns one bounded reviewer run under nonblocking reviewer `run.lock`; freezes receipt, resolves model before receipt creation, invokes native bootstrap, and uses only short home-lock receipt transitions. |
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
def begin_run(home: Path, packet: dict, intent: dict, *, model: str,
              config_digest: str, instruction_digest: str) -> dict: ...
def read_packet(home: Path, packet_id: str) -> dict: ...
def read_report(home: Path, report_id: str) -> dict: ...
def read_triage(home: Path, proposal_id: str) -> dict: ...
def list_proposals(home: Path) -> list[dict]: ...
def dispose(home: Path, proposal_id: str, status: str, reason: str | None,
            replacement: str | None, operator: dict) -> dict: ...
def recover(home: Path) -> dict: ...

# reviewer_runner.py
def assert_runtime_capability() -> dict: ...
def resolve_model(config: dict, omp_binary: str) -> str: ...
def launch(home: Path, review_run_id: str, packet: dict, model: str) -> dict: ...

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
git merge --no-ff origin/main -m "merge: reconcile approved runtime source"
git merge-base --is-ancestor "$PLAN_BASE" HEAD
git diff --check "$PLAN_BASE"..HEAD
```

Expected: every command exits 0 and retains approved documentation. Verify PR 5's merged state and merge SHA in the forge; if its original feature SHA is not an ancestor, compare reconciled `bin/agd_lib/observability.py` and its focused observability tests with the merged PR patch/tree, recording the squash mapping. Stop only if that source behavior is absent—not merely because a squash discarded original ancestry.

- [ ] **Step 2: Write failing core tests**

```python
INCARNATION = '00000000-0000-4000-8000-000000000001'

def ended_task(home, task_id, phase):
    brief = home / f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief))
    task.update(executor='worker', incarnation=INCARNATION, phase=phase, worker_alive=False)
    tasks.save(home, task)
    return task

def native_binding_with_message(home, task):
    binding = observability.worker_binding(home, task['id'], task['incarnation'])
    iso, millis = '2027-01-01T00:00:00.000Z', 1798761600000
    rows = [
        {'type':'session','version':3,'id':'00000000-0000-4000-8000-000000000010',
         'timestamp':iso,'cwd':str(home)},
        {'type':'message','id':'00000000-0000-4000-8000-000000000011','parentId':None,
         'timestamp':iso,'message':{'role':'user','content':'fixture failure evidence','timestamp':millis}},
    ]
    path = Path(binding['session_dir']) / 'fixture.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

def test_packet_uses_recorded_ended_native_sources():
    with lab() as (home, _):
        failed = ended_task(home, 'failed', 'failed')
        native_binding_with_message(home, failed)
        packet = reviewer.select_packet(home, cutoff=1798848000.0)
        assert [episode['task_id'] for episode in packet['episodes']] == ['failed']
        assert packet['packet_id'] == reviewer.packet_id(packet)
        assert sum(source['span']['bytes'] for source in packet['sources']) <= packet['limits']['max_input_bytes']
```

- [ ] **Step 3: Run focused tests to prove absence**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/reviewer.test.ts`

Expected: FAIL because reviewer modules/bootstrap do not exist.

- [ ] **Step 4: Establish the exact installed-OMP capability gate before any review is admissible**

`assert_runtime_capability()` resolves `omp` with `shutil.which`, takes its real path, walks only its real parent chain to `@oh-my-pi/pi-coding-agent/package.json`, and accepts version `18.3.2`. It does **not** invent `exports["./sdk"]`: it creates a Bun resolver rooted at the verified package entry, resolves the package's exported wildcard subpath `@oh-my-pi/pi-coding-agent/sdk`, then separately resolves `@oh-my-pi/pi-agent-core` and `@oh-my-pi/pi-ai`. Each resolved real path must be below the installed package/dependency roots named in the verified lock/package metadata. The gate returns those three file URLs plus executable/root/version and rejects any missing/escaping/mismatched target.

The offline Bun test imports those exact URLs and asserts SDK exports `SessionManager`, `AgentSession`, `Settings`, `discoverAuthStorage`, and `ModelRegistry`; core exports `Agent`; AI exports `streamSimple`. It proves positional `SessionManager.create(cwd, sessionDir)`, `AgentSession` prompt/flush-dispose lifecycle, `ModelRegistry.getApiKey`, and Agent `streamFn`/`sessionId`/`deadline`/`telemetry` option fields. Unsupported runtime fails closed; it never falls back to CLI `--tools`, `setActiveToolNames`, ambient extension discovery, or provider/event hooks.

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
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/supervision.py`
- Modify: `tests/test_reviewer.py`, `tests/test_supervision.py`

**Consumes:** Task 1 packet/run records and `supervision.emit`.

**Produces:** `submit_report`, `dispose`, `proposal_revision`, and `emit_batch`; Task 3 submits only through `submit_report`, Task 5 exposes them through CLI.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def packet_run(home):
    packet = {'schema': 1, 'packet_id': 'p1', 'cutoff': 1.0, 'limits': reviewer.DEFAULT_LIMITS,
              'sources': [{'source_id': 'e1', 'kind': 'native-jsonl', 'version': 'v1',
                           'sha256': '0' * 64, 'span': {'start': 0, 'bytes': 1}},
                          {'source_id': 'e2', 'kind': 'native-jsonl', 'version': 'v2',
                           'sha256': '1' * 64, 'span': {'start': 1, 'bytes': 1}}],
              'episodes': [], 'coverage': {}, 'skill_inventory': []}
    return reviewer.begin_run(home, packet, reviewer.enqueue_intent(home, kind='default'),
                              model='fixture/model', config_digest='fixture-config',
                              instruction_digest='fixture-instructions')

def report():
    return {'schema': 1, 'findings': [{'kind': 'existing-skill', 'target': 'agd-review',
        'finding': 'missing boundary check', 'citations': ['e1'], 'counterexamples': ['e2'],
        'uncertainty': 'low', 'resolution': 'add bound', 'validation': 'exercise bound',
        'applicability': 'all matching reviewer packets'}]}

def test_report_revision_and_disposition_are_private_and_immutable():
    with lab() as (home, _):
        run = packet_run(home); first = reviewer.submit_report(home, run['review_run_id'], report())
        proposal_id = first['proposal_ids'][0]
        triage = reviewer.dispose(home, proposal_id, 'rejected', None, None,
                                  {'uid': os.getuid(), 'identity': str(os.getuid())})
        assert reviewer.submit_report(home, run['review_run_id'], report())['report_id'] == first['report_id']
        assert reviewer.read_triage(home, proposal_id)['current'] == 'rejected'
        assert triage['history'][-1]['status'] == 'rejected'
        assert not (home / 'config' / 'worker-skills').exists()

def test_acknowledging_reviewer_event_does_not_dispose_proposal():
    with lab() as (home, _):
        run = packet_run(home); reviewer.submit_report(home, run['review_run_id'], report())
        event = next(row for row in supervision.pending(home) if row['purpose'] == 'background-skill-review')
        supervision.acknowledge(home, [event['id']])
        assert reviewer.read_triage(home, reviewer.list_proposals(home)[0]['proposal_id'])['current'] == 'pending'
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
    try:
        proposal_id = str(uuid.UUID(proposal_id))
        replacement = None if replacement is None else str(uuid.UUID(replacement))
    except (TypeError, ValueError) as error:
        raise store.Refusal('invalid proposal reference') from error
    if status not in {'accepted', 'rejected', 'deferred', 'superseded'}:
        raise store.Refusal('invalid reviewer disposition')
    if (status == 'superseded') != (replacement is not None):
        raise store.Refusal('superseded requires one replacement reference')
    if replacement is not None and not proposal_path(home, replacement).is_file():
        raise store.Refusal('replacement proposal does not exist')
    if not isinstance(reason, (str, type(None))) or type(operator.get('uid')) is not int:
        raise store.Refusal('local operator identity is required')
    with reviewer_lock(home):
        prior = read_triage(home, proposal_id)
        entry = {'at': now(), 'status': status, 'reason': reason, 'replacement': replacement,
                 'operator': {'uid': operator['uid'], 'identity': operator['identity']}}
        store.atomic_json(triage_path(home, proposal_id),
                          {'schema': 1, 'proposal_id': proposal_id, 'current': status,
                           'history': [*prior['history'], entry]})
    return read_triage(home, proposal_id)
```

`submit_report` first reloads the frozen run/packet receipt under short `store.lock`, rejects wrong run state, schema, unknown citations, excess UTF-8 serialized report, invalid proposal/counterexample/uncertainty/resolution/validation fields, and duplicate broker request ID. It writes content-addressed evidence/review/revision candidates with `O_CREAT|O_EXCL`, fsyncs them, then atomically publishes the report index/receipt. Existing matching `review_run_id + report_hash` returns the already published report; an incomplete candidate is retained and recovery decides its state from the receipt. Dedup fingerprint is schema/kind/target/normalized finding/applicability; certain match reuses proposal UUID, uncertain match creates distinct UUID marked `uncertain-distinct`. Triage is the separate atomic append-only history above. `accepted` writes no source/config/profile/task record and invokes no task command.

`emit_batch` writes one deterministic envelope per finalized `review_run_id` (`uuid5("reviewer-batch:"+run_id)`), never per broker retry/report re-read: `{schema:1,id,purpose:'background-skill-review',review_run_id,kind:'reviewer-batch',details:{report_id,proposal_ids}}`. Its authoritative commit point is the final run receipt: under short `store.lock`, atomically write report index then event; mark receipt `final` only after event exists. `recover` retains all immutable evidence and, when it finds published report/no event, recreates that deterministic event before finalizing. `supervision.pending`/acknowledge accept this purpose envelope alongside unchanged task envelopes; it must not call task-only `supervision.emit`.

- [ ] **Step 4: Verify lifecycle behavior and commit**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_supervision.py -q`

Expected: PASS; an acknowledged event leaves proposal state unchanged and no skill/task is created.

```sh
git add bin/agd_lib/reviewer.py bin/agd_lib/supervision.py tests/test_reviewer.py tests/test_supervision.py
git commit -m "feat: persist reviewer proposals"
```

### Task 3: Implement the restricted native OMP reviewer adapter and offline integration proof

**Files:**
- Modify: `bin/agd_lib/reviewer_runner.py`
- Create: `lib/agd-reviewer.ts`
- Modify: `tests/reviewer.test.ts`
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/observability.py`

**Consumes:** Task 1 packet/native binding and Task 2 `submit_report`.

**Produces:** `resolve_model`, `launch`, extension tools `reviewer_excerpt`, `reviewer_search`, `reviewer_submit`, native reviewer JSONL/artifact receipt, and a completed-or-retryable runner record.

- [ ] **Step 1: Write an offline real-native-loop integration test**


The test obtains the package root only through `assert_runtime_capability`, dynamically imports its SDK/core URLs, and creates a disposable `SessionManager`. A deterministic offline native `StreamFn` emits `reviewer_search`, then `reviewer_submit`; it does not mock the agent loop, call the broker directly, or use a handwritten report path. Assert native JSONL persistence, exactly `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit` in the `toolRegistry`/agent tools, and broker-visible calls in order. A second run requests `bash`, `write`, and an unknown source ID and proves no denied tool exists, the broker refuses the ID, source bytes remain unchanged, and provider/Langfuse credentials are absent from prompt, tool arguments/results, packet, report, and broker frames. A third deterministic stream makes 21 continuations and proves the wrapper admits 20 native stream calls then rejects the 21st; an expired deadline is rejected before the first call.

Run: `bun test tests/reviewer.test.ts`

Expected: FAIL because reviewer runner/bootstrap does not exist.

- [ ] **Step 3: Implement the verified direct native bootstrap and broker**

`lib/agd-reviewer.ts` receives only the three capability-gated package URLs, private session directory, frozen receipt, and broker descriptors. Bun resolves `@oh-my-pi/pi-coding-agent/sdk`, `@oh-my-pi/pi-agent-core`, and `@oh-my-pi/pi-ai` through the verified installed package resolver; it never reads a guessed SDK export/path or repository dependency.

```ts
const settings = await Settings.init({ cwd: reviewCwd, agentDir });
const authStorage = await discoverAuthStorage(agentDir, { cwd: reviewCwd, settings });
const modelRegistry = new ModelRegistry(authStorage, undefined, { settings });
const sessionManager = SessionManager.create(reviewCwd, sessionDir);
const telemetry = await nativeTelemetryFor(receipt); // initTelemetryExport first, then enabled/config
const agent = new Agent({
  initialState: { model, tools: [...toolRegistry.values()], systemPrompt: [reviewPrompt] },
  sessionId: receipt.native_session_id,
  deadline: receipt.deadline_epoch_ms,
  getApiKey: (requested, id, options) => modelRegistry.getApiKey(requested, id, options),
  streamFn: cappedStreamFn(streamSimple, receipt),
  telemetry,
});
const session = new AgentSession({
  agent, sessionManager, settings, modelRegistry, toolRegistry,
  memoryEnabled: false, skills: [], skillsReloadable: false, disableExtensionDiscovery: true,
  scoutAllowedBySpawnPolicy: false,
});
```

Tool exclusion is direct: the initial `Agent` tools and `AgentSession` `toolRegistry` are the same three-entry map; no `ExtensionRunner`, extension-path discovery, MCP/LSP/IRC, skills, rules, context files, slash commands, spawn policy, or ambient tool registry is constructed. The known `createAgentSession` options (`disableExtensionDiscovery`, `enableMCP`, `enableLsp`, `enableIrc`, `toolNames`, `restrictToolNames`, and `allowRestrictedCustomTools`) are deliberately not treated as access control because this bounded direct bootstrap does not invoke that factory. `setActiveToolNames` is status presentation only, never access control.

`cappedStreamFn(native, deadline, limit)` is the only provider boundary: before forwarding each request to native `streamSimple`, it checks absolute deadline and increments/persists the run receipt counter; call 21/expiry is rejected without calling the provider. It includes continuations and uses no extension event, custom tracing/span, prompt alteration, or provider hook. It does not scrub provider request options required by the transport; tests assert credentials cannot appear in prompt, tool arguments/results, JSONL report, or broker protocol. `session.prompt`, `sessionManager.flush`, `session.beginDispose()`, `session.dispose()`, and `sessionManager.close()` form native lifecycle. The direct `AgentSession` receives `memoryEnabled:false`, `skills:[]`, `skillsReloadable:false`, and `disableExtensionDiscovery:true`. Native storage persists independently of telemetry: use `initTelemetryExport`, `createTelemetryExportConfig` and `flushTelemetryExport` only when the installed exporter reports enabled; pass native Agent telemetry `attributes:{purpose:'background-skill-review',review_run_id}` and capture policy, never a custom SDK/span/exporter.

Python creates the binding/frozen receipt first, sanitizes the child environment, and runs the Bun host. Private JSONL broker requests are `{id, op, args}` and responses `{id, ok, value|error}`. Each request has a UUID, one response, schema-checked args, and a broker-side error code; duplicate/unknown IDs are rejected. Frame size is `frozen max_output_bytes + fixed 4096-byte envelope allowance`, measured as UTF-8 bytes before write—never a hidden 64 KiB ceiling—and excerpts/search replies consume the persisted input-byte budget before framing. Excerpt accepts only cited source ID; search scans packeted UTF-8 excerpts within remaining budget; submit validates citation IDs and frozen output limit, atomically calls `submit_report`, then returns report/proposal/revision IDs. On crash before publication receipt remains recoverable; after publication repeat submit returns the existing immutable report by review-run/report fingerprint. No fallback tool map, shell, generic file/network/MCP, or CLI `--tools` capability claim exists.

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
def test_calculate_due_has_one_signature_and_dst_rules():
    schedule = {'mode': 'calendar', 'timezone': 'Europe/Berlin', 'calendar': '02:00', 'interval_hours': None}
    checkpoint = {'periodic_anchor': None, 'last_periodic_due': None}
    gap = reviewer_timer.calculate_due(schedule, checkpoint, utc('2027-03-28T00:30:00Z'))
    fold = reviewer_timer.calculate_due(schedule, checkpoint, utc('2027-10-31T00:30:00Z'))
    assert gap.due_at == utc('2027-03-29T00:00:00Z')  # nonexistent local 02:00 is skipped
    assert fold.due_at == utc('2027-11-01T01:00:00Z') # next first valid occurrence after now

def test_interval_rearms_exact_absolute_deadline_and_manual_does_not_move_anchor():
    schedule = {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 12}
    checkpoint = {'periodic_anchor': 0.0, 'last_periodic_due': 0.0}
    decision = reviewer_timer.calculate_due(schedule, checkpoint, 36 * 3600 + 1)
    assert (decision.due_at, decision.next_due, decision.checkpoint['last_periodic_due']) == (12*3600, 48*3600, 36*3600)
    assert reviewer_timer.record_start(checkpoint, 5.0, trigger='manual') == checkpoint

def test_validate_schedule_rejects_missing_unknown_bool_clock_and_zone_injection():
    for invalid in ({'mode':'interval','timezone':None,'calendar':None,'interval_hours':True},
                    {'mode':'other','timezone':None,'calendar':None,'interval_hours':1},
                    {'mode':'calendar','timezone':'Europe/Berlin;bad','calendar':'25:00','interval_hours':None}):
        with pytest.raises(store.Refusal): reviewer_timer.validate_schedule(invalid)
```

`calculate_due(schedule, checkpoint, now_utc)` is the sole pure scheduling contract and returns `{due_at|None, next_due, checkpoint}`; its transaction caller persists that checkpoint and creates at most one intent. It treats missing local calendar times as skipped (does not shift to 03:00) and only chooses a fold occurrence that is still future. Calendar validation includes `zoneinfo.ZoneInfo` plus `systemd-analyze calendar` on the exact emitted expression at enable time.

For interval mode the timer is not `OnUnitActiveSec`: enable writes one owned absolute UTC `OnCalendar=<next_due>` timer with `Persistent=true`. Each successful/recovered timer service transaction recalculates and atomically rewrites/reloads/restarts that one timer for the next integral anchor; a boot-missed absolute time fires once then re-arms future cadence, without completion drift or hourly lateness. The service `TimeoutStartSec` is rendered from the frozen/validated runtime limit, not literal 1800; the host receipt deadline remains inner authority. Units live in `$XDG_CONFIG_HOME/systemd/user` under UUID-derived names and marker, as previously specified.

Unit tests parse the emitted semantic `TimerSpec`/`ServiceSpec`, then hand the rendered calendar expression to `systemd-analyze calendar`; they do not assert copied unit substrings. Timer/manual collisions use reviewer `run.lock` and private scope-compatible intents; manual runs never move periodic anchor.

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
        review_run = packet_run(home)
        proposal_id = reviewer.submit_report(home, review_run['review_run_id'], report())['proposal_ids'][0]
        command(home, 'reviewer', 'proposals', 'dispose', proposal_id, '--status', 'accepted')
        assert command(home, 'reviewer', 'proposals', 'show', proposal_id)['current'] == 'accepted'
        assert command(home, 'task', 'list') == []
        assert not (home / 'config' / 'worker-skills').exists()
```

```python
def test_reviewer_scoped_mutators_preserve_live_foreign_central_owner():
    with lab() as (home, _):
        owner = {'owner_pid': os.getpid(), 'owner_start': process_start(os.getpid()),
                 'token': 'foreign', 'generation': '1'}
        store.atomic_json(home / 'state' / 'owner.json', owner)
        before = (home / 'state' / 'owner.json').read_bytes()
        intent = reviewer.enqueue_intent(home, kind='default')
        assert reviewer.recover(home)['pending_intent_ids'] == [intent['intent_id']]
        assert (home / 'state' / 'owner.json').read_bytes() == before
        assert command(home, 'task', 'list') == []
```

```ts
// Extend the existing `session switch replaces only its owned watcher` fixture:
// write a reviewer-batch JSON event at home/state/events, wait for its real watcher
// delivery, then read the reviewer triage JSON written by the Python setup fixture.
expect(deliveries.at(-1)).toEqual({ deliverAs: 'followUp' });
expect(JSON.parse(readFileSync(join(home, 'data/reviewer/triage', proposalId + '.json'), 'utf8')).current)
  .toBe('pending');
```

- [ ] **Step 2: Run the focused CLI/extension tests to prove absence**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/primary.test.ts`

Expected: FAIL because `reviewer` parser/dispatch does not exist.

- [ ] **Step 3: Add parser/dispatch only for complete functions**

```python
reviewer_command = sub.add_parser('reviewer').add_subparsers(dest='operation', required=True)
run = reviewer_command.add_parser('run')
run.add_argument('--retrospective-run', action='append', default=[])
run.add_argument('--timer', action='store_true')
reviewer_command.add_parser('status')
reports = reviewer_command.add_parser('reports').add_subparsers(dest='report_operation', required=True)
reports.add_parser('list'); reports.add_parser('show').add_argument('report_id')
proposals = reviewer_command.add_parser('proposals').add_subparsers(dest='proposal_operation', required=True)
proposals.add_parser('list'); proposals.add_parser('show').add_argument('proposal_id')
dispose = proposals.add_parser('dispose')
dispose.add_argument('proposal_id'); dispose.add_argument('--status', required=True,
    choices=('accepted', 'rejected', 'deferred', 'superseded'))
dispose.add_argument('--reason'); dispose.add_argument('--replacement')
timer = reviewer_command.add_parser('timer').add_subparsers(dest='timer_operation', required=True)
timer.add_parser('enable'); timer.add_parser('disable'); timer.add_parser('status')
```

Read-only list/show/status use no mutation gate. Reviewer-scoped `run`, disposition, recovery, and timer invocation validate only canonical home UUID, effective UID, private reviewer paths, locked run/intent receipt, and never write `state/owner.json`, task records, leases, or ordinary task events; they remain admissible with no/dead/live-foreign central owner. Timer enable/disable is an explicit local user action writing/removing only its named reviewer unit; it has no invented `--approval` token. `--reason` reads a file only when supplied; dispatch derives operator UID/identity locally and never trusts a reviewer report. The primary extension keeps its existing `events list`/exact acknowledgement message; it may append a concise reviewer-batch count from event details, but must not render accept/reject controls or mutate triage.

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
        run = packet_run(home)
        result = reviewer_runner.launch(home, run['review_run_id'],
                                        reviewer.read_packet(home, run['packet_id']),
                                        model='offline-test-model')
        report = reviewer.read_report(home, result['report_id'])
        assert report['run_id'] == run['review_run_id']
        assert report['proposals'][0]['citations'] == ['e1']
        assert any(e['purpose'] == 'background-skill-review' for e in supervision.pending(home))
        assert not (home / 'config' / 'worker-skills').exists()
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

The final acceptance matrix must exercise: native JSONL selected with Langfuse unavailable and enabled-export metadata; explicit prior self-review versus default exclusion; no UI/no/dead/live-foreign central owner; report dedup/revision and uncertain distinct findings; broker invalid ID/oversize/duplicate request/submit-crash recovery; 20-call and whole-run persisted byte/deadline budgets across restart; timer gap/fold/reboot catch-up/manual-anchor invariants; and acknowledgement/accepted-proposal/new-skill inertness. Record the exact command and observable artifact for each row; unavailable user-systemd is reported as unexercised, not passed.

| Scenario | Input/action | Observable proof |
|---|---|---|
| Native source and optional Langfuse | Recorded JSONL binding with Langfuse unavailable; then enabled native export | Same packet provenance; only exporter attributes differ |
| Linked feedback/history | Immutable feedback link plus unrelated same-named task | Only linked source ID enters packet |
| Skill provenance | Assigned manifest skill and separately loaded catalog skill | Packet records source/version/assignment distinctly |
| Counterexample and late feedback | Failure + counterexample; append linked feedback after cutoff | Finding cites both first two; late item appears only next run |
| Self-review scope | Prior closed reviewer ID; current/later IDs | Only explicit prior ID eligible |
| Frozen retry limits | Crash after 20th call/input reservation | Recovery preserves counters and refuses 21st/over-limit resume |
| Broker failure | Invalid ID, oversized report, duplicate submit, crash after index | Refusal; deterministic report; recovery emits missing batch once |
| Proposal basis | Certain, uncertain, and changed applicability findings | Reused UUID, `uncertain-distinct`, and new basis respectively |
| Inert new skill | Accepted new-skill proposal with real catalog resolver before/after | Resolver output unchanged; draft exists only reviewer data |
| Scoped authority | No owner, dead owner, live foreign owner | Reviewer records/event mutate; owner/task bytes unchanged |
| Timer | Calendar gap/fold, missed absolute interval, manual run | `calculate_due`/systemd command proof of skip/fold/rearm/unchanged anchor |

```sh
git add README.md docs/operations.md docs/compatibility.md CHANGELOG.md tests/test_reviewer.py tests/reviewer.test.ts
git commit -m "docs: describe bounded skill review"
```

## Plan self-review

- **Runtime audit:** This revision uses verified OMP 18.3.2 wildcard/package dependency resolution, positional `SessionManager.create`, direct Agent deadline/session/telemetry options, ModelRegistry key lookup, and native exporter lifecycle; it does not claim CLI tool flags or extension events enforce limits.
- **Transaction audit:** Long runner lock and short `store.lock` scopes are separated; receipts persist whole-run budgets, event recovery has a deterministic per-run identity, and reviewer scope never claims central/task ownership.
- **Test audit:** Examples use `lab`, `command`, task persistence, real event storage, actual systemd consumption, and producer-defined packet/report helpers. The explicit acceptance matrix remains the implementation completion checklist rather than a passing claim.

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-10-27-background-skill-review.md`. Parent review is required before implementation; the parent controls any subsequent execution handoff.
