import { expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
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
agd('init','--backend','tmux')
if role == 'worker':
 brief=home/'brief.md'; brief.write_text('Native episode fixture.')
 seed=root/'seed'; seed.mkdir(); run(['git','init','-b','main',seed]); run(['git','config','user.name','Native Probe'],cwd=seed); run(['git','config','user.email','probe@example.invalid'],cwd=seed)
 (seed/'value.txt').write_text('base\\n'); run(['git','add','.'],cwd=seed); run(['git','commit','-m','initial'],cwd=seed)
 run(['git','clone','--bare',seed,root/'origin.git']); agd('project','add','sample','--source',str(root/'origin.git'))
 task=agd('task','create','native','--project','sample','--brief',str(brief),'--kind','scout','--skill-mode','none')
 task.update(executor='worker',phase='running',incarnation=str(uuid.uuid4()),worker_alive=True)
 binding=observability.worker_binding(home,task['id'],task['incarnation']); task['observability']={'session_dir':binding['session_dir']}; tasks.save(home,task)
 runner=supervision.process_identity(owner); runner.pop('state',None); runner.update(incarnation=task['incarnation'],phase='spawned',child_pid=owner,child_start=runner['start'],child=runner)
 store.atomic_json(home/'state/tasks'/task['id']/'runner.json',runner)
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
  const extensionWithStaleGate = (api: { on: (name: string, handler: (event: { willContinue?: boolean; messages?: unknown[] }) => unknown) => void }) => {
    api.on('agent_end', async event => {
      if (event.willContinue === true) return;
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
  };
  const { session } = await sdk.createAgentSession({
    cwd, agentDir, settings,
    model: modelRegistry.find('openai', 'gpt-4o-mini'), modelRegistry, sessionManager: manager,
    rules: [], contextFiles: [], noContextFiles: true, disableExtensionDiscovery: true,
    toolNames: [], enableMCP: false, enableLsp: false, enableIrc: false,
    skipPythonPreflight: true, spawns: '', autoTitle: false, systemPrompt: ['NATIVE-EPISODE-POLICY'],
    extensions: [extensionWithStaleGate],
  });
  let mode: 'final' | 'wait' | 'error' | 'abort' = 'final';
  const abortStarted = Promise.withResolvers<void>();
  session.agent.streamFn = (model: { api: string; provider: string; id: string }, context: Input, _options: { signal?: AbortSignal }) => {
    inputs.push(structuredClone({ systemPrompt: context.systemPrompt, messages: context.messages }));
    const stream = new ai.AssistantMessageEventStream();
    const message = { role: 'assistant', content: mode === 'final' ? [{ type: 'text', text: 'Native final episode.' }] : [],
      api: model.api, provider: model.provider, model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: mode === 'error' ? 'error' : mode === 'abort' ? 'aborted' : 'stop', timestamp: Date.now() };
    if (mode === 'abort') {
      abortStarted.resolve();
      const aborted = () => { stream.push({ type: 'error', reason: 'aborted', error: message }); stream.end(message); };
      if (_options.signal?.aborted) queueMicrotask(aborted);
      else _options.signal?.addEventListener('abort', aborted, { once: true });
    } else {
      queueMicrotask(() => { stream.push({ type: 'done', reason: message.stopReason, message }); stream.end(message); });
    }
  };
  try {
    await runtime.initializeExtensions(session, { reportSendError: (_action: string, error: Error) => { throw error; }, reportRuntimeError: (error: unknown) => { throw new Error(String(error)); } });
    const previousSessionFile = manager.getSessionFile();
    assert(previousSessionFile);
    const previousSessionId = manager.getSessionId();
    const firstPrompt = session.prompt('Produce a final response.');
    await oldEndSeen.promise;
    assert.equal(await session.newSession(), true);
    assert.notEqual(manager.getSessionFile(), previousSessionFile);
    releaseOldEnd.resolve();
    await firstPrompt;
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
    const oldMarker = oldRows.find(row => row.type === 'custom_message' &&
      row.customType === 'agd:episode-binding' && typeof row.details === 'object' && row.details !== null &&
      (row.details as Record<string, unknown>).source_id === oldReceipt.source_id) as Record<string, unknown> | undefined;
    assert(oldMarker);
    const markerDetails = oldMarker.details as Record<string, unknown>;
    assert.equal(typeof markerDetails.interaction_id, 'string');
    const oldFinal = oldRows.find(row => {
      if (row.type !== 'message' || typeof row.message !== 'object' || row.message === null) return false;
      const message = row.message as Record<string, unknown>;
      return message.role === 'assistant' && message.stopReason === 'stop' && typeof message.timestamp === 'number';
    });
    assert(oldFinal);
    const oldMessage = oldFinal.message as Record<string, unknown>;
    const completionPath = join(completionDir, oldReceipt.source_id as string, `${markerDetails.interaction_id}.json`);
    assert(existsSync(completionPath));
    const oldCompletion = JSON.parse(readFileSync(completionPath, 'utf8')) as Record<string, unknown>;
    assert.equal(oldCompletion.source_id, oldReceipt.source_id);
    assert.equal(oldCompletion.interaction_id, markerDetails.interaction_id);
    const finalRecord = oldCompletion.final as Record<string, unknown>;
    assert.equal(finalRecord.timestamp, oldMessage.timestamp);
    assert.equal(finalRecord.text_sha256, textFingerprint(oldMessage.content));
    mode = 'final';
    await session.prompt('Produce a final response after switching native sessions.');
    await manager.flush();
    assert(readdirSync(sourceDir).length >= sourceReceipts.length + 1);
    const completionsAfterSwitch = readdirSync(completionDir);
    injectGap = true;
    await session.prompt('Exercise a lost current binding.');
    await manager.flush();
    const completionsAfterGap = readdirSync(completionDir);
    const gapRejected = gapInjected && completionsAfterGap.length === completionsAfterSwitch.length;
    assert(gapRejected);
    injectAmbiguous = true;
    await session.prompt('Exercise ambiguous current metadata.');
    await manager.flush();
    const completionsAfterAmbiguous = readdirSync(completionDir);
    const ambiguousRejected = ambiguousInjected && completionsAfterAmbiguous.length === completionsAfterSwitch.length;
    assert(ambiguousRejected);
    await session.prompt('Wait for delegated work.');
    await manager.flush();
    const completionsAfterWait = readdirSync(completionDir);
    assert.equal(completionsAfterWait.length, completionsAfterSwitch.length);
    mode = 'error';
    await session.prompt('Exercise a provider error.');
    await manager.flush();
    const completionsAfterError = readdirSync(completionDir);
    assert.equal(completionsAfterError.length, completionsAfterSwitch.length);
    mode = 'abort';
    const completionCountBeforeAbort = readdirSync(completionDir).length;
    const interrupted = session.prompt('Interrupt this native turn.');
    await abortStarted.promise;
    await session.abort();
    await interrupted;
    await manager.flush();
    assert.equal(readdirSync(completionDir).length, completionCountBeforeAbort);
    mode = 'final';
    await session.resume();
    await manager.flush();
    assert(readdirSync(completionDir).length > completionCountBeforeAbort);
    return { role: data.role, sourceCount: readdirSync(sourceDir).length, completionCount: readdirSync(completionDir).length,
      oldSourceId: oldReceipt.source_id, oldInteractionId: markerDetails.interaction_id,
      markerHidden: inputs.every(input => !JSON.stringify(input.messages).includes('agd:episode-binding') && !JSON.stringify(input.messages).includes('agd:episode-gap')),
      gapsRejected: gapRejected,
      ambiguousRejected,
      waitsExcluded: completionsAfterWait.length === completionsAfterSwitch.length,
      errorsExcluded: completionsAfterError.length === completionsAfterSwitch.length,
      switchedSession: manager.getSessionId() !== previousSessionId };
  } finally {
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
      expect(result.waitsExcluded).toBe(true);
      expect(result.errorsExcluded).toBe(true);
      expect(result.oldSourceId).toMatch(/^[0-9a-f]{64}$/);
      expect(result.oldInteractionId).toMatch(/^[0-9a-f-]{36}$/);
    }
  }, 190000);
}
