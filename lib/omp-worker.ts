import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const cli = resolve(dirname(fileURLToPath(import.meta.url)), '../bin/agd');
const home = process.env.AGD_HOME;
const task = process.env.AGD_TASK_ID;
const incarnation = process.env.AGD_INCARNATION;

export default function workerExtension(api: {
  on: (name: string, handler: (event: { willContinue?: boolean }) => void) => void;
  sendUserMessage: (text: string, options?: { deliverAs?: string }) => unknown;
}) {
  if (!home || !task || !incarnation) return;
  function signal(kind: string, key: string) {
    const result = spawnSync(cli, ['--home', home!, 'worker', 'event', task!, '--incarnation', incarnation!,
      '--kind', kind, '--key', key, '--message', resolve(home!, 'data/tasks', task!, 'brief.md')],
      { encoding: 'utf8', timeout: 5000 });
    if (result.status !== 0) console.error(`agd worker event failed: ${result.stderr || result.error}`);
  }
  let timer: NodeJS.Timeout | undefined;
  let turn: 'unknown' | 'busy' | 'idle' = 'unknown';
  const rung = new Set<string>();
  function checkInbox() {
    if (turn !== 'idle') return;
    const result = spawnSync(cli, ['--home', home!, 'worker', 'inbox', task!],
      { encoding: 'utf8', timeout: 5000 });
    if (result.status !== 0) return;
    let messages: unknown;
    try { messages = JSON.parse(result.stdout); } catch { return; }
    if (!Array.isArray(messages)) return;
    for (const message of messages) {
      if (!message || typeof message !== 'object' || !('id' in message) ||
          typeof message.id !== 'string' || rung.has(message.id)) continue;
      rung.add(message.id);
      try {
        api.sendUserMessage(`Task steering inbox has message ${message.id}. Run ${cli} --home ${home} worker inbox ${task}, read it, then run ${cli} --home ${home} worker acknowledge ${task} ${message.id}. Continue work as instructed.`, { deliverAs: 'followUp' });
      } catch {
        rung.delete(message.id);
      }
    }
  }
  api.on('session_start', () => { timer = setInterval(checkInbox, 2000); checkInbox(); });
  api.on('session_shutdown', () => { clearInterval(timer); });
  api.on('agent_start', () => { turn = 'busy'; signal('busy', 'turn'); });
  api.on('agent_end', (event: { willContinue?: boolean }) => {
    if (!event.willContinue) {
      turn = 'idle';
      signal('idle', 'turn');
      checkInbox();
    }
  });
}
