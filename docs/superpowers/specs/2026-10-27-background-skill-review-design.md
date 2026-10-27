# Background skill review — approved design

## Status and scope

The conversation has approved this design. This file is awaiting user review before an implementation plan is written. It authorizes neither implementation, scheduler/timer installation, unattended model execution, skill/source edits, publication, landing, nor changes to a running instance.

The feature produces evidence-backed **proposals** for improving an existing skill or creating a new skill. It never changes reviewed sources/skills, profiles, task lifecycle records, or non-review runtime state. It may write only explicitly owned reviewer configuration, checkpoints, native reviewer output, evidence/review/proposal records, notification receipts, and operator-requested triage records. A human disposition is not permission to edit: each accepted proposal needs a separate, explicitly approved implementation task.

The feature is an observer of completed worker work and related central decisions/human feedback. It is not a general task scheduler, transcript query API, skill auto-author, source optimizer, or recursive reviewer.

## Existing baseline and non-assumptions

AGD currently has:

- task UUID incarnations, runner receipts, native OMP JSONL sessions, worker observations and task-local inbox acknowledgements;
- durable supervision events through `supervision.emit`, `pending`, and `acknowledge`, with the central OMP extension waking an available central session;
- frozen worker-skill snapshots/profiles, but no reviewer-specific read-only worker API or proposal store;
- a central watcher which is owned by, and ends with, the central OMP session;
- OMP native skill discovery and a usable OMP extension surface.

Current `worker launch` is task/worktree-oriented. It is not a reviewer-run API. Current worker-scoped ownership checks are accidental-misuse guards, not OS sandboxing. No existing AGD generic scheduler, transcript query API, reviewer session contract, or proposal lifecycle API is assumed.

OMP native JSONL is authoritative source evidence. Langfuse indexed traces are an optional discovery accelerator; absence, delay, or failure of Langfuse must not block native-backed review. The existing OpenTelemetry Collector remains an observability transport only: it does not read JSONL or execute reviewer logic. Neither existing OMP CLI flags nor extension/tool configuration are claimed sufficient until the implementation acceptance smoke establishes the actual enabled-tool inventory.

## Goals

1. Review bounded, selected excerpts from completed worker work for skill-improvement and new-skill opportunities.
2. Retain durable evidence, provenance, counterexamples, limitations, and proposal/triage history.
3. Permit manual runs and configurable nightly, calendar, or interval runs without a central UI remaining alive.
4. Prevent overlapping review runs per private home; coalesce triggers and recover safely after a crash.
5. Deliver batch notifications when the central is available without conflating notification acknowledgement, disposition, or edit authority.
6. Let a proposal distinguish a recurring pattern from one high-impact uncovered workflow without inventing frequency.

## Non-goals and hard boundaries

- No automatic skill/source edits, commits, installation, publishing, merging, or approval.
- No automatic implementation task creation from a proposal.
- No report-triggered reviewer run, self-approval, or recursive self-review. A separately requested retrospective review may include reviewer sessions only when explicitly selected.
- No general filesystem, shell, package-install, Git, network, SQL, generic transcript-query, or unrestricted tool access for the reviewer model.
- No new custom SDK, provider, transcript database/index, Collector deployment, or resident AGD scheduler service.
- No promise of perfect secret redaction. Withheld or redacted material is explicit coverage loss, not evidence that the material was harmless.
- No assumption that an event acknowledgement resolves a decision, accepts/rejects a proposal, or authorizes edits.

## Configuration and activation

A proposed private-home reviewer configuration has one narrow consumer: the reviewer commands. It is inert until an operator explicitly invokes a manual run or explicitly enables a native Linux user timer. `agd init` does not install, start, or re-enable a timer.

Proposed configuration shape (exact storage/file naming is an implementation decision, but unknown keys must fail):

