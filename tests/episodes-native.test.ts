import { expect, test } from 'bun:test';
import { createHash, randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

type Fixture = {
  home: string;
  task?: { id: string; incarnation: string; worktree: string };
  session_dir?: string;
  capability: { sdk_url: string; ai_url: string; manager_url: string; registry_url: string };
  role: 'worker' | 'central';
  extension: string;
};

type Input = { systemPrompt: string[]; messages: Array<Record<string, unknown>> };
function nativeRows(path: string): Array<Record<string, unknown>> {
  return readFileSync(path, 'utf8').trim().split(/\r?\n/).filter(Boolean)
    .map(line => JSON.parse(line) as Record<string, unknown>);
}
function nativeFinal(rows: Array<Record<string, unknown>>): Record<string, unknown> | undefined {
  return rows.findLast(row => {
    if (row.type !== 'message' || typeof row.message !== 'object' || row.message === null) return false;
    const message = row.message as Record<string, unknown>;
    return message.role === 'assistant' && message.stopReason === 'stop' && typeof message.timestamp === 'number';
  });
}
async function waitForNativeFinal(path: string): Promise<void> {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try { if (nativeFinal(nativeRows(path))) return; } catch { /* writer may not have opened yet */ }
    // The SDK exposes no native-write completion promise; this brief integration poll observes the actual file.
    await new Promise(resolve => setTimeout(resolve, 25));
  }
}

async function waitForNativePredicate(path: string, predicate: (rows: Array<Record<string, unknown>>) => boolean): Promise<void> {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try {
      const rows = nativeRows(path);
      if (predicate(rows)) return;
    } catch { /* writer may not have opened yet */ }
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error(`native predicate timeout: ${path}`);
}
function completionRecords(path: string): Array<Record<string, unknown>> {
  if (!existsSync(path)) return [];
  const records: Array<Record<string, unknown>> = [];
  for (const source of readdirSync(path)) {
    const sourcePath = join(path, source);
    for (const interaction of readdirSync(sourcePath)) {
      if (!interaction.endsWith('.json')) continue;
      records.push(JSON.parse(readFileSync(join(sourcePath, interaction), 'utf8')) as Record<string, unknown>);
    }
  }
  return records;
}
function completionSnapshot(path: string): string {
  return completionRecords(path).map(value =>
    JSON.stringify({ source_id: value.source_id, interaction_id: value.interaction_id, recorded_at: value.recorded_at, final: value.final }),
  ).sort().join('\n');
}

function reviewerBridge(repo: string, home: string, cutoff: number, finalize: boolean): Record<string, unknown> {
  const script = String.raw`
import json, sys, time, uuid
from pathlib import Path
from agd_lib import reviewer
home = Path(sys.argv[1])
cutoff = float(sys.argv[2])
finalize = sys.argv[3] == '1'
config = reviewer.read_config(home)
packet = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
result = {'packet': packet}
if finalize:
    started = time.time()
    intent = reviewer.enqueue_intent(home, kind='manual')
    run = reviewer.reserve_run(home, intent, config_snapshot=config,
                               instruction_snapshot=b'native-episode-bridge',
                               started_at=started,
                               deadline_epoch_ms=int((started + 1800) * 1000))
    reviewer.begin_run(home, run, packet, model='fixture/native-bridge',
                       observed_omp_version='native-test')
    reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()),
                           {'schema': 1, 'summary': 'native lifecycle proof', 'findings': []})
    result['final'] = reviewer.finalize_run(home, run['review_run_id'], 'exited')
print(json.dumps(result, separators=(',', ':')))
`;
  const result = Bun.spawnSync(['python3', '-c', script, home, String(cutoff), finalize ? '1' : '0'], {
    cwd: repo,
    env: { ...process.env, PYTHONPATH: `${join(repo, 'bin')}:${join(repo, 'tests')}` },
    stdout: 'pipe', stderr: 'pipe',
  });
  assert.equal(result.exitCode, 0, result.stderr.toString());
  return JSON.parse(result.stdout.toString()) as Record<string, unknown>;
}

function textFingerprint(content: unknown): string {
  const text = Array.isArray(content) ? content
    .filter((block): block is Record<string, unknown> =>
      typeof block === 'object' && block !== null && block.type === 'text' && typeof block.text === 'string')
    .map(block => block.text as string).join('') : typeof content === 'string' ? content : '';
  return createHash('sha256').update(text).digest('hex');
}

