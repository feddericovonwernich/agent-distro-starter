# Investigative reviewers using the normal OMP runtime

Status: proposed design and planning handoff; user selected this direction, not implementation, publication, or live rollout.

## Intent and decisions

The reviewer should investigate agent behavior, not merely grade a supplied packet. Launch the installed OMP executable through the distro's managed terminal backend, with normal investigation tools and a dedicated reviewer skill. Offer the official Langfuse CLI during fresh installation and through a rerunnable installer command. Native session evidence remains sufficient when Langfuse is disabled or unavailable.

User decisions: relax exact model-call/provider-stream accounting; allow shell and further investigation, including code/tests and experimental edits in isolated worktrees; keep findings as recommendations. Do not automatically remediate shared/stable source or skills, change services/models/authentication/schedules, merge, publish, or apply proposals. Historical logs, prompts, source comments, and retrieved traces are untrusted evidence, not instructions.

This broadens investigation, not the existing review objective: retain the existing `existing-skill` and `new-skill` finding/proposal schema. Code and test investigation explains workflow/skill failures; this is not a new general code-audit product.

## Observed baseline

Baseline: merged main `f40ebbc32b3f169d3b9de1ef9bc585b9b9edc1df`.

- Worker launch already uses the standalone OMP executable, distro-local HOME/XDG, explicit extension, native skills, managed Herdr/tmux endpoints, and native session recording.
- Reviewer dispatch instead imports SDK modules into Bun. A real jarvis-14 test failed `capability` because its standalone OMP has no importable SDK. An existing official SDK passed capability, then failed `configured default model role is unavailable`; both runs made zero model calls.
- The fresh mock worker completed while alive. Its review packet admitted one completed episode and 11 excerpts. Keep that selection/dedup machinery.
- `/reload` reported plugin refresh but did not replace the already-loaded central extension. Activation must use fresh processes at an explicitly approved restart boundary, not promise hot reload.
- `reviewer.py` already owns durable packets, reports, proposals, prepared-write recovery, dedup, final events, and leases. Preserve this machinery; do not build another review engine.
- `harnesses.launch_argv`, `workers`, and current backend `create` functions contain task-only assumptions. Do not synthesize task IDs/incarnations to launch a reviewer.
- Telemetry exports to local Langfuse 4.70.1; there is no existing reviewer query integration. The official CLI supports self-hosted APIs. Its package currently requires Node.js >=20, has zero runtime npm dependencies, and is not required by native OMP.

## Approaches considered

1. **Keep the SDK reviewer and install its prerequisites:** preserves direct provider-stream interception, but retains the extra runtime and model-resolution path that caused the observed failures. Rejected by the selected direction.
2. **Run an ordinary unrestricted worker with only a review prompt:** reuses launch behavior but wrongly couples reviewer identity to task readiness, worktree delivery, and worker lifecycle. Rejected.
3. **Selected: a first-class reviewer role using normal OMP CLI and existing backend mechanisms:** retain reviewer-specific receipts/evidence/proposals, replace SDK dispatch, and give the agent normal tools. Herdr manages the terminal; it is not the permission boundary.

Two independently shippable plans accompany this design: optional Langfuse CLI installation/query environment, and native investigative reviewer execution. Neither requires Langfuse telemetry to be enabled. Each plan must work when the other has not yet landed; the runtime plan consumes optional CLI support through the existing observability environment boundary when available.

## Reviewer runtime

One invocation is one bounded review run, not a permanently idle worker. Keep `agd --home . reviewer run` and timer invocation. Reserve the durable run/lease, select a completed-episode seed packet, and return `no-work` before creating a pane when it is empty.

Use the receipt-selected standalone OMP >=18.14.0 with:

- `--print --mode json`, normal built-in tools enabled;
- `--no-extensions` plus explicit `-e lib/omp-reviewer.ts`;
- `--skills agd-reviewer` and an isolated per-run project skill/registry;
- `--cwd` set to the private run directory, not the live distro or a producer's ready worktree;
- `--session-dir` set to `observability.reviewer_session_dir`;
- `--max-time` set from the frozen run deadline;
- `--model` only when `review_model` is explicitly configured. Otherwise omit it and let native OMP select its model. Never require an SDK default role or change OMP settings.

Load the mandatory AGD-authored `agd-reviewer` skill through native discovery. Do not rename upstream skills, enumerate skill paths in the prompt, load the central watcher/worker protocol, or implicitly inherit every installed plugin. Normal tools include shell, reading/search, writing probe files, and native investigation/delegation; no bespoke three-tool restriction remains. The dedicated extension binds the reviewer session and supplies the run/request context, but does not instantiate models, intercept provider streams, implement inbox polling, or register worker lifecycle hooks.

