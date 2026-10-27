# Background Skill Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in bounded reviewer that converts completed-worker native evidence into immutable, human-triaged improvement proposals without applying them.

**Architecture:** A Python reviewer-private store freezes native OMP evidence and every run’s provenance before the native TypeScript host starts. The host uses direct installed OMP Agent/AgentSession APIs with only three broker tools; reviewer-only systemd timers invoke bounded one-shot runs. Existing supervision carries a purpose-qualified reviewer event, never a fake ordinary task.

**Tech Stack:** Python 3.12 stdlib, existing AGD modules, Bun/TypeScript, installed OMP 18.3.2 (`pi-coding-agent`, `pi-agent-core`, `pi-ai`), pytest, Bun test, systemd user timers.

**Spec:** `docs/superpowers/specs/2026-10-27-background-skill-review-design.md`

## Global Constraints

- Source repository is `/home/fedderico/Workspace/projects/agent-distro-starter`. At execution, use `superpowers:using-git-worktrees` to create an isolated feature worktree from it; never edit/test `/home/fedderico/Workspace/eva-beta-01` or the stable checkout.
- Reconcile `origin/main` by normal merge. Preserve `PLAN_BASE` ancestry. Treat user-confirmed merged PR 5 as ground truth even when squash merging removes original SHA ancestry: verify forge merge SHA and reconciled observability tree/patch behavior.
- Native OMP JSONL bound by observability is authoritative. Do not add a Langfuse API/client/key/index. If a reconciled indexed discovery reader already exists, record consumed source ID/version; otherwise record `langfuse_index: "unused"` and collect identically online/offline.
- Reviewer never changes tasks, workers, skills, profiles, projects, leases, or `state/owner.json`; never sets `AGD_TASK_ID`/`AGD_INCARNATION`. Reviewer mutations work with no/dead/live-foreign central owner and preserve those bytes.
- Hold reviewer `state/reviewer/run.lock` nonblocking for full child/IPC/provider lifetime. Use `store.lock(home)` only for short intent/receipt/report/triage/event transitions; release it before OMP, provider, systemd, or network waits.
- Defaults: 20 candidates, 262144 UTF-8 input bytes, 20 native model calls, 65536 UTF-8 report bytes, 1800 seconds. Validate bounded configured overrides and freeze them with model, config/instruction digest, packet/cutoff/source hashes, counters, deadline, dispatch state, and native session ID before dispatch.
- Provider credentials may reach only `ModelRegistry.getApiKey`; never prompt/packet/tool/report/broker/Langfuse configuration. Model tool restriction is not an OS sandbox.
- Native session persistence occurs with telemetry disabled or enabled. Reuse installed telemetry exporter and its existing capture policy; do not add an SDK/spans/exporter.
- Timer enable is explicit user approval. Use systemd user units only; no cron, linger, daemon, or init auto-start. Default/periodic pending intents may deduplicate only identical frozen scope; explicit retrospective review-run IDs are never widened/dropped.
- Event acknowledgement, proposal disposition, and authorization to implement are separate. No proposal creates a task or modifies a skill.

---

## Code map and contracts

| Path | Change |
|---|---|
| `bin/agd_lib/reviewer.py` | private packet/intent/run/report/triage records and recovery |
| `bin/agd_lib/reviewer_runner.py` | installed runtime gate, model resolution, broker process/receipt transitions |
| `lib/agd-reviewer.ts` | direct native OMP host, tool registry, capped stream function |
| `bin/agd_lib/reviewer_timer.py` | strict scheduling and user-unit lifecycle |
| `bin/agd_lib/observability.py` | reviewer native-session binding after merged module reconciliation |
| `bin/agd_lib/supervision.py` | reviewer-purpose event envelope support without changing task envelopes |
| `bin/agd`, `.omp/extensions/agd-primary.ts` | complete backed CLI and reviewer event presentation |
| `tests/test_reviewer.py`, `tests/reviewer_bridge.py`, `tests/test_reviewer_timer.py`, `tests/test_supervision.py`, `tests/reviewer.test.ts`, `tests/primary.test.ts` | behavior proof |
| `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md` | user contract |

