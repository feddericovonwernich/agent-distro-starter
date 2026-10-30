import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const cli = resolve(dirname(fileURLToPath(import.meta.url)), '../bin/agd');

type WorkerEvent = {
  willContinue?: boolean;
  messages?: unknown[];
  systemPrompt?: string[];
  stop_hook_active?: boolean;
  last_assistant_message?: unknown;
  signal?: AbortSignal;
};

type HookContext = { abort: () => void };
type HookResult = {
  systemPrompt?: string[];
  messages?: unknown[];
  continue?: boolean;
  additionalContext?: string;
};
type ProtocolContext = {
  schema: 1;
  task_id: string;
  incarnation: string;
  phase: string;
  kind: 'scout' | 'ship';
  worktree: string;
  brief: string;
  report: string;
  agd_argv: string[];
  pr_grant: string | null;
  task_skills: string[];
  system_skills: string[];
  protocol: { path: string; sha256: string };
  outcome: 'unfinished' | 'ready' | 'blocked' | 'decision' | 'closed';
};

const coreMarker = '[agd:worker-protocol]\n';

function failedAssistant(message: unknown): boolean {
  return record(message) && message.role === 'assistant' &&
    (message.stopReason === 'error' || message.stopReason === 'aborted');
}

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
  on: (name: string, handler: (event: WorkerEvent, context?: HookContext) => HookResult | void) => void;
  sendUserMessage: (text: string) => unknown;
}, run: WorkerSpawn = spawnSync as unknown as WorkerSpawn) {
  const home = process.env.AGD_HOME;
  const task = process.env.AGD_TASK_ID;
  const incarnation = process.env.AGD_INCARNATION;
  if (!home || !task || !incarnation) return;
  function signal(kind: string, key: string, message = resolve(home, 'data/tasks', task, 'brief.md')) {
    const result = run(cli, ['--home', home, 'worker', 'event', task, '--incarnation', incarnation,
      '--kind', kind, '--key', key, '--message', message],
      { encoding: 'utf8', timeout: 5000 });
    if (result.status !== 0) console.error(`agd worker event failed: ${result.stderr || result.error}`);
    return result.status === 0;
  }
  let timer: NodeJS.Timeout | undefined;
  let turn: 'unknown' | 'busy' | 'idle' =
    process.env.AGD_WORKER_RESUME_READY === '1' ? 'idle' : 'unknown';
  let active: ActiveRequest | undefined;
  let core: { path: string; sha256: string; text: string } | undefined;
  let healthy = true;
  let shuttingDown = false;
  let corrected = false;
  let diagnosticEmitted = false;

  function query(): ProtocolContext {
    const result = run(cli, ['--home', home, 'worker', 'context', task, '--incarnation', incarnation],
      { encoding: 'utf8', timeout: 5000 });
    if (result.status !== 0) throw new Error(result.stderr || String(result.error));
    const value: unknown = JSON.parse(result.stdout);
    if (!record(value) || value.schema !== 1 || value.task_id !== task || value.incarnation !== incarnation ||
        !['running', 'ready', 'landed', 'cleaned'].includes(String(value.phase)) ||
        !['unfinished', 'ready', 'blocked', 'decision', 'closed'].includes(String(value.outcome)) ||
        !['scout', 'ship'].includes(String(value.kind)) ||
        typeof value.worktree !== 'string' || !isAbsolute(value.worktree) ||
        value.brief !== resolve(home, 'data/tasks', task, 'brief.md') ||
        value.report !== resolve(home, 'data/tasks', task, 'report-draft.md') ||
        !Array.isArray(value.agd_argv) || value.agd_argv.length !== 3 ||
        value.agd_argv[0] !== cli || value.agd_argv[1] !== '--home' || value.agd_argv[2] !== home ||
        !(value.pr_grant === null || typeof value.pr_grant === 'string' && value.pr_grant.length > 0) ||
        !Array.isArray(value.task_skills) || !value.task_skills.every(id => typeof id === 'string') ||
        !Array.isArray(value.system_skills) || !value.system_skills.includes('agd-worker') ||
        !record(value.protocol) || value.protocol.path !== resolve(value.worktree, '.omp/skills/agd-worker/SKILL.md') ||
        typeof value.protocol.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(value.protocol.sha256)) {
      throw new Error('invalid or foreign AGD worker context');
    }
    healthy = true;
    return value as ProtocolContext;
  }

  function loadCore(state: ProtocolContext): string {
    if (core?.path === state.protocol.path && core.sha256 === state.protocol.sha256) return core.text;
    const bytes = readFileSync(state.protocol.path);
    if (createHash('sha256').update(bytes).digest('hex') !== state.protocol.sha256) {
      throw new Error('AGD worker protocol checksum changed');
    }
    const text = bytes.toString('utf8').replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '').trim();
    if (!text) throw new Error('AGD worker protocol is empty');
    core = { ...state.protocol, text };
    return text;
  }

  function diagnostic(message: string) {
    console.error(message);
    if (diagnosticEmitted) return;
    try {
      const path = resolve(home, 'data/tasks', task, 'agd-contract-incomplete.md');
      writeFileSync(path, message + '\n', { mode: 0o600 });
      diagnosticEmitted = signal('blocked', 'agd-contract-incomplete', path);
    } catch (error) {
      console.error(`agd worker diagnostic persistence failed: ${error}`);
    }
  }

  function unavailable(error: unknown, context?: HookContext, abort = true) {
    healthy = false;
    diagnostic(`AGD worker protocol unavailable: ${error}`);
    // Throwing alone is insufficient: native extension errors are recoverable.
    if (abort) context?.abort();
  }

  api.on('before_agent_start', (event, context) => {
    try {
      const text = loadCore(query());
      return { systemPrompt: [
        ...(event.systemPrompt ?? []).filter(part => !part.startsWith(coreMarker)),
        coreMarker + text,
      ] };
    } catch (error) { unavailable(error, context); }
  });
  api.on('context', (event, context) => {
    try {
      const state = query();
      loadCore(state);
      const next = state.outcome === 'ready' ? 'Answer follow-ups read-only; do not replay the assignment.' :
        state.outcome === 'closed' ? 'Task closed; do not modify or publish anything.' :
        state.outcome === 'blocked' || state.outcome === 'decision' ? 'Address the recorded blocker or decision; do not claim readiness.' :
        state.pr_grant ? 'Complete the brief using agd-scoped-pr-relay and its guarded verification route.' :
        'Complete the brief, verify it, write the report, then use task ready or record a concrete blocker/decision.';
      return { messages: [
        ...(event.messages ?? []).filter(message => !record(message) || message.customType !== 'agd:worker-context'),
        { role: 'custom', customType: 'agd:worker-context', display: false, timestamp: Date.now(),
          content: JSON.stringify({
            task: state.task_id, incarnation: state.incarnation, phase: state.phase,
            kind: state.kind, worktree: state.worktree, brief: state.brief, report: state.report,
            agd_argv: state.agd_argv, pr_grant: state.pr_grant, outcome: state.outcome, next_action: next,
          }) },
      ] };
    } catch (error) { unavailable(error, context); }
  });
  api.on('session_switch', (_event, context) => {
    core = undefined;
    try { loadCore(query()); } catch (error) { unavailable(error, context); }
  });
  api.on('session_stop', (event, context) => {
    const last = event.last_assistant_message ?? event.messages?.findLast(
      message => record(message) && message.role === 'assistant');
    if (shuttingDown || active || event.signal?.aborted || failedAssistant(last)) return;
    try {
      const state = query();
      if (state.phase !== 'running' || state.outcome !== 'unfinished') return;
      if (!corrected && !event.stop_hook_active) {
        corrected = true;
        return { continue: true, additionalContext:
          'AGD still records this assignment as unfinished. Verify and submit its report through the appropriate guarded readiness route, or record a concrete blocker/decision. Do not claim success from chat alone.' };
      }
      corrected = true;
      if (!diagnosticEmitted) diagnostic(
        'AGD assignment remains unfinished after one corrective turn. No readiness or actionable blocker/decision was recorded. Central inspection is required; the task and worktree are retained.');
    } catch (error) { unavailable(error, context, false); }
  });

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
    if (turn !== 'idle' || shuttingDown || !healthy) return;
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
      // followUp only queues: a fresh ready worker has no initial turn to drain it.
      api.sendUserMessage(message.body);
    } catch {
      active = undefined;
    }
  }

  api.on('session_start', (_event, context) => {
    try { loadCore(query()); } catch (error) { unavailable(error, context); }
    timer = setInterval(checkInbox, 2000);
    timer.unref?.();
    checkInbox();
  });
  api.on('session_shutdown', () => {
    shuttingDown = true;
    if (timer !== undefined) clearInterval(timer);
    timer = undefined;
  });
  api.on('agent_start', () => {
    if (turn !== 'busy') {
      corrected = false;
      diagnosticEmitted = false;
    }
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
      const last = event.messages?.findLast(message => record(message) && message.role === 'assistant');
      active.completion = {
        status: reply && !failedAssistant(last) ? 'done' : 'failed',
        body: reply ?? 'steering turn ended without assistant text',
      };
    }
    checkInbox();
  });
}
