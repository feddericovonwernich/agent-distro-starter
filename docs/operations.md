# Local operations

All commands below are `bin/agd` from the distro checkout. Set `AGD_HOME` to an absolute private-home path, or pass `--home /absolute/home`, to target another initialized home. An active central exports its owner token to tool commands; another shell receives a read-only refusal. This is an accidental-misuse guard, **not** an OS sandbox.

## Guided central lifecycle

The installer initializes a fresh home and writes private launcher and dependency receipts. Start its selected central from an ordinary terminal:

```sh
bin/agd start
```

`start --backend tmux|herdr` is an invocation-only override. Without it, a proven active central backend wins over the saved launcher backend. A ready receipt attaches only after exact endpoint, OMP process, owner, and watcher correlation; a foreign, partial, replaced, or otherwise uncertain state refuses without reset, adoption, or cleanup. Start, attach/detach, failure, and refusal preserve receipts and every tmux or Herdr session containing a retained task endpoint.

For central Herdr, `bin/agd start --backend herdr` runs from an ordinary terminal and uses the generated non-default session recorded in the launcher receipt. It refuses a pre-existing unreceipted same-name server and preserves it. This central path does not use the pane-worker contract.

## Projects and task workspaces

After guided installation, run `project add ID --source URL_OR_PATH --base main` or register an existing clone at `projects/ID` with `project register ID --path ABSOLUTE_PATH`. `project refresh ID` requires a clean base branch and fast-forward. `project trust ID --instruction FILE` records explicit Cursor trust for that exact clone; registration alone does not grant it. `project remove ID` only unregisters. For irreversible deletion, obtain user instruction, run `project approve ID --action delete-clone --instruction FILE`, then `project remove ID --delete-clone --approval APPROVAL_ID`; tasks, dirty work, extra refs and unpublished reflog commits prevent deletion.

`task create NAME --project ID --brief FILE [--kind ship|scout]` obtains a distinct Treehouse v3.1.6 lease and worktree. `task status NAME` gives the path. Work and commits go **only** there, never in the stable clone. A task stuck `preparing` uses `task reconcile NAME`, not a second allocation. Scout tasks report unchanged work; ship tasks need committed changes. `task ready NAME --report FILE` checks the branch, lease, clean state and report. Readiness is not landing or publication approval.

## Worker orchestration

`worker launch NAME --harness omp` inherits a proven active central backend when `--backend` is omitted; without an active central it uses the saved launcher backend. `--backend tmux` or `--backend herdr` is a worker-only override. `worker inspect NAME`, `worker send NAME --file FILE` and `worker inbox NAME` expose state and durable steering; an OMP extension signals the queued message and the worker calls `worker acknowledge NAME MESSAGE_ID`. Enqueue/doorbell is **not** proof the instruction was processed. `worker interrupt NAME` sends Escape only for a confirmed busy OMP turn and does not assert process death. `worker stop NAME` submits `/quit` only after a native idle receipt and waits for process exit and endpoint absence; refusal retains the lease. `worker relaunch NAME` reuses the recorded worktree only after the previous process and endpoint are positively absent.

### Worker skill catalog

An `init` seeds an empty external private home, or a checkout home with no private `config`, `projects`, `data`, or `state`, with the `default` profile and nine reviewed bundles tracked in `bundled/worker-skills/`. Existing private-home state is untouched; init never upgrades or overwrites private worker files. Optional private-home configuration at `config/workers.json` defines a `default_profile` and flat `profiles`, whose `skills` arrays refer to bundles in `config/worker-skills/ID/`. A bundle has a regular `SKILL.md` with simple `---` frontmatter containing its exact lowercase `agd-` ID and a nonempty one-line description; its resources may be regular files or directories only. `worker skills list` and `worker profiles list` return deterministic JSON. Launch with the implicit default, `--profile NAME`, or `--skill-mode curated --skill ID`; curated selection needs at least one skill and cannot be combined with a profile. Without configuration, an implicit launch records no provisioned skills; a requested unknown profile or curated skill is refused rather than silently downgraded.

If seeding is interrupted after the catalog appears but before `workers.json`, init preserves it rather than retrying or overwriting it; inspect the catalog, then either write the matching profile manually or remove it deliberately before a manual opt-in.

