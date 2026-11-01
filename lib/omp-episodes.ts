import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { isAbsolute, relative, resolve } from 'node:path';

type RecordValue = Record<string, unknown>;

export type EpisodeSessionManager = {
  getSessionId: () => string;
  getSessionFile: () => string | undefined;
  ensureOnDisk: () => Promise<void>;
};

export type EpisodeContext = {
  sessionManager: EpisodeSessionManager;
};

export type EpisodeEvent = {
  reason?: 'prompt' | 'revive';
  messages?: unknown[];
  willContinue?: boolean;
  awaitingAsyncWork?: boolean;
};

export type EpisodeAPI = {
  on: (name: string, handler: (event: EpisodeEvent, context?: EpisodeContext) => unknown) => void;
};

export type EpisodeOptions = {
  home: string;
  cli: string;
  role: 'worker' | 'central';
  diagnostic: (message: string) => void;
  beforeBind?: () => Promise<boolean>;
};

type Binding = { source_id: string; [key: string]: unknown };
type Pending = { binding?: Binding };
type BindingDetails = { schema: 1; source_id: string; interaction_id: string };
type GapDetails = { schema: 1; interaction_id: string };
type Carrier = { customType: 'agd:episode-binding'; details?: BindingDetails } |
  { customType: 'agd:episode-gap'; details?: GapDetails };

function record(value: unknown): value is RecordValue {
  return typeof value === 'object' && value !== null;
}