```python
# reviewer.py
DEFAULT_LIMITS = {'max_candidates':20,'max_input_bytes':262144,'max_model_calls':20,
                  'max_output_bytes':65536,'max_runtime_seconds':1800}
def select_packet(home: Path, *, cutoff: float, retrospective_review_run_ids: tuple[str,...]=()) -> dict: ...
def enqueue_intent(home: Path, *, kind: str, retrospective_review_run_ids: tuple[str,...]=()) -> dict: ...
def begin_run(home: Path, packet: dict, intent: dict, *, model: str, config_digest: str,
              instruction_digest: str) -> dict: ...
def submit_report(home: Path, review_run_id: str, request_id: str, report: dict) -> dict: ...
def dispose(home: Path, proposal_id: str, status: str, reason: str|None,
            replacement_proposal_id: str|None, operator: dict) -> dict: ...
# reviewer_runner.py
def resolve_model(config: dict, omp_binary: str) -> str: ...
def launch(home: Path, review_run_id: str, packet: dict, model: str) -> dict: ...
def serve_broker(home: Path, review_run_id: str, stdin, stdout) -> None: ...
# reviewer_timer.py
def calculate_due(schedule: dict, checkpoint: dict, now_utc: float) -> dict: ...
```

### Task 1: Reconcile source and create authoritative reviewer packets

**Files:**
- Create: `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_runner.py`, `tests/test_reviewer.py`, `tests/reviewer_bridge.py`
- Modify: `bin/agd_lib/observability.py`

**Interfaces:** Consumes merged `observability.worker_binding(home, task_id, incarnation)`. Produces `select_packet`, `enqueue_intent`, `begin_run`, and reviewer binding.

- [ ] **Step 1: Reconcile isolated worktree**

```bash
PLAN_BASE=$(git rev-parse HEAD)
git fetch origin main
git merge --no-ff origin/main -m "merge: reconcile approved runtime source"
git merge-base --is-ancestor "$PLAN_BASE" HEAD
git diff --check "$PLAN_BASE"..HEAD
```

Expected: all commands exit 0; forge/merged-tree verification records any PR-5 squash mapping.

- [ ] **Step 2: Write failing native-evidence test**

```python
import json, sys
from pathlib import Path
from support import lab, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, tasks

def ended_worker(home, task_id, incarnation, text):
    brief = home/f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief))
    task.update(executor='worker', incarnation=incarnation, phase='failed', worker_alive=False)
    tasks.save(home, task)
    binding = observability.worker_binding(home, task_id, incarnation)
    iso, ms = '2026-01-01T00:00:00.000Z', 1767225600000
    rows = [{'type':'session','version':3,'id':'00000000-0000-4000-8000-000000000010','timestamp':iso,'cwd':str(home)},
            {'type':'message','id':'00000000-0000-4000-8000-000000000011','parentId':None,'timestamp':iso,
             'message':{'role':'user','content':text,'timestamp':ms}}]
    Path(binding['session_dir'],'fixture.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
    return task

def test_packet_has_distinct_failure_and_counterexample_sources():
    with lab() as (home, _):
        a = ended_worker(home, 'failed', '00000000-0000-4000-8000-000000000001', 'failure')
        b = ended_worker(home, 'success', '00000000-0000-4000-8000-000000000002', 'counterexample')
        packet = reviewer.select_packet(home, cutoff=1767312000.0)
        native = [s for s in packet['sources'] if s['kind'] == 'native-jsonl']
        assert {x['task_id'] for x in packet['episodes']} == {a['id'], b['id']}
        assert len({x['source_id'] for x in native}) == 2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py::test_packet_has_distinct_failure_and_counterexample_sources -q`

Expected: FAIL because `agd_lib.reviewer` does not exist.

- [ ] **Step 4: Implement packet/intent/run kernel**