```json
{
  "schedule": {
    "mode": "calendar",
    "timezone": "UTC",
    "calendar": "02:00",
    "interval_hours": null
  },
  "limits": {
    "max_candidates": 20,
    "max_input_bytes": 262144,
    "max_model_calls": 20,
    "max_output_bytes": 65536,
    "max_runtime_seconds": 1800
  },
  "review_model": null
}
```

These are proposed defaults, not approved cost or quality guarantees. A null `review_model` inherits the native configured OMP model. The runner resolves and records the concrete model identifier before a call; inability to resolve it refuses the run before any call. This does not introduce a telemetry API or promise exact token/cost accounting. `max_candidates` bounds selected distinct episode candidates per run. `max_input_bytes` and `max_output_bytes` are UTF-8 bytes for the whole run's broker-supplied input packet(s) and persisted structured report output respectively. `max_model_calls` bounds native reviewer model invocations, including tool-continuation invocations. `max_runtime_seconds` is the runner's elapsed wall-clock deadline. These bounds do not guarantee a USD ceiling or cover hidden provider HTTP retry/transport attempts.

`timezone` and `calendar` are required only for `calendar` mode and must be null for other modes. `interval_hours` is required only for `interval` and must be null otherwise; it is a positive whole number. The runner rejects every other combination. Manual invocation is available in every mode, so the calendar default is the approved nightly-plus-manual behavior without a second mode.

Supported schedule modes are:

- `manual-only`: no timer; only explicit `reviewer run` calls;
- `interval`: an integer-hour timer interval, such as 12 hours, plus manual runs;
- `calendar`: one local wall-clock time and IANA timezone, plus manual runs. This is the default.

Calendar scheduling is delegated to a native Linux user timer, not parsed by a bespoke cron/calendar engine. The design's chosen DST contract is: skip a nonexistent local wall-clock occurrence and run once for a repeated occurrence. Before relying on that behavior, implementation must verify that the selected native timer provides it; otherwise it must refuse calendar enablement rather than silently substitute semantics. Missed/offline executions run one bounded catch-up at the next enabled timer activation, then calculate the next due time from the configured calendar/interval rather than from run completion. Thus a slow run does not drift the schedule.

Enabling or installing the user timer is a separately explicit, target-scoped approval because it creates background model activity. Manual review works with no timer. A timer starts a bounded one-shot runner, not a new resident scheduler; the runner exits after its run. The user timer must not be installed or started implicitly by configuration, initialization, update, or central-session startup.

## Candidate selection and evidence collection

### Eligibility and cutoff

A candidate episode is a worker task/incarnation that has ended, including failed or otherwise exited tasks; successful completion is not the only eligible result. For a final worker-transcript review, the trusted evidence broker/reader requires evidence that the selected incarnation ended before the candidate cutoff. Tasks still actively executing are excluded from that final pass.

For each run, the broker records a durable cutoff and constructs one bounded evidence allowlist before model calls. It contains selected candidate episodes plus only explicitly selected historical/counterexample/prior-proposal context available at that cutoff. Each packet records the exact source IDs and source versions for:

- task/incarnation records, native worker OMP JSONL sessions, observations, runner receipts, and report/ready state when present;
- associated central decisions/answers and human feedback;
- selected historical episodes, counterexamples, and prior proposal revisions.

This permits comparison across independent tasks without an unlimited history query. Retried runs of the same incident are not independent examples. A report states its episode denominator/coverage and identifies a finding as a single high-impact incident or a recurrence only when the distinct selected evidence supports that conclusion.

Active central episodes may be linked only when their relationship is explicit in retained records. A missing link is `unknown`, not inferred from timing or prose. Reviewer-originated sessions are excluded by default. Explicit retrospective mode changes that eligibility rule but still cannot schedule or approve itself.

New human feedback, a new native source-session version, or any material evidence after cutoff does not rewrite a completed review. It creates a later evidence revision/follow-up candidate. Deduplication must never suppress materially new evidence.

### Evidence reader and Langfuse discovery

The reviewer runner contains the separate trusted evidence broker/reader. It reads native JSONL as data, never as executable instructions, and records source path/identity, byte range or line span, content hash, collection time, and whether content was withheld, redacted, unavailable, or complete. It does not alter or deploy the OpenTelemetry Collector.

