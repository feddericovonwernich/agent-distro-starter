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
  const deliveries: ({ deliverAs?: string } | undefined)[] = [];
  const statuses: { key: string; text: string | undefined }[] = [];
  const notifications: string[] = [];
  primary({ on: (name, handler) => { hooks[name] = handler; }, sendUserMessage: (_text, options) => { deliveries.push(options); } });
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
    expect(deliveries).toEqual([undefined]);
    hooks.before_agent_start({}, context);
    expect(statuses.at(-1)).toEqual({ key: 'agd', text: 'Distro · watching · 1 pending' });
    idle = false;
    writeFileSync(join(home, 'state', 'events', '22222222-2222-4222-8222-222222222222.json'),
      JSON.stringify({ id: '22222222-2222-4222-8222-222222222222', task: 'scout', kind: 'blocked' }));
    const nextDeadline = Date.now() + 5000;
    while (deliveries.length < 2 && Date.now() < nextDeadline) await Bun.sleep(50);
    expect(deliveries[1]).toEqual({ deliverAs: 'followUp' });
    writeFileSync(join(home, 'state', 'events', '33333333-3333-4333-8333-333333333333.json'),
      JSON.stringify({ schema: 1, id: '33333333-3333-4333-8333-333333333333', kind: 'background-skill-review', key: '00000000-0000-4000-8000-000000000030', observed_at: Date.now(), details: { purpose: 'background-skill-review', review_run_id: '00000000-0000-4000-8000-000000000030', report_ids: [], proposal_ids: [], report_count: 0, proposal_count: 0 } }));
    const reviewerDeadline = Date.now() + 5000;
    while (deliveries.length < 3 && Date.now() < reviewerDeadline) await Bun.sleep(50);
    expect(deliveries[2]).toEqual({ deliverAs: 'followUp' });
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
}, 15000);
