# Managed distro observability

## Status and scope

The design sections are approved; this written specification awaits user review before implementation planning. It authorizes neither Docker installation, service startup, credential generation, network access, model calls, nor changes to a running distro. It does not select dependency version pins: implementation must select compatible versions from verified release artifacts and record the chosen immutable versions/digests before provisioning.

The feature provides two independent observability records for the OMP central and OMP workers:

1. **Native transcript retention** under the effective private `AGD_HOME`. Native OMP JSONL and its companion artifact directories are the authoritative local record.
2. **Optional managed local telemetry**: one distro-owned Compose stack containing Langfuse and an OpenTelemetry Collector. It receives OMP OTLP traces, logs and metrics when explicitly enabled; only traces are sent onward to Langfuse. Langfuse is a trace UI/store, not the authority for transcript retention.

Cursor remains unsupported for transcript/telemetry claims until its existing authenticated, native transcript-bound acceptance gate succeeds. This feature adds no general harness abstraction, remote backend, custom queue, external telemetry backend, or automatic retention/deletion policy.

## Goals

- Retain full native OMP sessions and artifacts inside `AGD_HOME`, independently of telemetry availability.
- Associate every OMP worker session directory with its task and immutable worker incarnation, preserving every relaunch's history.
- Make central OMP transcript placement deliberate: a managed launcher creates future central sessions in an AGD-owned location; a bare `omp` start remains unmanaged and is warned about.
- Offer one opt-in, distro-owned local Compose stack per private home with unique ownership, loopback-only published endpoints, protected generated credentials, and internal-only databases.
- Export OMP traces, logs and metrics to the Collector; the Collector exports traces to Langfuse and archives all three signals locally.
- In disabled mode, do not start a stack and configure newly launched agents with no OTLP export, while native transcript retention continues. Existing agents remain stale until restart; a failed stop/disable is reported as a runtime failure, never as an observed disabled stack.
- Fail open for agent work while recording clear degraded observability state, queue state and stale-process configuration status.

## Non-goals

- No retroactive relocation, import, or mutation of an already-running central OMP session.
- No claim that OTLP/Langfuse is an exactly-once, complete or redacted transcript archive.
- No promise that unsupported attachment/reference shapes, provider-hidden reasoning, malformed/non-JSON values, downstream payload limits, or rejected telemetry are preserved in Langfuse.
- No automatic Docker/Compose installation, privilege elevation, global configuration change, service upgrade, retention deletion, volume deletion, or worktree-cleanup deletion.
- No Langfuse OTLP logs or metrics ingestion. Langfuse's supported Fern OpenTelemetry configuration accepts traces; logs and metrics remain local Collector file archives.
- No custom disk queue, replay daemon, secondary telemetry backend, or credential exposure to workers.

## Existing evidence and compatibility boundary

Installed OMP 18.3.2 has native JSONL session persistence, `--session-dir`, `--continue`, `--resume`, and `--no-session`. A no-model local probe imported OMP's real `SessionManager` and default `FileSessionStorage`, created a session in an explicit directory, appended synthetic user/assistant/tool-call/tool-result messages, reopened the file, and observed one version-3 JSONL with the expected message chain. This proves native storage/write/reopen, not a live model session.

OMP's native full OTLP capture is environment-controlled by `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Full capture serializes available system instructions, converted provider input messages, final assistant output messages, tool-call arguments, tool results, usage/timing/cost attributes, and error attributes. It is not equivalent to lossless raw provider wire capture. Full serializer behavior and downstream Collector/Langfuse maximum payload behavior must be acceptance-tested with deliberately large synthetic content before any product claim says all content is searchable.

No end-to-end Langfuse deployment, Collector-to-Langfuse export, live model export, or real credentialed telemetry run has been performed. Those are implementation acceptance work, not evidence already held by this design.

## Data ownership and layout

All observability runtime data is private under the effective `AGD_HOME`, follows existing home-path safety conventions, and is ignored by Git. The implementation must create private directories with owner-only permissions.

