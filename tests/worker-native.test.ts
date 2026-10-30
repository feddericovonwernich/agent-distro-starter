import { expect, test } from 'bun:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, watch, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';


async function nativeProbe() {
  const repo = resolve(import.meta.dir, '..');
  const fixturePath = process.env.AGD_NATIVE_FIXTURE;
  const root = process.env.AGD_NATIVE_ROOT ?? mkdtempSync(join(tmpdir(), 'agd-native-protocol-'));
  const isolated = join(root, 'isolated'); mkdirSync(isolated, { recursive: true });
  for (const key of ['HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME']) process.env[key] = isolated;
  if (!fixturePath) for (const key of ['AGD_HOME', 'AGD_TASK_ID', 'AGD_INCARNATION', 'AGD_OWNER', 'AGD_WORKER_RESUME_READY']) delete process.env[key];
  process.env.OPENAI_API_KEY = 'provider-free-probe';
  process.env.OMP_TELEMETRY_ENABLED = 'false';
  process.env.PYTHONPATH = join(repo, 'bin') + ':' + join(repo, 'tests');
  function run(argv: string[], input?: string) {
    const result = Bun.spawnSync(argv, { cwd: repo, env: process.env, timeout: 30000, stdin: input ? Buffer.from(input) : undefined, stdout: 'pipe', stderr: 'pipe' });
    assert.equal(result.exitCode, 0, result.stderr.toString());
    return result.stdout.toString();
  }
  const setup = `
import json, os, sys, time, uuid
from pathlib import Path
from support import run, write_native_plugins
from agd_lib import harnesses, reviewer_runner, store, tasks, supervision, worker_skills
root=Path(sys.argv[1]); repo=Path(sys.argv[2]); home=root/'distro'; seed=root/'seed'
run(['git','init','-b','main',seed]); run(['git','config','user.name','Native Probe'],cwd=seed); run(['git','config','user.email','probe@example.invalid'],cwd=seed)
(seed/'value.txt').write_text('base\\n'); run(['git','add','.'],cwd=seed); run(['git','commit','-m','initial'],cwd=seed)
run(['git','clone','--bare',seed,root/'origin.git'])
env=dict(os.environ,TREEHOUSE_ROOT=str(home/'data/treehouse'))
def agd(*args): return json.loads(run([repo/'bin/agd','--home',home,*args],env=env).stdout)
agd('init','--backend','tmux'); agd('project','add','sample','--source',str(root/'origin.git')); write_native_plugins(home)
brief=home/'brief.md'; brief.write_text('Inspect value.txt and report the unchanged value. Do not modify the workspace.')
task=agd('task','create','native','--project','sample','--brief',str(brief),'--kind','scout','--skill-mode','none')
curated=agd('task','create','curated','--project','sample','--brief',str(brief),'--kind','scout','--skill-mode','curated','--skill','systematic-debugging')
granted=agd('task','create','granted','--project','sample','--brief',str(brief),'--skill-mode','none')
instruction=home/'grant.md'; instruction.write_text('Task granted may publish origin:agd/granted.')
grant=tasks.grant_pr_publish(home,'granted','origin:agd/granted',instruction)
with store.lock(home):
 manifest=worker_skills.add_scoped_pr(home,granted,grant['id'])
 granted.update(worker_skills=manifest,worker_skill_selection=manifest['selection'],pr_grant=grant['id']); tasks.save(home,granted)
variants=[dict(task=row,argv=harnesses.launch_argv(row,home/'data/tasks'/row['id']/'brief.md')) for row in (task,curated,granted)]
task.update(executor='worker',phase='running',incarnation=str(uuid.uuid4()),worker_alive=False); tasks.save(home,task)
owner=int(sys.argv[3]); start=supervision.process_start(owner)
store.atomic_json(home/'state/owner.json',dict(owner_pid=owner,owner_start=start,watcher_pid=owner,watcher_start=start,heartbeat=time.time(),token='native-central'))
print(json.dumps(dict(home=str(home),task=task,variants=variants,capability=reviewer_runner.assert_runtime_capability(int(time.time()*1000)+20000))))
`;
  try {
    const fixture = fixturePath ? JSON.parse(readFileSync(fixturePath, 'utf8')) :
      JSON.parse(run(['python3', '-c', setup, root, repo, String(process.pid)]));
    const { home, task, capability } = fixture;
    process.env.AGD_HOME = home;
    process.env.AGD_TASK_ID = task.id;
    process.env.AGD_INCARNATION = task.incarnation;
    process.env.AGD_CLI = join(repo, 'bin/agd');
    if (!fixturePath) {
      process.env.AGD_NATIVE_FIXTURE = join(root, 'fixture.json');
      process.env.AGD_NATIVE_ROOT = root;
      writeFileSync(process.env.AGD_NATIVE_FIXTURE, JSON.stringify(fixture));
      const evidence = JSON.parse(run([process.execPath, '--no-env-file', import.meta.path]));
      const restarted = JSON.parse(run(['python3', '-c', `import json,os,uuid\nfrom pathlib import Path\nfrom agd_lib import tasks,workers,store,harnesses\nhome=Path(os.environ['AGD_HOME'])\nwith store.lock(home):\n task=tasks.read(home,'native'); assert task['phase']=='ready'\n task['incarnation']=str(uuid.uuid4()); tasks.save(home,task)\n request=json.loads(workers.enqueue_locked(home,task,'Read-only request after ready restart.').read_text())\nprint(json.dumps(dict(task=task,request=request['id'],argv=harnesses.launch_argv(task,home/'data/tasks/native/brief.md'))))`]));
      assert.notEqual(restarted.task.incarnation, task.incarnation);
      assert.equal(restarted.task.lease_id, task.lease_id);
      fixture.task = restarted.task;
      fixture.restart_request = restarted.request;
      fixture.restart_argv = restarted.argv;
      process.env.AGD_INCARNATION = restarted.task.incarnation;
      process.env.AGD_WORKER_RESUME_READY = '1';
      writeFileSync(process.env.AGD_NATIVE_FIXTURE, JSON.stringify(fixture));
      evidence.readyRestart = JSON.parse(run([process.execPath, '--no-env-file', import.meta.path])).readyRestart;
      console.log(JSON.stringify(evidence));
    } else {
    const workerExtension = (await import(pathToFileURL(join(repo, 'lib/omp-worker.ts')).href)).default;
    const sdk = await import(capability.sdk_url);
    const ai = await import(capability.ai_url);
    const { SessionManager } = await import(capability.manager_url);
    const { ModelRegistry } = await import(capability.registry_url);
    const runtime = await import(new URL('./modes/runtime-init.ts', capability.sdk_url).href);
    const agentDir = join(isolated, 'agent'); mkdirSync(agentDir, { recursive: true });
    for (const variant of fixture.variants) {
      const includeSkills = variant.argv[variant.argv.indexOf('--skills') + 1].split(',');
      const discovered = await sdk.discoverSkills(variant.task.worktree, agentDir, { includeSkills });
      assert.deepEqual(discovered.skills.map((skill: { name: string }) => skill.name).sort(), includeSkills);
      for (const skill of discovered.skills) {
        assert.equal(skill.filePath, join(variant.task.worktree, '.omp/skills', skill.name, 'SKILL.md'));
      }
      assert.equal(includeSkills.includes('agd-scoped-pr-relay'), variant.task.id === 'granted');
    }
    const settings = await sdk.Settings.init({ cwd: task.worktree, agentDir, readOnly: true, overrides: {
      'skills.includeSkills': ['agd-worker'],
      'compaction.enabled': false, 'compaction.midTurnEnabled': false, 'compaction.autoContinue': false,
      'compaction.asyncEnabled': false, 'compaction.idleEnabled': false,
      'retry.enabled': false, 'retry.maxRetries': 0, 'retry.modelFallback': false,
      'retry.usageAwareFallback': false, 'advisor.enabled': false,
      'autolearn.enabled': false, 'autolearn.autoContinue': false,
    }});
    const auth = await sdk.discoverAuthStorage(agentDir, { cwd: task.worktree, settings });
    const modelRegistry = new ModelRegistry(auth, undefined, { settings });
    const manager = SessionManager.create(task.worktree, join(root, 'sessions'));
    const events: Array<[string, boolean?]> = [];
    const inputs: Array<{ systemPrompt: string[]; messages: Array<Record<string, unknown>> }> = [];
    let settled = Promise.withResolvers<void>();
    const abortStarted = Promise.withResolvers<void>();
    const repliesSettled = Promise.withResolvers<void>();
    const replyVisibility = Promise.withResolvers<void>();
    let pendingReplies: string[] = fixture.restart_request ? [fixture.restart_request] : [];
    const replies = join(home, 'data/tasks/native/replies'); mkdirSync(replies, { recursive: true });
    // Real inbox polling crosses processes; observe filesystem and native terminal events.
    const replyWatcher = watch(replies, () => {
      if (pendingReplies.length && pendingReplies.every(id => existsSync(join(replies, id + '.json')))) replyVisibility.resolve();
    });
    type ProbeAPI = { on: (name: string, handler: (event: { messages?: Array<Record<string, unknown>>; stop_hook_active?: boolean; willContinue?: boolean; preparation: { firstKeptEntryId: string; tokensBefore: number } }) => unknown) => void };
    const { session } = await sdk.createAgentSession({ cwd: task.worktree, agentDir, settings,
      model: modelRegistry.find('openai', 'gpt-4o-mini'), modelRegistry, sessionManager: manager,
      rules: [], contextFiles: [], noContextFiles: true, disableExtensionDiscovery: true,
      toolNames: ['read'], enableMCP: false, enableLsp: false, enableIrc: false,
      skipPythonPreflight: true, spawns: '', autoTitle: false,
      systemPrompt: ['NATIVE-POLICY'], extensions: [workerExtension, (api: ProbeAPI) => {
        api.on('session_stop', event => { events.push(['stop', event.stop_hook_active]); });
        api.on('agent_end', event => {
          events.push(['end', !!event.willContinue]);
          if (!event.willContinue) {
            settled.resolve();
            if (pendingReplies.length && pendingReplies.every(id => existsSync(join(replies, id + '.json')))) repliesSettled.resolve();
          }
        });
        api.on('session_before_compact', event => ({ compaction: {
          summary: 'The native worker inspected its protocol and retained assignment.',
          firstKeptEntryId: event.preparation.firstKeptEntryId, tokensBefore: event.preparation.tokensBefore,
        } }));
        api.on('session_compact', () => { events.push(['compact']); });
      }],
    });
    async function prompt(text: string) {
      settled = Promise.withResolvers<void>();
      await session.prompt(text);
      await settled.promise;
    }
    let mode: 'guard' | 'ready' | 'reply' | 'abort' | 'error' = fixture.restart_request ? 'reply' : 'guard';
    let modeTurn = 0;
    const cli = join(repo, 'bin/agd');
    session.agent.streamFn = (model: { api: string; provider: string; id: string }, context: { systemPrompt: string[]; messages: Array<Record<string, unknown>> }, options: { signal?: AbortSignal }) => {
      inputs.push(structuredClone({ systemPrompt: context.systemPrompt, messages: context.messages }));
      const stream = new ai.AssistantMessageEventStream();
      const tool = (mode === 'guard' || mode === 'ready') && modeTurn++ === 0;
      if (mode === 'ready' && tool) {
        const report = join(home, 'data/tasks/native/report-draft.md'); writeFileSync(report, 'Verified value.txt remains base and the workspace is unchanged.');
        run([cli, '--home', home, 'task', 'ready', task.id, '--report', report]);
      }
      const content = tool ? [{ type: 'toolCall', id: crypto.randomUUID(), name: 'read', arguments: { path: 'skill://agd-worker/references/commands.md' } }]
        : [{ type: 'text', text: mode === 'reply' ? 'Verified retained reply.' : 'Native probe answer.' }];
      const message = { role: 'assistant', content, api: model.api, provider: model.provider, model: model.id,
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: tool ? 'toolUse' : 'stop', timestamp: Date.now() };
      const fail = (reason: 'error' | 'aborted') => { const error = { ...message, stopReason: reason, errorMessage: 'Scripted provider failure.' }; stream.push({ type: 'error', reason, error }); stream.end(error); };
      if (mode === 'abort') {
        if (options.signal?.aborted) fail('aborted'); else options.signal?.addEventListener('abort', () => fail('aborted'), { once: true });
        abortStarted.resolve();
      } else if (mode === 'error') queueMicrotask(() => fail('error'));
      else queueMicrotask(() => { stream.push({ type: 'done', reason: message.stopReason, message }); stream.end(message); });
      return stream;
    };
    try {
      await runtime.initializeExtensions(session, { reportSendError: (_action: string, error: Error) => { throw error; }, reportRuntimeError: (error: unknown) => { throw new Error(JSON.stringify(error)); } });
      assert.deepEqual(session.skills.map((skill: { name: string }) => skill.name), ['agd-worker']);
      assert.equal(session.skills[0].filePath, join(task.worktree, '.omp/skills/agd-worker/SKILL.md'));
      if (fixture.restart_request) {
        assert.equal(fixture.restart_argv.at(-1), join(repo, 'lib/omp-worker.ts'));
        await Promise.all([replyVisibility.promise, repliesSettled.promise]);
        assert.equal(inputs.length, 1);
        assert(JSON.stringify(inputs[0].messages).includes('Read-only request after ready restart.'));
        const reply = JSON.parse(readFileSync(join(replies, fixture.restart_request + '.json'), 'utf8'));
        assert.equal(reply.completed_incarnation, task.incarnation);
        assert.equal(reply.body, 'Verified retained reply.');
        console.log(JSON.stringify({ readyRestart: true }));
      } else {
      await prompt('Read the assigned task brief and complete the assignment in its workspace. ' + 'probe '.repeat(40000));
      assert.deepEqual(events.filter(row => row[0] === 'stop'), [['stop', false], ['stop', true]]);
      assert.equal(inputs.length, 3);
      assert(inputs.every(input => input.systemPrompt.includes('NATIVE-POLICY')));
      assert(inputs.every(input => input.systemPrompt.filter(part => part.startsWith('[agd:worker-protocol]')).length === 1));
      assert(JSON.stringify(inputs[1].messages).includes('# AGD worker commands'));
      const observations = join(home, 'data/tasks/native/observations');
      const guards = () => readdirSync(observations).map(file => JSON.parse(readFileSync(join(observations, file), 'utf8'))).filter(row => row.key === 'agd-contract-incomplete');
      assert.equal(guards().length, 1);
      const blockedEvents = JSON.parse(run(['python3', '-c',
        `import json,os\nfrom pathlib import Path\nfrom agd_lib import supervision\nprint(json.dumps(supervision.reconcile(Path(os.environ['AGD_HOME']))['pending']))`]));
      assert(blockedEvents.some((event: { kind: string; key: string; incarnation: string }) =>
        event.kind === 'blocked' && event.key === guards()[0].id && event.incarnation === task.incarnation));
      mode = 'error'; const beforeError = inputs.length;
      await prompt('Exercise a provider failure.');
      assert.equal(inputs.length, beforeError + 1);
      mode = 'abort'; const beforeAbort = events.filter(row => row[0] === 'stop').length;
      const interrupted = session.prompt('Interrupt this operation.');
      await abortStarted.promise; await session.abort(); await interrupted;
      assert.equal(events.filter(row => row[0] === 'stop').length, beforeAbort);
      mode = 'ready'; modeTurn = 0; const beforeReady = inputs.length;
      await prompt('Verify the unchanged assignment.');
      assert.equal(inputs.length, beforeReady + 2);
      assert(JSON.stringify(inputs.at(-1)!.messages).includes('\\"phase\\":\\"ready\\"'));
      await session.compact(); assert(events.some(row => row[0] === 'compact'));
      mode = 'reply'; await prompt('Read-only follow-up after compaction.');
      assert.equal(inputs.at(-1)!.systemPrompt.filter(part => part.startsWith('[agd:worker-protocol]')).length, 1);
      const queued = JSON.parse(run(['python3', '-c', `import json, os\nfrom pathlib import Path\nfrom agd_lib import tasks,workers,store\nhome=Path(os.environ['AGD_HOME'])\nwith store.lock(home):\n task=tasks.read(home,'native')\n rows=[json.loads(workers.enqueue_locked(home,task,body).read_text()) for body in ('First read-only follow-up.','Second read-only follow-up.')]\nprint(json.dumps(rows))`]));
      pendingReplies = queued.map((request: { id: string }) => request.id);
      await Promise.all([replyVisibility.promise, repliesSettled.promise]);
      for (const request of queued) {
        const reply = JSON.parse(readFileSync(join(replies, request.id + '.json'), 'utf8'));
        assert.equal(reply.body, 'Verified retained reply.'); assert.equal(reply.status, 'done'); assert.equal(reply.completed_incarnation, task.incarnation);
      }
      assert.equal(guards().length, 1);
      const replyEvents = JSON.parse(run(['python3', '-c',
        `import json,os\nfrom pathlib import Path\nfrom agd_lib import supervision\nprint(json.dumps(supervision.reconcile(Path(os.environ['AGD_HOME']))['pending']))`]));
      for (const request of queued) assert(replyEvents.some((event: { kind: string; key: string; details: string }) =>
        event.kind === 'done' && event.key === request.id && event.details === 'Verified retained reply.'));
      for (const input of inputs) assert.equal(JSON.stringify(input.messages).split('next_action').length - 1, 1);
      const corePath = join(task.worktree, '.omp/skills/agd-worker/SKILL.md');
      const original = readFileSync(corePath); writeFileSync(corePath, 'corrupt');
      const beforeCorruption = inputs.length;
      await session.prompt('Must not execute without the protocol.');
      assert.equal(inputs.length, beforeCorruption);
      writeFileSync(corePath, original);
      console.log(JSON.stringify({ native: capability.omp_version, requests: inputs.length,
        unfinishedGuardCount: 1, centralBlockedDiagnostic: true, protocolFailureDiagnosticCount: guards().length - 1,
        nativeSkillVariants: fixture.variants.length, referenceRead: true, compactionRetainedCore: true,
        sameIncarnationReplies: queued.length, corruptCoreAbortedBeforeProvider: true }));
      }
    } finally {
      replyWatcher.close();
      await session.extensionRunner.emit({ type: 'session_shutdown' });
      session.beginDispose(); await session.dispose(); await manager.close();
    }
    }
  } finally { if (!fixturePath) rmSync(root, { recursive: true, force: true }); }
}

if (process.env.AGD_NATIVE_CHECK === '1') {
  await nativeProbe();
} else {
  test('native worker discovery, policy, correction and retained replies', async () => {
    const child = Bun.spawn([process.execPath, '--no-env-file', import.meta.path], {
      env: { ...process.env, AGD_NATIVE_CHECK: '1' }, stdout: 'pipe', stderr: 'pipe',
      signal: AbortSignal.timeout(95000),
    });
    const [stdout, stderr, exit] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    expect(exit, stderr).toBe(0);
    const evidence = JSON.parse(stdout);
    expect(evidence.sameIncarnationReplies).toBe(2);
    expect(evidence.corruptCoreAbortedBeforeProvider).toBe(true);
    expect(evidence.readyRestart).toBe(true);
    expect(evidence.nativeSkillVariants).toBe(3);
    expect(evidence.unfinishedGuardCount).toBe(1);
    expect(evidence.centralBlockedDiagnostic).toBe(true);
  }, 100000);
}
