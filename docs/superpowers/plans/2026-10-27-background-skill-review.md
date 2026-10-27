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
| `bin/agd_lib/reviewer.py` | writes reviewer-purpose events using existing pending/handled atomic-file pattern; no supervision source change |
| `bin/agd`, `.omp/extensions/agd-primary.ts` | complete backed CLI and neutral durable-event presentation |
| `lib/agd-reviewer-instructions.md`, `tests/test_reviewer.py`, `tests/reviewer_bridge.py`, `tests/reviewer-fixture.ts`, `tests/test_reviewer_timer.py`, `tests/reviewer.test.ts`, `tests/primary.test.ts` | behavior proof |
| `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md` | user contract |

```python
# reviewer.py
DEFAULT_LIMITS = {'max_candidates':20,'max_input_bytes':262144,'max_model_calls':20,
                  'max_output_bytes':65536,'max_runtime_seconds':1800}
def select_packet(home: Path, *, cutoff: float, retrospective_review_run_ids: tuple[str,...]=()) -> dict: ...
def enqueue_intent(home: Path, *, kind: str, retrospective_review_run_ids: tuple[str,...]=()) -> dict: ...
def begin_run(home: Path, packet: dict, intent: dict, *, model: str, config_snapshot: dict,
              instruction_snapshot: bytes, started_at: float, deadline_epoch_ms: int) -> dict: ...
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

The run lease is acquired before packet collection; it persists `started_at` and absolute `deadline_epoch_ms` before collection/model resolution. Every collector, resolver, broker, and child uses that same deadline. Parent waits with monotonic remaining time, kills the child process group at expiry, and records coverage/state. Recovery never resets deadline/counters; pending-intent waiting occurs before a lease and does not consume active budget.

### Task 1: Reconcile source and create authoritative reviewer packets

**Files:**
- Create: `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_runner.py`, `tests/test_reviewer.py`, `tests/reviewer_bridge.py`, `lib/agd-reviewer-instructions.md`
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
import hashlib, json, os, sys, time, uuid
from pathlib import Path
import pytest
from support import lab, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, tasks, workers

def ended_worker(home, task_id, incarnation, text, status):
    brief=home/f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task=command(home,'task','create',task_id,'--project','sample','--brief',str(brief))
    task.update(executor='worker',incarnation=incarnation,phase='running',worker_alive=False); tasks.save(home,task)
    binding=observability.worker_binding(home,task_id,incarnation); sid=str(uuid.uuid4()); iso='2026-01-01T00:00:00.000Z'
    rows=[{'type':'session','version':3,'id':sid,'timestamp':iso,'cwd':str(home)},{'type':'message','id':str(uuid.uuid4()),'parentId':None,'timestamp':iso,'message':{'role':'user','content':text,'timestamp':1767225600000}}]
    Path(binding['session_dir'],f'{sid}.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
    note=tasks.task_file(home,task_id).parent/'runner-exit.txt'; note.write_text(f'worker process exited {status}')
    workers.event(home,task_id,incarnation,'failed' if status else 'idle','runner-exit',note)
    return task
def test_packet_has_distinct_failure_and_counterexample_sources():
    with lab() as (home,_):
        a=ended_worker(home,'failed',str(uuid.uuid4()),'failure',1); b=ended_worker(home,'success',str(uuid.uuid4()),'counterexample',0)
        packet=reviewer.select_packet(home,cutoff=time.time()); native=[s for s in packet['sources'] if s['kind']=='native-jsonl']
        assert {x['task_id'] for x in packet['episodes']}=={a['id'],b['id']}
        assert len({x['source_id'] for x in native})==2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py::test_packet_has_distinct_failure_and_counterexample_sources -q`

Expected: FAIL because `agd_lib.reviewer` does not exist.

- [ ] **Step 4: Implement packet/intent/run kernel**