```python
def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
def _digest(value): return hashlib.sha256(_canonical(value)).hexdigest()
def select_packet(home, *, cutoff, retrospective_review_run_ids=()):
    rows = _bound_ended_native_sources(home, cutoff)  # only worker_binding JSONL records
    rows = _bounded_distinct_sources(rows, read_config(home)['limits']['max_input_bytes'])
    packet = {'schema':1,'cutoff':cutoff,'sources':rows,'episodes':_episodes(rows),
              'limits':read_config(home)['limits'],'langfuse_index':'unused'}
    packet['packet_id'] = _digest(packet)
    store.atomic_json(paths(home)['packets']/f"{packet['packet_id']}.json", packet)
    return packet

def begin_run(home, packet, intent, *, model, config_digest, instruction_digest):
    receipt = {'schema':1,'review_run_id':str(uuid.uuid4()),'packet_id':packet['packet_id'],
      'cutoff':packet['cutoff'],'intent':intent,'model':model,'config_digest':config_digest,
      'instruction_digest':instruction_digest,'limits':packet['limits'],'input_bytes':0,
      'output_bytes':0,'model_calls':0,'dispatch':'none','state':'created'}
    store.atomic_json(paths(home)['runs']/f"{receipt['review_run_id']}.json", receipt)
    return receipt
```

Implement `_bound_ended_native_sources` by parsing only v3 session/message JSONL below merged worker bindings, using file hash/byte spans and task `phase != 'running'`/`worker_alive is not True`; no task-name association. Resolve review model before calling `begin_run`; null `review_model` inherits native default and unresolved default raises before receipt dispatch.

- [ ] **Step 5: Run packet tests to verify pass**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q`

Expected: PASS; no model, Langfuse API, Docker, or systemd call.

- [ ] **Step 6: Commit Task 1**

```bash
git add bin/agd_lib/reviewer.py bin/agd_lib/reviewer_runner.py bin/agd_lib/observability.py tests/test_reviewer.py tests/reviewer_bridge.py
git commit -m "feat: persist reviewer evidence packets"
```

### Task 2: Immutable reports, triage, and reviewer-purpose events

**Files:**
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/supervision.py`, `tests/test_reviewer.py`, `tests/test_supervision.py`

**Interfaces:** Consumes Task 1 receipt/packet. Produces `submit_report`, `dispose`, reviewer-purpose pending/handled event behavior.

- [ ] **Step 1: Write failing report/new-skill/triage tests**

```python
from agd_lib import store, supervision, worker_skills

def produced_run(home):
    ended_worker(home,'failed','00000000-0000-4000-8000-000000000001','failure')
    ended_worker(home,'success','00000000-0000-4000-8000-000000000002','counterexample')
    packet = reviewer.select_packet(home, cutoff=1767312000.0)
    return reviewer.begin_run(home, packet, reviewer.enqueue_intent(home, kind='default'),
      model='provider/model', config_digest='c'*64, instruction_digest='i'*64), packet

def test_new_skill_acceptance_is_inert_and_event_is_once():
    with lab() as (home, _):
        before_catalog, before_selection = worker_skills.catalog(home), worker_skills.resolve(home)
        before_bytes = {p.relative_to(home):p.read_bytes() for p in (home/'config'/'worker-skills').rglob('*') if p.is_file()}
        run, packet = produced_run(home); ids = [s['source_id'] for s in packet['sources']]
        report = {'schema':1,'summary':'x','findings':[{'kind':'new-skill','target':None,'finding':'gap','applicability':'all',
          'citations':[ids[0]],'counterexamples':[ids[1]],'uncertainty':'low','resolution':'draft','validation':'curated resolve',
          'new_skill':{'id':'agd-review-draft','draft':'# Draft\n','triggers':['review'],'scope':'review','prerequisites':[]}}]}
        result = reviewer.submit_report(home, run['review_run_id'], '00000000-0000-4000-8000-000000000020', report)
        reviewer.dispose(home, result['proposal_ids'][0], 'accepted', None, None, {'uid':os.getuid(),'identity':str(os.getuid())})
        assert worker_skills.catalog(home) == before_catalog
        assert worker_skills.resolve(home) == before_selection
        assert {p.relative_to(home):p.read_bytes() for p in (home/'config'/'worker-skills').rglob('*') if p.is_file()} == before_bytes
        with pytest.raises(store.Refusal): worker_skills.resolve(home, skill_mode='curated', skills=('agd-review-draft',))
        event = [e for e in supervision.pending(home) if e.get('review_run_id') == run['review_run_id']]
        assert len(event) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py::test_new_skill_acceptance_is_inert_and_event_is_once -q`

