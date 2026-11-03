import { createServer } from 'node:http';
import type { ServerResponse } from 'node:http';

const SENTINEL = 'AGD_NATIVE_REVIEWER_SENTINEL';
const SHELL_MARKER = 'AGD_NATIVE_REVIEWER_SHELL';

function reportFor(evidence: string, shell: string, sourceId: string): string {
  return JSON.stringify({
    schema: 1,
    summary: `Native process evidence: ${evidence.slice(0, 160)}; shell: ${shell.slice(0, 160)}`,
    findings: [{
      kind: 'new-skill',
      target: 'agd-reviewer-execution-trace',
      finding: `The native run read the reserved reviewer contract and observed ${SENTINEL} plus ${SHELL_MARKER}.`,
      applicability: 'native reviewer execution traces',
      citations: [sourceId],
      counterexamples: [],
      uncertainty: 'The deterministic local provider proves tool/result plumbing, not model judgement.',
      resolution: 'Add an AGD-authored process-trace review checklist covering tool evidence and bounded report handoff.',
      validation: `Read result contained ${SENTINEL}; shell result contained ${SHELL_MARKER}; write result was returned.`,
      validation_limits: ['Local scripted provider; no real-provider quality or policy claim.'],
      new_skill: {
        id: 'agd-reviewer-execution-trace',
        draft: '---\nname: agd-reviewer-execution-trace\ndescription: Use when reviewing native execution traces for process improvements\n---\n# Execution-trace review\n',
        triggers: ['native execution trace', 'review process evidence'],
        scope: 'AGD reviewer process traces',
        prerequisites: ['A bounded native session and durable tool results.'],
      },
    }],
  });
}

function resultText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value.filter((block): block is Record<string, unknown> =>
      !!block && typeof block === 'object' && (block as Record<string, unknown>).type === 'text')
      .map((block) => typeof block.text === 'string' ? block.text : '')
      .join('\n');
  }
  return value && typeof value === 'object' ? JSON.stringify(value) : '';
}

function toolResult(messages: unknown[], callId: string): Record<string, unknown> | undefined {
  return messages.find((row): row is Record<string, unknown> => {
    if (!row || typeof row !== 'object') return false;
    const value = row as Record<string, unknown>;
    const role = value.role;
    const id = value.tool_call_id ?? value.toolCallId;
    return (role === 'tool' || role === 'toolResult') && id === callId;
  });
}

function toolText(messages: unknown[]): string {
  return messages.filter((row): row is Record<string, unknown> => {
    if (!row || typeof row !== 'object') return false;
    const role = (row as Record<string, unknown>).role;
    return role === 'tool' || role === 'toolResult';
  }).map((row) => resultText(row.content)).join('\n');
}

function hasToolCall(messages: unknown[], name: string, argument?: string): boolean {
  return messages.some((row) => {
    if (!row || typeof row !== 'object') return false;
    const value = row as Record<string, unknown>;
    const calls = value.tool_calls;
    if (!Array.isArray(calls)) return false;
    return calls.some((call) => {
      if (!call || typeof call !== 'object') return false;
      const callValue = call as Record<string, unknown>;
      const functionValue = callValue.function;
      if (!functionValue || typeof functionValue !== 'object') return false;
      const functionRecord = functionValue as Record<string, unknown>;
      if (functionRecord.name !== name) return false;
      return argument === undefined || JSON.stringify(functionRecord.arguments).includes(argument);
    });
  });
}

