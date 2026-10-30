import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import workerExtension, {
  assistantReply,
  type WorkerSpawn,
} from '../lib/omp-worker';

let spawnImpl: WorkerSpawn;

const originalSetInterval = globalThis.setInterval;
const originalClearInterval = globalThis.clearInterval;
let intervalHandlers: (() => void)[];
let clearedTimers: unknown[];
let home: string;
let corePath: string;
const coreText = '---\nname: agd-worker\ndescription: Test protocol.\n---\nProtocol safety marker.\n';
const originalEnv = { ...process.env };

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), 'agd-worker-hooks-'));
  corePath = join(home, 'work/.omp/skills/agd-worker/SKILL.md');
  mkdirSync(resolve(corePath, '..'), { recursive: true });
  mkdirSync(join(home, 'data/tasks/retained'), { recursive: true });
  writeFileSync(corePath, coreText);
  process.env.AGD_HOME = home;
  process.env.AGD_TASK_ID = 'retained';
  process.env.AGD_INCARNATION = 'worker-one';
  delete process.env.AGD_WORKER_RESUME_READY;
  intervalHandlers = [];
  clearedTimers = [];
  globalThis.setInterval = ((handler: () => void) => {
    intervalHandlers.push(handler);
    return intervalHandlers.length as unknown as NodeJS.Timeout;
  }) as typeof setInterval;
  globalThis.clearInterval = ((timer: unknown) => {
    clearedTimers.push(timer);
  }) as typeof clearInterval;
});

afterEach(() => {
  globalThis.setInterval = originalSetInterval;
  globalThis.clearInterval = originalClearInterval;
  rmSync(home, { recursive: true, force: true });
  for (const key of ['AGD_HOME', 'AGD_TASK_ID', 'AGD_INCARNATION', 'AGD_WORKER_RESUME_READY']) {
    if (originalEnv[key] === undefined) delete process.env[key];
    else process.env[key] = originalEnv[key];
  }
});

type Message = {
  schema: number;
  id: string;
  body: string;
  incarnation: string;
  sequence: number;
  queued_at: number;
};

function extensionHarness(messages: Message[], completionStatuses: number[] = []) {
  const pending = [...messages];
  const completions: { args: string[]; input: unknown }[] = [];
  const runtime = {
    schema: 1, task_id: 'retained', incarnation: 'worker-one', phase: 'running',
    kind: 'scout', worktree: join(home, 'work'),
    brief: join(home, 'data/tasks/retained/brief.md'),
    report: join(home, 'data/tasks/retained/report-draft.md'),
    agd_argv: [resolve(import.meta.dir, '../bin/agd'), '--home', home],
    pr_grant: null, task_skills: ['optional-probe'], system_skills: ['agd-worker'],
    protocol: { path: corePath, sha256: createHash('sha256').update(coreText).digest('hex') },
    outcome: 'unfinished',
  };
  const failures = { context: false, event: false };
  const events: { kind: string; key: string; message: string }[] = [];
  const aborts: number[] = [];
  const hookContext = { abort: () => { aborts.push(1); } };
  spawnImpl = (_command, args, options) => {
    const operation = args[3];
    if (operation === 'context') {
      return { status: failures.context ? 3 : 0, stdout: JSON.stringify(runtime), stderr: 'context unavailable' };
    }
    if (operation === 'event') {
      const kind = args[args.indexOf('--kind') + 1];
      const key = args[args.indexOf('--key') + 1];
      events.push({ kind, key, message: kind === 'blocked'
        ? readFileSync(args[args.indexOf('--message') + 1], 'utf8') : '' });
      return { status: failures.event ? 3 : 0, stdout: '', stderr: 'event unavailable' };
    }
    if (operation === 'inbox') {
      return { status: 0, stdout: JSON.stringify(pending), stderr: '' };
    }
    if (operation === 'complete') {
      completions.push({ args: [...args], input: options.input });
      const status = completionStatuses.shift() ?? 0;
      if (status === 0) pending.shift();
      return { status, stdout: '', stderr: status ? 'temporary failure' : '' };
    }
    return { status: 0, stdout: '', stderr: '' };
  };
  const handlers: Record<string, (event: Record<string, unknown>) => unknown> = {};
  const deliveries: { text: string; options?: { deliverAs?: string } }[] = [];
  workerExtension({
    on: (name, handler) => {
      handlers[name] = event => handler(event, hookContext);
    },
    sendUserMessage: (text: string, options?: { deliverAs?: string }) => {
      deliveries.push({ text, options });
    },
  }, spawnImpl);
  return { handlers, deliveries, completions, runtime, failures, events, aborts };
}

function message(id: string, sequence: number, body: string): Message {
  return {
    schema: 1,
    id,
    body,
    incarnation: 'worker-one',
    sequence,
    queued_at: sequence,
  };
}