function uuid(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function sourceId(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
}

function runCli(cli: string, home: string, operation: 'bind' | 'complete', body: RecordValue): RecordValue {
  const result = spawnSync(cli, ['--home', home, 'reviewer', 'episode', operation], {
    encoding: 'utf8',
    timeout: 5000,
    input: JSON.stringify(body),
  });
  if (result.status !== 0) throw new Error(result.stderr || result.error?.toString() || `episode ${operation} failed`);
  const parsed: unknown = JSON.parse(result.stdout);
  if (!record(parsed)) throw new Error(`episode ${operation} returned invalid JSON`);
  return parsed;
}

function bindingDetails(value: unknown): BindingDetails | undefined {
  if (!record(value) || value.schema !== 1 || !sourceId(value.source_id) || !uuid(value.interaction_id)) return;
  return { schema: 1, source_id: value.source_id, interaction_id: value.interaction_id };
}

function gapDetails(value: unknown): GapDetails | undefined {
  if (!record(value) || value.schema !== 1 || !uuid(value.interaction_id)) return;
  return { schema: 1, interaction_id: value.interaction_id };
}

function finalProjection(messages: unknown[]): RecordValue | undefined {
  let candidate: RecordValue | undefined;
  for (let index = messages.length - 1; index >= 0; index--) {
    const value = messages[index];
    if (record(value) && value.role === 'assistant') {
      candidate = value;
      break;
    }
  }
  if (!candidate || candidate.stopReason !== 'stop' || Boolean(candidate.isError) ||
      'errorMessage' in candidate || Boolean(candidate.error) ||
      Boolean(candidate.errorDetails) || Boolean(candidate.truncated)) return;
  if (typeof candidate.timestamp !== 'number' || !Number.isFinite(candidate.timestamp)) return;
  const content = candidate.content;
  if (typeof content === 'string') {
    if (!content.trim()) return;
    return { role: 'assistant', timestamp: candidate.timestamp, stopReason: 'stop', content };
  }
  if (!Array.isArray(content)) return;
  let visible = '';
  for (const block of content) {
    if (!record(block)) return;
    if (block.type === 'thinking') continue;
    if (block.type !== 'text' || typeof block.text !== 'string') return;
    visible += block.text;
  }
  if (!visible.trim()) return;
  return { role: 'assistant', timestamp: candidate.timestamp, stopReason: 'stop', content };
}

function latestCarrier(messages: unknown[]): Carrier | undefined {
  for (let index = messages.length - 1; index >= 0; index--) {
    const value = messages[index];
    if (!record(value) || (value.customType !== 'agd:episode-binding' && value.customType !== 'agd:episode-gap')) continue;
    if (value.customType === 'agd:episode-binding') {
      return { customType: value.customType, details: bindingDetails(value.details) };
    }
    return { customType: value.customType, details: gapDetails(value.details) };
  }
}

/** Install independent native recording hooks without changing agent supervision. */
export function registerEpisodeCapture(api: EpisodeAPI, options: EpisodeOptions): void {
  const pending = new Map<string, Pending>();
  let latestInteractionId: string | undefined;
  const home = resolve(options.home);

  const diagnose = (message: string) => {
    try { options.diagnostic(message); } catch { /* diagnostics cannot affect agent work */ }
  };
  const gapCarrier = (interactionId: string) => ({
    message: {
      customType: 'agd:episode-gap' as const,
      display: false as const,
      content: 'Episode capture gap',
      details: { schema: 1 as const, interaction_id: interactionId },
    },
  });

  api.on('before_agent_start', async (event, context) => {
    if (event.reason === 'revive') {
      const interactionId = latestInteractionId;
      if (!interactionId) return;
      if (pending.get(interactionId)?.binding) return;
      return gapCarrier(interactionId);
    }
    const interactionId = randomUUID();
    latestInteractionId = interactionId;
    pending.set(interactionId, {});
    const manager = context?.sessionManager;
    if (!manager) {
      diagnose('Episode capture unavailable: native session manager is missing.');
      return gapCarrier(interactionId);
    }
    const sessionId = manager.getSessionId();
    const sessionFile = manager.getSessionFile();
    if (!sessionId || !sessionFile || !isAbsolute(sessionFile)) {
      diagnose('Episode capture unavailable: native session identity/file is missing.');
      return gapCarrier(interactionId);
    }
    try {
      await manager.ensureOnDisk();
      if (options.beforeBind && !(await options.beforeBind())) {
        diagnose('Episode capture skipped: managed owner readiness was not proved.');
        return gapCarrier(interactionId);
      }
      if (manager.getSessionId() !== sessionId || manager.getSessionFile() !== sessionFile) {
        diagnose('Episode capture skipped: native session changed while binding.');
        return gapCarrier(interactionId);
      }
      const relativeFile = relative(home, sessionFile);
      if (!relativeFile || relativeFile.startsWith('..') || isAbsolute(relativeFile)) {
        diagnose('Episode capture unavailable: native session is outside AGD home.');
        return gapCarrier(interactionId);
      }
      const binding = runCli(options.cli, home, 'bind', {
        role: options.role,
        session_id: sessionId,
        session_file: sessionFile,
      });
      if (!sourceId(binding.source_id)) throw new Error('episode bind returned no source receipt');
      pending.set(interactionId, { binding: binding as Binding });
      return {
        message: {
          customType: 'agd:episode-binding' as const,
          display: false as const,
          content: 'Episode binding',
          details: { schema: 1 as const, source_id: binding.source_id, interaction_id: interactionId },
        },
      };
    } catch (error) {
      diagnose(`Episode capture unavailable: ${error instanceof Error ? error.message : String(error)}`);
      return gapCarrier(interactionId);
    }
  });

  api.on('context', event => ({
    messages: (event.messages ?? []).filter(message => !record(message) ||
      (message.customType !== 'agd:episode-binding' && message.customType !== 'agd:episode-gap')),
  }));

  // Ponytail ceiling: compaction/revive can erase a provable binding; native event-bound
  // session/turn IDs are the upgrade path for eliminating this explicit eligibility gap.
  api.on('agent_end', event => {
    if (event.willContinue === true || event.awaitingAsyncWork === true) return;
    const marker = latestCarrier(event.messages ?? []);
    if (!marker) {
      diagnose('Episode capture gap: terminal event has no native binding marker.');
      return;
    }
    if (!marker.details) {
      diagnose('Episode capture gap: terminal event has an invalid native binding marker.');
      return;
    }
    if (marker.details.interaction_id !== latestInteractionId) {
      diagnose('Episode capture gap: terminal event marker belongs to an older interaction.');
      return;
    }
    const markerCount = (event.messages ?? []).filter(message =>
      record(message) &&
      (message.customType === 'agd:episode-binding' || message.customType === 'agd:episode-gap') &&
      record(message.details) && message.details.interaction_id === marker.details?.interaction_id).length;
    if (markerCount !== 1) {
      diagnose('Episode capture gap: terminal event has ambiguous current interaction metadata.');
      return;
    }
    if (marker.customType === 'agd:episode-gap') {
      diagnose('Episode capture gap: current interaction has no authorized native source.');
      return;
    }
    const pendingInteraction = pending.get(marker.details.interaction_id);
    const binding = pendingInteraction?.binding;
    if (!binding || binding.source_id !== marker.details.source_id) {
      diagnose('Episode capture gap: terminal event has no registered native binding.');
      return;
    }
    const message = finalProjection(event.messages ?? []);
    if (!message) return;
    try {
      runCli(options.cli, home, 'complete', {
        source_id: binding.source_id,
        interaction_id: marker.details.interaction_id,
        will_continue: false,
        awaiting_async_work: false,
        message,
      });
      pending.delete(marker.details.interaction_id);
    } catch (error) {
      diagnose(`Episode completion unavailable: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
}