Expected: FAIL because report/event functions are absent.

- [ ] **Step 3: Implement report/disposition/event kernel**

```python
def dispose(home, proposal_id, status, reason, replacement_proposal_id, operator):
    proposal_id = str(uuid.UUID(proposal_id))
    replacement = None if replacement_proposal_id is None else str(uuid.UUID(replacement_proposal_id))
    if status not in {'accepted','rejected','deferred','superseded'}: raise store.Refusal('invalid disposition')
    if (status == 'superseded') != (replacement is not None): raise store.Refusal('invalid replacement')
    if replacement and not proposal_path(home,replacement).is_file(): raise store.Refusal('unknown replacement')
    with store.lock(home):
        prior = read_triage(home, proposal_id)
        store.atomic_json(triage_path(home,proposal_id), {'schema':1,'proposal_id':proposal_id,'current':status,
          'history':[*prior['history'],{'status':status,'reason':reason,'replacement':replacement,'operator':operator,'at':time.time()}]})
    return read_triage(home,proposal_id)

def reviewer_event_id(review_run_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'reviewer-batch:'+review_run_id))
```

Validate canonical UTF-8 report size, UUID request ID, frozen source IDs, required finding fields, distinct citations/counterexamples, and draft schema. Content-address immutable report/revision blobs; under short `store.lock`, publish index, then deterministic reviewer-purpose event, then mark receipt final. Recovery preserves blobs and recreates event only when neither pending nor handled event path exists. Extend `supervision.pending/acknowledge` to retain ordinary task behavior while moving reviewer event IDs between the same pending/handled directories.

