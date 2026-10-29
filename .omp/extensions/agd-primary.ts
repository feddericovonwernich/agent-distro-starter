import { randomUUID } from 'node:crypto';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const home = process.env.AGD_HOME || root;
const cli = resolve(root, 'bin/agd');

type Health = { delegatedWork: boolean; watcherHealthy: boolean };
export function stopDecision(stopHookActive: boolean, health: Health) {
  if (stopHookActive || !health.delegatedWork || health.watcherHealthy) return;
  return { continue: true as const,
    additionalContext: 'Delegated work has no healthy watcher. Restore supervision or report the concrete failure; do not claim it is monitored.' };
}

export function centralWarning(managed: boolean) {
  return managed ? undefined : 'This bare OMP session is unmanaged: AGD does not control its transcript location or telemetry export. Start `agd observability central` for a managed central session.';
}

export function launcherOwnerToken(endpointToken: string | undefined, randomOwner: string) {
  return endpointToken || randomOwner;
}

function startEvidence(pid: number): string {
  return readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ', 2)[1].split(' ')[19];
}

function command(...args: string[]): { ok: boolean; data: unknown } {
  const result = spawnSync(cli, ['--home', home, ...args], { encoding: 'utf8', timeout: 10000 });
  if (result.status !== 0) return { ok: false, data: result.stderr || result.error?.message };
  try { return { ok: true, data: JSON.parse(result.stdout) }; }
  catch { return { ok: false, data: result.stdout }; }
}

async function readSessionHealth(): Promise<Health> {
  const result = command('session', 'status');
  if (!result.ok || !result.data || typeof result.data !== 'object')
    return { delegatedWork: true, watcherHealthy: false };
  const health = result.data;
  if (!('delegatedWork' in health) || !('watcherHealthy' in health) ||
      typeof health.delegatedWork !== 'boolean' || typeof health.watcherHealthy !== 'boolean')
    return { delegatedWork: true, watcherHealthy: false };
  return { delegatedWork: health.delegatedWork, watcherHealthy: health.watcherHealthy };
}


type Context = { isIdle: () => boolean; ui: {
  setStatus: (key: string, text: string | undefined) => void;
  notify: (message: string, type?: 'info' | 'warning' | 'error') => void;
} };
type ExtensionAPI = {
  on: (event: string, handler: (event: { stop_hook_active?: boolean }, context: Context) => unknown) => void;
  sendUserMessage: (text: string, options?: { deliverAs?: string }) => unknown;
};

