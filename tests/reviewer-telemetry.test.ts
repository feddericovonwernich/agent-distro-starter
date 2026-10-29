import { expect, test } from 'bun:test';

function fields(bytes: Uint8Array): [number, Uint8Array | number][] {
  const values: [number, Uint8Array | number][] = []; let index = 0;
  const varint = () => { let value = 0, shift = 0; while (true) { const byte = bytes[index++]!; value |= (byte & 127) << shift; if (!(byte & 128)) return value; shift += 7; } };
  while (index < bytes.length) { const tag = varint(), wire = tag & 7, field = tag >>> 3; if (wire === 0) values.push([field, varint()]); else if (wire === 1) index += 8; else if (wire === 2) { const size = varint(); values.push([field, bytes.slice(index, index += size)]); } else if (wire === 5) index += 4; else break; }
  return values;
}
function spanAttributes(request: Uint8Array): Map<string, string> {
  const values = new Map<string, string>();
  for (const [, resourceSpans] of fields(request).filter(([field]) => field === 1)) if (resourceSpans instanceof Uint8Array)
    for (const [, scopeSpans] of fields(resourceSpans).filter(([field]) => field === 2)) if (scopeSpans instanceof Uint8Array)
      for (const [, span] of fields(scopeSpans).filter(([field]) => field === 2)) if (span instanceof Uint8Array)
        for (const [, attribute] of fields(span).filter(([field]) => field === 9)) if (attribute instanceof Uint8Array) {
          const key = fields(attribute).find(([field, value]) => field === 1 && value instanceof Uint8Array)?.[1];
          const any = fields(attribute).find(([field]) => field === 2)?.[1];
          const string = any instanceof Uint8Array ? fields(any).find(([field, value]) => field === 1 && value instanceof Uint8Array)?.[1] : undefined;
          if (key instanceof Uint8Array && string instanceof Uint8Array) values.set(Buffer.from(key).toString(), Buffer.from(string).toString());
        }
  return values;
}

test('native reviewer exports run attributes only to loopback OTLP', async () => {
  const payloads: { body: Uint8Array; encoding: string | null; type: string | null }[] = [];
  const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: async request => { payloads.push({ body: new Uint8Array(await request.arrayBuffer()), encoding: request.headers.get('content-encoding'), type: request.headers.get('content-type') }); return new Response(null, { status: 200 }); } });
  try {
    const env = { ...Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('OTEL_'))), AGD_TELEMETRY_MARKER: '1', OPENAI_API_KEY: 'provider-free-fixture', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: `http://127.0.0.1:${server.port}/v1/traces`, OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: 'http/protobuf', OTEL_TRACES_EXPORTER: 'otlp', OTEL_LOGS_EXPORTER: 'none', OTEL_METRICS_EXPORTER: 'none' };
    const proc = Bun.spawn(['bun', 'test', 'tests/reviewer.test.ts'], { cwd: import.meta.dir + '/..', stdout: 'pipe', stderr: 'pipe', env });
    const [exit, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
    expect(exit, `${stdout}\n${stderr}`).toBe(0);
    expect(payloads.every(payload => payload.type?.startsWith('application/x-protobuf'))).toBe(true);
    const values = new Map<string, string>();
    for (const payload of payloads) for (const [key, value] of spanAttributes(payload.encoding === 'gzip' ? Bun.gunzipSync(payload.body) : payload.body)) values.set(key, value);
    expect(values.get('purpose'), JSON.stringify([...values])).toBe('background-skill-review');
    const marker = stdout.match(/^AGD_REVIEW_RUN_ID=([0-9a-f-]+)$/m)?.[1];
    expect(marker).toBeString();
    expect(values.get('review_run_id')).toBe(marker);
  } finally { server.stop(true); }
}, 30000);