- [ ] **Step 4: Run report/supervision tests to verify pass**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_supervision.py -q`

Expected: PASS; accepted draft remains undiscoverable and acknowledgement never changes triage.

- [ ] **Step 5: Commit Task 2**

```bash
git add bin/agd_lib/reviewer.py bin/agd_lib/supervision.py tests/test_reviewer.py tests/test_supervision.py
git commit -m "feat: persist reviewer proposals"
```

### Task 3: Restricted native OMP host and bridge proof

**Files:**
- Create: `lib/agd-reviewer.ts`
- Modify: `bin/agd_lib/reviewer_runner.py`, `tests/reviewer.test.ts`, `tests/reviewer_bridge.py`, `tests/test_reviewer.py`

**Interfaces:** `runReviewer({cwd,agentDir,sessionDir,receipt,broker,model,reviewPrompt}, streamFn = streamSimple): Promise<{reportId:string}>`; production entry invokes it without second argument. `serve_broker(home, review_run_id, stdin, stdout)` processes real broker frames.

- [ ] **Step 1: Write failing bridge/native-loop test**

```ts
import { test, expect } from 'bun:test';
import { runReviewer } from '../lib/agd-reviewer';
import { scriptedStream } from './reviewer-fixture';
test('native loop calls actual bridge then persists report', async () => {
  const bridge = await startPythonBridge(); // tests/reviewer_bridge.py fixture, disposable AGD_HOME/run
  const result = await runReviewer(bridge.options, scriptedStream([
    toolCall('reviewer_search', {query:'failure'}), toolCall('reviewer_submit', bridge.report)
  ]));
  expect(result.reportId).toBe(bridge.reportId);
  expect(bridge.readReport().review_run_id).toBe(bridge.runId);
  expect(bridge.toolNames()).toEqual(['reviewer_excerpt','reviewer_search','reviewer_submit']);
});
```

`tests/reviewer_bridge.py` creates sources through `ended_worker`/`select_packet`/`begin_run`, starts `serve_broker` on stdin/stdout, and exposes `{options,runId,reportId,report}` JSON. `scriptedStream` returns an actual `AssistantMessageEventStream` built with pi-ai’s installed event constructors/types; it is passed only as `runReviewer`’s second argument, never through CLI/config/model name.

- [ ] **Step 2: Run test to verify it fails**

Run: `bun test tests/reviewer.test.ts`

Expected: FAIL because host/bridge are absent.

- [ ] **Step 3: Implement native host and cap**

```ts
export async function runReviewer(input: ReviewerInput, streamFn: StreamFn = streamSimple) {
  await initTelemetryExport();
  const settings = await Settings.init({cwd:input.cwd, agentDir:input.agentDir});
  const auth = await discoverAuthStorage(input.agentDir,{cwd:input.cwd,settings});
  const registry = new ModelRegistry(auth,undefined,{settings});
  const manager = SessionManager.create(input.cwd,input.sessionDir);
  await input.broker.persistNativeSessionId(manager.getSessionId()!);
  const agent = new Agent({initialState:{model:input.model,tools:[...input.tools.values()],systemPrompt:[input.reviewPrompt]},
    sessionId:manager.getSessionId(),deadline:input.receipt.deadline_epoch_ms,
    getApiKey:(m,id,opt)=>registry.getApiKey(m,id,opt),streamFn:cappedStreamFn(streamFn,input.broker)});
  const session = new AgentSession({agent,sessionManager:manager,settings,modelRegistry:registry,toolRegistry:input.tools,
    memoryEnabled:false,skills:[],skillsReloadable:false,disableExtensionDiscovery:true,scoutAllowedBySpawnPolicy:false});
  try { await session.prompt(input.reviewPrompt); await manager.flush(); return input.broker.finalResult(); }
  finally { session.beginDispose(); await session.dispose(); await manager.close(); await flushTelemetryExport(); }
}
function cappedStreamFn(native: StreamFn, broker: Broker): StreamFn {
  return (...args) => { broker.reserveModelCall(); return native(...args); };
}
```

`reserveModelCall` runs short receipt transaction before native call, rejects deadline/call 21, marks dispatch sent, and stream settlement marks it settled. Broker measures actual UTF-8 JSONL frame bytes: excerpt/search reserve remaining input, submit reserves remaining output; no fixed 64 KiB cap. Configure native telemetry via installed `createTelemetryExportConfig` after `initTelemetryExport`, retaining existing exporter capture policy plus attributes purpose/run ID.

- [ ] **Step 4: Run bridge/native tests to verify pass**

Run: `bun test tests/reviewer.test.ts && PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/reviewer_bridge.py tests/test_reviewer.py -q`

Expected: PASS without paid provider calls; invalid source, `bash`, `write`, 21st call, expired deadline, duplicate frame, and oversized report all refuse.

- [ ] **Step 5: Commit Task 3**

```bash
git add lib/agd-reviewer.ts bin/agd_lib/reviewer_runner.py tests/reviewer.test.ts tests/reviewer_bridge.py tests/test_reviewer.py
git commit -m "feat: run restricted native reviewers"
```

### Task 4: User timer with fixed UTC-hour interval anchor

**Files:**
- Create: `bin/agd_lib/reviewer_timer.py`, `tests/test_reviewer_timer.py`
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_runner.py`

**Interfaces:** `calculate_due(schedule,checkpoint,now_utc)->{'due_at':float|None,'next_due':float,'checkpoint':dict}`. Interval anchor is `floor(enable_time/3600)*3600`; valid interval is positive integer hours and each due is anchor + N*3600.

- [ ] **Step 1: Write failing schedule tests**

```python
def test_hour_anchor_catches_up_once_without_manual_drift():
    schedule={'mode':'interval','timezone':None,'calendar':None,'interval_hours':12}
    d=reviewer_timer.calculate_due(schedule,{'anchor':0.0,'last_due':0.0},36*3600+1)
    assert d == {'due_at':12*3600,'next_due':48*3600,'checkpoint':{'anchor':0.0,'last_due':36*3600}}
    assert reviewer_timer.record_manual({'anchor':0.0,'last_due':0.0}) == {'anchor':0.0,'last_due':0.0}
def test_calendar_gap_skips_and_fold_zero_is_verified():
    schedule={'mode':'calendar','timezone':'Europe/Berlin','calendar':'02:00','interval_hours':None}
    assert reviewer_timer.calendar_expression(schedule) == '*-*-* 02:00:00 Europe/Berlin'
    assert reviewer_timer.native_calendar_ok(schedule)
def test_schedule_rejects_bad_types():
    for x in ({'mode':'interval','timezone':None,'calendar':None,'interval_hours':True},
              {'mode':'calendar','timezone':'Europe/Berlin;bad','calendar':'25:00','interval_hours':None}):
        with pytest.raises(store.Refusal): reviewer_timer.validate_schedule(x)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --python 3.12 --with pytest pytest tests/test_reviewer_timer.py -q`

