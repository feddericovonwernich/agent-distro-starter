import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createServer } from 'node:http';
import type { ServerResponse } from 'node:http';

function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.filter((row): row is Record<string, unknown> =>
    !!row && typeof row === 'object' && (row as Record<string, unknown>).type === 'text')
    .map(row => typeof row.text === 'string' ? row.text : '').join('\n');
  return value && typeof value === 'object' ? JSON.stringify(value) : '';
}
function result(messages: unknown[], id: string): Record<string, unknown> | undefined {
  return messages.find((row): row is Record<string, unknown> => {
    if (!row || typeof row !== 'object') return false;
    const v = row as Record<string, unknown>;
    return (v.role === 'tool' || v.role === 'toolResult') && (v.tool_call_id ?? v.toolCallId) === id;
  });
}
function parsed<T>(messages: unknown[], id: string): T {
  const row = result(messages, id);
  if (!row || row.isError === true) throw new Error(`missing tool result ${id}`);
  return JSON.parse(text(row.content));
}
function sse(res: ServerResponse, payload: object) {
  res.write(`data: ${JSON.stringify(payload)}\n\n`);
  res.write('data: [DONE]\n\n');
  res.end();
}

export default async function fixture(pi: {
  registerProvider: (name: string, config: object) => void;
  on: (event: string, handler: (value: Record<string, unknown>) => void) => void;
}) {
  const errors = new Set<string>();
  pi.on('tool_result', event => {
    if (event.isError === true && typeof event.toolCallId === 'string') errors.add(event.toolCallId);
  });
  const server = createServer(async (req, res) => {
    if (req.method !== 'POST' || !req.url?.endsWith('/chat/completions')) {
      res.writeHead(404).end(); return;
    }
    let body = '';
    for await (const chunk of req) body += chunk;
    const messages = (JSON.parse(body).messages ?? []) as unknown[];
    const context = messages.filter(row => !!row && typeof row === 'object' &&
      ['system', 'developer'].includes(String((row as Record<string, unknown>).role)));
    if (context.length) writeFileSync(resolve(process.cwd(), 'native-provider-context.json'), JSON.stringify(context));
    writeFileSync(resolve(process.cwd(), 'native-provider-transcript.json'), JSON.stringify(messages));
    const id = `fixture-${Date.now()}`;
    const call = (callId: string, name: string, args: object) => {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      sse(res, { id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', tool_calls: [{ index: 0, id: callId, type: 'function',
          function: { name, arguments: JSON.stringify(args) } }] }, finish_reason: 'tool_calls' }] });
    };
    try {
      if (errors.size) throw new Error(`tool failure: ${Array.from(errors).join(', ')}`);
      if (!result(messages, 'read-request')) return call('read-request', 'read', { path: 'request.json:raw' });
      const request = parsed<{ schema: number; report_schema: number; manifest_id: string }>(messages, 'read-request');
      if (request.schema !== 2 || request.report_schema !== 2 || !request.manifest_id) throw new Error('bad request');
      if (!result(messages, 'read-manifest')) return call('read-manifest', 'read', { path: 'selection-manifest.json:raw' });
      const manifestText = text(result(messages, 'read-manifest')?.content);
      const manifest: { manifest_id: string; episodes: Array<{ task_id?: string; session_id: string; episode_id: string }> } = JSON.parse(manifestText);
      if (manifest.manifest_id !== request.manifest_id || Buffer.byteLength(manifestText) > 16384 ||
          manifestText.includes('AGD_NATIVE_REVIEWER_SENTINEL') || manifestText.includes('AGD_UNRELATED_PRIVATE_SENTINEL')) {
        throw new Error('manifest was not metadata-only');
      }
      const episode = manifest.episodes.find(row => row.task_id?.startsWith('managed-'));
      if (!episode) throw new Error('selected episode missing');
      const base = '"$AGD_CLI" --home "$AGD_HOME" reviewer';
      if (!result(messages, 'metadata')) return call('metadata', 'bash', {
        command: `[ ! -e packet.json ] && [ ! -e catalog.json ] && ${base} inspect "$AGD_REVIEW_RUN_ID" --episode ${episode.episode_id}`,
      });
      const metadata = parsed<{ source: string; rows: Array<{ message_id?: string }> }>(messages, 'metadata');
      if (metadata.source !== 'native' || JSON.stringify(metadata).includes('AGD_UNRELATED_PRIVATE_SENTINEL')) {
        throw new Error('native fallback or scope failure');
      }
      const ids = [`${episode.session_id}-prompt-0`, `${episode.session_id}-action-0`, `${episode.session_id}-final-0`];
      if (!ids.every(id => metadata.rows.some(row => row.message_id === id))) throw new Error('metadata locators missing');
      const names = ['intent', 'action', 'outcome'];
      for (let index = 0; index < names.length; index++) {
        if (!result(messages, names[index])) return call(names[index], 'bash', {
          command: `${base} inspect "$AGD_REVIEW_RUN_ID" --episode ${episode.episode_id} --source native --record ${ids[index]} --part content`,
        });
      }
      type Page = { text: string; source_id: string };
      const intent = parsed<Page>(messages, 'intent'), action = parsed<Page>(messages, 'action'), outcome = parsed<Page>(messages, 'outcome');
      if (!intent.text.includes('AGD_NATIVE_REVIEWER_CONSTRAINT') || !action.text.includes('AGD_NATIVE_REVIEWER_ACTION') ||
          !outcome.text.includes('AGD_NATIVE_REVIEWER_SENTINEL')) throw new Error('bounded content missing');
      if (!result(messages, 'target')) return call('target', 'bash', {
        command: `${base} target inspect "$AGD_REVIEW_RUN_ID" --scope project --project sample --path value.txt --operation modify --surface tooling`,
      });
      const target = parsed<{ target_ref: string }>(messages, 'target');
      if (!target.target_ref) throw new Error('target missing');
      if (!result(messages, 'write-report')) {
        const review = { episode_id: episode.episode_id, assessment: 'reflected',
          intent_sources: [intent.source_id], action_sources: [action.source_id],
          outcome_sources: [outcome.source_id], limitations: [] };
        const finding = { episode_ids: [episode.episode_id], target_ref: target.target_ref,
          finding: 'The observed action omitted the required validation.', proposed_change: 'Add validation to the project tool.',
          expected_benefit: 'The missing validation becomes visible.', applicability: 'project',
          scope_rationale: 'The check concerns the selected project tool.', citations: [intent.source_id, action.source_id, outcome.source_id],
          counterexamples: [], alternatives: 'Use a manual check.', uncertainty: 'No counterexample was observed; causality is unproven.',
          validation: 'Exercise the project tool with an invalid result.', validation_limits: ['Synthetic fixture only.'],
          risks: ['The check may reject valid results.'], draft: null };
        return call('write-report', 'write', { path: 'report-draft.json',
          content: JSON.stringify({ schema: 2, summary: 'Bounded native fixture.', episode_reviews: [review], findings: [finding] }) });
      }
      if (!text(result(messages, 'write-report')?.content).trim()) throw new Error('report write not acknowledged');
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      sse(res, { id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', content: 'Bounded native review draft written.' }, finish_reason: 'stop' }] });
    } catch (error) {
      res.writeHead(502).end(String(error));
    }
  });
  const listening = Promise.withResolvers<void>();
  server.listen(0, '127.0.0.1', listening.resolve);
  await listening.promise;
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fixture endpoint did not bind');
  pi.registerProvider('agd-fixture', {
    baseUrl: `http://127.0.0.1:${address.port}/v1`, api: 'openai-completions', apiKey: 'local-only',
    models: [{ id: 'reviewer', name: 'AGD local-only reviewer', reasoning: false, input: ['text'], supportsTools: true,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32768, maxTokens: 2048 }],
  });
  pi.on('session_shutdown', () => server.close());
}
