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

`task create NAME --project ID --brief FILE [--kind ship|scout] [--harness omp|cursor] [--profile PROFILE | --skill-mode curated --skill NAME... | --skill-mode none]` resolves and records the immutable worker selection before obtaining a distinct Treehouse v3.1.6 lease and worktree. It publishes `prepared` only after selected skills are frozen and materialized. `task status NAME` gives the path. Work and commits go **only** there, never in the stable clone. A task stuck `preparing` uses `task reconcile NAME`, which completes the same recorded allocation and selection rather than allocating again or adopting changed sources. Scout tasks report unchanged work; ship tasks need committed changes. `task ready NAME --report FILE` checks the branch, lease, clean state and report. Readiness is not landing or publication approval.

## Worker orchestration

`worker launch NAME` reads the task's recorded harness and immutable skill snapshot. When `--backend` is omitted it inherits a proven active central backend; without an active central it uses the saved launcher backend. `--backend tmux` or `--backend herdr` is a worker-only override. Harness/profile/skill controls are intentionally not launch options. Every OMP worker disables ambient extension discovery and exposes exactly the frozen optional plus mandatory system skills while retaining AGD's explicit worker extension.

Workers are retained task executors. `worker send NAME --file FILE` accepts only a `running` or `ready` worker whose process and endpoint are presently verified. It durably sequences an asynchronous request; the OMP extension delivers one request at a time in the same session, and the terminal assistant reply becomes a correlated `done` event (`blocked` when the turn ends without assistant text). Inspect with `events list` and acknowledge with `events acknowledge EVENT_ID`. Completion, not enqueueing, proves processing. Requests accepted before a crash remain queued for at-least-once delivery after relaunch; stopped, missing, and unknown workers reject new sends. A live `ready` worker may perform read-only follow-ups. After its endpoint is proved absent, `worker relaunch NAME` verifies the unchanged ready head and clean workspace, starts the OMP extension without replaying the completed brief, and consumes retained requests. A modified ready workspace refuses relaunch.

### Worker protocol

Every newly created OMP task freezes `agd-worker`, including `--skill-mode none`. The immutable selection records optional IDs in `skills` and mandatory IDs in `system_skills`; `task status NAME` exposes both. A valid scoped-PR grant appends `agd-scoped-pr-relay` to the system set without replacing the original base snapshot. Plugin/custom sources may not claim either reserved AGD name. Cursor does not receive this protocol or its lifecycle hooks; its existing empty-selection compatibility path is unchanged.

OMP discovers these frozen native skills through its exact allowlist. Do not put skill inventories, absolute skill paths, or blanket “read all skills” instructions in briefs. Optional skills are loaded when relevant. The worker extension injects verified `agd-worker` core instructions into system context and refreshes one hidden task binding before each model request. The detailed CLI recipes remain available at `skill://agd-worker/references/commands.md`; scoped-PR recipes live in its separate adapter. Compaction does not remove the core or rely on an old task binding.

The worker-only `worker context NAME --incarnation TOKEN` reads authoritative state under the distro lock without backend inspection. It requires the runner's current task/incarnation environment and verifies the frozen protocol. It supplies workspace, brief/report paths, executable argv, grant identity, and current ready/blocker/decision outcome. Missing, corrupt, legacy, or stale protocol context aborts the turn before a provider request; a scoped diagnostic is recorded when possible. Recreate pre-protocol tasks rather than relaunching them or mutating their frozen selection.

An ordinary assignment completes through `task ready`; a grant-bound PR assignment uses `worker verify-pr` and live readiness checks. If an initial assignment ends without readiness, an actionable blocker, or an open decision, the extension permits one corrective continuation. A second unfinished stop emits a durable blocked diagnostic and retains the task. It does not interrupt tools, retry provider errors or user aborts, correct retained FIFO replies, or replay ready work. This is a reminder mechanism, not guaranteed model obedience or a permission boundary: CLI identity, approval, Git, lease, and process checks remain authoritative.

### Worker skill catalog