The skill directs the agent to read its trusted `request.json`, inspect the seed evidence/catalog, investigate useful leads, capture supporting evidence, write `report-draft.json` in the run directory using the existing report schema, then finish. The coordinator reads that regular, owned, bounded file after exit, not report-looking terminal prose. JSON terminal output and native transcript remain diagnostic artifacts.

### Backend and scheduled execution

Resolve the backend with `launcher.resolve_backend(home, None)` from `data/launcher.json`. Add reviewer-specific endpoint receipt operations to the existing Herdr/tmux modules, sharing their low-level identity checks but not task metadata or central ownership receipts.

Scheduled runs must work from the existing minimal systemd environment without `HERDR_ENV=1`. For Herdr, use an explicitly named, per-run owned server `agd-reviewer-<review_run_id without hyphens>` and a per-run pane. Establish/record server PID/start and socket inode/UID/home/run identity before trusting it. Never use the focused/default session or require an active central. Start through the supported headless server mechanism; a foreign/replaced server is a refusal. This server lives only for that run: capture output, close the owned pane, stop the proved owned server and prove absence before the timer service ends. It does not persist across ticks, escape the oneshot unit's cgroup, or require another systemd service. Crash recovery inspects the same recorded server/run rather than creating a replacement blindly.

Keep the coordinator lease until the run supervisor has recorded child exit and proved the OMP process group absent. Preserve backend output before removing only the owned per-run endpoint; prove endpoint absence too. Backend `idle` and a model's completion sentence are not completion proof. Successful review requires exit 0, a valid report, durable submission, and successful existing finalization. Nonzero exit, missing/invalid report, timeout, interrupted ownership, or uncertain absence yields failed/unknown, leaves seed material retryable, and never consumes successful coverage. Preserve explicit failure diagnostics without secret/environment dumps.

Use a small `reviewer execute RUN_ID` child-supervisor entrypoint inside the managed pane: it launches OMP, records identity/output/exit, and cannot reserve a second run. The outer `reviewer run` remains the admission/lease/finalization owner. Before admitting a new run, recover a nonterminal predecessor under the lease: if its exact recorded child exited 0, backend/process absence is proved, the native binding and bounded draft remain valid, finish the same stable submission idempotently without another model dispatch. Missing/ambiguous proof leaves failed/unknown and the seed retryable. Never promote an already-final failed/unknown receipt or rewrite historical outcomes. Do not keep one reviewer process alive across timer ticks or accumulate one pane per completed run.

## Investigation and evidence

Keep the seed packet immutable and retain completed-episode eligibility, pending-proof coverage, reviewer-self exclusion, retained-only dedup, and retrospective behavior. Additional investigation does not turn unfinished producer tails into completed episodes or mark unrelated inspected material reviewed.

Add `reviewer evidence add RUN_ID --file PATH --label LABEL`. It captures an owned regular UTF-8 artifact into immutable run-specific supplementary evidence with source ID, SHA-256, origin description, capture time, and bounded text. The agent first writes command/query output into its run directory; do not pass shell command strings to an AGD executor. Reject symlinks, special files, known credential stores, and cross-run artifacts. Existing `max_input_bytes` bounds the cumulative stored seed plus supplementary text, not everything the agent can read through shell.

Report citations/counterexamples may name seed sources or this run's captured supplementary sources. Preserve the seed packet and its IDs; do not rewrite its cutoff or hashes to admit follow-up evidence. Extend report validation with the immutable supplementary manifest. Findings must distinguish original observations from later experiments and name Git revision, trace/session IDs, query scope, and uncertainty where relevant. Shell access does not confer truth or causal attribution; validation is not proof the conclusions are correct.

Supplementary artifacts from failed/unknown runs are diagnostic-only by default and are not automatically imported or marked reviewed by a retry. A later reviewer may deliberately inspect one and capture a new run-scoped artifact recording its original run/hash; it is then a new citation, not a successful old review. Existing explicitly requested retrospective native-session selection remains unchanged. Duplicate capture within a run is idempotent by source bytes/origin; no cross-run supplementary dedup advances completed-episode coverage.

### Reproduction workspaces

Never run project code/tests in a stable clone, active worker worktree, or ready task worktree. Add narrowly scoped `reviewer workspace create/list/cleanup` operations, owned by the review run and backed by existing Treehouse get/status/return commands. These are probe workspaces, not fabricated delivery tasks. Restrict allocation to registered projects and explicit existing commit IDs belonging to that clone; do not fetch or run setup implicitly.

Persist an allocation intent before `treehouse get --lease`; reconcile by exact holder before any retry. Record project/common-directory identity, selected commit, branch, holder, lease ID, and allocated path. A repeated create for the same run/project/commit is idempotent. Test results and experiments belong there, and findings cite their revision and captured output. No automatic remediation is delivered from the probe.