export default function primary(api: ExtensionAPI) {
  const token = launcherOwnerToken(process.env.AGD_ENDPOINT_TOKEN, randomUUID());
  let generation = 0;
  let child: ChildProcess | null = null;
  let startup = '';
  let outstanding = new Set<string>();
  let restarted = false;
  let inFlight = new Set<string>();
  let sessionContext: Context | undefined;

  function showStatus(context: Context, result = command('session', 'status')) {
    const data = result.ok && result.data && typeof result.data === 'object'
      ? result.data as { watcherHealthy?: boolean; pending?: unknown[] }
      : undefined;
    const count = Array.isArray(data?.pending) ? data.pending.length : 0;
    context.ui.setStatus('agd', data?.watcherHealthy
      ? `Distro · watching · ${count} pending`
      : 'Distro · watcher unavailable');
    return result;
  }

  async function ring(ids: string[], active: number) {
    if (active !== generation) return;
    const fresh = ids.filter(id => !outstanding.has(id) && !inFlight.has(id));
    if (!fresh.length) return;
    fresh.forEach(id => inFlight.add(id));
    try {
      await api.sendUserMessage(`New durable task events: ${fresh.join(', ')}. Run ${cli} --home ${home} events list, handle them, then acknowledge exact IDs with that CLI and home. Inspect open decisions independently.`, sessionContext?.isIdle() ? undefined : { deliverAs: 'followUp' });
      if (active === generation) fresh.forEach(id => outstanding.add(id));
      if (active === generation && sessionContext) showStatus(sessionContext);
    } catch {
      if (active === generation) {
        startup = 'Supervision wake enqueue failed; retrying durable events.';
        setTimeout(() => { void ring(fresh, active); }, 2000);
      }
    } finally {
      fresh.forEach(id => inFlight.delete(id));
    }
  }

  let retirement: Promise<void> = Promise.resolve();
  function stop(): Promise<void> {
    const previous = child;
    child = null;
    if (previous) {
      const closed = new Promise<void>(resolve => previous.once('close', () => resolve()));
      previous.kill('SIGTERM');
      retirement = retirement.then(() => closed);
    }
    return retirement;
  }

  function launchWatch(active: number) {
    command('init');
    const ownerStart = startEvidence(process.pid);
    const watcher = spawn(cli, ['--home', home, 'session', 'start',
      '--owner-pid', String(process.pid), '--owner-start', ownerStart,
      '--generation', String(active)], { stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, AGD_OWNER: token } });
    child = watcher;
    let pending = '';
    watcher.stdout?.on('data', (chunk: Buffer) => {
      pending += chunk.toString();
      while (pending.includes('\n')) {
        const index = pending.indexOf('\n');
        const row = pending.slice(0, index);
        pending = pending.slice(index + 1);
        if (active !== generation) continue;
        try {
          const item: unknown = JSON.parse(row);
          if (!item || typeof item !== 'object' || !('type' in item)) continue;
          if (item.type === 'ready') {
            startup = 'Supervision watcher ready.';
            if (sessionContext) showStatus(sessionContext);
          }
          if (item.type === 'events' && 'ids' in item && Array.isArray(item.ids)) {
            const ids = item.ids.filter((id): id is string => typeof id === 'string');
            void ring(ids, active);
          }
        } catch { startup = 'Supervision output unreadable; inspect bin/agd session status.'; }
      }
    });
    watcher.stderr?.on('data', (chunk: Buffer) => {
      if (active === generation) startup = `Supervision error: ${chunk.toString().slice(0, 1000)}`;
    });
    watcher.on('close', (code) => {
      if (active !== generation || child !== watcher) return;
      child = null;
      if (!restarted && code !== 3) {
        restarted = true;
        sessionContext?.ui.setStatus('agd', 'Distro · reconnecting');
        startup = 'Supervision watcher exited; attempting one restart.';
        launchWatch(active);
      } else {
        startup = 'Supervision watcher stopped; delegated work is not monitored.';
        sessionContext?.ui.setStatus('agd', 'Distro · watcher unavailable');
        sessionContext?.ui.notify(startup, 'error');
      }
    });
  }

  async function startSession(_event: unknown, context: Context) {
    process.env.AGD_OWNER = token;
    sessionContext = context;
    context.ui.setStatus('agd', 'Distro · starting');
    const active = ++generation;
    outstanding = new Set();
    restarted = false;
    inFlight = new Set();
    await stop();
    if (active === generation) launchWatch(active);
  }
  api.on('session_start', startSession);
  api.on('session_switch', startSession);
  api.on('before_agent_start', (_event, context) => {
    const status = showStatus(context);
    const events = command('events', 'list');
    const warning = centralWarning(process.env.AGD_MANAGED_CENTRAL === '1');
    const content = [warning, startup, `Home: ${home}`,
      status.ok ? `Supervision: ${JSON.stringify(status.data)}` : `Supervision unavailable: ${status.data}`,
      events.ok ? `Pending events: ${JSON.stringify(events.data)}` : `Pending events unavailable: ${events.data}`].filter(Boolean).join('\n');
    startup = '';
    return { message: { customType: 'agd:supervision', content: content.slice(0, 16000), display: false } };
  });
  api.on('session_stop', async event => stopDecision(event.stop_hook_active === true, await readSessionHealth()));
  api.on('session_shutdown', () => {
    generation++;
    sessionContext?.ui.setStatus('agd', undefined);
    if (process.env.AGD_OWNER === token) delete process.env.AGD_OWNER;
    return stop();
  });
}