For an existing home, opt in only when requested: copy the chosen reviewed bundle directories from `bundled/worker-skills/` without replacing existing directories, then save a matching `workers.json`. For the reviewed default set, use `{"default_profile":"default","profiles":{"default":{"skills":["agd-ponytail","agd-ponytail-audit","agd-ponytail-debt","agd-ponytail-gain","agd-ponytail-help","agd-ponytail-review","agd-superpowers-systematic-debugging","agd-superpowers-test-driven-development","agd-superpowers-verification-before-completion"]}}}`. The Relay integration adds five upstream central skills under `.agents/skills`; ordinary workers still receive only these nine reviewed default bundles. A valid `pr-publish` grant adds the sole AGD-authored `agd-scoped-pr-relay` bundle for that task. Pinned upstream SHAs and license notices are in `third_party/PROVENANCE.md`.

Both paths are relative to the effective private home (`AGD_HOME` when set), not the tracked distro checkout. A minimal catalog is:


```json
{"default_profile":"implementer","profiles":{"implementer":{"skills":["agd-review"]}}}
```

Save that as `config/workers.json`, then create `config/worker-skills/agd-review/SKILL.md`:

```markdown
---
name: agd-review
description: Review changes carefully.
---
Read references/checklist.txt before reviewing.
```

`config/worker-skills/agd-review/references/checklist.txt` is a regular relative resource. Bundles are instructions, not permissions to publish, merge, run unattended, or clean up.

Provisioning refuses an existing destination, native-name alias or ancestor collision, symlinked parent, tracked path, or unsafe bundle entry. It freezes complete regular-file copies with hashes and modes; relaunch verifies/restores that snapshot and never re-resolves the mutable catalog. Exact generated paths are shared-Git-common-directory `info/exclude` entries with locked ownership: sibling worktrees and user lines are preserved, and a managed entry is released only after its last owner. Cleanup retains snapshots/journals, retries uncertain return state, and a discard retry must reuse its exact recorded approval.

Herdr workers remain pane-bound: run their controls inside a Herdr-managed pane (`HERDR_ENV=1`) and set `AGD_HERDR_SESSION` to a separate named, non-default server before `worker launch NAME --harness omp --backend herdr`. The worker adapter records and verifies its server, workspace, tab, pane, terminal, and socket identity; it never uses the focused/default session. Keep that server alive until every retained task endpoint is cleaned. This is distinct from ordinary-terminal central `start`.

The central extension starts one watcher per session and re-arms it once after unexpected exit. `session status`, `session reconcile`, `events list` and `events acknowledge EVENT_ID` inspect and handle durable wakes. Acknowledging a wake does **not** answer a decision: use `task status NAME` to inspect `open_decisions`, obtain the requested answer, and `decision resolve NAME KEY --answer FILE` to persist and enqueue it. Event receipt survives acknowledgement separately. A competing central must not take over while the first owner is alive. Worker phase `ready` can still mean its process is alive; stop it before cleanup.

Routine supervision is hidden from the conversation but stays available to the agent. Interactive OMP shows `Distro · watching · N pending` in the status footer; actionable task events still wake the agent visibly, and an unrecoverable watcher exit raises an error notification. Clients that do not render extension status requests can inspect `session status` and `events list`.

Cursor Agent still needs user authentication and a live transcript bound to its runner before either Cursor/backend combination can be claimed operational. Herdr 0.8.2 with protocol 20 has live OMP worker evidence only inside an explicitly named session. `doctor --harness cursor --backend herdr` diagnoses executable versions; it does not authenticate Cursor or provision a Herdr session.

## Background reviewer

`reviewer run` creates one bounded manual receipt. With no eligible completed evidence it returns `state: "no-work"` before native/model dispatch. Its private `data/reviewer/config.json` defaults to 20 candidates, 262144 UTF-8 input bytes, 20 native model calls, 65536 UTF-8 report bytes, and a 1800-second deadline. A null `review_model` inherits the configured native OMP model; a non-null selector is resolved before any call and an unavailable model refuses the run. Config, instruction bytes, model, cutoff, source IDs/versions and hashes, limits, counters, and deadline are frozen in the receipt/packet; later edits and later evidence do not alter it.

Native JSONL is the authoritative evidence source. The broker reads it as data, records frozen source hashes/offsets and coverage at cutoff, and retains reviewer-private native session/artifact, packet, receipt, report, proposal-revision, triage, and event paths; there is no pruning. Langfuse has no reviewer query, key, or client: online and offline collection are identical, and late material is a bounded follow-up/coverage state. Existing observability exporter capture is reused with reviewer purpose/run metadata when enabled; no Collector role or observability stack is added. Provider credentials reach only the native `ModelRegistry.getApiKey`, never a packet, prompt, report, broker, tool, or Langfuse configuration. There is no USD budget or token/cost guarantee.