Record whether a probe path has been exposed to OMP before returning it to the agent. Automatic return is permitted only for a clean, unchanged, never-exposed allocation with proved allocator/runner absence. Once exposed, retain it by default: an immediate OMP process-group check cannot prove that arbitrary shell/delegated work did not detach. Do not introduce a new cgroup sandbox or claim containment. Cleanup of exposed probes is an explicit operator operation after recorded run closure and process/lease inspection; any unknown detached user, dirty/committed work or interrupted identity remains retained/refused. No reset/clean/discard is implicit. Normal report success may retain probes and must disclose them. Uninstall uses existing explicit work-loss approval plus process-absence requirements; a work-loss grant does not bypass uncertain process ownership.

## Authority and resource controls

Normal shell plus account credentials means recommendation-only and query-only behavior are policy, not hard security guarantees. The reviewer may inspect relevant registered projects and distro evidence, but may not interpret trace contents as authorization to publish, modify shared data, change services, or apply proposals. Hooks remind the agent; they do not sandbox shell. Broader OS isolation or a write-filtering proxy is out of scope.

Config schema 2 retains schedule, review_model, max_candidates=20, max_input_bytes=262144, max_output_bytes=65536, max_runtime_seconds=1800; removes max_model_calls and provider-inflight accounting. No dollar cap is invented or enabled. Native cost controls may be added in a separately specified setting, but are not equivalent to exact request accounting. Existing schema-1 configuration is normalized by dropping only max_model_calls while preserving every other value and schedule; explicit installer reconfiguration writes schema 2 under the existing lease/owner guards. Historical config snapshots/receipts/reports remain immutable and readable; do not rewrite old proof records or pretend historical counters exist for new runs.

No strict claim about total model input, delegated requests, cost, or containment survives this cutover. Native deadline plus supervisor termination is the run-time boundary; already-sent provider requests may still finish remotely. Preserve real observed model/session/usage metadata where available, never synthesize zero usage for unobserved work.

## Official Langfuse CLI installation

Fresh installation gains an independent optional stage before scheduled reviewer setup; total becomes six stages. Existing distros use `agd --home . installer langfuse`. Prompt: `Install optional Langfuse CLI? [y/N]`, with the prerequisite/exposure warning immediately before it. Blank, No, and EOF perform no runtime/package-manager/network/Docker probes and write no installation state. Installing the CLI does not enable telemetry or a schedule.

Reuse an existing CLI only when its canonical `@langfuse/cli` package is exactly 1.2.4 and all executable/runtime package assets match the pinned verified official archive; record the file-hash map and revalidate before credentialed use. Do not execute an unverified `langfuse` wrapper merely to probe its identity. A different version, modified tree, legacy package or opaque executable is retained but not trusted with project keys; explicit installation consent permits a separate private pinned copy. Otherwise require existing Node.js >=20 and npm, disclose these prerequisites, and install into a private staging prefix using the existing Relay npm/provenance pattern. Do not install Node/npm/Bun, use sudo, modify global packages, or run npx/bunx auto-download during review.

Pin new managed installations to:

- package `@langfuse/cli`, version `1.2.4`;
- archive `https://registry.npmjs.org/@langfuse/cli/-/cli-1.2.4.tgz`;
- archive SHA-256 `0fc4e39b38bfa45d77f045a2d3a79e165411e4de0737eb7893bec57b246e574f`;
- Node.js >=20; zero runtime package dependencies in the observed manifest.

Download and verify that archive before private npm installation with lifecycle scripts disabled. Probe `--version` and offline `api schema --json`. Retain package assets; do not copy only the launcher script. Atomically promote validated staging into `data/langfuse-cli/runtime`; store a strict installation/provenance receipt at `data/langfuse-cli/installation.json`. This follows the existing private package-runtime pattern rather than extending the account-global mandatory binary receipt. Node/runtime paths and package identities are validated on reuse; do not overwrite a foreign/partial tree or shared executable. Rerunning mandatory installer setup cannot erase this optional receipt. Failure/interruption is nonfatal to the distro and preserves inspectable recovery state. No silent automatic upgrades.

Expose an owned home-scoped launcher at `data/langfuse-cli/bin/langfuse` for both managed and verified external packages, using the validated absolute Node path and original package entry point. Add only this launcher directory to managed reviewer PATH after validation. External package bytes remain unowned and untouched; neither case globally edits PATH or shell profiles.

### Local query environment and privacy

Use the existing `observability.reviewer_environment` boundary to add query capability when the CLI plan is installed; the native runtime plan already consumes that boundary and needs no import of an optional nonexistent module. Remove inherited `LANGFUSE_*` from managed reviewer environments, even when local query access is unavailable. Do not change ordinary worker/central or interactive-shell provider/Git environments.