```text
AGD_HOME/
  data/
    observability/
      config.json                    # durable desired mode and selected immutable image/package evidence
      secrets.json                   # generated service credentials, never displayed in normal status
      central/omp/                   # managed central OMP session JSONL + same-basename artifact directories
      tasks/<task>/omp/<incarnation>/ # worker native session JSONL + artifacts, never reused by a relaunch
  state/
    observability/
      status.json                    # last inspected desired/runtime/export state and stale configuration evidence
      compose/                       # generated Compose/Collector configuration and ownership receipt
      collector/queue/               # Collector persistent sending queue only
      archives/traces/               # Collector OTLP trace file archive
      archives/logs/                 # Collector OTLP log file archive
      archives/metrics/              # Collector OTLP metric file archive
```

Exact directory names may be refined during implementation only if every relationship above remains explicit, task/incarnation identity remains one-to-one, and all locations stay under `AGD_HOME` and Git-ignored. Compose database/blob data is held in named, distro-owned persistent volumes. The ownership receipt records the project name, private home identity and exact volume names; commands refuse a collision with another home instead of adopting it by label alone.

`config.json` contains non-secret desired state, selected immutable version/digest evidence, owner/home identity, network/project identity and public loopback endpoint choices. `secrets.json` is generated once, owner-only, excluded from command output, prompts, worker environments, task records, native transcripts and telemetry attributes. The implementation never accepts credentials through a command argument.

Archives, queues, native sessions, artifacts, databases and blobs persist across restart, stop, disable, task cleanup and worktree cleanup. Despite its `state/` path, `state/observability/collector/queue` and every `state/observability/archives/` directory are durable records and are excluded from disposable-state cleanup. There is **no default age deletion, truncation, pruning or volume removal**. Destructive retention operations are outside this feature and require future explicit design and approval.

## Native OMP transcript behavior

### Central

`agd observability central` is the explicit managed central launcher. It resolves the effective `AGD_HOME`, reads the desired observability state, and launches OMP in the distro root with an explicit `--session-dir` under `data/observability/central/omp`. It preserves ordinary OMP arguments that do not undermine the managed home/session directory. It does not attach to, move or rewrite a prior session.

When telemetry is enabled, this launcher makes a bounded ensure-start/health attempt for the managed stack, then execs OMP whether that attempt succeeds or fails. It supplies the Collector's loopback OTLP trace, log and metric endpoints plus the required OMP full-content environment; it does not supply Langfuse credentials to OMP. A failed ensure-start/health attempt emits an actionable warning and records `degraded`, but does not stop agent work. When telemetry is disabled, it removes/sanitizes ambient `OTEL_*` export and content-capture variables before execing OMP, so a disabled managed central cannot accidentally export to a shell-provided endpoint. It leaves native JSONL/artifacts enabled.

A bare `omp` invoked directly from the distro remains valid but unmanaged: the extension/status surface must present a durable, actionable warning that its central transcript location and export configuration are outside AGD control. It must never claim the session is retained under AGD or attempt retroactive relocation. The warning is informational and must not stop user work.

A pre-existing central process cannot change OTLP exporter initialization: OMP initializes exporters at process startup. `status` therefore reports a configuration generation and marks a central process stale when its observed/recorded generation differs from the desired configuration. Toggle changes require central restart through the managed launcher to take effect.

### Workers

An OMP worker launch allocates and persists a transcript binding before creating its backend endpoint:

```text
(task ID, worker incarnation UUID, harness=omp, native session directory)
```

The worker OMP argv always receives that directory through `--session-dir`. The task record is the authoritative association; a lightweight receipt may duplicate it for recovery, but neither backend pane IDs nor OMP-generated session filenames replace the binding. OMP may materialize a session JSONL lazily, so a valid binding can initially contain no JSONL; status must distinguish `directory allocated`, `native session not materialized`, and `native files present`.

When telemetry is enabled, the worker receives the same Collector trace, log and metric endpoint configuration and full-content setting as the managed central. Worker launch reuses the distro-owned stack; it does not invoke Docker, provision credentials, or obtain Langfuse access. If the Collector is unavailable, the worker launch records/warns `degraded` and continues agent work.

A relaunch creates a new worker incarnation and a new directory. It never overwrites, merges or uses `--continue` against a prior incarnation directory. Existing AGD relaunch semantics remain workspace/task-state recovery; native chat resume is not promised. If a future operator explicitly resumes a worker's native OMP session, it must select the recorded incarnation directory and must not change task-incarnation authorization.

Cursor worker launches receive no native transcript retention or OTLP configuration until the existing verification gate establishes a supported native contract. Their task state remains recorded normally.