```python
import hashlib, json, os, re, tempfile, uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from agd_lib import store
def paths(home):
    root=store.home(home); return {'config':root/'data/reviewer/config.json','packets':root/'data/reviewer/packets','reports':root/'data/reviewer/reports','proposals':root/'data/reviewer/proposals','manifests':root/'data/reviewer/manifests','triage':root/'data/reviewer/triage','receipts':root/'state/reviewer/receipts','pending':root/'state/reviewer/pending','checkpoint':root/'state/reviewer/checkpoint.json','lock':root/'state/reviewer/run.lock'}
def publish_immutable(path, value):
    body = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != body: raise store.Refusal('immutable collision')
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        os.unlink(temporary)
DEFAULT_LIMITS={'max_candidates':20,'max_input_bytes':262144,'max_model_calls':20,'max_output_bytes':65536,'max_runtime_seconds':1800}
DEFAULT_CONFIG={'schema':1,'schedule':{'mode':'calendar','timezone':'UTC','calendar':'02:00','interval_hours':None},'limits':DEFAULT_LIMITS,'review_model':None}
def validate_schedule(s):
    if not isinstance(s,dict) or set(s)!={'mode','timezone','calendar','interval_hours'}: raise store.Refusal('schedule schema')
    if s['mode']=='calendar':
        if not isinstance(s['timezone'],str) or not isinstance(s['calendar'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',s['calendar']) or s['interval_hours'] is not None: raise store.Refusal('calendar schedule')
        try: ZoneInfo(s['timezone'])
        except ZoneInfoNotFoundError as e: raise store.Refusal('timezone') from e
    elif s['mode']=='interval':
        if type(s['interval_hours']) is not int or s['interval_hours']<=0 or s['timezone'] is not None or s['calendar'] is not None: raise store.Refusal('interval schedule')
    elif s['mode']=='manual-only':
        if any(s[k] is not None for k in ('timezone','calendar','interval_hours')): raise store.Refusal('manual schedule')
    else: raise store.Refusal('schedule mode')
    return s
def validate_config(v):
    if not isinstance(v,dict) or set(v)!={'schema','schedule','limits','review_model'} or type(v['schema']) is not int or v['schema']!=1: raise store.Refusal('config schema')
    if not isinstance(v['limits'],dict) or set(v['limits'])!=set(DEFAULT_LIMITS) or any(type(n) is not int or n<=0 for n in v['limits'].values()) or (v['review_model'] is not None and (not isinstance(v['review_model'],str) or not v['review_model'].strip())): raise store.Refusal('config values')
    validate_schedule(v['schedule']); return v
def read_config(home):
    p=paths(home)['config']
    if not p.exists(): return {'schema':1,'schedule':dict(DEFAULT_CONFIG['schedule']),'limits':dict(DEFAULT_LIMITS),'review_model':None}
    try: return validate_config(json.loads(p.read_text()))
    except json.JSONDecodeError as e: raise store.Refusal('invalid config JSON') from e
def _hex_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value): raise store.Refusal('invalid content id')
def read_packet(home,packet_id): _hex_id(packet_id); row=json.loads((paths(home)['packets']/f'{packet_id}.json').read_text()); row['packet_id']=packet_id; return row
def read_report(home,report_id): _hex_id(report_id); row=json.loads((paths(home)['reports']/f'{report_id}.json').read_text()); row['report_id']=report_id; return row
# Public contracts implemented by the seven-stage algorithm below:
# assert_runtime_capability() -> {'ai_url':str,'core_url':str,'sdk_url':str,'telemetry_url':str}
# select_packet(home,*,cutoff:float,retrospective_review_run_ids:tuple[str,...]=()) -> dict; begin_run(home,packet,intent,*,model,config_snapshot,instruction_snapshot,started_at:float,deadline_epoch_ms:int) -> dict
```

`select_packet` snapshots validated config/limits and instruction bytes before collection. It (1) finds retained `runner-exit` observations at/before cutoff and their task/incarnation bindings: phase is irrelevant; a current incarnation needs `worker_alive is False`; a prior incarnation requires its closed observation and remains eligible even while a newer incarnation runs; (2) admits at most configured `max_candidates` distinct task/incarnation episodes; group retries of same task/incident together, never count them as independent recurrence; (3) incrementally parses each bound native v3 JSONL only to a frozen byte watermark, validates header and outer record timestamp, and stores bounded actual text excerpts with `source_id,kind,task_id,incarnation,session_id,message_id,span,source_path,version,sha256,cutoff,relation`; it never copies whole histories; (4) retains only explicitly linked decisions/human feedback matching task/incarnation/session plus bounded historical counterevidence; errors, partial files, withheld excerpts, active/unknown links, and no retained end proof become named coverage records rather than fabricated timestamps or negative findings; (5) snapshots real worker-skill catalog plus manifest bytes/hash, recording assigned/loaded/used/causal provenance separately and unknown when not evidenced; (6) admits closed prior review IDs only, records current/later/unknown IDs in coverage, excludes reviewer feedback by default, and permits one bounded later follow-up only when a material-basis watermark gains a linked feedback/span hash; (7) charges canonical packet metadata plus stored UTF-8 excerpts against the whole packet bound and rejects infeasible metadata before model dispatch. Packet fields are `schema,packet_id,cutoff,limits,episodes,sources,coverage,skill_inventory,material_basis`; episode fields are `task_id,incarnation,exit_observation_id,exit_observed_at,binding`; coverage has `unknown_relation,inaccessible,partial,withheld,retrospective`; inventory contains catalog and manifest content hashes plus provenance labels.

Selected excerpts and packet are content-hash create-only immutable files; live JSONL is not claimed immutable. `INSTRUCTION_FILE=Path(__file__).resolve().parents[2]/'lib/agd-reviewer-instructions.md'`; snapshot it as immutable JSON `{text,raw_utf8_sha256}` and make `instruction_snapshot_ref` point to it. `begin_run` writes mutable receipt `{review_run_id,intent_id,packet_id,model,config_snapshot_ref,instruction_snapshot_ref,started_at,deadline_epoch_ms,native_session_id,model_calls,input_bytes,output_bytes,dispatch}` under `state/reviewer/receipts`, preserving its passed intent/start/deadline. Native `reviewPrompt` loads this frozen blob text, never the live bundle. After actual native session ID exists and before first reserve, create-only publish the immutable run manifest. Default config is calendar `02:00 UTC`; `review_model=None` inherits native model.

