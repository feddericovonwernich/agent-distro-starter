import { expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { runReviewer } from '../lib/agd-reviewer';
import { call, scriptedStream, startBridge } from './reviewer-fixture';

test('native loop persists actual bridge report', async () => {
  const bridge = await startBridge();
  try {
    const observed: string[] = [], messages: unknown[][] = [];
    const result = await runReviewer({ ...bridge.hello.input, broker: bridge.broker, native: bridge.native }, scriptedStream(bridge.native.ai, [[call('reviewer_search', { query: 'failure' })], [call('reviewer_submit', { report: bridge.hello.report })], []], observed, messages));
    expect([...observed].sort()).toEqual(['reviewer_excerpt', 'reviewer_search', 'reviewer_submit']);
    const first = JSON.stringify(messages);
    expect(first).toContain(bridge.hello.report.findings[0].citations[0]);
    expect(first).toContain('failure');
    expect(first).toContain('catalog');
    expect(first).not.toContain('OUTSIDE_PACKET_SENTINEL');
    await expect(bridge.broker.rpc('reviewer_excerpt', { source_id: 'outside' })).rejects.toThrow();
    expect(readFileSync(join(bridge.hello.reportDir, `${result.reportId}.json`), 'utf8')).toContain('native fixture');
    const binding = JSON.parse(readFileSync(join(bridge.hello.input.sessionDir, '..', 'binding.json'), 'utf8'));
    expect(binding.native_session_id).toBeString();
    const transcript = readdirSync(bridge.hello.input.sessionDir).find(name => name.endsWith(`_${binding.native_session_id}.jsonl`));
    expect(transcript).toBeString();
    expect(readFileSync(join(bridge.hello.input.sessionDir, transcript!), 'utf8')).toContain('"type":"session"');
    bridge.finish(result);
  } finally { await bridge.close(); }
}, 30000);