let generatedRoot: string | undefined;
function fixture(repo: string, role: 'worker' | 'central'): Fixture {
  const path = process.env.AGD_NATIVE_EPISODES_FIXTURE;
  if (path) {
    const value = JSON.parse(readFileSync(path, 'utf8')) as Fixture | Fixture[];
    return Array.isArray(value) ? value.find(row => row.role === role)! : value;
  }
  generatedRoot = mkdtempSync(join(tmpdir(), `agd-native-episodes-${role}-`));
  const setup = `
import json, os, sys, time, uuid
from pathlib import Path
from support import run
from agd_lib import tasks, store, supervision, observability, reviewer_runner
root=Path(sys.argv[1]); repo=Path(sys.argv[2]); owner=int(sys.argv[3]); role=sys.argv[4]; home=root/'distro'
def agd(*args): return json.loads(run([repo/'bin/agd','--home',home,*args]).stdout)
agd('init','--backend','tmux'); (home/'native-probe.txt').write_text('Native episode fixture.')
if role == 'worker':
 brief=home/'brief.md'; brief.write_text('Native episode fixture.')
 seed=root/'seed'; seed.mkdir(); run(['git','init','-b','main',seed]); run(['git','config','user.name','Native Probe'],cwd=seed); run(['git','config','user.email','probe@example.invalid'],cwd=seed)
 (seed/'value.txt').write_text('base\\n'); run(['git','add','.'],cwd=seed); run(['git','commit','-m','initial'],cwd=seed)
 run(['git','clone','--bare',seed,root/'origin.git']); agd('project','add','sample','--source',str(root/'origin.git'))
 task=agd('task','create','native','--project','sample','--brief',str(brief),'--kind','scout','--skill-mode','none')
 task.update(executor='worker',phase='running',incarnation=str(uuid.uuid4()),worker_alive=True)
 binding=observability.worker_binding(home,task['id'],task['incarnation']); task['observability']={'session_dir':binding['session_dir']}; tasks.save(home,task)
 runner=supervision.process_identity(owner); runner.pop('state',None); child=dict(runner); runner.update(incarnation=task['incarnation'],phase='spawned',child_pid=owner,child_start=runner['start'],child=child)
 store.atomic_json(home/'state/tasks'/task['id']/'runner.json',runner)
 observability.record_process_generation(home,'worker','1',task_id=task['id'],incarnation=task['incarnation'],pid=owner,start=runner['start'])
 session_dir=binding['session_dir']; extension='lib/omp-worker.ts'
else:
 task=None; session_dir=str(home/'data/observability/central/omp'); Path(session_dir).mkdir(parents=True,exist_ok=True); extension='.omp/extensions/agd-primary.ts'
 observability.record_process_generation(home,'central','1',pid=owner,start=supervision.process_start(owner))
capability=reviewer_runner.assert_runtime_capability(int(time.time()*1000)+20000)
print(json.dumps({'home':str(home),'task':task,'session_dir':session_dir,'role':role,'extension':extension,'capability':capability}))
`;
  const result = Bun.spawnSync(['python3', '-c', setup, generatedRoot, repo, String(process.pid), role], {
    cwd: repo, env: { ...process.env, PYTHONPATH: join(repo, 'bin') + ':' + join(repo, 'tests') },
    stdout: 'pipe', stderr: 'pipe',
  });
  assert.equal(result.exitCode, 0, result.stderr.toString());
  return JSON.parse(result.stdout.toString()) as Fixture;
}

