import { createInterface } from 'node:readline';
import { Readable } from 'node:stream';

function reportResult(value: unknown): { reportId: string } {
  if (!value || typeof value !== 'object' || !('reportId' in value) || typeof value.reportId !== 'string') throw new Error('invalid report result');
  return { reportId: value.reportId };
}

export async function startBridge() {
  const proc = Bun.spawn(['uv', 'run', '--python', '3.12', '--with', 'pytest', 'tests/reviewer_bridge.py'], { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' });
  const body = new Response(proc.stdout).body;
  if (!body) throw new Error('bridge stdout is unavailable');
  const lines = createInterface({ input: Readable.fromWeb(body), crlfDelay: Infinity })[Symbol.asyncIterator]();
  const stderr = new Response(proc.stderr).text();
  const next = async () => {
    const row = await lines.next();
    if (row.done) throw new Error(`bridge EOF: ${await stderr}`);
    return row.value;
  };
  const hello = JSON.parse(await next());
  const native = { ai: await import(hello.aiUrl), core: await import(hello.coreUrl), sdk: await import(hello.sdkUrl), telemetry: await import(hello.telemetryUrl) };
  const rpc = async (op: string, args: unknown) => {
    const id = crypto.randomUUID();
    proc.stdin.write(`${JSON.stringify({ id, op, args })}\n`);
    const reply = JSON.parse(await next());
    if (reply.id !== id || !reply.ok) throw new Error(reply.error || 'bridge refusal');
    return reply.value;
  };
  return { hello, native, broker: {
    rpc,
    reserveModelCall: async () => { await rpc('reserve', {}); },
    settleModelCall: async () => { await rpc('settle', {}); },
    persistNativeSessionId: async (id: string) => { await rpc('session', { id }); },
    finalResult: async () => reportResult(await rpc('result', {})),
  }, close: async () => { proc.stdin.end(); await proc.exited; await lines.return?.(); } };
}

export function scriptedStream(ai: { AssistantMessageEventStream: new () => { push(event: unknown): void; end(result: unknown): void } }, turns: unknown[][], observed: string[], messages: unknown[][]) {
  let turn = 0;
  return (model: { api: string; provider: string; id: string }, context: { tools: { name: string }[]; messages: unknown[] }) => {
    observed.splice(0, observed.length, ...context.tools.map(tool => tool.name));
    if (!turn) messages.push(structuredClone(context.messages));
    const stream = new ai.AssistantMessageEventStream();
    const content = turns[turn++] ?? [];
    const message = { role: 'assistant', content, api: model.api, provider: model.provider, model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, stopReason: content.length ? 'toolUse' : 'stop', timestamp: Date.now() };
    queueMicrotask(() => { stream.push({ type: 'done', reason: message.stopReason, message }); stream.end(message); });
    return stream;
  };
}

export const call = (name: string, arguments_: unknown) => ({ type: 'toolCall', id: crypto.randomUUID(), name, arguments: arguments_ });
