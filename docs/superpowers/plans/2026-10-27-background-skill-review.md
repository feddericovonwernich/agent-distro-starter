# Background Skill Review Implementation Plan

**Goal:** Add an opt-in bounded reviewer that produces immutable, human-triaged skill proposals from completed-worker native OMP evidence. It never applies a proposal, creates an ordinary task, or claims central ownership.

## Non-negotiable contracts

## Execution preflight

- Implementation source is `/home/fedderico/Workspace/projects/agent-distro-starter`. Before any runtime edit, use `superpowers:using-git-worktrees` to create an isolated feature worktree from that source; reconcile and edit only there.
- Never read, edit, test, merge, or run this work in `/home/fedderico/Workspace/eva-beta-01` (the running distro) or in the stable source checkout.
- The reviewer `run.lock` is held for the full child/IPC/provider lifetime. Only short `store.lock(home)` transactions are released before model/network/systemd waits.

- Reconcile an implementation worktree with `origin/main` by normal merge only. `PLAN_BASE` must remain an ancestor of the result. PR 5 is user-confirmed merged work: verify its forge merge SHA and the resulting `observability.py` tree/patch behavior; do **not** require original feature SHA ancestry after a squash merge.
- Native JSONL bound through merged observability is authoritative. No Langfuse client, credential, index, API, or new query path is added. If the reconciled repository already has an indexed span-discovery reader, it may provide separately recorded optional source IDs; otherwise record `langfuse_index: "unused"` and collect identically with Langfuse online/offline. A late consumed indexed source is a separately bounded follow-up keyed by immutable source hash/version, never inferred from task names.
- The reviewer never writes task, worker, skill, profile, project, lease, or `state/owner.json` data; it never sets `AGD_TASK_ID`/`AGD_INCARNATION`. Reviewer mutation authority is canonical `AGD_HOME` + effective UID + reviewer-private path validation + reviewer locks. It works with no/dead/live-foreign central owner and leaves ordinary owner/task bytes unchanged.
- Use `store.lock(home)` for short reviewer receipt/intent/triage/event transactions only. Hold reviewer `state/reviewer/run.lock` nonblocking for the entire one-shot process. Release the home lock before any OMP/provider/systemd wait; do not release `run.lock` during a run.
- Frozen per-run receipt fields: `review_run_id`, packet ID/cutoff/source hashes, normalized intent, resolved model, config/instruction digest, limits, `deadline_epoch_ms`, native session ID, input/output bytes, model call count, dispatch state, and final report/event state. Persist a reservation before each broker response or native provider dispatch. A crash after dispatch but before settlement is `unknown-provider-outcome`; recovery does not retry that model request or claim exactly-once.
- Defaults are candidates=20, input=262144 UTF-8 bytes, calls=20, report=65536 UTF-8 bytes, runtime=1800 seconds. Configured overrides are type-checked bounded values and frozen into the receipt; all service/host limits derive from the frozen value.
- Provider credentials are available only to native transport through `ModelRegistry.getApiKey`. They must not appear in prompt, packet, broker frame, tool argument/result, report, JSONL artifact, or Langfuse configuration.

## Files and interfaces

| Path | Responsibility |
|---|---|
| `bin/agd_lib/reviewer.py` | reviewer-private paths, config, evidence packet/intent/run/report/triage persistence and recovery |
| `bin/agd_lib/reviewer_runner.py` | capability gate, resolved-model-before-run, process lifetime and receipt transitions |
| `lib/agd-reviewer.ts` | direct installed-OMP host: restricted Agent/AgentSession, broker tools, native stream cap |
| `bin/agd_lib/reviewer_timer.py` | strict schedule validation, pure due calculation, owned user-unit install/rearm |
| `bin/agd_lib/supervision.py` | backward-compatible purpose event envelope support; task event behavior unchanged |
| `bin/agd`, `.omp/extensions/agd-primary.ts` | complete backed CLI and purpose-event presentation only |
| `tests/test_reviewer.py`, `tests/test_reviewer_timer.py`, `tests/test_supervision.py`, `tests/reviewer.test.ts`, `tests/primary.test.ts` | behavioral proof below |

