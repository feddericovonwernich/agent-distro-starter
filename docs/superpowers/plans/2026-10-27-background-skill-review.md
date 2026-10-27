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
| `tests/test_reviewer.py`, `tests/reviewer_bridge.py`, `tests/test_reviewer_timer.py`, `tests/reviewer.test.ts`, `tests/primary.test.ts` | behavior proof |
| `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md` | user contract |

```python
# reviewer.py
DEFAULT_LIMITS = {'max_candidates':20,'max_input_bytes':262144,'max_model_calls':20,
                  'max_output_bytes':65536,'max_runtime_seconds':1800}
def select_packet(home: Path, *, cutoff: float, retrospective_review_run_ids: tuple[str,...]=()) -> dict: ...
def enqueue_intent(home: Path, *, kind: str, retrospective_review_run_ids: tuple[str,...]=()) -> dict: ...
def begin_run(home: Path, packet: dict, intent: dict, *, model: str, config_digest: str,
              instruction_digest: str, started_at: float, deadline_epoch_ms: int) -> dict: ...
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
import hashlib, json, os, sys, time, uuid
from pathlib import Path
import pytest
from support import lab, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, tasks

def ended_worker(home, task_id, incarnation, text):
    brief=home/f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task=command(home,'task','create',task_id,'--project','sample','--brief',str(brief))
    task.update(executor='worker',incarnation=incarnation,phase='failed',worker_alive=False); tasks.save(home,task)
    binding=observability.worker_binding(home,task_id,incarnation); sid=str(uuid.uuid4()); iso='2026-01-01T00:00:00.000Z'
    rows=[{'type':'session','version':3,'id':sid,'timestamp':iso,'cwd':str(home)},
          {'type':'message','id':str(uuid.uuid4()),'parentId':None,'timestamp':iso,'message':{'role':'user','content':text,'timestamp':1767225600000}}]
    Path(binding['session_dir'],f'{sid}.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows)); return task
def test_packet_has_distinct_failure_and_counterexample_sources():
    with lab() as (home,_):
        a=ended_worker(home,'failed',str(uuid.uuid4()),'failure'); b=ended_worker(home,'success',str(uuid.uuid4()),'counterexample')
        packet=reviewer.select_packet(home,cutoff=time.time()); native=[s for s in packet['sources'] if s['kind']=='native-jsonl']
        assert {x['task_id'] for x in packet['episodes']}=={a['id'],b['id']}; assert len({x['source_id'] for x in native})==2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py::test_packet_has_distinct_failure_and_counterexample_sources -q`

Expected: FAIL because `agd_lib.reviewer` does not exist.

- [ ] **Step 4: Implement packet/intent/run kernel**

```python
def _canonical(v): return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def _digest(v): return hashlib.sha256(_canonical(v)).hexdigest()
def paths(home):
    root=store.home(home); return {'config':root/'data/reviewer/config.json','packets':root/'data/reviewer/packets','runs':root/'data/reviewer/runs','pending':root/'state/reviewer/pending','lock':root/'state/reviewer/run.lock'}
def _sources(home,cutoff,max_candidates):
    found=[]
    for p in sorted((store.home(home)/'data/tasks').glob('*/task.json')):
        task=json.loads(p.read_text())
        if task.get('worker_alive') is not False or task.get('phase')=='running': continue
        b=store.home(home)/f"data/observability/tasks/{task['id']}/omp/{task['incarnation']}/binding.json"
        if not b.is_file(): continue
        for jsonl in sorted(Path(json.loads(b.read_text())['session_dir']).glob('*.jsonl')):
            raw=jsonl.read_bytes()
            if jsonl.stat().st_mtime<=cutoff: found.append({'source_id':_digest([task['id'],task['incarnation'],hashlib.sha256(raw).hexdigest()]),'kind':'native-jsonl','task_id':task['id'],'incarnation':task['incarnation'],'version':hashlib.sha256(raw).hexdigest(),'sha256':hashlib.sha256(raw).hexdigest(),'span':{'start':0,'bytes':len(raw)},'relation':'ended-worker'})
    return found[:max_candidates]
def select_packet(home,*,cutoff,retrospective_review_run_ids=()):
    config=read_config(home); src=[]; used=0
    for row in _sources(home,cutoff,config['limits']['max_candidates']):
        if used+row['span']['bytes']<=config['limits']['max_input_bytes']: src.append(row); used+=row['span']['bytes']
    packet={'schema':1,'cutoff':cutoff,'sources':src,'episodes':[{'task_id':x['task_id'],'incarnation':x['incarnation']} for x in src],'limits':config['limits'],'skill_inventory':[],'coverage':{'unknown_relation':[],'inaccessible':[],'withheld':[]},'langfuse_index':'unused'}; packet['packet_id']=_digest(packet); store.atomic_json(paths(home)['packets']/f"{packet['packet_id']}.json",packet); return packet
def begin_run(home,packet,intent,*,model,config_digest,instruction_digest,started_at,deadline_epoch_ms):
    if not model or not all(len(x)==64 for x in (config_digest,instruction_digest)) or deadline_epoch_ms<=int(started_at*1000): raise store.Refusal('unfrozen run')
    run={'schema':1,'review_run_id':str(uuid.uuid4()),'packet_id':packet['packet_id'],'cutoff':packet['cutoff'],'model':model,'config_digest':config_digest,'instruction_digest':instruction_digest,'limits':packet['limits'],'started_at':started_at,'deadline_epoch_ms':deadline_epoch_ms,'native_session_id':None,'input_bytes':0,'output_bytes':0,'model_calls':0,'dispatch':'none','state':'created'}; store.atomic_json(paths(home)['runs']/f"{run['review_run_id']}.json",run); return run
```