The model receives only the frozen packet and exactly `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit`. It has no shell, edit/write, install/publish, Git, MCP, task-spawn, repository extension, generic read/query, or arbitrary filesystem capability. This is a model-tool boundary, not an OS sandbox. Missing, malformed, withheld, redacted, active, unknown, or out-of-cutoff material is explicit coverage limitation; invalid citations/report shape or out-of-packet source requests are refused. Input/output/call/deadline exhaustion stops work with coverage/checkpoint state; a model/preparation failure creates a terminal receipt with a controlled error code while frozen/checkpoint evidence remains available to a later explicit run, which creates a new intent and receipt rather than resuming the failed one.

`reviewer status`, `reviewer reports list|show ID`, and `reviewer proposals list|show ID` only inspect retained data. Reports/evidence/revisions are immutable; triage is separate, audited human state. `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded` appends a disposition only: event acknowledgement changes neither it nor edit authority, and even an accepted proposal creates no task or edit. Existing-skill and new-skill proposals remain inert; new drafts never enter skills/profiles. Reviewer operations are central-independent and preserve `state/owner.json`, task data, and worker skill/catalog bytes. Default selection excludes reviewer sessions. An explicit retrospective scope may select only closed reviewer-run IDs and freezes a cutoff excluding the current and later runs; it cannot trigger, approve, schedule, or alter itself.

`reviewer timer enable|disable|status` manages the sole possible systemd user-unit lifecycle, but enablement requires separate explicit target-scoped approval. Configuration, `init`, central startup, and manual runs neither enable nor start it; AGD uses no cron, linger, daemon, or autostart. `calendar` is the default (`02:00` `UTC`); `manual-only` has no timer; `interval` uses a positive whole number of hours and a fixed UTC-hour anchor. Native calendar scheduling skips a DST gap and runs once in a fold, and enablement refuses disagreement with IANA `ZoneInfo`. Persistent timer behavior allows one missed-run catch-up; concurrent triggers coalesce and the next due time does not drift from completion. Units are private to the home and marked by the owning UUID/UID. User-systemd activation remains unavailable until separately approved disposable-home proof; tests and unit rendering are not substitutes. Set the real native agent directory with `OMP_AGENT_DIR` before a nonempty native reviewer run; AGD does not substitute a private empty `.omp` directory.

## Managed observability

Managed observability is opt-in and needs Docker Engine 29.6.0, Docker Compose v5.1.4, and the private Treehouse v3.1.6 binary on `PATH`:

```sh
export PATH=/tmp/agd-treehouse-v3.1.6:$PATH
bin/agd --home "$AGD_HOME" init
bin/agd --home "$AGD_HOME" observability enable
bin/agd --home "$AGD_HOME" observability status
bin/agd --home "$AGD_HOME" observability stop
bin/agd --home "$AGD_HOME" observability start
bin/agd --home "$AGD_HOME" observability disable
```

Each home owns a distinct Docker project, named volumes, Langfuse project credentials, and loopback-only browser, media, OTLP, health, and diagnostics ports. Never reuse one home's private observability records in another home: identity validation refuses them. `enable` does not install Docker, Compose, or elevate privileges.

`agd observability central [OMP_ARGS...]` starts a managed OMP central from the private home with a fresh native session under `data/observability/central/omp`. It refuses forwarded working-directory, session, resume, continue, and no-session controls. When enabled, it attempts bounded stack health first and warns rather than blocking OMP on degradation; when disabled, it starts no Docker services and removes ambient OTEL configuration. A directly started `omp` session remains unmanaged and is warned by the extension; AGD does not relocate it.

OMP worker launch and relaunch reserve a fresh, owner-only task/incarnation session directory before the endpoint exists. Prior native JSONL and artifacts remain in their original incarnation directory. Managed OMP processes receive only Collector loopback OTLP settings, full native capture, and role/home/distro/task/incarnation resource identity when enabled; workers never receive Langfuse credentials or create service records. Cursor remains unsupported for native observability and receives no such configuration.

`stop` and `disable` preserve native JSONL/artifacts, Collector archives and persistent queue files, credentials, and named volumes. The durable config holds the selected exact immutable lock and non-secret Langfuse project ID; mismatched lock evidence is refused rather than upgraded. Status reads a configured stack even when desired mode is disabled or the last runtime was stopped, so desired mode and observed runtime stay separate. It reports central receipt history, worker session directories/materialization, and durable queue/archive paths with byte sizes without exposing content. A mode toggle advances the desired generation; existing managed processes can therefore be `stale-restart-required` until relaunched. A failed lifecycle operation is `degraded` or `unknown`, never proof that export stopped. Collector queue data is accepted only after `file_storage` admission; receiver acknowledgement, in-memory buffering, archive writes, and historical sent counters do not prove queueing or delivery. The configured queue is 1000 batches with bounded retry; permanent rejection, queue/full-storage admission failure, pre-queue loss, duplicate delivery, non-fsynced archives, unsupported attachment/reference shapes, and payload limits remain accepted limits rather than lossless-delivery claims.