```python
# reviewer.py
DEFAULT_LIMITS = {'max_candidates':20, 'max_input_bytes':262144,
                  'max_model_calls':20, 'max_output_bytes':65536,
                  'max_runtime_seconds':1800}
def select_packet(home: Path, *, cutoff: float,
                  retrospective_review_run_ids: tuple[str, ...] = ()) -> dict: ...
def enqueue_intent(home: Path, *, kind: str,
                   retrospective_review_run_ids: tuple[str, ...] = ()) -> dict: ...
def begin_run(home: Path, packet: dict, intent: dict, *, model: str,
              config_digest: str, instruction_digest: str) -> dict: ...
def submit_report(home: Path, review_run_id: str, request_id: str, report: dict) -> dict: ...
def dispose(home: Path, proposal_id: str, status: str, reason: str | None,
            replacement_proposal_id: str | None, operator: dict) -> dict: ...
# reviewer_timer.py
def calculate_due(schedule: dict, checkpoint: dict, now_utc: float) -> dict: ...
# returns {'due_at': float|None, 'next_due': float, 'checkpoint': dict}
# reviewer_runner.py
def launch(home: Path, review_run_id: str, packet: dict, model: str,
           *, test_stream_spec: dict | None = None) -> dict: ...
```

`test_stream_spec` is accepted only by a Python test-only direct call and serializes into the disposable child protocol; CLI/config/environment never accept it. Production launch always resolves an installed configured model.

## Task 1 — reconcile, native evidence, packets, intents, frozen runs

1. Fetch and normally merge `origin/main`; prove `PLAN_BASE` ancestor and `git diff --check`. Verify merged PR 5 by forge merge SHA plus reconciled observability tree/tests, allowing squash mapping.
2. Reuse merged `observability.worker_binding(home, task_id, incarnation)`. It validates task ID and UUID incarnation and allocates `data/observability/tasks/<task>/omp/<incarnation>/binding.json`. Reviewer binding is parallel and contains only `review_run_id`, `purpose`, `session_dir`, `state`.
3. Use real test setup imports and valid native JSONL—not plaintext:

```python
import json, sys
from pathlib import Path
from support import lab, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, tasks

INC = '00000000-0000-4000-8000-000000000001'
def ended_worker(home, task_id):
    brief = home / f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief))
    task.update(executor='worker', incarnation=INC, phase='failed', worker_alive=False)
    tasks.save(home, task)
    binding = observability.worker_binding(home, task_id, INC)
    iso, millis = '2027-01-01T00:00:00.000Z', 1798761600000
    rows = [
      {'type':'session','version':3,'id':'00000000-0000-4000-8000-000000000010','timestamp':iso,'cwd':str(home)},
      {'type':'message','id':'00000000-0000-4000-8000-000000000011','parentId':None,'timestamp':iso,
       'message':{'role':'user','content':'failed boundary evidence','timestamp':millis}},
    ]
    Path(binding['session_dir'], 'fixture.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
    return task

def test_native_packet_uses_ended_bound_evidence_only():
    with lab() as (home, _):
        failed = ended_worker(home, 'failed')
        packet = reviewer.select_packet(home, cutoff=1798848000.0)
        assert [e['task_id'] for e in packet['episodes']] == [failed['id']]
        assert packet['sources'][0]['kind'] == 'native-jsonl'
        assert sum(s['span']['bytes'] for s in packet['sources']) <= packet['limits']['max_input_bytes']
```

A second fixture creates a distinct ended task with distinct UUID incarnation/source as counterevidence; default selection is deterministic across both ended workers. Retrospective input accepts only closed **reviewer** UUIDs, validates prior-to-current ordering, and cannot name a worker incarnation.
4. Packet collection reads only binding-referenced JSONL entries, records source path-relative ID/version/hash/byte span/relation, and uses explicit immutable feedback links only. It writes packet bytes first, hashes canonical JSON, then enqueues intent. Intent fingerprint is kind + exact normalized retrospective review IDs + frozen config/instruction digest + schedule anchor. Only equal default/periodic intents deduplicate; explicit retrospective records never merge/widen.
5. Resolve model and validate config/instructions before `begin_run`; it writes the frozen receipt and native session directory before the first model dispatch.

## Task 2 — report, proposal, disposition, reviewer-purpose event