Langfuse indexed traces may identify useful spans efficiently. If Langfuse is late or unavailable, the broker selects from native JSONL directly and records the resulting coverage. Late Langfuse material can create a bounded follow-up; it cannot induce infinite pending/retry behavior. No reviewer-specific Langfuse key is given to the model or introduced by this design.

### Scoped evidence broker

The reviewer model receives only an evidence packet and narrowly scoped broker tools. The packet has stable IDs, a cutoff, selected excerpts, span references, skill inventory/snapshot provenance, and explicit omissions. It never contains provider credentials.

The proposed broker may expose only:

1. read a cited excerpt by its packet/span ID;
2. bounded search over the packet's pre-authorized evidence set; and
3. submit a structured review report.

Search/fetch arguments are validated against the packet allowlist, cutoff, byte budget, and call budget. They cannot name arbitrary paths, SQL, URLs, Langfuse keys, task IDs outside the packet allowlist, or a general query language. The reviewer receives no shell, edit, write, install, publish, Git, MCP, repository-discovered extension, or unrestricted filesystem tool.

A trusted OMP extension/runner owns the provider credential in its native process environment and captures the result. Credentials are never inserted into the model prompt. The report-submit capability validates citations against the broker allowlist and validates proposal shape, but cannot prove the report's semantic truth. Tool restriction is a model-tool boundary, not a same-UID OS sandbox.

Before claiming this boundary works, an acceptance smoke must launch the real installed OMP version with the reviewer extension/configuration and prove the enabled inventory includes only the intended broker tools, while extra built-ins, shell, MCP, repository extensions, and discovered extras are denied. This is required runtime evidence, not a claim based only on prospective CLI flags.

## Reviewer instruction, native persistence, and skill provenance

The reviewer instruction bundle is frozen for each run and recorded by exact bytes hash/version, like worker skill snapshots. Each report identifies the resolved model identifier, installed OMP version, observer instruction-bundle hash, and every skill snapshot/catalog version consulted.

Reviewer execution uses shared native OMP exporter/session infrastructure but has a distinct reviewer-run native JSONL and artifact namespace under `AGD_HOME`. It is always persisted even when telemetry is disabled. When existing Collector/Langfuse transport is enabled, it receives reviewer observability annotated with `purpose=background-skill-review` and `review_run_id`; it does not receive a new Collector deployment or reviewer-specific Langfuse key. Native conversation IDs remain unchanged, and the runner must not invent ordinary task IDs. Current APIs do not yet provide this reviewer adapter/extension; implementation must add it rather than misrepresenting `worker launch` as support.

Before proposing a change, the broker supplies the available relevant-skill inventory and provenance. The reviewer must compare an opportunity with existing skills rather than manufacture a duplicate. A proposal is one of:

- **improve existing skill**: names the existing skill/version and the bounded suggested change; or
- **new skill**: establishes an uncovered workflow and gives intended scope, triggers, draft content, prerequisites, and validation limits.

An assigned worker-skill manifest establishes only provisioned bytes, not that a skill was loaded, used, or caused a failure. Skill usage/version/cause is `unknown` unless the selected evidence proves it. A new-skill proposal is inert: it does not materialize a skill bundle, alter profiles, or become visible in any skill-discovery root. A singular high-impact uncovered workflow may be proposed with its impact evidence and uncertainty; a recurrence count is reported only when evidence supports it.

### Retrospective self-review

Retrospective self-review is an explicit `reviewer run` scope with selected closed reviewer-run IDs, a frozen cutoff, and an allowlist that excludes the current run and all later runs. It uses the same read/report-only tools and cannot approve, trigger, alter its own policy, or schedule another review. Default candidate selection excludes all reviewer-run native output.

## Proposal and report model

A review record and its evidence record are immutable. A proposal has a stable identity and immutable revisions. A separate mutable triage record holds the current disposition and appends an audited history; it never rewrites review/evidence/revision bytes.