- [ ] **Step 5: Run packet tests to verify pass**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q`

Expected: PASS; no model, Langfuse API, Docker, or systemd call.

- [ ] **Step 6: Commit Task 1**

```bash
git add bin/agd_lib/reviewer.py bin/agd_lib/reviewer_runner.py bin/agd_lib/observability.py lib/agd-reviewer-instructions.md tests/test_reviewer.py tests/reviewer_bridge.py
git commit -m "feat: persist reviewer evidence packets"
```

### Task 2: Immutable reports, triage, and reviewer-purpose events

**Files:**
- Modify: `bin/agd_lib/reviewer.py`, `tests/test_reviewer.py`

**Interfaces:** `submit_report(home,review_run_id,request_id,report)->{'report_id':str,'proposal_ids':list[str]}`, `finalize_run(home,review_run_id,outcome)->dict`, `dispose(home,proposal_id,status,reason,replacement_proposal_id,operator)->dict`; `proposal_path(home,id)->Path`, `triage_path(home,id)->Path`, `read_triage(home,id)->{'schema':1,'proposal_id':str,'current':'pending|accepted|rejected|deferred|superseded','history':list[dict]}`, `list_proposals(home)->list[dict]`.

- [ ] **Step 1: Write failing report/revision/triage tests**

```python
from agd_lib import store, supervision, worker_skills
def produced_run(home):
    started=time.time(); ended_worker(home,'failed',str(uuid.uuid4()),'failure',1); ended_worker(home,'success',str(uuid.uuid4()),'counterexample',0); ended_worker(home,'additional',str(uuid.uuid4()),'additional failure',1)
    config=reviewer.read_config(home); packet=reviewer.select_packet(home,cutoff=time.time()); instruction=reviewer.INSTRUCTION_FILE.read_bytes()
    return reviewer.begin_run(home,packet,reviewer.enqueue_intent(home,kind='default'),model='openai/reviewer-fixture',config_snapshot=config,instruction_snapshot=instruction,started_at=started,deadline_epoch_ms=int((started+config['limits']['max_runtime_seconds'])*1000)),packet
def build_report(packet):
    failed=next(s['source_id'] for s in packet['sources'] if s['kind']=='native-jsonl' and s['task_id']=='failed'); success=next(s['source_id'] for s in packet['sources'] if s['kind']=='native-jsonl' and s['task_id']=='success')
    return {'schema':1,'summary':'native fixture','findings':[{'kind':'new-skill','target':'bounded-review','finding':'failure','applicability':'all','citations':[failed],'counterexamples':[success],'uncertainty':'low','resolution':'draft','validation':'curated resolve','validation_limits':['Requires human verification; not proof of causality'],'new_skill':{'id':'agd-review-draft','draft':'---\nname: agd-review-draft\ndescription: Review bounded evidence\n---\n# Draft\n','triggers':['review'],'scope':'review','prerequisites':[]}}]}