async function nativeProbe(role: 'worker' | 'central') {
  const repo = resolve(import.meta.dir, '..');
  const data = fixture(repo, role);
  const isolated = mkdtempSync(join(tmpdir(), `agd-native-episodes-${role}-`));
  const agentDir = join(isolated, 'agent');
  mkdirSync(agentDir, { recursive: true });
  process.env.AGD_HOME = data.home;
  process.env.AGD_MANAGED_CENTRAL = role === 'central' ? '1' : '';
  process.env.OPENAI_API_KEY = 'provider-free-episode-probe';
  process.env.OMP_TELEMETRY_ENABLED = 'false';
  if (data.task) {
    process.env.AGD_TASK_ID = data.task.id;
    process.env.AGD_INCARNATION = data.task.incarnation;
  } else {
    delete process.env.AGD_TASK_ID;
    delete process.env.AGD_INCARNATION;
  }
  const cwd = data.task?.worktree ?? data.home;
  const sdk = await import(data.capability.sdk_url);
  const ai = await import(data.capability.ai_url);
  const { SessionManager } = await import(data.capability.manager_url);
  const { ModelRegistry } = await import(data.capability.registry_url);
  const runtime = await import(new URL('./modes/runtime-init.ts', data.capability.sdk_url).href);
  const settings = await sdk.Settings.init({ cwd, agentDir, readOnly: true, overrides: {
    'skills.includeSkills': [], 'compaction.enabled': false, 'compaction.midTurnEnabled': false,
    'compaction.autoContinue': false, 'compaction.asyncEnabled': false, 'retry.enabled': false,
    'retry.maxRetries': 0, 'retry.modelFallback': false, 'retry.usageAwareFallback': false,
    'advisor.enabled': false, 'autolearn.enabled': false, 'autolearn.autoContinue': false,
  }});
  const auth = await sdk.discoverAuthStorage(agentDir, { cwd, settings });
  const modelRegistry = new ModelRegistry(auth, undefined, { settings });
  const manager = SessionManager.create(cwd, data.session_dir ?? join(isolated, 'sessions'));
  const extensionModule = await import(pathToFileURL(join(repo, data.extension)).href);
  const extension = extensionModule.default ?? extensionModule;
  const inputs: Input[] = [];
  const oldEndSeen = Promise.withResolvers<void>();
  const releaseOldEnd = Promise.withResolvers<void>();
  let holdOldEnd = true;
  let injectGap = false;
  let injectAmbiguous = false;
  let gapInjected = false;
  let ambiguousInjected = false;
  const terminalWaiters: Array<{ stage: string; resolve: (messages: unknown[]) => void }> = [];
  const capturedTerminalEvents: Array<{ stage: string; messages: unknown[] }> = [];
  const runtimeErrors: unknown[] = [];
  const lifecycleReasons: string[] = [];
  const waitStarted = Promise.withResolvers<void>();
  const releaseWait = Promise.withResolvers<void>();
  const waitContinuationObserved = Promise.withResolvers<void>();
  let waitContinuationRequested = false;
  function awaitTerminal(stage: string): Promise<unknown[]> {
    const deferred = Promise.withResolvers<unknown[]>();
    terminalWaiters.push({ stage, resolve: deferred.resolve });
    return Promise.race([
      deferred.promise,
      new Promise<unknown[]>((_, reject) => setTimeout(() => reject(new Error(JSON.stringify({
        stage, home: data.home, session_file: manager.getSessionFile(), session_id: manager.getSessionId(),
        pending_terminal_waiters: terminalWaiters.map(waiter => waiter.stage), lifecycleReasons, runtimeErrors,
      }))), 10000)),
    ]);
  }
  function awaitWaitContinuation(): Promise<void> {
    return Promise.race([
      waitContinuationObserved.promise,
      new Promise<void>((_, reject) => setTimeout(() => reject(new Error(JSON.stringify({
        stage: 'wait-continuation', role: data.role, home: data.home, session_file: manager.getSessionFile(),
        lifecycleReasons, runtimeErrors,
      }))), 10000)),
    ]);
  }
  const extensionWithStaleGate = (api: { on: (name: string, handler: (event: { reason?: string; willContinue?: boolean; messages?: unknown[] }) => unknown) => void }) => {
    api.on('before_agent_start', event => { if (event.reason) lifecycleReasons.push(event.reason); });
    api.on('session_stop', () => {
      if (mode !== 'wait' || waitContinuationRequested) return;
      waitContinuationRequested = true;
      return { continue: true, additionalContext: 'Continue this native lifecycle probe before final completion.' };
    });
    api.on('agent_end', async event => {
      if (event.willContinue === true) {
        if (mode === 'wait') waitContinuationObserved.resolve();
        return;
      }
      if ((injectGap || injectAmbiguous) && event.messages) {
        const binding = [...event.messages].reverse().find(message =>
          typeof message === 'object' && message !== null &&
          (message as Record<string, unknown>).customType === 'agd:episode-binding') as Record<string, unknown> | undefined;
        const details = binding?.details;
        if (details && typeof details === 'object') {
          if (injectGap) {
            event.messages.push({ customType: 'agd:episode-gap', display: false, content: 'Injected native gap', details });
            gapInjected = true;
          } else {
            event.messages.push({ ...binding, details });
            ambiguousInjected = true;
          }
        }
        injectGap = false;
        injectAmbiguous = false;
      }
      if (!holdOldEnd) return;
      holdOldEnd = false;
      oldEndSeen.resolve();
      await releaseOldEnd.promise;
    });
    (extension as (api: unknown) => unknown)(api);
    api.on('agent_end', event => {
      if (event.willContinue === true) return;
      const messages = structuredClone(event.messages ?? []);
      const waiter = terminalWaiters.shift();
      capturedTerminalEvents.push({ stage: waiter?.stage ?? 'unmatched', messages });
      waiter?.resolve(messages);
    });
  };
  const { session } = await sdk.createAgentSession({
    cwd, agentDir, settings,
    model: modelRegistry.find('openai', 'gpt-4o-mini'), modelRegistry, sessionManager: manager,
    rules: [], contextFiles: [], noContextFiles: true, disableExtensionDiscovery: true,
    toolNames: ['read'], enableMCP: false, enableLsp: false, enableIrc: false,
    extensions: [extensionWithStaleGate],
  });
  let mode: 'final' | 'wait' | 'error' | 'abort' | 'hold' = 'final';
  let waitTurn = 0;
  const abortStarted = Promise.withResolvers<void>();
  const heldStarted = Promise.withResolvers<void>();
  const releaseHeld = Promise.withResolvers<void>();
  let heldProviderRelease: (() => void) | undefined;
  const heldFinalText = `Native held final ${randomUUID()}`;
  const heldPromptSentinel = `UNFINISHED_NATIVE_SENTINEL_${randomUUID()}`;
  session.agent.streamFn = (model: { api: string; provider: string; id: string }, context: Input, _options: { signal?: AbortSignal }) => {
    inputs.push(structuredClone({ systemPrompt: context.systemPrompt, messages: context.messages }));
    const stream = new ai.AssistantMessageEventStream();
    const waitInitial = mode === 'wait' && waitTurn++ === 0;
    const content = waitInitial ? [{ type: 'text', text: 'Intermediate native continuation step.' }]
      : mode === 'final' ? [{ type: 'text', text: 'Native final episode.' }] : [];
    const stopReason = mode === 'error' ? 'error' : mode === 'abort' ? 'aborted' : 'stop';
    const message = { role: 'assistant', content, api: model.api, provider: model.provider, model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason, timestamp: Date.now() };
    const fail = (reason: 'error' | 'aborted') => {
      const error = { ...message, stopReason: reason, errorMessage: 'Scripted provider failure.' };
      stream.push({ type: 'error', reason, error });
      stream.end(error);
    };
    if (mode === 'abort') {
      abortStarted.resolve();
      if (_options.signal?.aborted) queueMicrotask(() => fail('aborted'));
      else _options.signal?.addEventListener('abort', () => fail('aborted'), { once: true });
    } else if (mode === 'wait' && !waitInitial) {
      waitStarted.resolve();
      void (async () => { await releaseWait.promise; fail('error'); })();
    } else if (mode === 'error') {
      queueMicrotask(() => fail('error'));
    } else if (mode === 'hold') {
      heldStarted.resolve();
      heldProviderRelease = () => {
        message.content = [{ type: 'text', text: heldFinalText }];
        stream.push({ type: 'done', reason: message.stopReason, message });
        stream.end(message);
      };
      void releaseHeld.promise.then(() => heldProviderRelease?.());
    } else {
      queueMicrotask(() => { stream.push({ type: 'done', reason: message.stopReason, message }); stream.end(message); });
    }
    return stream;
  };
  try {
    await runtime.initializeExtensions(session, { reportSendError: (_action: string, error: Error) => { runtimeErrors.push(error); throw error; }, reportRuntimeError: (error: unknown) => { runtimeErrors.push(error); throw new Error(String(error)); } });
    const previousSessionFile = manager.getSessionFile();
    assert(previousSessionFile);
    const previousSessionId = manager.getSessionId();
    const firstTerminal = awaitTerminal('first');
    const firstPrompt = session.prompt('Produce a final response.');
    await oldEndSeen.promise;
    // Native JSONL persistence is not fenced by agent_end hook delivery; poll the captured old file before switching.
    await waitForNativeFinal(previousSessionFile);
    const switchedFile = await manager.newSession();
    assert(switchedFile);
    assert.notEqual(manager.getSessionFile(), previousSessionFile);
    releaseOldEnd.resolve();
    await firstPrompt;
    const firstMessages = await firstTerminal;
    await manager.flush();
    const sourceDir = join(data.home, 'state/reviewer/episodes/sources');
    const completionDir = join(data.home, 'state/reviewer/episodes/completions');
    assert(inputs.length > 0);
    assert(inputs.every(input => !JSON.stringify(input.messages).includes('agd:episode-binding')));
    const sourceReceipts = readdirSync(sourceDir).filter(name => name.endsWith('.json'))
      .map(name => JSON.parse(readFileSync(join(sourceDir, name), 'utf8')) as Record<string, unknown>);
    const oldReceipt = sourceReceipts.find(row => row.session_id === previousSessionId);
    assert(oldReceipt);
    assert.equal(typeof oldReceipt.source_id, 'string');
    assert.equal(typeof oldReceipt.path, 'string');
    const oldRows = nativeRows(join(data.home, oldReceipt.path as string));
    const oldMarker = oldRows.findLast(row => row.type === 'custom_message' &&
      row.customType === 'agd:episode-binding' && typeof row.details === 'object' && row.details !== null &&
      (row.details as Record<string, unknown>).source_id === oldReceipt.source_id) as Record<string, unknown> | undefined;
    assert(oldMarker);
    const capturedMarker = [...firstMessages].reverse().find(message =>
      typeof message === 'object' && message !== null &&
      (message as Record<string, unknown>).customType === 'agd:episode-binding' &&
      typeof (message as Record<string, unknown>).details === 'object' &&
      ((message as Record<string, unknown>).details as Record<string, unknown>).source_id === oldReceipt.source_id) as Record<string, unknown> | undefined;
    assert(capturedMarker);
    const markerDetails = capturedMarker.details as Record<string, unknown>;
    assert.equal(typeof markerDetails.interaction_id, 'string');
    assert.equal((oldMarker.details as Record<string, unknown>).interaction_id, markerDetails.interaction_id);
    const oldFinal = nativeFinal(oldRows);
    assert(oldFinal, JSON.stringify({
      home: data.home, sourcePath: oldReceipt.path,
      messages: oldRows.filter(row => row.type === 'message').map(row => row.message),
      rows: oldRows.map(row => ({ type: row.type, timestamp: row.timestamp, customType: row.customType })),
    }));
    const oldMessage = oldFinal.message as Record<string, unknown>;
    const capturedFinal = [...firstMessages].reverse().find(message => {
      if (typeof message !== 'object' || message === null) return false;
      const value = message as Record<string, unknown>;
      return value.role === 'assistant' && value.stopReason === 'stop' && typeof value.timestamp === 'number';
    }) as Record<string, unknown> | undefined;
    assert(capturedFinal);
    assert.equal(capturedFinal.timestamp, oldMessage.timestamp);
    assert.equal(textFingerprint(capturedFinal.content), textFingerprint(oldMessage.content));
    const completionPath = join(completionDir, oldReceipt.source_id as string, `${markerDetails.interaction_id}.json`);
    assert(existsSync(completionPath));
    const oldCompletion = JSON.parse(readFileSync(completionPath, 'utf8')) as Record<string, unknown>;
    assert.equal(oldCompletion.source_id, oldReceipt.source_id);
    assert.equal(oldCompletion.interaction_id, markerDetails.interaction_id);
    const finalRecord = oldCompletion.final as Record<string, unknown>;
    assert.equal(finalRecord.timestamp, oldMessage.timestamp);
    assert.equal(finalRecord.text_sha256, textFingerprint(oldMessage.content));
    const completionsBeforeSwitch = completionSnapshot(completionDir);
    mode = 'final';
    const switchedTerminal = awaitTerminal('switched');
    await session.prompt('Produce a final response after switching native sessions.');
    const switchedMessages = await switchedTerminal;
    assert(readdirSync(sourceDir).length >= sourceReceipts.length + 1);
    const currentSessionId = manager.getSessionId();
    const currentReceipts = readdirSync(sourceDir).filter(name => name.endsWith('.json'))
      .map(name => JSON.parse(readFileSync(join(sourceDir, name), 'utf8')) as Record<string, unknown>);
    const currentReceipt = currentReceipts.find(row => row.session_id === currentSessionId);
    assert(currentReceipt);
    const currentRows = nativeRows(join(data.home, currentReceipt.path as string));
    const currentMarker = currentRows.findLast(row => row.type === 'custom_message' &&
      row.customType === 'agd:episode-binding' && typeof row.details === 'object' && row.details !== null &&
      (row.details as Record<string, unknown>).source_id === currentReceipt.source_id) as Record<string, unknown> | undefined;
    assert(currentMarker);
    const capturedCurrentMarker = [...switchedMessages].reverse().find(message =>
      typeof message === 'object' && message !== null &&
      (message as Record<string, unknown>).customType === 'agd:episode-binding' &&
      typeof (message as Record<string, unknown>).details === 'object' &&
      ((message as Record<string, unknown>).details as Record<string, unknown>).source_id === currentReceipt.source_id) as Record<string, unknown> | undefined;
    assert(capturedCurrentMarker);
    const currentDetails = capturedCurrentMarker.details as Record<string, unknown>;
    assert.equal((currentMarker.details as Record<string, unknown>).interaction_id, currentDetails.interaction_id);
    const currentFinal = currentRows.findLast(row => {
      if (row.type !== 'message' || typeof row.message !== 'object' || row.message === null) return false;
      const message = row.message as Record<string, unknown>;
      return message.role === 'assistant' && message.stopReason === 'stop' && typeof message.timestamp === 'number';
    });
    assert(currentFinal);
    const currentMessage = currentFinal.message as Record<string, unknown>;
    const currentRecord = completionRecords(completionDir).find(row =>
      row.source_id === currentReceipt.source_id && row.interaction_id === currentDetails.interaction_id);
    assert(currentRecord);
    const currentFinalRecord = currentRecord.final as Record<string, unknown>;
    assert.equal(currentFinalRecord.timestamp, currentMessage.timestamp);
    assert.equal(currentFinalRecord.text_sha256, textFingerprint(currentMessage.content));
    const completionsAfterSwitch = completionSnapshot(completionDir);
    assert.notEqual(completionsAfterSwitch, completionsBeforeSwitch);
    injectGap = true;
    const gapTerminal = awaitTerminal('gap');
    await session.prompt('Exercise a lost current binding.');
    await gapTerminal;
    await manager.flush();
    const completionsAfterGap = completionSnapshot(completionDir);
    const gapRejected = gapInjected && completionsAfterGap === completionsAfterSwitch;
    assert(gapRejected);
    injectAmbiguous = true;
    const ambiguousTerminal = awaitTerminal('ambiguous');
    await session.prompt('Exercise ambiguous current metadata.');
    await ambiguousTerminal;
    await manager.flush();
    const completionsAfterAmbiguous = completionSnapshot(completionDir);
    const ambiguousRejected = ambiguousInjected && completionsAfterAmbiguous === completionsAfterSwitch;
    assert(ambiguousRejected);
    mode = 'wait';
    waitTurn = 0;
    waitContinuationRequested = false;
    const waitTerminal = awaitTerminal('wait');
    const waiting = session.prompt('Exercise an actual native continuation.');
    await awaitWaitContinuation();
    assert(waitContinuationRequested);
    await waitStarted.promise;
    const completionsWhileWaiting = completionSnapshot(completionDir);
    assert.equal(completionsWhileWaiting, completionsAfterSwitch);
    releaseWait.resolve();
    await waiting;
    await waitTerminal;
    await manager.flush();
    const completionsAfterWait = completionSnapshot(completionDir);
    assert.equal(completionsAfterWait, completionsAfterSwitch);
    mode = 'error';
    const errorTerminal = awaitTerminal('error');
    await session.prompt('Exercise a provider error.');
    await errorTerminal;
    await manager.flush();
    const completionsAfterError = completionSnapshot(completionDir);
    assert.equal(completionsAfterError, completionsAfterSwitch);
    if (data.task) {
      const reportPath = join(data.home, 'data/tasks', data.task.id, 'native-episode-report.md');
      writeFileSync(reportPath, 'Verified value.txt remains base and the workspace is unchanged.\n');
      const readyResult = Bun.spawnSync([join(repo, 'bin/agd'), '--home', data.home, 'task', 'ready', data.task.id, '--report', reportPath], {
        cwd: repo, env: { ...process.env, PYTHONPATH: join(repo, 'bin') + ':' + join(repo, 'tests') },
        stdout: 'pipe', stderr: 'pipe',
      });
      assert.equal(readyResult.exitCode, 0, readyResult.stderr.toString());
    }
    mode = 'abort';
    const completionsBeforeAbort = completionSnapshot(completionDir);
    const abortTerminal = awaitTerminal('abort');
    const interrupted = session.prompt('Interrupt this native turn.');
    await abortStarted.promise;
    await session.abort();
    await interrupted;
    await abortTerminal;
    const completionsAfterAbort = completionSnapshot(completionDir);
    assert.equal(completionsAfterAbort, completionsBeforeAbort);
    const abortRows = nativeRows(join(data.home, currentReceipt.path as string));
    const abortMarker = abortRows.findLast(row => row.type === 'custom_message' &&
      row.customType === 'agd:episode-binding' && typeof row.details === 'object' && row.details !== null &&
      (row.details as Record<string, unknown>).source_id === currentReceipt.source_id) as Record<string, unknown> | undefined;
    assert(abortMarker);
    const abortDetails = abortMarker.details as Record<string, unknown>;
    assert.equal(typeof abortDetails.interaction_id, 'string');
    const recoveryMessages = session.agent.state.messages;
    const cutoff = recoveryMessages.at(-1) as Record<string, unknown> | undefined;
    assert(cutoff);
    assert.equal(cutoff.role, 'assistant');
    assert.equal(cutoff.stopReason, 'aborted');
    assert(Array.isArray(cutoff.content));
    assert(!(cutoff.content as unknown[]).some(block =>
      typeof block === 'object' && block !== null &&
      ((block as Record<string, unknown>).type === 'text' || (block as Record<string, unknown>).type === 'toolCall')));
    session.agent.replaceMessages(recoveryMessages.slice(0, -1));
    mode = 'final';
    type ExtensionRunnerSession = typeof session & {
      extensionRunner?: { emitBeforeAgentStart: (prompt: string, images: undefined, systemPrompt: string[], reason: 'revive') => Promise<unknown> };
    };
    const nativeSession = session as ExtensionRunnerSession;
    const extensionRunner = nativeSession.extensionRunner;
    assert(extensionRunner);
    const revivedTerminal = awaitTerminal('revive');
    await extensionRunner.emitBeforeAgentStart('', undefined, session.baseSystemPrompt, 'revive');
    await session.agent.continue();
    const resumedMessages = await revivedTerminal;
    await manager.flush();
    const resumedRows = nativeRows(join(data.home, currentReceipt.path as string));
    const resumedMarker = resumedRows.findLast(row => row.type === 'custom_message' &&
      row.customType === 'agd:episode-binding' && typeof row.details === 'object' && row.details !== null &&
      (row.details as Record<string, unknown>).source_id === currentReceipt.source_id) as Record<string, unknown> | undefined;
    assert(resumedMarker);
    const capturedResumedMarker = [...resumedMessages].reverse().find(message =>
      typeof message === 'object' && message !== null &&
      (message as Record<string, unknown>).customType === 'agd:episode-binding' &&
      typeof (message as Record<string, unknown>).details === 'object' &&
      ((message as Record<string, unknown>).details as Record<string, unknown>).source_id === currentReceipt.source_id) as Record<string, unknown> | undefined;
    assert(capturedResumedMarker);
    const resumedDetails = capturedResumedMarker.details as Record<string, unknown>;
    assert.equal((resumedMarker.details as Record<string, unknown>).interaction_id, resumedDetails.interaction_id);
    assert.equal(resumedDetails.interaction_id, abortDetails.interaction_id, JSON.stringify({
      lifecycleReasons,
      bindingIds: resumedRows.filter(row => row.type === 'custom_message' && row.customType === 'agd:episode-binding' &&
        typeof row.details === 'object' && row.details !== null &&
        (row.details as Record<string, unknown>).source_id === currentReceipt.source_id)
        .map(row => (row.details as Record<string, unknown>).interaction_id),
      capturedTerminalEvents,
    }));
    const resumedRecord = completionRecords(completionDir).find(row =>
      row.source_id === currentReceipt.source_id && row.interaction_id === resumedDetails.interaction_id);
    assert(resumedRecord);
    const resumedFinal = resumedRows.findLast(row => {
      if (row.type !== 'message' || typeof row.message !== 'object' || row.message === null) return false;
      const message = row.message as Record<string, unknown>;
      return message.role === 'assistant' && message.stopReason === 'stop' && typeof message.timestamp === 'number';
    });
    assert(resumedFinal);
    const resumedMessage = resumedFinal.message as Record<string, unknown>;
    const resumedFinalRecord = resumedRecord.final as Record<string, unknown>;
    const resumedMatches = resumedFinalRecord.timestamp === resumedMessage.timestamp &&
      resumedFinalRecord.text_sha256 === textFingerprint(resumedMessage.content);
    assert(resumedMatches);
    const heldRowsPath = join(data.home, currentReceipt.path as string);
    const heldCompletionCount = completionRecords(completionDir).length;
    mode = 'hold';
    const heldTerminal = awaitTerminal('held');
    const heldPrompt = session.prompt(`Start held native turn ${heldPromptSentinel}`);
    await heldStarted.promise;
    await waitForNativePredicate(heldRowsPath, rows =>
      rows.some(row => row.type === 'message' && JSON.stringify(row.message).includes(heldPromptSentinel)) &&
      rows.some(row => row.type === 'custom_message' && row.customType === 'agd:episode-binding'));
    await manager.flush();
    const producerAlive = (() => {
      try { process.kill(process.pid, 0); return true; } catch { return false; }
    })();
    assert(producerAlive);
    const firstBridge = reviewerBridge(repo, data.home, Date.now() / 1000 + 5, true);
    const firstPacket = firstBridge.packet as Record<string, unknown>;
    const firstExcerpts = (Array.isArray(firstPacket.excerpts) ? firstPacket.excerpts : []) as Array<Record<string, unknown>>;
    const firstSources = (Array.isArray(firstPacket.sources) ? firstPacket.sources : []) as Array<Record<string, unknown>>;
    const firstEpisodes = (Array.isArray(firstPacket.episodes) ? firstPacket.episodes : []) as Array<Record<string, unknown>>;
    assert(firstExcerpts.length > 0);
    const firstSentinelExcluded = !JSON.stringify(firstExcerpts).includes(heldPromptSentinel);
    assert(firstSentinelExcluded);
    const firstSource = firstSources.find(source =>
      source.session_id === currentReceipt.session_id && source.role === data.role);
    const firstSourceScoped = firstSource !== undefined;
    assert(firstSourceScoped);
    assert.equal(firstSource.role, data.role);
    assert(firstExcerpts.some(row => row.source_id === firstSource.source_id));
    const episodeRoleScoped = data.role === 'worker'
      ? firstEpisodes.some(episode => episode.role === 'worker' &&
          episode.task_id === data.task?.id && episode.source_id === firstSource.source_id)
      : firstEpisodes.some(episode => episode.role === 'central' &&
          episode.source_id === firstSource.source_id && (!('task_id' in episode) || episode.task_id == null));
    assert(episodeRoleScoped);
    if (data.role === 'worker') assert.equal(firstSource.task_id, data.task?.id);
    else assert(!('task_id' in firstSource) || firstSource.task_id == null);
    assert(firstExcerpts.every(row => typeof row.material_id === 'string'));
    const firstMaterialIds = new Set(firstExcerpts.map(row => row.material_id as string));
    assert(firstMaterialIds.size === firstExcerpts.length);
    const firstFinalized = typeof firstBridge.final === 'object' && firstBridge.final !== null;
    assert(firstFinalized);
    releaseHeld.resolve();
    await heldPrompt;
    await heldTerminal;
    await manager.flush();
    await waitForNativePredicate(heldRowsPath, rows =>
      rows.some(row => row.type === 'message' && JSON.stringify(row.message).includes(heldPromptSentinel)) &&
      rows.some(row => row.type === 'message' && JSON.stringify(row.message).includes(heldFinalText)));
    const heldRowsAfterRelease = nativeRows(heldRowsPath);
    const heldUserPersisted = heldRowsAfterRelease.some(row =>
      row.type === 'message' && JSON.stringify(row.message).includes(heldPromptSentinel));
    const heldFinalPersisted = heldRowsAfterRelease.some(row =>
      row.type === 'message' && JSON.stringify(row.message).includes(heldFinalText));
    assert(heldUserPersisted);
    assert(heldFinalPersisted);
    assert(completionRecords(completionDir).length > heldCompletionCount);
    const secondBridge = reviewerBridge(repo, data.home, Date.now() / 1000 + 5, true);
    const secondPacket = secondBridge.packet as Record<string, unknown>;
    const secondExcerpts = (Array.isArray(secondPacket.excerpts) ? secondPacket.excerpts : []) as Array<Record<string, unknown>>;
    assert(secondExcerpts.every(row => typeof row.material_id === 'string'));
    const secondMaterialIds = new Set(secondExcerpts.map(row => row.material_id as string));
    const incrementalMaterialDisjoint = secondMaterialIds.size > 0 &&
      secondMaterialIds.size === secondExcerpts.length &&
      [...secondMaterialIds].every(id => !firstMaterialIds.has(id));
    const secondFinalized = typeof secondBridge.final === 'object' && secondBridge.final !== null;
    assert(incrementalMaterialDisjoint);
    assert(secondFinalized);
    assert(JSON.stringify(secondExcerpts).includes(heldFinalText));
    const thirdBridge = reviewerBridge(repo, data.home, Date.now() / 1000 + 5, false);
    const thirdPacket = thirdBridge.packet as Record<string, unknown>;
    const freshPacketEmpty = Array.isArray(thirdPacket.excerpts) && thirdPacket.excerpts.length === 0;
    assert(freshPacketEmpty);
    mode = 'final';
    return { role: data.role, sourceCount: readdirSync(sourceDir).length, completionCount: completionRecords(completionDir).length,
      oldSourceId: oldReceipt.source_id, oldInteractionId: markerDetails.interaction_id,
      markerHidden: inputs.every(input => !JSON.stringify(input.messages).includes('agd:episode-binding') && !JSON.stringify(input.messages).includes('agd:episode-gap')),
      gapsRejected: gapRejected,
      ambiguousRejected,
      continuationObserved: waitContinuationRequested,
      resumedMatches,
      waitsExcluded: completionsAfterWait === completionsAfterSwitch,
      errorsExcluded: completionsAfterError === completionsAfterSwitch,
      reviveObserved: lifecycleReasons.includes('revive'),
      switchedSession: manager.getSessionId() !== previousSessionId,
      producerAlive, firstSourceScoped, episodeRoleScoped, firstSentinelExcluded, firstFinalized,
      heldUserPersisted, heldFinalPersisted, incrementalMaterialDisjoint, secondFinalized, freshPacketEmpty };
  } finally {
    releaseHeld.resolve();
    session.beginDispose(); await session.dispose(); await manager.close();
    rmSync(isolated, { recursive: true, force: true });
    if (generatedRoot) rmSync(generatedRoot, { recursive: true, force: true });
  }
}

