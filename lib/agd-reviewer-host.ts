import { createInterface } from 'node:readline';
import { runReviewer, type Broker, type Native, type ReviewerInput } from './agd-reviewer';

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity })[Symbol.asyncIterator]();
const first = await lines.next();
if (first.done) throw new Error('missing reviewer input');
const input = JSON.parse(first.value);
let sequence = 0;
const rpc = async (op: string, args: unknown) => {
  const id = `${++sequence}`;
  process.stdout.write(`${JSON.stringify({ id, op, args })}\n`);
  const reply = await lines.next();
  if (reply.done) throw new Error('broker closed');
  const value = JSON.parse(reply.value);
  if (value.id !== id || !value.ok) throw new Error(value.error || 'broker refused');
  return value.value;
};
const urls = input.urls;
const native: Native = { ai: await import(urls.ai), core: await import(urls.core), sdk: await import(urls.sdk), telemetry: await import(urls.telemetry), resolver: await import(urls.resolver) };
const broker: Broker = { rpc, reserveModelCall: async () => { await rpc('reserve', {}); }, settleModelCall: async () => { await rpc('settle', {}); }, persistNativeSessionId: async id => { await rpc('session', { id }); }, persistModel: async id => { await rpc('model', { id }); }, finalResult: async () => rpc('result', {}) as Promise<{reportId: string}> };
const result = await runReviewer({ ...input.input, native, broker } as ReviewerInput);
process.stdout.write(`${JSON.stringify({ complete: true, result })}\n`);