Expected: FAIL because `agd_lib.reviewer_timer` does not exist.

- [ ] **Step 3: Implement timer kernel**

```python
def calculate_due(schedule, checkpoint, now_utc):
    if schedule['mode'] == 'interval':
        step=3600*schedule['interval_hours']; anchor=checkpoint['anchor']; due=anchor+step
        while due <= now_utc: due += step
        previous=due-step
        return {'due_at':previous if previous>checkpoint['last_due'] else None,'next_due':due,
                'checkpoint':{'anchor':anchor,'last_due':max(checkpoint['last_due'],previous)}}
    return _calendar_due(schedule, checkpoint, now_utc)
```

Interval unit is `OnCalendar=hourly` with `Persistent=true`; eligibility is fixed UTC-hour anchored, not activation-relative, and each tick uses `calculate_due`. Calendar uses emitted IANA expression plus `systemd-analyze calendar --base-time=<before transition> --iterations=2`; fold=0 is accepted only if native parser output matches it, nonexistent gap day is skipped. Persist arm-intent under short home lock, release it, enable/verify systemd, then short-lock mark armed; boot recovery sees unarmed/spent receipt and enables hourly timer before any reviewer run. Render `TimeoutStartSec` from frozen runtime limit. Unit names include canonical home UUID and live only in `$XDG_CONFIG_HOME/systemd/user` with UUID marker.

- [ ] **Step 4: Run timer tests to verify pass**

Run: `uv run --python 3.12 --with pytest pytest tests/test_reviewer_timer.py tests/test_reviewer.py -q`

Expected: PASS; no real unit enabled except separately approved disposable user-session acceptance.

- [ ] **Step 5: Commit Task 4**

```bash
git add bin/agd_lib/reviewer_timer.py bin/agd_lib/reviewer.py bin/agd_lib/reviewer_runner.py tests/test_reviewer_timer.py tests/test_reviewer.py
git commit -m "feat: schedule bounded reviewer runs"
```

### Task 5: Backed CLI and primary reviewer notification

**Files:** Modify `bin/agd`, `.omp/extensions/agd-primary.ts`, `tests/test_reviewer.py`, `tests/primary.test.ts`.

**Interfaces:** CLI provides `reviewer run [--timer] [--retrospective-review-run UUID]`, `status`, reports/proposals list/show/dispose, timer enable/disable/status.

- [ ] **Step 1: Write failing CLI/watcher test**

```python
def test_reviewer_cli_preserves_foreign_owner(tmp_path):
    with lab() as (home,_):
        before=(home/'state'/'owner.json').read_bytes() if (home/'state'/'owner.json').exists() else None
        command(home,'reviewer','status')
        assert ((home/'state'/'owner.json').read_bytes() if (home/'state'/'owner.json').exists() else None) == before
```

Extend existing `session switch replaces only its owned watcher` test only after its `home`, `deliveries`, and `hooks` definitions:

```ts
const reviewRunId='00000000-0000-4000-8000-000000000030';
writeFileSync(join(home,'state','events','33333333-3333-4333-8333-333333333333.json'),JSON.stringify({
 id:'33333333-3333-4333-8333-333333333333',purpose:'background-skill-review',review_run_id:reviewRunId,kind:'reviewer-batch',details:{report_ids:[],proposal_ids:[]}}));
while (deliveries.length < 3 && Date.now() < Date.now()+5000) await Bun.sleep(50);
expect(deliveries.at(-1)).toEqual({deliverAs:'followUp'});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/primary.test.ts`

Expected: FAIL because parser/reviewer-purpose watcher handling is absent.