Use Task 1’s real packet producer in all tests; do not construct `p1`, fake hashes, or source IDs. The fixture returns two distinct source IDs, one failure finding and one successful counterexample. The report schema is exact: top-level schema/summary/findings; each finding has kind, target, finding, applicability, citations, distinct counterexamples, uncertainty, resolution, validation, and optional new-skill draft/triggers/scope/prerequisites.

`submit_report` holds `store.lock` only while checking receipt/request ID, writing content-addressed immutable blobs, and publishing. It measures UTF-8 canonical report bytes, validates every citation/counterexample against frozen packet provenance, and returns `{review_run_id, report_id, proposal_ids, revision_ids}`. Same review run + report hash returns existing report. Evidence is never deleted.

`dispose` validates UUIDs before building paths. `superseded` requires exactly one existing replacement proposal UUID; other statuses require no replacement. Under short `store.lock` it appends triage history and atomically updates current state.

`emit_batch` uses deterministic `str(uuid.uuid5(uuid.NAMESPACE_URL, 'reviewer-batch:'+review_run_id))`, one event per finalized run, with `{purpose:'background-skill-review',review_run_id,kind:'reviewer-batch',details:{report_ids,proposal_ids}}`. Publish report index, then event, then receipt final state. Recovery retains blobs; if index exists and event is neither pending nor handled, recreates the deterministic event before finalizing. `supervision.pending` and `acknowledge` accept this envelope without a fake task/incarnation; handled receipt prevents re-emission.

The accepted-new-skill test uses the existing resolver rather than filesystem absence: `before_catalog = worker_skills.catalog(home)`; `before_selection = worker_skills.resolve(home)`; `before_bytes = {p.relative_to(home): p.read_bytes() for p in (home/'config'/'worker-skills').rglob('*') if p.is_file()}`. Submit/accept a report with `new_skill` draft ID, then assert `worker_skills.catalog(home) == before_catalog`, `worker_skills.resolve(home) == before_selection`, bytes equal `before_bytes`, and draft ID absent from catalog IDs; finally assert `worker_skills.resolve(home, skill_mode='curated', skills=(draft_id,))` raises `store.Refusal`. The draft remains only in reviewer-private proposal data.

## Task 3 — installed native OMP host and offline proof

Capability gate: find `omp`, realpath to verified `@oh-my-pi/pi-coding-agent` 18.3.2 package. Use Bun resolution rooted at that installed entry for `@oh-my-pi/pi-coding-agent/sdk` (wildcard export), `@oh-my-pi/pi-agent-core`, and `@oh-my-pi/pi-ai`; verify all real paths remain below installed dependency roots. Verify SDK exports `Settings`, `discoverAuthStorage`, `ModelRegistry`, `SessionManager`, `AgentSession`; core exports `Agent`; AI exports `streamSimple`.

Host bootstrap is direct because `createAgentSession` does not expose streamFn:

```ts
await initTelemetryExport();
const settings = await Settings.init({ cwd, agentDir });
const auth = await discoverAuthStorage(agentDir, { cwd, settings });
const registry = new ModelRegistry(auth, undefined, { settings });
const manager = SessionManager.create(cwd, sessionDir);
const nativeSessionId = manager.getSessionId()!;
await broker.persistNativeSessionId(nativeSessionId); // before Agent construction
const telemetry = createTelemetryExportConfig({
  attributes: { purpose: 'background-skill-review', review_run_id },
  captureMessageContent: false,
});
const agent = new Agent({
  initialState: { model, tools: [...toolRegistry.values()], systemPrompt: [reviewPrompt] },
  sessionId: nativeSessionId, deadline: receipt.deadline_epoch_ms, telemetry,
  getApiKey: (m, id, opt) => registry.getApiKey(m, id, opt),
  streamFn: cappedStreamFn(streamSimple, broker, receipt),
});
const session = new AgentSession({agent, sessionManager: manager, settings, modelRegistry: registry,
  toolRegistry, memoryEnabled:false, skills:[], skillsReloadable:false,
  disableExtensionDiscovery:true, scoutAllowedBySpawnPolicy:false});
try { await session.prompt(reviewPrompt); await manager.flush(); }
finally { session.beginDispose(); await session.dispose(); await manager.close(); await flushTelemetryExport(); }
```