`read_config` validates exact schema/schedule/limits/review_model once, and `review_model=None` inherits native default before `begin_run`; retrospective branch accepts only closed prior reviewer UUIDs and records unknown/current/later IDs in coverage.

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
- Modify: `bin/agd_lib/reviewer.py`, `tests/test_reviewer.py`

**Interfaces:** Consumes Task 1 receipt/packet. Produces `submit_report`, `dispose`, reviewer-purpose pending/handled event behavior through unchanged supervision helpers.

- [ ] **Step 1: Write failing report/new-skill/triage tests**

```python
from agd_lib import store, supervision, worker_skills

def produced_run(home):
    ended_worker(home,'failed',str(uuid.uuid4()),'failure'); ended_worker(home,'success',str(uuid.uuid4()),'counterexample')
    packet=reviewer.select_packet(home,cutoff=time.time()); config=reviewer.read_config(home); started=time.time()
    instruction=b'Review only supplied evidence.'
    return reviewer.begin_run(home,packet,reviewer.enqueue_intent(home,kind='default'),model='openai/reviewer-fixture',
      config_digest=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),instruction_digest=hashlib.sha256(instruction).hexdigest(),
      started_at=started,deadline_epoch_ms=int((started+packet['limits']['max_runtime_seconds'])*1000)),packet

def build_report(packet):
    first, second=(s['source_id'] for s in packet['sources'][:2])
    return {'schema':1,'summary':'native fixture','findings':[{'kind':'new-skill','target':None,'finding':'failure','applicability':'all','citations':[first],'counterexamples':[second],'uncertainty':'low','resolution':'draft','validation':'curated resolve','new_skill':{'id':'agd-review-draft','draft':'# Draft\n','triggers':['review'],'scope':'review','prerequisites':[]}}]}

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

Validate canonical UTF-8 report size, UUID request ID, frozen source IDs, required finding fields, distinct citations/counterexamples, and draft schema. Content-address immutable report/revision blobs; under short `store.lock`, publish index, then reviewer-purpose event file using the existing `state/events/<uuid>.json` pattern, then mark receipt final. Recovery preserves blobs and recreates event only when neither pending nor handled event path exists. Existing `supervision.pending/acknowledge` already load/move arbitrary UUID envelopes unchanged.

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
export function scriptedStream(ai:any,turns:any[][],observed:string[]){let i=0;return(model:any,context:any)=>{observed.splice(0,observed.length,...context.tools.map((x:any)=>x.name));const stream=new ai.AssistantMessageEventStream(),content=turns[i++]??[],message={role:'assistant',content,api:model.api,provider:model.provider,model:model.id,usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:content.length?'toolUse':'stop',timestamp:Date.now()};queueMicrotask(()=>{stream.push({type:'done',reason:message.stopReason,message});stream.end(message)});return stream;};}
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
  const settings=await sdk.Settings.init({cwd:input.cwd,agentDir:input.agentDir}), auth=await sdk.discoverAuthStorage(input.agentDir,{cwd:input.cwd,settings}), registry=new sdk.ModelRegistry(auth,undefined,{settings}), manager=sdk.SessionManager.create(input.cwd,input.sessionDir);
  await input.broker.persistNativeSessionId(manager.getSessionId());
  const tools=reviewerTools(input.broker);
  const telemetry=exporter.isTelemetryExportEnabled()?exporter.createTelemetryExportConfig({attributes:{purpose:'background-skill-review',review_run_id:input.reviewRunId},captureMessageContent:'full'}):undefined;
  const agent=new core.Agent({initialState:{model:input.model,tools:[...tools.values()],systemPrompt:[input.reviewPrompt]},sessionId:manager.getSessionId(),deadline:input.receipt.deadline_epoch_ms,telemetry,getApiKey:(m:any,id:any,opt:any)=>registry.getApiKey(m,id,opt),streamFn:cappedStreamFn(streamFn,input.broker,ai)});
  const session=new sdk.AgentSession({agent,sessionManager:manager,settings,modelRegistry:registry,toolRegistry:tools,memoryEnabled:false,skills:[],skillsReloadable:false,disableExtensionDiscovery:true,scoutAllowedBySpawnPolicy:false});
  try {await session.prompt(input.reviewPrompt);await manager.flush();return await input.broker.finalResult();} finally {session.beginDispose();await session.dispose();await manager.close();await exporter.flushTelemetryExport();}
}
function reviewerTools(broker:Broker) { return new Map(['reviewer_excerpt','reviewer_search','reviewer_submit'].map(name=>[name,{name,label:name,description:name,parameters:{type:'object',properties:name==='reviewer_search'?{query:{type:'string'}}:name==='reviewer_excerpt'?{source_id:{type:'string'}}:{report:{type:'object'}},required:name==='reviewer_search'?['query']:name==='reviewer_excerpt'?['source_id']:['report'],additionalProperties:false},execute:async(_id:string,args:any)=>{const text=await broker.rpc(name,args);if(typeof text!=='string')throw new Error('invalid broker text');return {content:[{type:'text',text}],details:{}};}}]));}
```

`serve_broker` reads one JSONL request at a time, validates UUID `id`, invokes packet excerpt/search/submit, and writes `{id,ok,value|error}` with the same ID. Python receipt reservation checks absolute deadline/counter before returning; settlement writes `dispatch:'settled'`. If the host dies after reservation, recovery leaves `unknown-provider-outcome` and rejects resume.

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

- [ ] **Step 1: Write failing end-to-end bridge test**

```python
def test_bridge_report_can_be_triaged_without_skill_mutation():
    with bridge_fixture() as result:
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