## Managed observability commands

The implementation adds one narrowly scoped command group, conceptually `agd observability`, with these operations:

| Operation | Behavior |
|---|---|
| `enable` | Validates installed Docker and Compose availability without installing either; atomically stages protected config/secrets/generated Compose and Collector files; generates credentials; starts this home's stack; waits for bounded health checks; initializes the Langfuse organization/project; then records desired enabled generation. A failure records the precise failed stage and observed runtime state, returns an error, and never reports a successfully enabled/healthy stack. |
| `start` | Requires successfully enabled configuration; verifies exact private ownership and immutable image evidence; starts only this home's Compose project; waits for bounded health checks; reports Collector and Langfuse loopback endpoints without credentials. It never changes global Docker configuration. |
| `stop` | Stops only the recorded Compose project while preserving all named volumes, queue and archives. Native transcript retention continues. A failure leaves runtime state `degraded`/`unknown`, not `stopped`. |
| `status` | Read-only inspection of desired state, observed stack/container health, endpoint reachability, queue/archive paths and sizes, most recent exporter error, central generation/staleness, and per-worker binding metadata. It never displays secrets or transcript content. |
| `disable` | Requests stop of the recorded stack and changes desired state to disabled without deleting config, secrets, archives, queues, database/blob volumes, native sessions or artifacts. If stop cannot be confirmed, status retains the failed/unknown observed runtime state rather than reporting runtime disabled. New managed central/worker processes receive no OTLP export configuration; current processes are reported stale until restarted. |
| `central [OMP_ARGS...]` | The managed central launcher described above. When enabled it makes its bounded ensure-start/health attempt; when disabled it does not start Docker. |

Commands refuse missing Docker/Compose, invalid generated files, ownership mismatch, unsafe filesystem state, unavailable loopback port, incompatible selected pin, or uncertain container identity with a clear error and without destructive recovery. A stopped/disabled stack does not block task creation, worker launch or agent work.

`enable`/`start` select a unique Compose project and network name derived from a stable home identifier, never from a human project/task label alone. Every externally reachable service port binds only to `127.0.0.1`; PostgreSQL/ClickHouse/Redis-style internal data services have no published host ports and communicate only on the private Compose network. Implementation selects the exact Langfuse topology compatible with its verified pinned release, but must maintain this boundary.

## Compose and credential model

The managed stack comprises Langfuse, its required internal persistence services, and one OpenTelemetry Collector. All service images are immutable version/digest references selected and verified during implementation. No floating tag is accepted after enablement. A later version change is a distinct explicit upgrade operation outside this design.

The Collector is the only component given Langfuse ingestion credentials. OMP processes send OTLP traces, logs and metrics only to the Collector's loopback endpoints; workers and the central do not receive Langfuse keys, database credentials or Collector administrative credentials. Langfuse bootstrap credentials, public/secret project keys, database credentials and generated internal passwords remain in `secrets.json`/Compose secret mechanisms and are never included in normal CLI output.

Enablement initializes exactly one Langfuse organization and one Langfuse project for this distro home. Its stable project identifier is recorded in private config and used as a resource/span attribute. A second distro receives distinct Compose project/network/volumes, Langfuse credentials and Langfuse project identity; it cannot attach to or stop the first distro's stack.

## OTLP routing and span identity

### Resource attributes

Every managed central and worker OMP process receives a resource envelope that identifies the distro without putting task-specific attributes into a global process where they do not apply:

```text
service.name=agent-distro
agd.home_id=<private stable home UUID>
agd.distro_id=<stable distro instance identifier>
agd.langfuse_project=<private Langfuse project identifier>
agd.role=central|worker
agd.session_scope=central|task-incarnation
```

The worker additionally receives `agd.task_id` and `agd.incarnation`; the central must not claim a task/incarnation. The implementation must preserve OMP's native conversation/session identifier rather than replacing it. When that identifier is present, the Collector's standard attribute transform maps it to `langfuse.session.id` and maps `agd.home_id`, `agd.distro_id`, `agd.role`, `agd.task_id` and `agd.incarnation` to `langfuse.trace.metadata.*` on each relevant span, while preserving OMP trace/span IDs. It must not invent a session ID when OMP did not emit one. Langfuse therefore groups one native OMP conversation into one Langfuse session, not one central process or worker incarnation into one session: `/new` can produce several sessions during either lifetime. The task/incarnation metadata links those sessions to their worker history.