`init` writes a `default` profile naming the canonical Ponytail skills (`ponytail`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`, `ponytail-help`, `ponytail-review`) and Superpowers skills (`systematic-debugging`, `test-driven-development`, `verification-before-completion`). It does not copy upstream files. `worker skills list` resolves exact skill directories from the validated project-scoped OMP plugin registrations installed during setup. A default-profile task refuses before allocation when those registrations are absent or invalid. Choose a profile or curated set on `task create`; explicit `--skill-mode none` selects no ordinary worker skills.

Optional `config/workers.json` profiles refer to canonical plugin names or custom skills under `config/worker-skills/NAME/`. Plugin and custom sources share one namespace; any duplicate name refuses without precedence or aliasing. Existing prefixed names such as `agd-ponytail` are not migrated. A custom skill is a real directory containing a regular `SKILL.md`; its frontmatter name must exactly match the directory. Descriptions may be plain, quoted, folded, or literal scalar text. Resources may contain regular files and directories only; symlinks, hard links, special files, traversal names, and `.git` content refuse.

A minimal custom profile is:

```json
{"default_profile":"implementer","profiles":{"implementer":{"skills":["review"]}}}
```

Save that as `config/workers.json`, then create `config/worker-skills/review/SKILL.md`:

```markdown
---
name: review
description: Review changes carefully.
---
Read references/checklist.txt before reviewing.
```

`config/worker-skills/review/references/checklist.txt` is a regular relative resource. Catalog entries are instructions, not permissions to publish, merge, run unattended, or clean up. The valid `pr-publish` path adds only the separate AGD-authored `agd-scoped-pr-relay` adapter.

Provisioning refuses an existing destination, native-name alias or ancestor collision, symlinked parent, tracked path, or unsafe bundle entry. During task creation it freezes complete regular-file copies with hashes, modes, and native plugin provenance; launch is source-independent, and relaunch verifies/restores only that completed snapshot. Exact generated paths are shared-Git-common-directory `info/exclude` entries with locked ownership: sibling worktrees and user lines are preserved, and a managed entry is released only after its last owner. Cleanup retains snapshots/journals, retries uncertain return state, and a discard retry must reuse its exact recorded approval.

Herdr workers remain pane-bound: from a Herdr-managed pane (`HERDR_ENV=1`) set `AGD_HERDR_SESSION` to a separate named, non-default server, create the task with `--harness omp`, then run `worker launch NAME --backend herdr`. The worker adapter records and verifies its server, workspace, tab, pane, terminal, and socket identity; it never uses the focused/default session. Keep that server alive until every retained task endpoint is cleaned. This is distinct from ordinary-terminal central `start`.

The central extension starts one watcher per session and re-arms it once after unexpected exit. `session status`, `session reconcile`, `events list` and `events acknowledge EVENT_ID` inspect and handle durable wakes. Acknowledging a wake does **not** answer a decision: use `task status NAME` to inspect `open_decisions`, obtain the requested answer, and `decision resolve NAME KEY --answer FILE` to persist and enqueue it. Event receipt survives acknowledgement separately. A competing central must not take over while the first owner is alive. Worker phase `ready` can still mean its process is alive; stop it before cleanup.

Routine supervision is hidden from the conversation but stays available to the agent. Interactive OMP shows `Distro · watching · N pending` in the status footer; actionable task events still wake the agent visibly, and an unrecoverable watcher exit raises an error notification. Clients that do not render extension status requests can inspect `session status` and `events list`.

Cursor Agent still needs user authentication and a live transcript bound to its runner before either Cursor/backend combination can be claimed operational. Herdr 0.8.2 with protocol 20 has live OMP worker evidence only inside an explicitly named session. `doctor --harness cursor --backend herdr` diagnoses executable versions; it does not authenticate Cursor or provision a Herdr session.

## Native plugin lifecycle smoke

`python3 tests/smoke_native_plugins.py --backend tmux|herdr` is an opt-in networked acceptance check. It resolves compatible host dependencies, installs both official plugins into a disposable home, starts and verifies an actual `agd start` central, and exercises Ponytail's command lifecycle and Superpowers' bootstrap from the installed payloads. It launches real selected and none-mode workers, verifies their session-reported skill names and worktree sources, completes two correlated follow-ups in one retained incarnation, and stops both through the guarded idle path. It also checks reviewer isolation, reads upstream supporting resources through native OMP, removes a source and proves snapshot restoration/new-task refusal, returns and destroys both leases, stops exact owned central/watcher/backend children, compares account-global OMP state, deletes the temporary home, and verifies no child recreates it across four former heartbeat intervals. Run the Herdr variant only inside an owned managed pane; the tmux variant requires tmux. The script never targets a default/shared backend session.

## Background reviewer

`reviewer run` creates one bounded manual receipt. With no eligible completed evidence it returns `state: "no-work"` before native/model dispatch. Its private `data/reviewer/config.json` defaults to 20 candidates, 262144 UTF-8 input bytes, 20 native model calls, 65536 UTF-8 report bytes, and a 1800-second deadline. A null `review_model` inherits the configured native OMP model; a non-null selector is resolved before any call and an unavailable model refuses the run. Config, instruction bytes, model, cutoff, source IDs/versions and hashes, limits, counters, and deadline are frozen in the receipt/packet; later edits and later evidence do not alter it.

Native JSONL is the authoritative evidence source. The broker reads it as data, records frozen source hashes/offsets and coverage at cutoff, and retains reviewer-private native session/artifact, packet, receipt, report, proposal-revision, triage, and event paths; there is no pruning. Langfuse has no reviewer query, key, or client: online and offline collection are identical, and late material is a bounded follow-up/coverage state. Existing observability exporter capture is reused with reviewer purpose/run metadata when enabled; no Collector role or observability stack is added. Provider credentials reach only the native `ModelRegistry.getApiKey`, never a packet, prompt, report, broker, tool, or Langfuse configuration. There is no USD budget or token/cost guarantee.

The model receives only the frozen packet and exactly `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit`. It has no shell, edit/write, install/publish, Git, MCP, task-spawn, repository extension, generic read/query, or arbitrary filesystem capability. This is a model-tool boundary, not an OS sandbox. Missing, malformed, withheld, redacted, active, unknown, or out-of-cutoff material is explicit coverage limitation; invalid citations/report shape or out-of-packet source requests are refused. Input/output/call/deadline exhaustion stops work with coverage/checkpoint state; a model/preparation failure creates a terminal receipt with a controlled error code while frozen/checkpoint evidence remains available to a later explicit run, which creates a new intent and receipt rather than resuming the failed one.

The reviewer uses the distro-local OMP settings/auth agent directory but runs the native SDK host in a private per-run working directory containing an explicit empty project plugin registry. Orchestrator plugins, project extensions, skills, bootstrap injection, and command discovery therefore do not enter the reviewer; only the three broker tools above are registered. Direct Bun probe/host launches disable Bun dotenv loading. The standalone OMP release binary does not expose importable SDK modules, so reviewer execution reports a `capability` prerequisite unless an importable compatible OMP package is available; packet collection and `no-work` remain available without model dispatch.

`reviewer status`, `reviewer reports list|show ID`, and `reviewer proposals list|show ID` only inspect retained data. Reports/evidence/revisions are immutable; triage is separate, audited human state. `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded` appends a disposition only: event acknowledgement changes neither it nor edit authority, and even an accepted proposal creates no task or edit. Existing-skill and new-skill proposals remain inert; new drafts never enter skills/profiles. Reviewer operations are central-independent and preserve `state/owner.json`, task data, and worker skill/catalog bytes. Default selection excludes reviewer sessions. An explicit retrospective scope may select only closed reviewer-run IDs and freezes a cutoff excluding the current and later runs; it cannot trigger, approve, schedule, or alter itself.

`reviewer timer enable|disable|status` manages the UUID/UID-owned systemd user timer and requires explicit home-scoped approval. Configuration, `init`, central startup, and manual runs do not authorize activation. The installer can obtain that approval through its dedicated default-No reviewer prompt; AGD installs no cron job and does not enable lingering. Calendar scheduling defaults to 02:00 UTC, validates native/IANA agreement, skips a DST gap, and runs once in a fold. Intervals use positive whole hours anchored to UTC hours; persistent catch-up coalesces missed occurrences without drifting from completion. `manual-only` has no timer. Timer status retains lifecycle/checkpoint evidence, not proof of model authentication or completed reviews.

### Install-time reviewer scheduling

Stage `5 / 5  Configure scheduled reviewer`, or `bin/agd installer reviewer` later, offers daily 02:00 UTC, every 6 hours, every hour, custom daily time/timezone, and a custom whole-hour interval. Explicit opt-in warns about evidence sent to the configured model provider and potential charges; bounded call/byte/runtime limits are not a dollar budget. Review proposals remain inert until separate human action. This feature neither requires nor enables Langfuse.

Custom daily time uses HH:MM and an IANA zone (UTC by default); custom intervals must be positive whole hours. Invalid input can be corrected or cancelled. Blank/No at consent, menu 0, or EOF before a complete valid selection writes no reviewer config or units. A complete choice preserves existing model and limits, writes private validated config, and only then attempts activation. An active reviewer lease or non-disabled timer receipt refuses reconfiguration; inspect and explicitly disable the timer first.

Activation verifies the selected importable OMP SDK, Bun, existing distro-private native agent directory, and user-systemd support without a model call. Bun automatic installation is disabled for both capability probes and native reviewer runs. The owned service binds the actual Python interpreter and a minimal executable search path, preserving selected SDK/Bun precedence. It starts with only HOME, that PATH, and LANG; unrelated user-manager credentials and ambient OTEL settings are discarded rather than copied into units or inherited. Configure authentication through OMP's distro-private agent directory, not this installer; environment-only provider credentials in the user manager do not reach scheduled runs. Native active state is checked before reporting enabled; that snapshot is not provider/model readiness.

The standalone OMP release alone does not satisfy SDK capability. Missing prerequisites keep the selected schedule and report configured/not enabled; no runtime is installed. Fix the prerequisite and run `bin/agd reviewer timer enable`. If activation failed after unit creation, keep the home, inspect `bin/agd reviewer timer status`, and use `bin/agd reviewer timer disable` before reconfiguration or deletion. Optional failure does not roll back a successful mandatory installation or change telemetry.

## Managed observability

Managed observability is opt-in and needs Docker Engine 29.6.0, Docker Compose v5.1.4, and the private Treehouse v3.1.6 binary on `PATH`:

```sh
export PATH=/tmp/agd-treehouse-v3.1.6:$PATH
bin/agd --home "$AGD_HOME" init --backend tmux
bin/agd --home "$AGD_HOME" observability enable
bin/agd --home "$AGD_HOME" observability status
bin/agd --home "$AGD_HOME" observability stop
bin/agd --home "$AGD_HOME" observability start
bin/agd --home "$AGD_HOME" observability disable
```

Fresh installation offers this lifecycle in stage `4 / 5  Configure observability`, defaulting to No. Consent starts the local stack before the first central launch; declining starts no Docker activity and leaves local native session recording available. The prompt warns about image downloads, resource use, and sensitive captured content. It does not configure Langfuse Cloud or install Docker/Compose.

Optional enablement failure does not undo the mandatory installation. A visible incomplete warning and recovery commands accompany public status when available; generated Compose files, identity, credentials, and queues remain in the distro so partial services can be inspected and stopped. Interruption after mandatory setup also retains the home. Do not delete that directory while services remain: run the home-scoped `observability status`, retry `enable`, or use `disable` to stop its stack. Disabled desired mode after a failed attempt is not proof that no containers were created. Existing installations require explicit enablement and normal central/worker restart; the installer changes no active sessions.

When the first enable fails before publishing its config, `stop` and `disable` can recover from the retained release lock and home-bound secrets. They validate those records and preserve the rendered Compose files and credentials; missing, changed, or foreign records refuse rather than guessing ownership. A successful stop does not delete volumes or history.

Each home owns a distinct Docker project, named volumes, Langfuse project credentials, and loopback-only browser, media, OTLP, health, and diagnostics ports. Never reuse one home's private observability records in another home: identity validation refuses them. `enable` does not install Docker, Compose, or elevate privileges.

`agd observability central [OMP_ARGS...]` starts a managed OMP central from the private home with a fresh native session under `data/observability/central/omp`. It refuses forwarded working-directory, session, resume, continue, and no-session controls. When enabled, it attempts bounded stack health first and warns rather than blocking OMP on degradation; when disabled, it starts no Docker services and removes ambient OTEL configuration. A directly started `omp` session remains unmanaged and is warned by the extension; AGD does not relocate it.

OMP worker launch and relaunch reserve a fresh, owner-only task/incarnation session directory before the endpoint exists. Prior native JSONL and artifacts remain in their original incarnation directory. Managed OMP processes receive only Collector loopback OTLP settings, full native capture, and role/home/distro/task/incarnation resource identity when enabled; workers never receive Langfuse credentials or create service records. Cursor remains unsupported for native observability and receives no such configuration.

`stop` and `disable` preserve native JSONL/artifacts, Collector archives and persistent queue files, credentials, and named volumes. The durable config holds the selected exact immutable lock and non-secret Langfuse project ID; mismatched lock evidence is refused rather than upgraded. Status reads a configured stack even when desired mode is disabled or the last runtime was stopped, so desired mode and observed runtime stay separate. It reports central receipt history, worker session directories/materialization, and durable queue/archive paths with byte sizes without exposing content. A mode toggle advances the desired generation; existing managed processes can therefore be `stale-restart-required` until relaunched. A failed lifecycle operation is `degraded` or `unknown`, never proof that export stopped. Collector queue data is accepted only after `file_storage` admission; receiver acknowledgement, in-memory buffering, archive writes, and historical sent counters do not prove queueing or delivery. The configured queue is 1000 batches with bounded retry; permanent rejection, queue/full-storage admission failure, pre-queue loss, duplicate delivery, non-fsynced archives, unsupported attachment/reference shapes, and payload limits remain accepted limits rather than lossless-delivery claims.

Full captured prompts, system instructions, tool arguments/results, and any supported attachments may be sensitive. They remain in private native sessions and may reach the local Langfuse project while enabled; do not copy credentials or payloads into shell history, tickets, or logs. Collector routes traces to Langfuse and local trace archives only. OTLP logs and metrics are archived locally and are not Langfuse ingestion.

## Reviewer scheduling

Scheduling is activated only through explicit `reviewer timer enable` or the approved installer reviewer opt-in. No configuration edit, initialization, central lifecycle action, or retained timer receipt alone authorizes new background model work. Use `reviewer timer status` to inspect retained lifecycle evidence; model authentication, actual due execution, and report completion are separate checks.

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