`cappedStreamFn(native, broker, receipt)` receives the exact native StreamFn arguments. It atomically reserves the next call count and sets `dispatch:'sent'` in receipt before calling `native(...args)`; it rejects count > frozen limit or expired deadline. It wraps stream completion to persist settled status. It neither alters prompt nor creates spans. `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit` are the only tools in both Agent state and toolRegistry.

Broker frames are UTF-8 JSONL `{id,op,args}`/`{id,ok,value|error}`. Before write, measure encoded bytes of the actual frame; excerpt/search reserve from frozen input budget; submit reserves frozen output bytes. Frame maximum is that operation’s remaining frozen budget plus its actual encoded fixed envelope—not a 64 KiB cap. Duplicate/unknown IDs and invalid schemas refuse. Production has no offline fallback.

`tests/reviewer.test.ts` creates a temporary broker fixture and passes `test_stream_spec` only through `reviewer_runner.launch` test call. Its deterministic StreamFn emits a real native tool call sequence search→submit; assertions cover three-tool inventory, native JSONL, broker-persisted report/triage, denied `bash`/`write`, invalid source refusal, 20th/21st call boundary, expired deadline, and absent credentials in model-visible messages/tool frames. This is the same host path as production with only `streamFn` injected.

## Task 4 — timer

`calculate_due(schedule, checkpoint, now_utc)` is the sole pure contract and returns dictionary keys `due_at`, `next_due`, `checkpoint`. Calendar validation requires exact keys/types, non-bool positive interval, valid `HH:MM`, `ZoneInfo` zone, and no unknown mode. Calendar algorithm constructs fold=0 only; if that local time is nonexistent it skips that calendar day rather than shifting. Enable verifies systemd behavior with `systemd-analyze calendar --base-time=<before-gap-or-fold> --iterations=2 <emitted-expression>` for both DST transitions and refuses enable if native output disagrees.

Interval uses an owned absolute UTC `OnCalendar=<next_due>` timer with `Persistent=true`, never `OnUnitActiveSec`. Before model start the timer service transaction computes/persists checkpoint and arms the next absolute deadline; then releases home lock and runs. Crash recovery sees arm receipt: rearm if absent, preserve it if already enabled, and enqueue at most one overdue intent. This avoids completion drift and boot lateness. Manual runs do not change anchor.

Write UUID-marked per-home units under `$XDG_CONFIG_HOME/systemd/user`; `TimeoutStartSec` uses frozen runtime limit. `daemon-reload` then enable/rearm the owned name. Tests use concrete dict access (`decision['due_at']`), real `zoneinfo`, and `systemd-analyze` parsed results—not string-substring checks.

## Task 5 — CLI and primary fixture

Add backed parser flags `reviewer run [--timer] [--retrospective-review-run UUID]`, reports/proposals list/show/dispose, and timer enable/disable/status. No `--approval` token exists; explicit enable is approval. Extend the existing `tests/primary.test.ts` `session switch replaces only its owned watcher` body: after its declared `home`, `deliveries`, watcher startup and `state/events` directory exist, write a reviewer-purpose event with declared `reviewRunId`/`proposalId`, wait on the same delivery loop, and assert `deliveries.at(-1) == {deliverAs:'followUp'}`. No free fixture variables or alternate watcher are introduced.

## Task 6 — acceptance

Run focused pytest/Bun suites then full pytest. Record exact artifact/command for: native-only Langfuse-unavailable and existing-index-unconsumed behavior; explicit immutable feedback link vs same-named unrelated task; assigned vs loaded skill provenance; failure/counterexample and late linked follow-up; prior/self/current retrospective boundaries; crash-budget persistence; invalid/oversize/duplicate broker frames and submit/event recovery; certain/uncertain/new-basis dedup; real inert accepted new skill; no/dead/live foreign owner; DST gap/fold, missed interval, manual anchor; acknowledgement and proposal inertness. Missing user-systemd is reported unexercised, never passed.

## Review checklist

- No generic queue, fake task/incarnation, central ownership takeover, Langfuse API, custom telemetry SDK, CLI-only tool enforcement, or production offline model path.
- Every long wait occurs outside `store.lock`; `run.lock` remains held for process exclusivity.
- All tests use producer-defined packets/reports or actual native/session/systemd fixtures; no plaintext JSONL, free variables, fake worker retrospective IDs, or incidental source-text assertions.