The managed environment must include full content capture and the Collector trace, log and metric endpoints only when enabled. It must use OMP's native OTLP exporters, not custom spans or copied prompts. It must not emit credentials, private filesystem paths beyond the intended identifiers, or transcript content as ad-hoc resource attributes.

### Collector pipelines

The Collector has three independent signal routes:

```text
traces:  OMP -> OTLP receiver -> Langfuse export route -> persistent sending queue -> Langfuse
                              \-> local OTLP trace file archive
logs:    OTLP receiver -> local OTLP log file archive only
metrics: OTLP receiver -> local OTLP metric file archive only
```

The OTLP receiver's HTTP/gRPC acknowledgement, receiver-owned in-memory state and any in-memory batch are **not** a durable-queue acknowledgement and do not prove either fan-out branch ran. The trace fan-out has no transaction/atomic acknowledgement spanning the local archive and Langfuse route. The design therefore does not promise an acknowledgement guarantee for one branch because the other branch accepted or persisted data.

The Langfuse trace route keeps OMP-native trace/span IDs unchanged through retry. Its exporter uses Collector `file_storage` persistent sending queue and bounded retries/backoff configured by the selected verified Collector release. A trace becomes queued durably only after that exporter has committed it to its `file_storage` directory; it is not called queued merely because OMP sent it or the receiver acknowledged it. The queue directory is under `state/observability/collector/queue`, distinct from every archive directory, and survives stack restarts.

The trace pipeline must not configure an asynchronous batch/pre-queue processor ahead of this persistent queue. This eliminates an additional implementation-created buffering window; unavoidable OMP, receiver and exporter in-memory windows remain accepted pre-queue loss windows. The implementation must verify the selected Collector components' ordering and acknowledgement behavior rather than infer it from configuration names.

The local file exporters are append-preserving archives, not replay sources and not substitutes for queues. Implementation must verify the selected exporter writes without truncating prior archive records and configure separate trace, log and metric archive paths. It must not combine append mode with rotation if that release documents them as incompatible; in that case it must use an append-safe non-rotating archive configuration. Archive paths must never overlap `file_storage`. The implementation must not configure archived trace JSON as an automatic re-export input, build a custom replay loop, or imply that file output is fsync-durable. Trace archival begins only after the Collector receives the signal; it cannot capture events lost before the Collector.

Operational logs and metrics emitted by OMP are retained in their local archive files only. This does not promise capture of arbitrary agent-process stdout/stderr or terminal/backend output. The status command reports their archive paths and latest Collector exporter errors, but does not parse/archive them into Langfuse and does not add another observability product.

## Delivery, failure and durability semantics

Telemetry is observability, never a task-lifecycle dependency. Agent and worker work continue when the Collector, Langfuse, queue, archive or Compose stack is unhealthy. The managed launcher and AGD status record/report degradation; they do not block ready/landing decisions or retry through a custom side channel.

The accepted durability boundary is intentionally limited:

- Data may be lost before an OMP process sends telemetry, while the receiver/exporter holds it only in memory, or before `file_storage` commits it. Receiver acknowledgement is not delivery or durable queue evidence.
- Disk exhaustion, Collector queue exhaustion/corruption, permanent exporter rejection, invalid payload, network failure and shutdown can lose telemetry. Permanent rejections are surfaced but are not promised retries; transient errors use only the selected Collector's configured retry policy.
- Retries of a persistently queued item preserve its trace/span identity. The design makes neither an at-least-once guarantee nor an absence-of-duplicates promise until an implementation acceptance test proves the selected exporter/Langfuse behavior.
- The local file exporter archive is not a replay queue and is not promised to be fsync-durable.
- Native JSONL/artifacts are independent of all of these telemetry failures. They remain authoritative local records but have their own ordinary filesystem failure limits.

Status exposes, without secrets or content: desired enabled/disabled mode; config generation; current central generation/staleness; stack health; Collector endpoint health; queued item/byte counts only when Collector exposes post-`file_storage` values; archive path/size; latest transient and permanent/rejected exporter errors; and whether each worker binding has materialized native files. It uses `healthy`, `degraded`, `disabled`, `stopped`, `unknown`, and `stale-restart-required` rather than reporting delivery success from a receiver acknowledgement or queued send.