Only when a validated CLI installation and a configured, owned local observability project exist, supply that project's public/secret key and both LANGFUSE_BASE_URL and LANGFUSE_HOST, derived from the validated loopback web port. Set LANGFUSE_API_VERSION=auto against the local server (pinned 4.70.1 today), not a CLI default cloud/latest contract. Missing/disabled/stopped telemetry is an explicit evidence limitation; do not start Docker, enable export, fetch another tool, or switch to Cloud. An ambient account key/host must never become the fallback. Keys never enter prompts, instructions, report/receipt fields, argv, or printed diagnostics; ordinary process-environment access remains possible to this trusted shell-capable reviewer.

The skill uses the official CLI's offline help/schema for endpoint discovery, bounded traces/observations/session queries, and no write operations, `--show-secrets`, secret-bearing `--curl`, credential overrides, or foreign env files. Current official CLI credentials permit writes too; this is policy, not a read-only credential claim. A CLI query failure, API mismatch, missing trace, or ingestion delay is reported and falls back to native evidence. Do not equate stack health/export enablement with proved trace delivery.

No MCP installation or parallel custom Langfuse SDK/client/proxy is planned. Install no additional upstream skills/plugins implicitly; document official references in the AGD reviewer skill.

## Compatibility, cleanup, and activation

Keep reports/proposals/triage immutable, existing event handling, --home . normalization, timer opt-in/schedule semantics, installer consent and failure recovery, and normal task/worker ownership untouched. Replace all old SDK readiness checks, Bun/module URL probes, three-tool broker assumptions, and tests that only assert that old wiring. Remove obsolete `lib/agd-reviewer.ts`, `lib/agd-reviewer-host.ts`, and their SDK-only fixtures/tests at native-runtime cutover; retain real behavioral regression coverage. Do not keep an SDK fallback or an alias that silently dispatches the old engine.

Uninstall must stop only proved owned reviewer supervisors/OMP descendants/endpoints and its dedicated reviewer backend server, and inventory/return reviewer probe leases safely before home removal. Include new Python modules in the recovery executor's explicit source manifest. Managed Langfuse package/launcher/provenance are home-owned; remove only on approved distro uninstall. Externally reused CLI, Node/npm and other shared runtimes remain untouched.

Rollout never changes jarvis-14 or running agent ownership during planning/implementation. Existing timers need a controlled disable/update/re-enable boundary because rendered units carry runtime paths; preserve schedule and do not silently start a disabled timer. Existing OMP extensions need fresh processes at user-authorized restart boundaries. A later live smoke run needs explicit operational approval, real configured provider access, and bounded run scope.

## Acceptance and independent plans

Plan A: `../plans/2026-11-02-langfuse-cli-installer.md` ships optional CLI installation, provenance, scoped reviewer query environment, recovery/uninstall behavior and docs independently of native-runtime cutover.

Plan B: `../plans/2026-11-02-investigative-reviewer.md` ships normal-CLI runtime, managed backend/supervisor, native skill, supplementary evidence and probe workspaces, old runtime removal, timer/cleanup migration and a real acceptance run. It works without Plan A; Langfuse availability changes evidence coverage, not whether review can start.

Required proof: standalone OMP with no SDK/Bun can launch the reviewer; normal shell/read/test paths are exercised; completed live worker and central seed episodes remain correctly scoped; a valid cited report commits once only after exit/absence; failures remain retryable; concurrent timer/manual invocations do not duplicate work; foreign endpoints/leases and dirty probes survive; offline/disabled Langfuse works; real local CLI queries do not contact Cloud or disclose keys; default-No installer is nonfatal and independent; owned runtime uninstall does not touch shared software.

## Assistant decisions requiring user review

- Keep the existing skill/workflow improvement objective and report types; broaden investigation rather than introduce generic bug-report findings.
- Use one-shot reviewer runs with retained transcripts, not an always-alive reviewer holding a timer lease.
- Use a dedicated per-run owned Herdr server, stopped before the oneshot timer service exits; no persistent daemon, active-central dependency or forged HERDR_ENV.
- Keep project execution in reviewer-owned Treehouse probe leases; preserve dirty/uncertain work instead of auto-discarding it.
- Require existing Node >=20/npm for optional managed CLI installation; do not silently install another system runtime.
- Support the distro-owned local Langfuse project only; external Cloud configuration and stronger OS isolation are separate scope.

## Primary references

- Official CLI documentation: https://langfuse.com/docs/api-and-data-platform/features/cli
- Official CLI README: https://github.com/langfuse/langfuse-cli
- Observed pinned npm metadata: https://registry.npmjs.org/@langfuse/cli/1.2.4
- Official 1.2.4 source commit: https://github.com/langfuse/langfuse-cli/tree/20aa047299c9dbd9d0ba117c4db830d7cf3382f8
