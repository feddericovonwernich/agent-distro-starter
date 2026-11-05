import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, expect } from 'bun:test';
import { centralWarning, launcherOwnerToken, stopDecision } from '../.omp/extensions/agd-primary';

test('active delegated work cannot end without monitoring', () => {
  const broken = { delegatedWork: true, watcherHealthy: false };
  expect(stopDecision(false, broken)?.continue).toBe(true);
  expect(stopDecision(true, broken)).toBeUndefined();
  expect(stopDecision(false, { ...broken, watcherHealthy: true })).toBeUndefined();
  expect(stopDecision(false, { ...broken, delegatedWork: false })).toBeUndefined();
});

test('bare OMP central warning distinguishes managed launch', () => {
  expect(centralWarning(false)).toContain('unmanaged');
  expect(centralWarning(true)).toBeUndefined();
});

test('launcher endpoint token becomes the central owner token', () => {
  expect(launcherOwnerToken('endpoint-token', 'random-owner')).toBe('endpoint-token');
  expect(launcherOwnerToken(undefined, 'random-owner')).toBe('random-owner');
});

test('session switch replaces only its owned watcher', async () => {
  const home = mkdtempSync(join(tmpdir(), 'agd-primary-'));
  const previous = process.env.AGD_HOME;
  const previousOwner = process.env.AGD_OWNER;
  delete process.env.AGD_OWNER;
  process.env.AGD_HOME = home;
  // Exercise a second module instance with its own home; the static import above binds the default home.
  const { default: primary } = await import('../.omp/extensions/agd-primary.ts?switch-test');
  const hooks: Record<string, (event: { stop_hook_active?: boolean }, context: {
    isIdle: () => boolean; ui: { setStatus: (key: string, text: string | undefined) => void;
      notify: (message: string, type?: string) => void };
  }) => unknown> = {};
  const deliveries: { text: string; options?: { deliverAs?: string } }[] = [];
  const statuses: { key: string; text: string | undefined }[] = [];
  const notifications: string[] = [];
  primary({ on: (name, handler) => { hooks[name] = handler; }, sendUserMessage: (text, options) => { deliveries.push({ text, options }); } });
  // Real watcher subprocess and filesystem notifications cannot use a fake clock.
  async function owner(generation: string) {
    const path = join(home, 'state', 'owner.json');
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      try {
        const record = JSON.parse(readFileSync(path, 'utf8'));
        if (record.generation === generation) return record;
      } catch { /* watcher has not published yet */ }
      await Bun.sleep(50);
    }
    throw new Error(`watcher generation ${generation} never became ready`);
  }
  function checkOwnerCommands() {
    const cli = join(import.meta.dir, '../bin/agd');
    const args = ['-c', 'exec "$1" --home "$2" session reconcile', 'owner-check', cli, home];
    // OMP refreshes its shell environment from process.env after lifecycle hooks.
    const owned = spawnSync('/bin/bash', args, { encoding: 'utf8', env: { ...process.env } });
    expect(owned.stderr).toBe('');
    expect(owned.status).toBe(0);
    const foreignEnv = { ...process.env };
    delete foreignEnv.AGD_OWNER;
    const foreign = spawnSync('/bin/bash', args, { encoding: 'utf8', env: foreignEnv });
    expect(foreign.status).toBe(3);
    expect(foreign.stderr).toContain('another live central owns this home');
  }
  let idle = true;
  const context = { isIdle: () => idle, ui: {
    setStatus: (key: string, text: string | undefined) => { statuses.push({ key, text }); },
    notify: (message: string) => { notifications.push(message); },
  } };
  try {
    const initialized = spawnSync(join(import.meta.dir, '../bin/agd'), ['--home', home, 'init', '--backend', 'tmux'], {
      encoding: 'utf8', env: { ...process.env },
    });
    expect(initialized.status, initialized.stderr).toBe(0);
    await hooks.session_start({}, context);
    const first = await owner('1');
    checkOwnerCommands();
    const routine = hooks.before_agent_start({}, context) as {
      message: { customType: string; content: string; display: boolean };
    };
    expect(routine.message).toMatchObject({ customType: 'agd:supervision', display: false });
    expect(routine.message.content).toContain(`Home: ${home}`);
    expect(statuses.at(-1)).toEqual({ key: 'agd', text: 'Distro · watching · 0 pending' });
    expect(notifications).toEqual([]);
    await hooks.session_switch({}, context);
    const second = await owner('2');
    expect(second.owner_pid).toBe(first.owner_pid);
    expect(second.watcher_pid).not.toBe(first.watcher_pid);
    expect(second.token).toBe(first.token);
    checkOwnerCommands();
    mkdirSync(join(home, 'state', 'events'), { recursive: true });
    writeFileSync(join(home, 'state', 'events', '11111111-1111-4111-8111-111111111111.json'),
      JSON.stringify({ id: '11111111-1111-4111-8111-111111111111', task: 'scout', kind: 'ready' }));
    const deadline = Date.now() + 5000;
    while (!deliveries.length && Date.now() < deadline) await Bun.sleep(50);
    expect(deliveries[0].options).toBeUndefined();
    hooks.before_agent_start({}, context);
    expect(statuses.at(-1)).toEqual({ key: 'agd', text: 'Distro · watching · 1 pending' });
    idle = false;
    writeFileSync(join(home, 'state', 'events', '22222222-2222-4222-8222-222222222222.json'),
      JSON.stringify({ id: '22222222-2222-4222-8222-222222222222', task: 'scout', kind: 'blocked' }));
    const nextDeadline = Date.now() + 5000;
    while (deliveries.length < 2 && Date.now() < nextDeadline) await Bun.sleep(50);
    expect(deliveries[1].options).toEqual({ deliverAs: 'followUp' });
    writeFileSync(join(home, 'state', 'events', '33333333-3333-4333-8333-333333333333.json'),
      JSON.stringify({ schema: 1, id: '33333333-3333-4333-8333-333333333333', kind: 'background-skill-review', key: '00000000-0000-4000-8000-000000000030', observed_at: Date.now(), details: { purpose: 'background-skill-review', review_run_id: '00000000-0000-4000-8000-000000000030', report_ids: [], proposal_ids: [], report_count: 0, proposal_count: 0 } }));
    const reviewerDeadline = Date.now() + 5000;
    while (deliveries.length < 3 && Date.now() < reviewerDeadline) await Bun.sleep(50);
    expect(deliveries[2].options).toEqual({ deliverAs: 'followUp' });
    // Publish a real schema-2 non-skill proposal and a retained schema-1 event.
    const script = String.raw`
import json, sys, uuid
from pathlib import Path
from agd_lib import reviewer
from test_reviewer import completed_session
from test_reviewer_inspection import stage, report_v2_inputs, report_v2_finding, report_v2, target
from reviewer_history_fixture import historical_final
home = Path(sys.argv[1])
old, _ = historical_final(home)
old_event = reviewer._receipt(home, old['review_run_id'])['event_id']
hook = home / 'config' / 'hooks' / 'primary-delivery-tool'
hook.parent.mkdir(parents=True, exist_ok=True)
hook.write_text('original tool bytes\n')
native, _, _ = completed_session(home, texts=('A tooling check was missed.',))
run_id, manifest = stage(home)
row, _, citation = report_v2_inputs(home, run_id, manifest, native)
dest = target(home, run_id, scope='distro', path='config/hooks/primary-delivery-tool')
submitted = reviewer.submit_report(home, run_id, str(uuid.uuid4()),
    report_v2(row, [report_v2_finding(row, dest['target_ref'], citation, 'distro')]))
final = reviewer.finalize_run(home, run_id, 'exited')
print(json.dumps({'old_event': old_event, 'event': final['event_id'],
                  'proposal': submitted['proposal_ids'][0], 'manifest': manifest['manifest_id']}))
`;
    const root = join(import.meta.dir, '..');
    const seeded = spawnSync('python3', ['-c', script, home], {
      encoding: 'utf8', env: { ...process.env, PYTHONPATH: `${join(root, 'bin')}:${join(root, 'tests')}` },
    });
    expect(seeded.status, seeded.stderr).toBe(0);
    const generated = JSON.parse(seeded.stdout) as {
      old_event: string; event: string; proposal: string; manifest: string;
    };
    // The durable watcher runs in another process; fake timers cannot drive its notification pipe.
    const generatedDeadline = Date.now() + 10000;
    while (Date.now() < generatedDeadline &&
           ![generated.old_event, generated.event].every(id => deliveries.some(row => row.text.includes(id)))) {
      await Bun.sleep(50);
    }
    for (const eventId of [generated.old_event, generated.event]) {
      const delivery = deliveries.find(row => row.text.includes(eventId));
      expect(delivery?.options).toEqual({ deliverAs: 'followUp' });
    }
    const pendingContext = hooks.before_agent_start({}, context) as {
      message: { content: string };
    };
    expect(pendingContext.message.content).toContain(generated.old_event);
    expect(pendingContext.message.content).toContain(generated.event);
    const cli = join(root, 'bin', 'agd');
    const invoke = (...args: string[]) => {
      const result = spawnSync(cli, ['--home', home, ...args], { encoding: 'utf8', env: { ...process.env } });
      expect(result.status, result.stderr).toBe(0);
      return JSON.parse(result.stdout);
    };
    const listed = invoke('events', 'list') as Array<{ id: string; kind: string; details: {
      purpose: string; manifest_id?: string; targets?: Array<{ identity: { kind: string; locator: string } }>;
    } }>;
    const legacy = listed.find(row => row.id === generated.old_event);
    const general = listed.find(row => row.id === generated.event);
    expect(legacy?.kind).toBe('background-skill-review');
    expect(general?.kind).toBe('background-review');
    expect(general?.details.purpose).toBe('background-review');
    expect(general?.details.manifest_id).toBe(generated.manifest);
    expect(general?.details.targets?.[0].identity).toMatchObject({
      kind: 'file', locator: 'config/hooks/primary-delivery-tool',
    });
    const hook = join(home, 'config', 'hooks', 'primary-delivery-tool');
    const before = readFileSync(hook, 'utf8');
    const disposition = invoke('reviewer', 'proposals', 'dispose', generated.proposal, '--status', 'accepted');
    expect(disposition.current).toBe('accepted');
    expect(readFileSync(hook, 'utf8')).toBe(before);
    invoke('events', 'acknowledge', generated.event);
    const after = invoke('events', 'list') as Array<{ id: string }>;
    expect(after.some(row => row.id === generated.old_event)).toBe(true);
    expect(after.some(row => row.id === generated.event)).toBe(false);
    // The watcher is a real subprocess; wait for its persisted replacement PID before stopping it.
    process.kill(second.watcher_pid, 'SIGTERM');
    let replacement: number | undefined;
    const restartDeadline = Date.now() + 5000;
    while (!replacement && Date.now() < restartDeadline) {
      const record = JSON.parse(readFileSync(join(home, 'state', 'owner.json'), 'utf8'));
      if (record.watcher_pid !== second.watcher_pid) replacement = record.watcher_pid;
      else await Bun.sleep(50);
    }
    expect(replacement).toBeDefined();
    process.kill(replacement!, 'SIGTERM');
    const failureDeadline = Date.now() + 5000;
    while (!notifications.length && Date.now() < failureDeadline) await Bun.sleep(50);
    expect(notifications).toEqual(['Supervision watcher stopped; delegated work is not monitored.']);
    expect(statuses.at(-1)).toEqual({ key: 'agd', text: 'Distro · watcher unavailable' });
  } finally {
    await hooks.session_shutdown({}, context);
    if (previousOwner === undefined) delete process.env.AGD_OWNER;
    else process.env.AGD_OWNER = previousOwner;
    if (previous === undefined) delete process.env.AGD_HOME;
    else process.env.AGD_HOME = previous;
    rmSync(home, { recursive: true, force: true });
  }
}, 30000);
