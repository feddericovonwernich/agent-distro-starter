type StreamEvent = { type: string };
type Stream = AsyncIterable<StreamEvent> & { result(): Promise<unknown> };
type OuterStream = Stream & { forwardLocalWorkFrom(stream: Stream): void; trackLocalWork(work: Promise<void>): Promise<void>; push(event: StreamEvent): void; end(result: unknown): void; fail(error: unknown): void };
type StreamFn = (...args: unknown[]) => Promise<Stream>;
type Model = { provider: string; id: string };
type Settings = { getModelRole(role: string): unknown };
type Registry = { getApiKey(...args: unknown[]): Promise<unknown>; getAvailable(): Model[] };
type Manager = { getSessionId(): string; flush(): Promise<void>; close(): Promise<void> };
type Session = { prompt(text: string): Promise<void>; beginDispose(): void; dispose(): Promise<void> };
export type Native = {
  ai: { streamSimple: StreamFn; AssistantMessageEventStream: new () => OuterStream };
  core: { Agent: new (options: unknown) => unknown };
  sdk: { Settings: { init(options: unknown): Promise<Settings> }; discoverAuthStorage(agentDir: string, options: unknown): Promise<unknown>; ModelRegistry: new (...args: unknown[]) => Registry; SessionManager: { create(cwd: string, sessionDir: string): Manager }; AgentSession: new (options: unknown) => Session };
  resolver: { resolveCliModel(options: unknown): { model: Model | undefined } };
  telemetry: { initTelemetryExport(): Promise<void>; isTelemetryExportEnabled(): boolean; createTelemetryExportConfig(options: unknown): unknown; flushTelemetryExport(): Promise<void> };
};
export interface Broker { rpc(op: string, args: unknown): Promise<unknown>; reserveModelCall(): Promise<void>; settleModelCall(): Promise<void>; persistNativeSessionId(id: string): Promise<void>; persistModel(id: string): Promise<void>; finalResult(): Promise<{ reportId: string }>; }
export interface ReviewerInput { cwd: string; agentDir: string; sessionDir: string; reviewRunId: string; receipt: { deadline_epoch_ms: number; omp_version: string }; modelSelector: string | null; fixtureModel?: Model; reviewPrompt: string; broker: Broker; native: Native; }

function reviewerTools(broker: Broker) {
  return new Map(['reviewer_excerpt', 'reviewer_search', 'reviewer_submit'].map(name => [name, {
    name, label: name, description: name,
    parameters: { type: 'object', properties: name === 'reviewer_search' ? { query: { type: 'string' } } : name === 'reviewer_excerpt' ? { source_id: { type: 'string' } } : { report: { type: 'object' } }, required: name === 'reviewer_search' ? ['query'] : name === 'reviewer_excerpt' ? ['source_id'] : ['report'], additionalProperties: false },
    execute: async (_id: string, args: unknown) => { const text = await broker.rpc(name, args); if (typeof text !== 'string') throw new Error('invalid broker text'); return { content: [{ type: 'text', text }], details: {} }; },
  }]));
}

function cappedStreamFn(native: StreamFn, broker: Broker, ai: Native['ai']): StreamFn {
  return async (...args) => {
    await broker.reserveModelCall(); let settled = false;
    const settle = async () => { if (!settled) { settled = true; await broker.settleModelCall(); } };
    try {
      const inner = await native(...args); const outer = new ai.AssistantMessageEventStream(); outer.forwardLocalWorkFrom(inner);
      void (async () => { try { for await (const event of inner) { if (event.type === 'done' || event.type === 'error') await settle(); outer.push(event); } await settle(); outer.end(await inner.result()); } catch (error) { await settle(); outer.fail(error); } })();
      return outer;
    } catch (error) { await settle(); throw error; }
  };
}

function resolveModel(input: ReviewerInput, settings: Settings, registry: Registry): Model {
  if (input.fixtureModel) return input.fixtureModel;
  const selector = input.modelSelector;
  if (selector === null && settings.getModelRole('default') === undefined) throw new Error('configured default model role is unavailable');
  const resolved = input.native.resolver.resolveCliModel({ cliProvider: undefined, cliModel: selector ?? 'default', modelRegistry: registry, settings });
  if (!resolved.model || !resolved.model.provider || !resolved.model.id) throw new Error('configured reviewer model is unavailable');
  return resolved.model;
}

export async function runReviewer(input: ReviewerInput, streamFn: StreamFn = input.native.ai.streamSimple): Promise<{ reportId: string }> {
  const { sdk, core, ai, telemetry } = input.native; await telemetry.initTelemetryExport();
  const settings = await sdk.Settings.init({ cwd: input.cwd, agentDir: input.agentDir, readOnly: true, overrides: { 'compaction.enabled': false, 'compaction.midTurnEnabled': false, 'compaction.autoContinue': false, 'compaction.asyncEnabled': false, 'compaction.idleEnabled': false, 'retry.enabled': false, 'retry.maxRetries': 0, 'retry.modelFallback': false, 'retry.usageAwareFallback': false, 'advisor.enabled': false, 'autolearn.enabled': false, 'autolearn.autoContinue': false } });
  const auth = await sdk.discoverAuthStorage(input.agentDir, { cwd: input.cwd, settings });
  const registry = new sdk.ModelRegistry(auth, undefined, { settings }); const model = resolveModel(input, settings, registry);
  await input.broker.persistModel(`${model.provider}/${model.id}`);
  const manager = sdk.SessionManager.create(input.cwd, input.sessionDir); await input.broker.persistNativeSessionId(manager.getSessionId());
  const tools = reviewerTools(input.broker); const capped = cappedStreamFn(streamFn, input.broker, ai);
  const exporter = telemetry.isTelemetryExportEnabled() ? telemetry.createTelemetryExportConfig({ attributes: { purpose: 'background-skill-review', review_run_id: input.reviewRunId }, captureMessageContent: 'full' }) : undefined;
  const agent = new core.Agent({ initialState: { model, tools: [...tools.values()], systemPrompt: [input.reviewPrompt] }, sessionId: manager.getSessionId(), deadline: input.receipt.deadline_epoch_ms, telemetry: exporter, getApiKey: (...args: unknown[]) => registry.getApiKey(...args), streamFn: capped });
  const session = new sdk.AgentSession({ agent, sessionManager: manager, settings, modelRegistry: registry, toolRegistry: tools, sideStreamFn: capped, advisorStreamFn: capped, memoryEnabled: false, skills: [], skillsReloadable: false, disableExtensionDiscovery: true, scoutAllowedBySpawnPolicy: false });
  try { const packet = await input.broker.rpc('packet', {}); if (typeof packet !== 'string') throw new Error('invalid packet text'); await session.prompt(packet); await manager.flush(); return await input.broker.finalResult(); }
  finally { session.beginDispose(); await session.dispose(); await manager.close(); await telemetry.flushTelemetryExport(); }
}