Minimum proposed immutable review fields:

```json
{
  "schema": 1,
  "review_id": "uuid",
  "candidate_id": "stable-cutoff-hash",
  "purpose": "background-skill-review",
  "cutoff": "timestamp",
  "sources": [{"task": "id", "incarnation": "uuid", "session": "native-id", "spans": ["evidence-id"]}],
  "coverage": {"complete": false, "withheld": [], "unavailable": [], "late_followup_possible": false},
  "observer": {"omp_version": "observed-version", "model": "configured-id", "bundle_hash": "sha256"},
  "skill_provenance": [],
  "proposals": []
}
```

Each proposal revision contains: stable proposal ID; revision ID; kind (`existing-skill` or `new-skill`); evidence citation IDs; counterexamples considered; uncertainty/coverage limitations; suggested resolution; suggested validation; deduplication basis; and, for new skills, draft content, intended triggers/scope, prerequisites, and validation limits. Proposals without actual evidence references are refused.

The runner assigns a persistent proposal UUID when it first records a proposal. Its deduplication fingerprint is a canonical tuple of proposal schema, kind, target (`existing skill ID` or normalized uncovered-workflow key), normalized finding/problem, and applicability key; it excludes the growing evidence set and proposed wording. The revision ID hashes the persistent proposal UUID, evidence basis, and proposed change. A matching fingerprint permits an equivalent re-observation to append evidence or a revision without erasing history, but it is not a promise of semantic deduplication. If a match is uncertain or a fingerprint collision describes a distinct finding, the runner assigns a distinct proposal UUID rather than suppressing it. Materially new evidence may create a new revision of a rejected/deferred/superseded proposal only with an explicit statement of the prior disposition and new basis; it never auto-accepts or overwrites a prior human outcome.

Minimum mutable triage entry:

```json
{
  "proposal_id": "stable-id",
  "current": "accepted|rejected|deferred|superseded",
  "history": [{"at": "timestamp", "status": "deferred", "reason": null, "operator": {"uid": 1000, "identity": "observed-local-cli-identity"}}]
}
```

A disposition reason is optional, though useful for rejection/defer/supersede. The CLI records the observed local operator identity/UID as local evidence; it does not take an unverified model-supplied `actor` value. `accepted` means only that the human accepts the proposal as a candidate for a separate task; it performs no edit and creates no task. `superseded` references the replacement proposal/revision. Triage tools must not let the reviewer approve or alter its own proposal state.

## Durability, notifications, and recovery

Private durable data is separate from scheduler state. Under the effective `AGD_HOME`, the reviewer-owned namespace contains native reviewer JSONL/artifacts managed by the native OMP lifecycle, finalized immutable evidence/review/proposal-revision records, and mutable proposal-triage records; a sibling reviewer scheduler namespace contains only the dedicated lock, checkpoint, trigger/catch-up state, and runner receipt. Existing `state/events` and `state/events-handled` retain notification receipts. These namespaces must not be conflated with ordinary task directories or task IDs.

Old sessions, evidence, reports, proposals, and triage history are retained; this design includes no pruning policy.

The scheduler has one dedicated reviewer lock per private home. It must not hold the existing home mutation lock while waiting on a model or network. One active run is allowed; concurrent manual/timer triggers are recorded and coalesced. The active runner checkpoints candidate discovery and each persisted review/proposal result. Stable candidate/evidence/revision IDs let a crash retry resume safely, but the system must not claim exactly-once external/model effects.

Once one or more proposals are durably persisted, the runner emits a single batch-summary durable event using the existing event pattern. The existing central extension can wake the central when available; if it is offline, the event remains pending. Event identity derives from the batch/report revision identity so retry/reconcile coalesces safely. The summary identifies report/proposal IDs and counts but does not substitute for report access.

Notification acknowledgement moves only the event receipt to handled. It does not accept, reject, defer, supersede, create a task, modify a skill, or grant permission to do so. Proposal dispositions use separate explicit commands/records; implementation uses a separately approved ordinary task.