Stopping, disabling, worker stop/relaunch, task cleanup, landing and worktree cleanup preserve the records above. If a worker is relaunched, the old incarnation's telemetry may complete later; it remains associated with the old incarnation and must not be re-labeled as the new one.

## Security and privacy

Full native sessions and full OTLP content are intentionally sensitive. They can include user prompts, system prompts, model responses, tool arguments, tool results, images, attachment data, source content, secrets entered into prompts or tool outputs, and error text. This design does not promise redaction, secret detection or safe sharing. Operators must treat `AGD_HOME` observability data, Collector files, persistent queue, Compose volumes and Langfuse local data as private records.

The stack binds published endpoints to loopback, isolates databases on its internal network, restricts credentials to their necessary components, and does not put Langfuse credentials into agent/worker environments. Those controls reduce accidental exposure; they are not an OS sandbox against the same Unix user or a malicious local process.

Ambient telemetry environment is untrusted configuration for disabled mode. Managed launchers must clear export endpoints, exporter selections, protocols, headers and content-capture controls that could activate OMP export before supplying the managed enabled envelope. Unmanaged bare OMP sessions are outside that guarantee and visibly warned.

## Verification and acceptance

Implementation must add focused, deterministic tests and isolated smoke coverage. It must not run live services, model calls or credentialed probes against the user’s running distro without separate explicit approval.

1. **Disabled silence and native retention:** in a disposable home, launch central/worker OMP paths with observability disabled; prove no Compose project/container is started and no OTLP export is configured, while explicit native session directories/JSONL/artifacts persist.
2. **Both managed roles:** exercise disposable central and worker OMP launches; assert their native session directories and resource/span attributes bind to central versus exact task/incarnation and do not leak Langfuse credentials.
3. **Full-content contract:** feed synthetic system/user/assistant/tool-call/tool-result/error payloads, including intentionally large tool results and image/attachment/reference shapes, through the native serializer and Collector. Record exactly which content reaches full trace attributes, which known unsupported shapes are omitted, and the selected Collector/Langfuse size/rejection boundaries. Do not claim losslessness beyond the observed contract.
4. **Signal routing and persistence boundary:** assert traces route to Langfuse export plus local trace archive; logs and metrics route only to their local archives; no Langfuse log/metric exporter or asynchronous pre-queue batch processor is configured; archive paths do not overlap `file_storage`; and archive configuration preserves existing records across restart. Prove that receiver acknowledgement/in-memory buffering is not reported as queued or delivered.
5. **Outage/recovery identity:** simulate a Langfuse outage after durable `file_storage` enqueue, inspect transient-error/queue/degraded status, restart Collector, restore Langfuse, and prove the resent trace/span identity equals the pre-outage identity. Separately exercise a permanent rejection and queue/disk-full failure, verify they remain visible/degraded and do not block OMP work or trigger automatic queue deletion. Record whether duplicates occur; do not convert an unverified result into an exactly-once or at-least-once claim.
6. **Lifecycle preservation:** prove stop, disable, central restart, worker stop/relaunch, task cleanup and worktree cleanup preserve volumes, archives, queue and prior incarnation native transcripts. Confirm disabled configuration takes effect only in newly launched/restarted agents and stale status is explicit.
7. **Two-distro isolation:** run two disposable homes concurrently; prove distinct Compose project/network/volumes/credentials/Langfuse project identities, loopback endpoint allocation and refusal of cross-home ownership/adoption.

A disposable end-to-end Langfuse acceptance run is required before declaring the managed stack operational. It must include no production credentials and must preserve its test evidence without importing it into a user home.

## Completion boundary

Completion means the requested native retention and optional managed OMP telemetry behavior are implemented, documented and verified against the acceptance matrix above; private paths are ignored by Git; disabled and degraded semantics are observable; credentials do not reach OMP workers; and records survive the declared lifecycle operations.

Completion does not mean every OMP content type, arbitrary attachment, downstream payload size or provider reasoning format is losslessly represented in Langfuse. It does not add a retention/deletion feature, remote service, log/metric backend, automatic upgrade, Docker installer, telemetry replay system, or support claim for Cursor.
