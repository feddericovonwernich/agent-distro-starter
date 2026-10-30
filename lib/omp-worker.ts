import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const cli = resolve(dirname(fileURLToPath(import.meta.url)), '../bin/agd');

type WorkerEvent = {
  willContinue?: boolean;
  messages?: unknown[];
};

export type WorkerSpawn = (
  command: string,
  args: string[],
  options: { encoding: 'utf8'; timeout: number; input?: string },
) => { status: number | null; stdout: string; stderr: string; error?: unknown };

type ActiveRequest = {
  id: string;
  started: boolean;
  completion?: {
    status: 'done' | 'failed';
    body: string;
  };
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function assistantReply(messages: unknown[]): string | undefined {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (!record(message) || message.role !== 'assistant') continue;
    if (typeof message.content === 'string') {
      return message.content.trim() ? message.content : undefined;
    }
    if (!Array.isArray(message.content)) return undefined;
    const text = message.content
      .filter((block): block is Record<string, unknown> =>
        record(block) && block.type === 'text' && typeof block.text === 'string')
      .map(block => block.text as string)
      .join('');
    return text.trim() ? text : undefined;
  }
  return undefined;
}

export default function workerExtension(api: {
  on: (name: string, handler: (event: WorkerEvent) => void) => void;
  sendUserMessage: (text: string, options?: { deliverAs?: string }) => unknown;
}, run: WorkerSpawn = spawnSync as unknown as WorkerSpawn) {
  const home = process.env.AGD_HOME;
  const task = process.env.AGD_TASK_ID;
  const incarnation = process.env.AGD_INCARNATION;
  if (!home || !task || !incarnation) return;
  function signal(kind: string, key: string) {
    const result = run(cli, ['--home', home, 'worker', 'event', task, '--incarnation', incarnation,
      '--kind', kind, '--key', key, '--message', resolve(home, 'data/tasks', task, 'brief.md')],
      { encoding: 'utf8', timeout: 5000 });
    if (result.status !== 0) console.error(`agd worker event failed: ${result.stderr || result.error}`);
  }
  let timer: NodeJS.Timeout | undefined;
  let turn: 'unknown' | 'busy' | 'idle' =
    process.env.AGD_WORKER_RESUME_READY === '1' ? 'idle' : 'unknown';
  let active: ActiveRequest | undefined;

  function persistCompletion() {
    if (!active?.completion) return false;
    const result = run(cli, [
      '--home', home, 'worker', 'complete', task, active.id,
      '--incarnation', incarnation, '--status', active.completion.status,
    ], {
      encoding: 'utf8',
      timeout: 5000,
      input: active.completion.body,
    });
    if (result.status !== 0) return false;
    active = undefined;
    return true;
  }

  function checkInbox() {
    if (turn !== 'idle') return;
    if (active?.completion && !persistCompletion()) return;
    if (active) return;
    const result = run(cli, ['--home', home, 'worker', 'inbox', task],
      { encoding: 'utf8', timeout: 5000 });
    if (result.status !== 0) return;
    let messages: unknown;
    try { messages = JSON.parse(result.stdout); } catch { return; }
    if (!Array.isArray(messages)) return;
    const message = messages[0];
    if (!record(message) || typeof message.id !== 'string' ||
        typeof message.body !== 'string' || !message.body.trim()) return;
    active = { id: message.id, started: false };
    try {
      api.sendUserMessage(message.body, { deliverAs: 'followUp' });
    } catch {
      active = undefined;
    }
  }

  api.on('session_start', () => {
    timer = setInterval(checkInbox, 2000);
    checkInbox();
  });
  api.on('session_shutdown', () => {
    if (timer !== undefined) clearInterval(timer);
    timer = undefined;
  });
  api.on('agent_start', () => {
    turn = 'busy';
    if (active) active.started = true;
    signal('busy', 'turn');
  });
  api.on('agent_end', event => {
    if (event.willContinue) return;
    turn = 'idle';
    signal('idle', 'turn');
    if (active?.started && !active.completion) {
      const reply = assistantReply(event.messages ?? []);
      active.completion = reply
        ? { status: 'done', body: reply }
        : { status: 'failed', body: 'steering turn ended without assistant text' };
    }
    checkInbox();
  });
}