## Proposed operations

These are minimal conceptual CLI operations, not a requirement for a generic CLI framework:

- `reviewer run`: manually run one bounded collection/review cycle.
- `reviewer timer enable|disable|status`: inspect or explicitly install/manage the native per-user timer; enable requires separate approval.
- `reviewer status`: show lock/runner/checkpoint/schedule state without triggering a run.
- `reviewer reports list|show`: read immutable reports/evidence coverage and proposals.
- `reviewer proposals list|show`: inspect a proposal and all revisions/history.
- `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded [--reason FILE] [--replacement ID]`: append a human disposition only.

The exact command spelling, config location, event kind, and record paths are implementation details. They must follow AGD's existing effective-private-home, atomic JSON, locking, ownership, and durable-event conventions. No command may accept a report disposition as authority to edit a skill or source.

## Failure and coverage behavior

- A missing, malformed, or inaccessible source produces a coverage entry; it never becomes a negative finding.
- Selected redaction/withholding is identified by category/span metadata when safely possible; no promise is made that all secrets were detected.
- Input/output/call/runtime limits stop additional collection or calls and yield a coverage-limited report or checkpointed follow-up as appropriate, never silent truncation.
- A failed model call preserves the candidate checkpoint and exposes a retryable failure; it does not create a fabricated report.
- Crash recovery uses stable records and reuses the frozen cutoff. It does not silently reconsider later human feedback under an earlier review.
- A candidate with no grounded proposal may be recorded as reviewed with explicit coverage, rather than producing an empty or invented recommendation.

## Implementation acceptance criteria

An implementation is not complete until it demonstrates all of the following using disposable private homes/projects and real installed OMP where stated:

1. The real reviewer launch inventory grants only the intended broker tools; shell, filesystem mutation, generic read/query, MCP, repo extensions, task spawning, install, publish, and extra discovered tools are denied.
2. The broker cannot fetch outside a packet allowlist/cutoff and rejects invalid citations/report shape; provider credentials do not appear in the reviewer prompt/transcript packet.
3. Native JSONL-backed collection works with Langfuse discovery available and unavailable; late material creates one bounded follow-up/coverage state rather than an infinite pending loop.
4. A reviewer run writes its own native JSONL/artifacts and `review_run_id` metadata under the reviewer namespace even with telemetry disabled. When existing observability transport is enabled, purpose/run metadata is present without changing OpenTelemetry Collector roles or adding a reviewer Langfuse key.
5. Manual review works with no user timer; configuration alone starts no model work. Timer installation/enabling requires the explicit approval boundary, survives central UI absence, catches up once after offline time, avoids schedule drift, and does not overlap runs.
6. Runner crash/fault recovery preserves checkpoints and evidence/history without duplicate destructive effects or an exactly-once claim.
7. A central-offline batch event remains durable; an available central sees it; acknowledging it changes neither proposal disposition nor edit authority.
8. Deduplication preserves rejected/deferred/superseded history, appends equivalent new evidence safely, permits a materially new basis to resurface explicitly, and preserves a distinct finding on uncertain merge.
9. Existing-skill and new-skill proposals both require evidence/counterexamples/uncertainty/validation. A proposed new skill remains absent from discovery roots and profiles until a separately approved implementation task changes it.
10. Reviewer reports do not auto-trigger a reviewer, alter their own policy/disposition, create tasks, or apply an accepted proposal. An explicit retrospective run proves its cutoff excludes its current/later reviewer runs and cannot self-approve.
11. Associated central decisions/human feedback are linked only when retained evidence proves the relationship; active/unknown relations and withheld evidence are reported as coverage limitations.

## Handoff

The approved design is now recorded. User review of this written file is required before invoking `writing-plans` or implementing any part of it. Any later implementation plan must preserve the existing worker/task lifecycle and use focused additions rather than reinterpreting current worker APIs as reviewer support.