- [ ] **Step 3: Add parser/dispatch**

```python
reviewer_p=sub.add_parser('reviewer').add_subparsers(dest='reviewer_op',required=True)
run_p=reviewer_p.add_parser('run'); run_p.add_argument('--timer',action='store_true'); run_p.add_argument('--retrospective-review-run',action='append',default=[])
timer_p=reviewer_p.add_parser('timer').add_subparsers(dest='timer_op',required=True)
for name in ('enable','disable','status'): timer_p.add_parser(name)
```

Route reviewer mutations through reviewer-private validation/locks, not `store.require_owner`; ordinary commands retain existing guard. Primary renders purpose event as ordinary follow-up only and never creates proposal controls.

- [ ] **Step 4: Run CLI tests to verify pass**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_supervision.py -q && bun test tests/primary.test.ts`

Expected: PASS; live foreign owner/task bytes remain unchanged.

- [ ] **Step 5: Commit Task 5**

```bash
git add bin/agd .omp/extensions/agd-primary.ts tests/test_reviewer.py tests/primary.test.ts
git commit -m "feat: expose reviewer inbox"
```

### Task 6: Documentation and end-to-end acceptance

**Files:** Modify `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`, `tests/test_reviewer.py`, `tests/reviewer.test.ts`.

- [ ] **Step 1: Write failing end-to-end bridge test**

```python
def test_bridge_report_can_be_triaged_without_skill_mutation():
    result=run_bridge_fixture() # Task 3 fixture: actual packet→native tool→Python submit
    assert reviewer.read_report(result['home'],result['report_id'])['review_run_id']==result['review_run_id']
    command(result['home'],'reviewer','proposals','dispose',result['proposal_id'],'--status','accepted')
    with pytest.raises(store.Refusal): worker_skills.resolve(result['home'],skill_mode='curated',skills=('agd-review-draft',))
```

- [ ] **Step 2: Run test to verify it fails before final wiring**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py::test_bridge_report_can_be_triaged_without_skill_mutation -q`

Expected: FAIL until Task 3 bridge and Task 5 CLI are wired.

- [ ] **Step 3: Write operator documentation**

`README.md`: reviewer is opt-in and never auto-applies. `docs/operations.md`: commands, explicit timer enable, all five bounds, native JSONL authority, sensitive evidence, manual recovery, no task ownership. `docs/compatibility.md`: OMP 18.3.2 gate, systemd user requirements, unexercised prerequisites. `CHANGELOG.md`: bounded reviewer, purpose events, no Langfuse query API.

- [ ] **Step 4: Run final verification**

```bash
PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/reviewer_bridge.py tests/test_reviewer_timer.py tests/test_supervision.py -q
bun test tests/reviewer.test.ts tests/primary.test.ts
PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests -q
```

Expected: PASS. Record native-only online/offline optional index behavior, linked-vs-heuristic feedback, assigned-vs-loaded skill provenance, late follow-up, self-review boundaries, crash budgets, broker recovery, dedup bases, inert new draft, owner variants, DST/boot/manual timer paths, and acknowledgement inertness. Report unavailable user-systemd as unexercised.

- [ ] **Step 5: Commit Task 6**

```bash
git add README.md docs/operations.md docs/compatibility.md CHANGELOG.md tests/test_reviewer.py tests/reviewer.test.ts
git commit -m "docs: describe bounded skill review"
```

## Plan self-review

- **Spec coverage:** Tasks 1–6 cover authoritative native evidence/optional existing index, frozen scope/budgets, OMP call enforcement/persistence, human-only proposal lifecycle, reviewer-purpose notifications, explicit timer operation, self-review restrictions, and user docs.
- **Placeholder scan:** No TODO/future-step shorthand. Each task declares files/interfaces, failing proof, implementation kernel, pass command, and commit.
- **Type consistency:** All callers use `review_run_id`, `packet_id`, `report_id`, `proposal_id`, exact `calculate_due` dictionary fields, and direct `runReviewer` test seam.

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-10-27-background-skill-review.md`. Parent review is required before implementation; parent selects subsequent execution handoff.