Full captured prompts, system instructions, tool arguments/results, and any supported attachments may be sensitive. They remain in private native sessions and may reach the local Langfuse project while enabled; do not copy credentials or payloads into shell history, tickets, or logs. Collector routes traces to Langfuse and local trace archives only. OTLP logs and metrics are archived locally and are not Langfuse ingestion.

## Reviewer scheduling

The reviewer scheduling contract is operated only through the explicit reviewer commands above. No configuration edit, initialization, central lifecycle action, or prior timer state authorizes background model work; inspect `reviewer timer status` before asking for separately approved activation.

## Landing, publication, and retention

For local landing obtain a concrete user instruction, `task approve NAME --action local-merge --target PROJECT:BASE --instruction FILE`, then `task land NAME --approval ID`. The exact ready head must fast-forward a clean stable clone. After a stopped worker, `task cleanup NAME` conditionally returns the lease. Dirty, unlanded or identity-uncertain work stays intact.

`publish` remains the existing one-head publication approval: `task approve NAME --action publish --target REMOTE:BRANCH --instruction FILE` records evidence for a ready head and does not run an iterative PR repair loop. The separate `pr-publish` grant is only for a running `ship` task's assigned branch and authorizes iterative non-force publication to that one target:

```sh
bin/agd task approve NAME --action pr-publish --target REMOTE:agd/NAME --instruction FILE
bin/agd worker launch NAME --pr-grant UUID
bin/agd worker publish-pr NAME --incarnation "$AGD_INCARNATION" --grant UUID \
  --title <TASK_FILES>/pr-title.txt --body <TASK_FILES>/pr-body.md
bin/agd worker relay-poll NAME --incarnation "$AGD_INCARNATION" --grant UUID
bin/agd worker relay-reply NAME --incarnation "$AGD_INCARNATION" --grant UUID \
  --lease LEASE --action start
bin/agd worker relay-reply NAME --incarnation "$AGD_INCARNATION" --grant UUID \
  --lease LEASE --action complete --payload <TASK_FILES>/relay-reply.json
bin/agd worker verify-pr NAME --incarnation "$AGD_INCARNATION" --grant UUID \
  --report <TASK_FILES>/report-draft.md
```

`<TASK_FILES>` is `$AGD_HOME/data/tasks/NAME`: the task-owned title and body for publication, report for final verification, and reply file for terminal Relay replies. The complete reply file is bounded JSON with `{"result":{"summary":"..."},"effects":[]}`; `start` has no payload. A Relay delivery is persisted and acknowledged before a potentially lengthy repair or test; acknowledgement is not proof the repair is complete. The worker settles that lease before polling again. Invalid, uncertain, or expired delivery/reply authority, ambiguous PR reconciliation, or failed provider checks leaves durable `blocked` evidence rather than guessing or retrying against another task.

`publish-pr` only pushes the granted task branch without force and reconciles at most one exact open PR; it never merges, tags, changes the base branch, or publishes another branch. `verify-pr` permits `ready` only after a live GitHub observation of the exact current head with effective required checks and reviews satisfied and `mergeStateStatus` `CLEAN`. The CLI checks task/incarnation/grant and owner state to prevent accidental same-UID misuse; it is not an OS sandbox.

Relay is installed on demand and privately under AGD home from Open Relay commit `dac2865837bcab9444a8636053f4f1406a3f9291`; it requires Node >=22.19 and does not modify user-global npm packages. Deployment requires central `AGD_RELAY_GITHUB_READ_TOKEN` with fine-grained repository **Checks**, **Pull requests**, and **Metadata** read access. The central connector uses `generic-check-runs` and the central human-review watcher reads review evidence. Producer/admin tokens remain in the central runtime and never enter a worker prompt. No target credentials are supplied by this documentation, and no live GitHub PR publication or acceptance is claimed here.

Discard is irreversible: inventory the task and obtain explicit user instruction, then `task approve NAME --action discard --target NAME --instruction FILE`; `task cleanup NAME --discard --approval ID` applies only that approval and only after the worker is proved stopped. Keep an uncertain lease rather than risk losing work.