test('assistantReply returns only the final assistant text blocks', () => {
  expect(assistantReply([
    { role: 'assistant', content: 'superseded' },
    { role: 'toolResult', content: [{ type: 'text', text: 'tool output' }] },
    { role: 'assistant', content: [
      { type: 'thinking', thinking: 'private' },
      { type: 'text', text: 'Final ' },
      { type: 'toolCall', name: 'read' },
      { type: 'text', text: 'answer.' },
    ] },
  ])).toBe('Final answer.');
  expect(assistantReply([
    { role: 'assistant', content: [{ type: 'thinking', thinking: 'only' }] },
  ])).toBeUndefined();
});

test('ready relaunch consumes its first retained request without an initial task turn', async () => {
  const id = '55555555-5555-4555-8555-555555555555';
  process.env.AGD_WORKER_RESUME_READY = '1';
  const { handlers, deliveries } = await extensionHarness([
    message(id, 1, 'Continue the retained conversation.'),
  ]);

  handlers.session_start({});

  expect(deliveries).toEqual([
    {
      text: 'Continue the retained conversation.',
      options: { deliverAs: 'followUp' },
    },
  ]);
});

test('retained worker completes one terminal follow-up before delivering the next', async () => {
  const first = '11111111-1111-4111-8111-111111111111';
  const second = '22222222-2222-4222-8222-222222222222';
  const { handlers, deliveries, completions } = await extensionHarness([
    message(first, 1, 'First follow-up.'),
    message(second, 2, 'Second follow-up.'),
  ]);

  handlers.session_start({});
  expect(deliveries).toEqual([]);
  handlers.agent_end({ willContinue: false, messages: [] });
  expect(deliveries).toEqual([
    { text: 'First follow-up.', options: { deliverAs: 'followUp' } },
  ]);

  handlers.agent_start({});
  expect(handlers.session_stop({ stop_hook_active: false })).toBeUndefined();
  handlers.agent_end({
    willContinue: true,
    messages: [{ role: 'assistant', content: 'not terminal' }],
  });
  expect(completions).toEqual([]);

  handlers.agent_end({
    willContinue: false,
    messages: [{ role: 'assistant', content: [
      { type: 'text', text: 'Completed ' },
      { type: 'thinking', thinking: 'hidden' },
      { type: 'text', text: 'first.' },
    ] }],
  });
  expect(completions).toHaveLength(1);
  expect(completions[0].args).toContain(first);
  expect(completions[0].args.slice(-2)).toEqual(['--status', 'done']);
  expect(completions[0].input).toBe('Completed first.');
  expect(deliveries.at(-1)).toEqual(
    { text: 'Second follow-up.', options: { deliverAs: 'followUp' } },
  );
});

test('failed persistence retries the captured response without another model turn', async () => {
  const id = '33333333-3333-4333-8333-333333333333';
  const { handlers, deliveries, completions } = await extensionHarness(
    [message(id, 1, 'Retry me.')], [1, 0]);

  handlers.session_start({});
  handlers.agent_end({ willContinue: false, messages: [] });
  handlers.agent_start({});
  handlers.agent_end({
    willContinue: false,
    messages: [{ role: 'assistant', content: 'Durable response.' }],
  });
  expect(completions).toHaveLength(1);
  expect(deliveries).toHaveLength(1);

  intervalHandlers[0]();
  expect(completions).toHaveLength(2);
  expect(completions[1].input).toBe('Durable response.');
  expect(deliveries).toHaveLength(1);
});

test('terminal turn without assistant text records failure and shutdown clears polling', async () => {
  const id = '44444444-4444-4444-8444-444444444444';
  const { handlers, completions } = await extensionHarness([
    message(id, 1, 'Answer without silence.'),
  ]);

  handlers.session_start({});
  handlers.agent_end({ willContinue: false, messages: [] });
  handlers.agent_start({});
  handlers.agent_end({
    willContinue: false,
    messages: [{ role: 'assistant', content: [{ type: 'thinking', thinking: 'none' }] }],
  });
  expect(completions[0].args.slice(-2)).toEqual(['--status', 'failed']);
  expect(completions[0].input).toBe('steering turn ended without assistant text');

  handlers.session_shutdown({});
  expect(clearedTimers).toEqual([1]);
});

