import { afterEach, beforeEach, expect, test } from 'bun:test';
import workerExtension, {
  assistantReply,
  type WorkerSpawn,
} from '../lib/omp-worker';

let spawnImpl: WorkerSpawn;

const originalSetInterval = globalThis.setInterval;
const originalClearInterval = globalThis.clearInterval;
let intervalHandlers: (() => void)[];
let clearedTimers: unknown[];

beforeEach(() => {
  process.env.AGD_HOME = '/tmp/agd-worker-home';
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
  delete process.env.AGD_WORKER_RESUME_READY;
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
  spawnImpl = (_command, args, options) => {
    const operation = args[3];
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
  const handlers: Record<string, (event: Record<string, unknown>) => void> = {};
  const deliveries: { text: string; options?: { deliverAs?: string } }[] = [];
  workerExtension({
    on: (name: string, handler: (event: Record<string, unknown>) => void) => {
      handlers[name] = handler;
    },
    sendUserMessage: (text: string, options?: { deliverAs?: string }) => {
      deliveries.push({ text, options });
    },
  }, spawnImpl);
  return { handlers, deliveries, completions };
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