def test_revisions_and_finalization_are_inert():
    with lab() as (home,_):
        before=worker_skills.catalog(home); run,packet=produced_run(home); report=build_report(packet); first=reviewer.submit_report(home,run['review_run_id'],str(uuid.uuid4()),report); proposal=first['proposal_ids'][0]
        reviewer.dispose(home,proposal,'rejected','insufficient',None,{'uid':os.getuid(),'identity':str(os.getuid())}); rejected=reviewer.read_triage(home,proposal)
        report['findings'][0]['citations'].append(next(s['source_id'] for s in packet['sources'] if s['kind']=='native-jsonl' and s['task_id']=='additional')); second=reviewer.submit_report(home,run['review_run_id'],str(uuid.uuid4()),report)
        assert second['proposal_ids']==[proposal]; assert reviewer.read_triage(home,proposal)==rejected; assert reviewer.read_proposal(home,proposal)['head_revision'] != reviewer.read_proposal(home,proposal)['revisions'][0]['revision_hash']
        bad=build_report(packet); bad['findings'][0]['citations']=['unknown-source']
        with pytest.raises(store.Refusal): reviewer.submit_report(home,run['review_run_id'],str(uuid.uuid4()),bad)
        reviewer.finalize_run(home,run['review_run_id'],'exited'); assert worker_skills.catalog(home)==before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q`

Expected: FAIL because report/revision/finalization functions are absent.

- [ ] **Step 3: Implement report, revision, triage, and finalization kernels**

```python
def _digest(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def normalize(value): return ' '.join(value.split()).casefold()
def normalize_target(value):
    if not isinstance(value,str) or not value.strip(): raise store.Refusal('target')
    return normalize(value)
def proposal_path(home,proposal_id):
    proposal_id=str(uuid.UUID(proposal_id)); return paths(home)['proposals']/f'{proposal_id}.json'
def triage_path(home,proposal_id):
    proposal_id=str(uuid.UUID(proposal_id)); return paths(home)['triage']/f'{proposal_id}.json'
def read_proposal(home,proposal_id):
    p=proposal_path(home,proposal_id)
    if not p.is_file(): raise store.Refusal('unknown proposal')
    return json.loads(p.read_text())
def read_triage(home,proposal_id):
    proposal=read_proposal(home,proposal_id); p=triage_path(home,proposal['proposal_id'])
    return json.loads(p.read_text()) if p.is_file() else {'schema':1,'proposal_id':proposal['proposal_id'],'current':'pending','history':[]}
def parse_existing_proposal_or_revision(home,value):
    proposal,sep,revision=value.partition('@'); row=read_proposal(home,proposal)
    if sep and revision not in {r['revision_hash'] for r in row['revisions']}: raise store.Refusal('unknown revision')
    return value
def fingerprint(f): return _digest([1,f['kind'],normalize_target(f['target']),normalize(f['finding']),normalize(f['applicability'])])
def revision_hash(proposal_id,finding): return _digest([proposal_id,sorted(finding['citations']),sorted(finding.get('counterexamples',[])),finding['resolution'],finding['validation'],finding.get('validation_limits',[]),finding.get('new_skill'),finding['uncertainty'],finding.get('target_source_version')])
def dispose(home,proposal_id,status,reason,replacement_proposal_id,operator):
    uuid.UUID(proposal_id)
    if status not in {'accepted','rejected','deferred','superseded'} or (reason is not None and not isinstance(reason,str)) or not isinstance(operator,dict) or type(operator.get('uid')) is not int or not isinstance(operator.get('identity'),str): raise store.Refusal('disposition')
    with store.lock(home):
        prior=read_triage(home,proposal_id); proposal=read_proposal(home,proposal_id); replacement=None if replacement_proposal_id is None else parse_existing_proposal_or_revision(home,replacement_proposal_id)
        if (status=='superseded') != bool(replacement): raise store.Refusal('replacement')
        row={'status':status,'reason':reason,'replacement':replacement,'operator':operator,'at':time.time(),'basis_revision':proposal['head_revision']}
        store.atomic_json(triage_path(home,proposal_id),{'schema':1,'proposal_id':proposal_id,'current':status,'history':[ *prior['history'],row]})
    return read_triage(home,proposal_id)
```

Validate report schema, UTF-8 bound, UUID request ID, source IDs against the frozen packet, required fields, and draft front matter; counterexamples may be empty only with stated uncertainty. Existing-skill findings require an actual packet inventory target/version. A proposal has `{proposal_id,fingerprint,head_revision,revisions,uncertain_distinct}` and revisions have `{revision_hash,basis_source_ids,proposed_change,resurfaced}`. Scan proposal headers once per batch: exact fingerprint plus exact basis reuses UUID/revision idempotently; material new basis creates a new immutable revision with `resurfaced:true` without changing triage; uncertain/collision basis gets a distinct UUID marked `uncertain_distinct`. Never auto-accept or erase history.

`submit_report` only creates immutable `reports/<64hex>.json`, revisions, and mutable receipt state `report-committed`; it does not finalize. `finalize_run` runs after native disposal/child exit, unions receipt report/proposal IDs, creates one deterministic UUID5 batch event only if proposal IDs exist, checks both pending and handled paths, and atomically marks the receipt final. Recovery recreates a missing event from committed reports. Empty findings are valid coverage and emit no event. Bridge `finish` maps to `finalize_run` after `runReviewer` returns; production parent does the same after child exit; broker `finalResult` only reads committed report state.

- [ ] **Step 4: Run report tests to verify pass**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q`

Expected: PASS; accepted draft remains undiscoverable and acknowledgement never changes triage.

- [ ] **Step 5: Commit Task 2**

```bash
git add bin/agd_lib/reviewer.py tests/test_reviewer.py
git commit -m "feat: persist reviewer proposals"
```

### Task 3: Restricted native OMP host and bridge proof

**Files:**
- Create: `lib/agd-reviewer.ts`, `tests/reviewer.test.ts`, `tests/reviewer-fixture.ts`, `tests/reviewer_bridge.py`
- Modify: `bin/agd_lib/reviewer_runner.py`, `tests/test_reviewer.py`

**Interfaces:**

```ts
export type Native = { ai:any; core:any; sdk:any; telemetry:any };
export interface Broker { rpc(op:string,args:unknown):Promise<unknown>; reserveModelCall():Promise<void>; settleModelCall():Promise<void>; persistNativeSessionId(id:string):Promise<void>; finalResult():Promise<{reportId:string}>; }
export interface ReviewerInput { cwd:string; agentDir:string; sessionDir:string; reviewRunId:string; receipt:{deadline_epoch_ms:number}; model:any; reviewPrompt:string; broker:Broker; native:Native; }
export function runReviewer(input:ReviewerInput, streamFn?:any):Promise<{reportId:string}>;
```

- [ ] **Step 1: Write complete fixture, bridge client, and failing test**
```ts
// tests/reviewer-fixture.ts
export function scriptedStream(ai:any,turns:any[][],observed:string[],messages:any[]){let i=0;return(model:any,context:any)=>{observed.splice(0,observed.length,...context.tools.map((x:any)=>x.name));messages.splice(0,messages.length,...context.messages);const stream=new ai.AssistantMessageEventStream(),content=turns[i++]??[],message={role:'assistant',content,api:model.api,provider:model.provider,model:model.id,usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:content.length?'toolUse':'stop',timestamp:Date.now()};queueMicrotask(()=>{stream.push({type:'done',reason:message.stopReason,message});stream.end(message)});return stream;};}
export const call=(name:string,args:unknown)=>({type:'toolCall',id:crypto.randomUUID(),name,arguments:args});
```

```ts
// tests/reviewer.test.ts
import {expect,test} from 'bun:test';import {createInterface} from 'node:readline';import {Readable} from 'node:stream';import {readFileSync} from 'node:fs';import {join} from 'node:path';import {runReviewer} from '../lib/agd-reviewer';import {scriptedStream,call} from './reviewer-fixture';
async function startBridge() {
 const proc = Bun.spawn(['uv','run','--python','3.12','--with','pytest','tests/reviewer_bridge.py'], {stdin:'pipe',stdout:'pipe',stderr:'pipe'});
 const lines = createInterface({input:Readable.fromWeb(proc.stdout as any),crlfDelay:Infinity})[Symbol.asyncIterator]();
 const stderr = new Response(proc.stderr).text();
 const close = async () => {
  proc.stdin.end();
  const timeout = setTimeout(() => { if (proc.exitCode === null) proc.kill(); }, 5000);
  try { await proc.exited; } finally { clearTimeout(timeout); await lines.return?.(); }
 };
 const next = async () => { const row = await lines.next(); if(row.done) throw new Error(`bridge EOF: ${await stderr}`); return row.value; };
 try {
  const hello = JSON.parse(await next());
  const ai = await import(hello.aiUrl), core = await import(hello.coreUrl), sdk = await import(hello.sdkUrl), telemetry = await import(hello.telemetryUrl);
  let serial: Promise<unknown> = Promise.resolve();
  const rpc = (op:string,args:unknown) => {
   const result = serial.then(async () => {
    const id = crypto.randomUUID(); proc.stdin.write(JSON.stringify({id,op,args})+'\n'); await proc.stdin.flush();
    const reply = JSON.parse(await next()); if(reply.id !== id) throw new Error('bridge reply mismatch');
    if(!reply.ok) throw new Error(reply.error); return reply.value;
   });
   serial = result.catch(() => undefined); return result;
  };
  return {hello,native:{ai,core,sdk,telemetry},broker:{rpc,reserveModelCall:()=>rpc('reserve',{}),settleModelCall:()=>rpc('settle',{}),persistNativeSessionId:(id:string)=>rpc('session',{id}),finalResult:()=>rpc('result',{})},close};
 } catch(error) { await close(); throw error; }
}
test('native loop persists actual bridge report',async()=>{const b=await startBridge();try{const observed:string[]=[],r=await runReviewer({...b.hello.input,broker:b.broker,native:b.native},scriptedStream(b.native.ai,[[call('reviewer_search',{query:'failure'})],[call('reviewer_submit',{report:b.hello.report})],[]],observed));expect(observed.sort()).toEqual(['reviewer_excerpt','reviewer_search','reviewer_submit']);const saved=JSON.parse(readFileSync(join(b.hello.reportDir,`${r.reportId}.json`),'utf8'));expect(saved.findings[0]).toMatchObject({citations:b.hello.report.findings[0].citations,uncertainty:'low',new_skill:b.hello.report.findings[0].new_skill});}finally{await b.close();}});
```

```python
# tests/reviewer_bridge.py
import json, sys
from support import lab
from test_reviewer import produced_run, build_report
from agd_lib import reviewer_runner
with lab() as (home, _):
    run,packet=produced_run(home); report=build_report(packet); cap=reviewer_runner.assert_runtime_capability()
    print(json.dumps({'aiUrl':cap['ai_url'],'coreUrl':cap['core_url'],'sdkUrl':cap['sdk_url'],'telemetryUrl':cap['telemetry_url'],'report':report,'reportDir':str(home/'data/reviewer/reports'),'input':{'cwd':str(home),'agentDir':str(home/'.omp'),'sessionDir':str(home/'data/observability/reviewer'/run['review_run_id']/'omp'),'reviewRunId':run['review_run_id'],'receipt':run,'reviewPrompt':'Review supplied evidence only.','model':{'provider':'openai','api':'openai-completions','id':'reviewer-fixture','name':'Reviewer fixture','baseUrl':'http://127.0.0.1:1','reasoning':False,'input':['text'],'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},'contextWindow':8192,'maxTokens':1024}}}),flush=True)
    reviewer_runner.serve_broker(home,run['review_run_id'],sys.stdin,sys.stdout)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bun test tests/reviewer.test.ts`

Expected: FAIL because host, fixture, and bridge do not exist.

- [ ] **Step 3: Implement host, RPC tools, and awaited cap**

```ts
function cappedStreamFn(native:any,broker:Broker,ai:any) {
  return async (...args:any[]) => {
    await broker.reserveModelCall();
    const inner=await native(...args), outer=new ai.AssistantMessageEventStream(); outer.forwardLocalWorkFrom(inner);
    void (async()=>{try { for await (const event of inner) { if(event.type==='done'||event.type==='error') await outer.trackLocalWork(broker.settleModelCall()); outer.push(event); } outer.end(await inner.result()); } catch(error) { outer.fail(error); }})();
    return outer;
  };
}
export async function runReviewer(input:ReviewerInput,streamFn:any=input.native.ai.streamSimple) {
  const {sdk,core,ai,telemetry:exporter}=input.native; await exporter.initTelemetryExport();
  const settings=await sdk.Settings.init({cwd:input.cwd,agentDir:input.agentDir,readOnly:true,overrides:{context:{compaction:{enabled:false,midTurnEnabled:false,autoContinue:false,asyncEnabled:false,idleEnabled:false}},retry:{enabled:false,maxRetries:0,modelFallback:false,usageAwareFallback:false},advisor:{enabled:false},autolearn:{enabled:false,autoContinue:false}}}), auth=await sdk.discoverAuthStorage(input.agentDir,{cwd:input.cwd,settings}), registry=new sdk.ModelRegistry(auth,undefined,{settings}), manager=sdk.SessionManager.create(input.cwd,input.sessionDir);
  await input.broker.persistNativeSessionId(manager.getSessionId());
  const tools=reviewerTools(input.broker);
  const telemetry=exporter.isTelemetryExportEnabled()?exporter.createTelemetryExportConfig({attributes:{purpose:'background-skill-review',review_run_id:input.reviewRunId},captureMessageContent:'full'}):undefined;
  const agent=new core.Agent({initialState:{model:input.model,tools:[...tools.values()],systemPrompt:[input.reviewPrompt]},sessionId:manager.getSessionId(),deadline:input.receipt.deadline_epoch_ms,telemetry,getApiKey:(m:any,id:any,opt:any)=>registry.getApiKey(m,id,opt),streamFn:cappedStreamFn(streamFn,input.broker,ai)});
  const session=new sdk.AgentSession({agent,sessionManager:manager,settings,modelRegistry:registry,toolRegistry:tools,sideStreamFn:cappedStreamFn(streamFn,input.broker,ai),advisorStreamFn:cappedStreamFn(streamFn,input.broker,ai),memoryEnabled:false,skills:[],skillsReloadable:false,disableExtensionDiscovery:true,scoutAllowedBySpawnPolicy:false});
  try {const packetText=await input.broker.rpc('packet',{});if(typeof packetText!=='string')throw new Error('invalid packet text');await session.prompt(packetText);await manager.flush();return await input.broker.finalResult();} finally {session.beginDispose();await session.dispose();await manager.close();await exporter.flushTelemetryExport();}
}
function reviewerTools(broker:Broker) { return new Map(['reviewer_excerpt','reviewer_search','reviewer_submit'].map(name=>[name,{name,label:name,description:name,parameters:{type:'object',properties:name==='reviewer_search'?{query:{type:'string'}}:name==='reviewer_excerpt'?{source_id:{type:'string'}}:{report:{type:'object'}},required:name==='reviewer_search'?['query']:name==='reviewer_excerpt'?['source_id']:['report'],additionalProperties:false},execute:async(_id:string,args:any)=>{const text=await broker.rpc(name,args);if(typeof text!=='string')throw new Error('invalid broker text');return {content:[{type:'text',text}],details:{}};}}]));}
```

`serve_broker` reads JSONL requests, validates UUID IDs, and returns the same `{id,ok,value|error}` ID. Trusted-host control ops are `packet->{UTF-8 frozen rendered packet}` (reserves receipt input bytes before exposure), `reserve/settle/session/result/finish` with structured receipt/result values; only excerpt/search/submit are model tools and each returns UTF-8 budget-reserved rendered text. Agent `streamFn`, maintenance `sideStreamFn`, and advisor `advisorStreamFn` share the durable cap. An offline forced context-overflow/maintenance test proves no uncapped provider call and fails closed when a required stream is absent. Reservation checks absolute deadline/counter; settlement writes `dispatch:'settled'`; a post-reservation host death is `unknown-provider-outcome` and rejects resume.

- [ ] **Step 4: Run tests to verify pass**

Run: `bun test tests/reviewer.test.ts && PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/reviewer_bridge.py tests/test_reviewer.py -q`

Expected: PASS; actual runtime inventory denies `bash`/`write`, returned report ID reads persisted report, bridge closes cleanly, and no production CLI/config offline hook exists.

- [ ] **Step 5: Commit Task 3**

```bash
git add lib/agd-reviewer.ts bin/agd_lib/reviewer_runner.py tests/reviewer.test.ts tests/reviewer-fixture.ts tests/reviewer_bridge.py tests/test_reviewer.py
git commit -m "feat: run restricted native reviewers"
```

### Task 4: User timer with fixed UTC-hour interval anchor

**Files:**
- Create: `bin/agd_lib/reviewer_timer.py`, `tests/test_reviewer_timer.py`
- Modify: `bin/agd_lib/reviewer.py`, `bin/agd_lib/reviewer_runner.py`

**Interfaces:** `calculate_due(schedule,checkpoint,now_utc)->{'due_at':float|None,'next_due':float|None,'checkpoint':dict}`. Config defaults to `{'mode':'calendar','timezone':'UTC','calendar':'02:00','interval_hours':None}`; config alone is inert.

- [ ] **Step 1: Write failing schedule and manual-path tests**

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
def utc(s): return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def test_interval_hour_anchor_catches_up_once():
    s={'mode':'interval','timezone':None,'calendar':None,'interval_hours':12}
    assert reviewer_timer.calculate_due(s,{'anchor':0.,'last_due':0.},36*3600+1)=={'due_at':12*3600,'next_due':48*3600,'checkpoint':{'anchor':0.,'last_due':36*3600}}
def test_native_calendar_gap_and_fold():
    s={'mode':'calendar','timezone':'Europe/Berlin','calendar':'02:00','interval_hours':None}
    assert reviewer_timer.native_next_calendar(s,utc('2027-03-28T00:59Z'))==utc('2027-03-29T00:00Z')
    assert reviewer_timer.native_next_calendar(s,utc('2027-10-30T00:00Z'))==utc('2027-10-31T00:00Z')
    assert reviewer_timer.native_next_calendar(s,utc('2027-10-31T00:15Z'))==utc('2027-11-01T01:00Z')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --python 3.12 --with pytest pytest tests/test_reviewer_timer.py -q`

Expected: FAIL because the timer kernel does not exist.

- [ ] **Step 3: Implement timer kernel and explicit enablement**

```python
import os, re, subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
def validate_schedule(s):
    if not isinstance(s,dict) or set(s)!={'mode','timezone','calendar','interval_hours'}: raise store.Refusal('schedule keys')
    if s['mode']=='manual-only':
        if s['timezone'] is not None or s['calendar'] is not None or s['interval_hours'] is not None: raise store.Refusal('manual fields')
    elif s['mode']=='interval':
        if s['timezone'] is not None or s['calendar'] is not None or isinstance(s['interval_hours'],bool) or not isinstance(s['interval_hours'],int) or s['interval_hours']<=0: raise store.Refusal('interval')
    elif s['mode']=='calendar':
        if s['interval_hours'] is not None or not isinstance(s['calendar'],str) or not isinstance(s['timezone'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',s['calendar']): raise store.Refusal('calendar')
        try: ZoneInfo(s['timezone'])
        except ZoneInfoNotFoundError as e: raise store.Refusal('timezone') from e
    else: raise store.Refusal('mode')
    return s
def native_next_calendar(s,base):
    text=subprocess.run(['systemd-analyze','calendar',f"*-*-* {s['calendar']}:00 {s['timezone']}",'--base-time',datetime.fromtimestamp(base,timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),'--iterations=1'],env={**os.environ,'LC_ALL':'C','TZ':'UTC'},timeout=10,check=True,capture_output=True,text=True).stdout
    m=re.search(r'^\s*Next elapse:\s*(.+)$',text,re.M)
    if not m: raise store.Refusal('native calendar unavailable')
    try: return datetime.strptime(m.group(1).replace(' UTC',''),'%a %Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    except ValueError as e: raise store.Refusal('native calendar invalid') from e
def calculate_due(schedule,checkpoint,now_utc):
    if schedule['mode']=='manual-only': return {'due_at':None,'next_due':None,'checkpoint':checkpoint}
    if schedule['mode']=='interval':
        step=3600*schedule['interval_hours']; anchor=checkpoint['anchor']; last=checkpoint['last_due']; latest=anchor+max(0,int((now_utc-anchor)//step))*step; next_due=latest+step
        return {'due_at':last+step if latest>last else None,'next_due':next_due,'checkpoint':{'anchor':anchor,'last_due':max(last,latest)}}
    old=checkpoint['next_due']
    if now_utc<old:return {'due_at':None,'next_due':old,'checkpoint':checkpoint}
    future=native_next_calendar(schedule,now_utc)
    return {'due_at':old,'next_due':future,'checkpoint':{'next_due':future}}
```

`enable` alone writes an intent/owned-unit receipt under the brief home transaction, releases it, runs `daemon-reload` then enable/start, and records success or exposed failure in a new brief transaction—never under the home lock. It is idempotent and preserves the UTC-hour `floor(enable_time/3600)*3600` anchor unless the operator changes schedule and explicitly re-enables. It installs only marked owned names; disable removes only those names. No run, startup, boot, or config edit auto-enables.

Render the interval timer as `OnCalendar=*-*-* *:00:00 UTC` and `Persistent=true`; docs state the first period is hour-aligned and the global hourly wake is eligible only at its N-hour deadline. The service has trusted absolute home/source-CLI argv, home UUID unit name/marker, `TimeoutStartSec` from configured runtime, and exits before collection on an ineligible interval tick. Persistent native hourly scheduling provides one reboot catch-up. Calendar checkpoints store `next_due`; a not-due check remains unchanged, while overdue emits one old `next_due` and stores `native_next_calendar(schedule,now_utc)`. Native-enable validation requires systemd's selected zone/expression to agree with `ZoneInfo` gap/fold-0 behavior; otherwise refuse and honestly report user-systemd integration unavailable. Schedule changes require operator re-enable; later run receipts remain frozen.

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
def test_reviewer_cli_mutates_reviewer_data_not_foreign_owner():
    with lab() as (home,_):
        run,packet=produced_run(home); result=reviewer.submit_report(home,run['review_run_id'],str(uuid.uuid4()),build_report(packet)); reviewer.finalize_run(home,run['review_run_id'],'exited')
        task_before=(home/'data/tasks').read_bytes() if (home/'data/tasks').exists() else b''; catalog=worker_skills.catalog(home); selection=worker_skills.resolve(home)
        owner={'pid':os.getpid(),'started':supervision.process_start(os.getpid()),'token':str(uuid.uuid4())}; owner_path=home/'state/owner.json'; owner_path.parent.mkdir(parents=True,exist_ok=True); owner_path.write_text(json.dumps(owner)); before=owner_path.read_bytes()
        command(home,'reviewer','proposals','dispose',result['proposal_ids'][0],'--status','accepted')
        assert owner_path.read_bytes()==before and task_before==((home/'data/tasks').read_bytes() if (home/'data/tasks').exists() else b'') and worker_skills.catalog(home)==catalog and worker_skills.resolve(home)==selection

```python
def test_manual_empty_corpus_leaves_private_timer_state_and_makes_no_model_call():
    with lab() as (home,_):
        checkpoint=home/'state/reviewer/checkpoint.json'; checkpoint.parent.mkdir(parents=True); checkpoint.write_bytes(b'{"anchor":0,"last_due":0}')
        units=home/'private-units'; units.mkdir(); (units/'owned.timer').write_text('owned')
        before=(checkpoint.read_bytes(),{p.name:p.read_bytes() for p in units.iterdir()})
        result=command(home,'reviewer','run')
        assert result['state']=='no-work'
        assert (checkpoint.read_bytes(),{p.name:p.read_bytes() for p in units.iterdir()})==before
```

Manual `reviewer run` with no eligible evidence exits `no-work` before native-host/model dispatch; this is the sole manual-inertness acceptance path and never enables a user unit.

Extend existing `session switch replaces only its owned watcher` test only after its `home`, `deliveries`, and `hooks` definitions:

```ts
const reviewRunId='00000000-0000-4000-8000-000000000030';
const beforeDeliveries=deliveries.length, deadline=Date.now()+5000;
writeFileSync(join(home,'state','events','33333333-3333-4333-8333-333333333333.json'),JSON.stringify({
 id:'33333333-3333-4333-8333-333333333333',purpose:'background-skill-review',review_run_id:reviewRunId,kind:'reviewer-batch',details:{report_ids:[],proposal_ids:[]}}));
while (deliveries.length === beforeDeliveries && Date.now() < deadline) await Bun.sleep(50);
expect(deliveries.length).toBe(beforeDeliveries+1);
expect(deliveries.at(-1)).toEqual({deliverAs:'followUp'});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q && bun test tests/primary.test.ts`

Expected: FAIL because parser/reviewer-purpose watcher handling is absent.

- [ ] **Step 3: Add parser/dispatch**

```python
reviewer_p=sub.add_parser('reviewer').add_subparsers(dest='reviewer_op',required=True)
run_p=reviewer_p.add_parser('run'); run_p.add_argument('--timer',action='store_true'); run_p.add_argument('--retrospective-review-run',action='append',default=[])
reviewer_p.add_parser('status')
reports=reviewer_p.add_parser('reports').add_subparsers(dest='report_op',required=True); reports.add_parser('list'); reports.add_parser('show').add_argument('report_id')
proposals=reviewer_p.add_parser('proposals').add_subparsers(dest='proposal_op',required=True); proposals.add_parser('list'); proposals.add_parser('show').add_argument('proposal_id')
dispose=proposals.add_parser('dispose'); dispose.add_argument('proposal_id'); dispose.add_argument('--status',required=True,choices=('accepted','rejected','deferred','superseded')); dispose.add_argument('--reason'); dispose.add_argument('--replacement')
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

- [ ] **Step 1: Run a throwaway native bridge/CLI smoke**

Move/export `startBridge` (Node imports only; no `bun:test`) into `tests/reviewer-fixture.ts`; reviewer tests import it. Generate `tests/.reviewer-smoke.ts` by here-doc, run it with `PATH=/tmp/agd-treehouse-v3.1.6:$PATH bun tests/.reviewer-smoke.ts`, and delete it in `finally`. It uses real `startBridge`, `runReviewer`, `scriptedStream`, three tool calls, then `broker.rpc('finish',{})` after host disposal; actual returned IDs drive `bin/agd --home H reviewer reports show`, proposals show/dispose, events list/ack, worker skills/profile/task list. It snapshots catalog/profile/task/skill bytes, asserts accepted proposal and acknowledged event do not change them, acknowledgement leaves triage pending, receipt session ID equals native JSONL v3 header ID, event purpose/run match, three runtime tools exactly, and first model context includes authorized packet citation/inventory but not sentinel. No model HTTP, user unit, fixed IDs, handwritten report, or undefined helper.

- [ ] **Step 2: Add acceptance/docs matrix**

| Case | Action | Observable |
|---|---|---|
| inventory/denial/injection | packet with sentinel | three tools, authorized evidence only |
| LF native-only | LF available/unavailable | no query client, same result |
| late feedback | one linked span | one follow-up only |
| crash budget | reserve/kill | unknown outcome/no resume |
| foreign owner | CLI triage | owner/task unchanged |
| event lifecycle | finalize/ack | pending→handled; triage unchanged |
| dedup | exact/material/uncertain basis | reuse/revision/distinct |
| proposals | existing/new high-impact | validation schema |
| skills | assigned not loaded | unknown provenance |
| self review | closed/current/later IDs | only closed/no recursion |
| links | explicit/unrelated | explicit only |

Document defaults `262144/65536/20/1800/20`, null model inheritance, IANA/DST/UTC-hour anchor, frozen evidence privacy/retention/no pruning, triage/recovery/no USD budgets, native private metadata, inert drafts, explicit timer approval/no autostart/no OS sandbox. Record OMP 18.3.2 and readonly systemd 255 DST evidence; separately approved disposable systemd activation is never pytest and unavailable prerequisite is reported.

- [ ] **Step 3: Run final verification**

```bash
PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py tests/test_reviewer_timer.py tests/test_supervision.py -q
PATH=/tmp/agd-treehouse-v3.1.6:$PATH bun test tests/reviewer.test.ts tests/primary.test.ts
git diff --check
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