function sourceId(messages: unknown[]): string | undefined {
  const content = ['read-reviewer-skill', 'read-evidence']
    .map((callId) => resultText(toolResult(messages, callId)?.content))
    .join('\n');
  return content.match(/\b[a-f0-9]{64}\b/)?.[0];
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
  const toolErrors = new Set<string>();
  pi.on('tool_result', event => {
    if (event.isError === true && typeof event.toolCallId === 'string') {
      toolErrors.add(event.toolCallId);
    }
  });
  const server = createServer(async (req, res) => {
    if (req.method !== 'POST' || !req.url?.endsWith('/chat/completions')) {
      res.writeHead(404).end();
      return;
    }
    let body = '';
    for await (const chunk of req) body += chunk;
    const messages = (JSON.parse(body).messages ?? []) as unknown[];
    const id = `fixture-${Date.now()}`;
    const send = (payload: object) => {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      sse(res, payload);
    };
    const skillResult = toolResult(messages, 'read-reviewer-skill');
    const skillText = resultText(skillResult?.content);
    if (hasToolCall(messages, 'read')
      && (!skillResult || toolErrors.has('read-reviewer-skill')
        || !skillText.includes('# Reviewer execution contract'))) {
      res.writeHead(502).end('reserved agd-reviewer core skill read failed');
      return;
    }
    const evidenceResult = toolResult(messages, 'read-evidence');
    const bashResult = toolResult(messages, 'shell-diagnostic');
    const writeResult = toolResult(messages, 'write-report');
    if (hasToolCall(messages, 'read', 'evidence.txt')
      && (!evidenceResult || toolErrors.has('read-evidence')
        || !resultText(evidenceResult.content).includes(SENTINEL))) {
      res.writeHead(502).end('evidence read failed or lacked sentinel');
      return;
    }
    if (hasToolCall(messages, 'bash')
      && (!bashResult || toolErrors.has('shell-diagnostic')
        || !resultText(bashResult.content).includes(SHELL_MARKER))) {
      res.writeHead(502).end('bash diagnostic failed or lacked marker');
      return;
    }
    if (hasToolCall(messages, 'write')
      && (!writeResult || toolErrors.has('write-report') || !resultText(writeResult.content).trim())) {
      res.writeHead(502).end('report write failed');
      return;
    }
    if (!hasToolCall(messages, 'read')) {
      send({ id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', tool_calls: [{ index: 0, id: 'read-reviewer-skill', type: 'function',
          function: { name: 'read', arguments: JSON.stringify({
            path: 'skill://agd-reviewer/references/commands.md',
          }) } }] },
        finish_reason: 'tool_calls' }] });
    } else if (!hasToolCall(messages, 'read', 'evidence.txt')) {
      send({ id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', tool_calls: [{ index: 0, id: 'read-evidence', type: 'function',
          function: { name: 'read', arguments: JSON.stringify({ path: 'evidence.txt' }) } }] },
        finish_reason: 'tool_calls' }] });
    } else if (!hasToolCall(messages, 'bash')) {
      send({ id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', tool_calls: [{ index: 0, id: 'shell-diagnostic', type: 'function',
          function: { name: 'bash', arguments: JSON.stringify({
            command: `printf '${SHELL_MARKER}\\n'`,
          }) } }] },
        finish_reason: 'tool_calls' }] });
    } else if (!hasToolCall(messages, 'write')) {
      const evidence = toolText(messages);
      const source = sourceId(messages);
      if (!source || !evidence.includes(SENTINEL) || !evidence.includes(SHELL_MARKER)) {
        res.writeHead(502).end('fixture did not receive sentinel tool evidence');
        return;
      }
      send({ id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', tool_calls: [{ index: 0, id: 'write-report', type: 'function',
          function: { name: 'write', arguments: JSON.stringify({
            path: 'report-draft.json', content: reportFor(evidence, SHELL_MARKER, source),
          }) } }] },
        finish_reason: 'tool_calls' }] });
    } else {
      if (!writeResult || writeResult.isError === true || !resultText(writeResult.content).trim()) {
        res.writeHead(502).end('fixture did not receive write result');
        return;
      }
      send({ id, object: 'chat.completion.chunk', choices: [{ index: 0,
        delta: { role: 'assistant', content: 'Native skill, file, shell, and report-write evidence was verified.' },
        finish_reason: 'stop' }] });
    }
  });
  const listening = Promise.withResolvers<void>();
  server.listen(0, '127.0.0.1', listening.resolve);
  await listening.promise;
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fixture endpoint did not bind');
  pi.registerProvider('agd-fixture', {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    api: 'openai-completions',
    apiKey: 'local-only',
    models: [{ id: 'reviewer', name: 'AGD local-only reviewer', reasoning: false,
      input: ['text'], supportsTools: true,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32768, maxTokens: 2048 }],
  });
  pi.on('session_shutdown', () => server.close());
}