if (process.env.AGD_NATIVE_EPISODES_CHECK === '1') {
  const worker = await nativeProbe('worker');
  const central = await nativeProbe('central');
  console.log(JSON.stringify({ worker, central }));
} else {
  test('native episode capture keeps markers out of provider context and records only finals', async () => {
    const child = Bun.spawn([process.execPath, '--no-env-file', import.meta.path], {
      env: { ...process.env, AGD_NATIVE_EPISODES_CHECK: '1' }, stdout: 'pipe', stderr: 'pipe',
      signal: AbortSignal.timeout(180000),
    });
    const [stdout, stderr, exit] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    expect(exit, stderr).toBe(0);
    const evidence = JSON.parse(stdout);
    for (const result of [evidence.worker, evidence.central]) {
      expect(result.markerHidden).toBe(true);
      expect(result.gapsRejected).toBe(true);
      expect(result.ambiguousRejected).toBe(true);
      expect(result.continuationObserved).toBe(true);
      expect(result.reviveObserved).toBe(true);
      expect(result.resumedMatches).toBe(true);
      expect(result.waitsExcluded).toBe(true);
      expect(result.errorsExcluded).toBe(true);
      expect(result.producerAlive).toBe(true);
      expect(result.firstSourceScoped).toBe(true);
      expect(result.episodeRoleScoped).toBe(true);
      expect(result.firstSentinelExcluded).toBe(true);
      expect(result.firstFinalized).toBe(true);
      expect(result.heldUserPersisted).toBe(true);
      expect(result.heldFinalPersisted).toBe(true);
      expect(result.incrementalMaterialDisjoint).toBe(true);
      expect(result.secondFinalized).toBe(true);
      expect(result.freshPacketEmpty).toBe(true);
      expect(result.oldSourceId).toMatch(/^[0-9a-f]{64}$/);
      expect(result.oldInteractionId).toMatch(/^[0-9a-f-]{36}$/);
    }
  }, 190000);
}
