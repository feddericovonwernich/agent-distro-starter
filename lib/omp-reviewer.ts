import { spawnSync } from 'node:child_process';
import { isAbsolute, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

type SessionManager = {
  getSessionId: () => string;
  getSessionFile: () => string | undefined;
  ensureOnDisk: () => Promise<void>;
};

type ReviewerContext = { sessionManager: SessionManager; shutdown: () => void };
type ReviewerEvent = { systemPrompt?: string[] };

type ReviewerAPI = {
  on: (name: string, handler: (event: ReviewerEvent, context: ReviewerContext) => unknown) => void;
};

const marker = '[agd:reviewer-policy]\n';
const fatalReviewer = (message: string): never => {
  console.error(message);
  // OMP 18.14 guards process.exit during extension loading and can still drain a
  // queued print prompt; this one-shot role has no native fatal-startup API yet.
  process.kill(process.pid, 'SIGKILL');
  throw new Error(message);
};

export default function reviewerExtension(api: ReviewerAPI) {
  const home = process.env.AGD_HOME;
  const runId = process.env.AGD_REVIEW_RUN_ID;
  const cli = resolve(dirname(fileURLToPath(import.meta.url)), '../bin/agd');

  api.on('session_start', async (_event, context) => {
    if (!home || !isAbsolute(home) || !runId || !/^[0-9a-f-]{36}$/.test(runId)) {
      fatalReviewer('AGD reviewer context is unavailable or malformed');
    }
    try {
      const manager = context.sessionManager;
      await manager.ensureOnDisk();
      const sessionId = manager.getSessionId();
      const sessionFile = manager.getSessionFile();
      if (!sessionId || !sessionFile || !isAbsolute(sessionFile)) {
        throw new Error('native reviewer session identity/file is unavailable');
      }
      const result = spawnSync(cli, [
        '--home', home, 'reviewer', 'session-bind', runId,
        '--session-id', sessionId, '--session-file', sessionFile,
      ], { encoding: 'utf8', timeout: 5000 });
      if (result.status !== 0) throw new Error(result.stderr || 'reviewer session bind failed');
    } catch (error) {
      fatalReviewer(`AGD reviewer session binding failed: ${error}`);
    }
  });

  api.on('before_agent_start', event => ({
    systemPrompt: [
      ...(event.systemPrompt ?? []).filter(part => !part.startsWith(marker)),
      marker + `Review run ${runId ?? 'unbound'}. Use only admitted execution evidence, preserve the reserved reviewer role, and keep recommendations inert; cite observed native tool results.`,
    ],
  }));
}