test('native policy and fresh context preserve unrelated input without duplication', () => {
  const { handlers, runtime } = extensionHarness([]);
  const first = handlers.before_agent_start({ systemPrompt: ['Native policy.'] }) as { systemPrompt: string[] };
  expect(first.systemPrompt[0]).toBe('Native policy.');
  expect(first.systemPrompt.filter(part => part.includes('Protocol safety marker.'))).toHaveLength(1);
  const second = handlers.before_agent_start(first) as { systemPrompt: string[] };
  expect(second.systemPrompt).toEqual(first.systemPrompt);
  const history = [{ role: 'user', content: 'Keep me.' },
    { role: 'custom', customType: 'agd:worker-context', content: 'obsolete' }];
  const injected = handlers.context({ messages: history }) as { messages: Array<Record<string, unknown>> };
  expect(history[1].content).toBe('obsolete');
  expect(injected.messages[0]).toEqual(history[0]);
  expect(injected.messages.filter(row => row.customType === 'agd:worker-context')).toHaveLength(1);
  runtime.phase = 'ready'; runtime.outcome = 'ready';
  const refreshed = handlers.context(injected) as { messages: Array<Record<string, unknown>> };
  const state = refreshed.messages.find(row => row.customType === 'agd:worker-context')!;
  expect(state.display).toBe(false);
  expect(JSON.parse(state.content as string).phase).toBe('ready');
  expect(state.content).not.toContain('optional-probe');
  writeFileSync(corePath, 'tampered');
  handlers.session_switch({});
  expect(handlers.before_agent_start(first)).toBeUndefined();
});

test('unfinished assignment gets one correction and one durable diagnostic', () => {
  const { handlers, events, runtime } = extensionHarness([]);
  handlers.agent_start({});
  expect(handlers.session_stop({ stop_hook_active: false })).toMatchObject({ continue: true });
  handlers.agent_end({ willContinue: true });
  handlers.agent_start({});
  expect(handlers.session_stop({ stop_hook_active: true })).toBeUndefined();
  expect(handlers.session_stop({ stop_hook_active: false })).toBeUndefined();
  expect(events.filter(row => row.kind === 'idle')).toEqual([]);
  expect(events.filter(row => row.key === 'agd-contract-incomplete')).toHaveLength(1);
  expect(runtime.phase).toBe('running');
});

test.each([
  ['ready', 'ready'], ['landed', 'closed'], ['cleaned', 'closed'],
  ['running', 'blocked'], ['running', 'decision'],
])('no assignment correction in phase %s outcome %s', (phase, outcome) => {
  const { handlers, runtime, events } = extensionHarness([]);
  runtime.phase = phase; runtime.outcome = outcome;
  handlers.agent_start({});
  expect(handlers.session_stop({ stop_hook_active: false })).toBeUndefined();
  expect(events.filter(row => row.kind === 'blocked')).toEqual([]);
});

test('interruption provider errors and shutdown never request correction', () => {
  const { handlers, events } = extensionHarness([]);
  const controller = new AbortController(); controller.abort();
  expect(handlers.session_stop({ signal: controller.signal })).toBeUndefined();
  for (const stopReason of ['error', 'aborted']) {
    expect(handlers.session_stop({ last_assistant_message: { role: 'assistant', stopReason } })).toBeUndefined();
  }
  handlers.session_shutdown({});
  expect(handlers.session_stop({ stop_hook_active: false })).toBeUndefined();
  expect(events).toEqual([]);
});

test('unavailable context aborts preparation and does not start a corrective loop', () => {
  const { handlers, failures, aborts, events } = extensionHarness([]);
  failures.context = true;
  expect(handlers.before_agent_start({ systemPrompt: ['Native policy.'] })).toBeUndefined();
  expect(aborts).toEqual([1]);
  expect(handlers.session_stop({ stop_hook_active: false })).toBeUndefined();
  expect(events.filter(row => row.key === 'agd-contract-incomplete')).toHaveLength(1);
});

test('diagnostic persistence failure is visible without another corrective turn', () => {
  const { handlers, failures, runtime } = extensionHarness([]);
  const errors = spyOn(console, 'error').mockImplementation(() => {});
  try {
    failures.event = true;
    handlers.agent_start({});
    expect(handlers.session_stop({ stop_hook_active: false })).toMatchObject({ continue: true });
    expect(handlers.session_stop({ stop_hook_active: true })).toBeUndefined();
    expect(handlers.session_stop({ stop_hook_active: false })).toBeUndefined();
    expect(errors.mock.calls.flat().join('\n')).toContain('event unavailable');
    expect(runtime.phase).toBe('running');
  } finally { errors.mockRestore(); }
});

test('provider failure cannot publish a partial follow-up as successful', () => {
  const { handlers, completions } = extensionHarness([
    message('77777777-7777-4777-8777-777777777777', 1, 'Verify the result.'),
  ]);
  handlers.session_start({});
  handlers.agent_end({ messages: [] });
  handlers.agent_start({});
  handlers.agent_end({ messages: [{ role: 'assistant', content: 'Partial response.', stopReason: 'error' }] });
  expect(completions[0].args.slice(-2)).toEqual(['--status', 'failed']);
  expect(completions[0].input).toBe('Partial response.');
});
