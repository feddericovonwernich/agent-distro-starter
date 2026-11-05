# Local operations

All commands below are `bin/agd` from the distro checkout. Set `AGD_HOME` to an absolute private-home path, or pass `--home /absolute/home`, to target another initialized home. An active central exports its owner token to tool commands; another shell receives a read-only refusal. This is an accidental-misuse guard, **not** an OS sandbox.

Every command also accepts relative `--home` paths, including `--home .`, `--home ..`, and named relative directories. They are made absolute against the invoking shell's current directory before dispatch; symlink home arguments remain refused. Explicit `--home` still takes precedence over `AGD_HOME`, whose default remains an absolute path. For example, run a manual reviewer from its distro directory with `./bin/agd --home . reviewer run`.

## Guided central lifecycle

The installer initializes a fresh home and writes private launcher and dependency receipts. Start its selected central from an ordinary terminal:

```sh
bin/agd start
```

`start --backend tmux|herdr` is an invocation-only override. Without it, a proven active central backend wins over the saved launcher backend. A ready receipt attaches only after exact endpoint, OMP process, owner, and watcher correlation; a foreign, partial, replaced, or otherwise uncertain state refuses without reset, adoption, or cleanup. Start, attach/detach, failure, and refusal preserve receipts and every tmux or Herdr session containing a retained task endpoint.

For central Herdr, `bin/agd start --backend herdr` runs from an ordinary terminal and uses the generated non-default session recorded in the launcher receipt. It refuses a pre-existing unreceipted same-name server and preserves it. This central path does not use the pane-worker contract.

## Uninstall and recovery

Use an independent ordinary terminal, not a central/worker process tree being retired. Relative `--home .` is normalized before handoff:

```sh
./bin/agd --home . uninstall --dry-run
./bin/agd --home . uninstall && cd ..
```

Dry-run is read-only, works without a TTY, and returns the target identity, exact resource identities, local-work findings, shared exclusions and blockers as pretty JSON. It does not render Compose, create secrets, reconcile tasks, install tools or take over a live owner. Features with no installation/runtime evidence do not require their optional runtimes. Git is required for Git identity and dependency inspection.

Destructive execution requires an interactive terminal and the exact canonical home path. A distinct displayed `DISCARD` token binds any dirty/ignored custom data, unpublished refs/commits or unverified work; changed work requires refreshed consent. Publication evidence uses existing local remote refs, never a network fetch. Fetch separately before preview if newer publication evidence is needed. Decline/EOF before consent changes nothing. A managed caller, foreign/reassigned lease, unknown PID/socket/pane, unsafe mount or conflicting journal cannot be overridden by extra confirmation.

A new resource or changed identity under an existing resource key requires a fresh canonical-home confirmation before the refreshed scope is granted. Partial-home recovery revalidates the original device/inode/UID immediately before deletion, even if its marker was already removed; a replacement directory is not continuation evidence.

Owned removal covers central/watcher and native OMP processes; worker runners/children and exact tmux/Herdr endpoints; bounded reviewer runs and the active scheduled service/cgroup, timer, activation link and persistent timer state; Relay processes; owned local Docker containers (including proven orphans), networks and data volumes; frozen generated skill files and their exact shared-Git exclusion owner; conditional Treehouse lease return followed by single-slot destroy; Git registrations, project/source checkouts and private distro data. Normal `task cleanup`, `project remove`, reviewer disable and observability disable retain their existing narrower meanings.

Shared OMP/Bun/Python/Herdr/tmux/Treehouse/Relay installations, account-global config/auth, Docker images/daemon, unrelated units/panes/pools/homes, remote repositories and external Git common directories/refs survive. An external unowned worktree blocks deletion of its backing clone. A linked distro source is removed with native Git worktree removal, preserving its common repo and sibling registrations.

After confirmation, a private sibling `.agd-uninstall-…/` journal fences all new distro activity. The foreground coordinator copies only its fixed application allowlist and release-lock asset, uses an outside interpreter with `-I -S -B`, moves its own cwd outside removal roots, and prints a `Recovery:` invocation of its private `bootstrap.py`. No OMP auth/plugin/session payload is staged. Run that exact printed command after a crash, even when the original `bin/agd` or home has disappeared. A different process receives a fresh preview/consent; it cannot reuse an old grant for a replacement path/inode/UUID.

Before irreversible removal, declining a resumed operation clears retirement but does not restart stopped services. After irreversible removal, refusal/interruption retains the external journal/stage; resolve the stated blocker and resume instead of deleting evidence or rerunning `rm -rf`. Native absence is checked before data deletion, then the missing home/resources are checked again over at least eight seconds. A recreated home is a failure, never permission for another blind delete. Success removes the executor and retains non-secret result/journal evidence.

Legacy evidence is deliberately conservative: bare/reused PIDs, busy reviewer locks without a proven owner, unreceipted native slots, Herdr records lacking the captured control namespace, UUID-only Docker labels or changed engine/context may block automatic cleanup. Do not relabel/adopt such resources merely to make uninstall pass; establish ownership independently first.

| Exit | Meaning |
|---|---|
| `0` | Ready dry-run, handled cancellation, verified uninstall or already absent without pending retirement |
| `2` | Malformed arguments |
| `3` | Unsafe/uncertain state or incomplete retirement |
| `4` | Missing essential inspection dependency |

`python3 tests/smoke_uninstall.py --backend herdr --with-docker` is an opt-in disposable native check; it accepts no existing-home target and never pulls an image. Use `--backend tmux` only where that runtime is available. It retains scoped evidence after a failed proof rather than widening native force flags.

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

`reviewer run` creates one bounded manual receipt; no eligible completed evidence returns `state: "no-work"` before dispatch. Config schema 2 defaults to 20 candidates, 262144 input bytes, 65536 report bytes and a 1800-second deadline. A null `review_model` inherits native OMP configuration; an unavailable explicit selector refuses the run. The selection manifest and receipt freeze cutoff, scope and instruction/config identities. Historical schema-1 artifacts remain readable without inferring destination scope.

New runs materialize only `request.json`, `selection-manifest.json` and the dedicated reviewer skill at launch, not an eager packet or catalog. The trusted request supplies `manifest_id`, `report_schema` 2, `allowed_scope.episodes`, `inspection_bounds`, `deadline_epoch_ms`, `evidence.artifact_root` and `report_path`; optional Langfuse availability is sanitized. Historical schema-1 data supports reads, triage and already-prepared recovery only: no fresh legacy runs, evidence or reports.

The reviewer admits a proved final assistant response, not an OMP process or task merely reaching a lifecycle state. Native capture is independent of telemetry. Canonical home, role, native session and worker task/incarnation bindings and a durable completion record establish the boundary. Live tails, pending proof, failed runs, malformed rows and cutoff-late material remain explicit limitations or eligible for later selection.
Confirmed runner-exit evidence remains available for legacy worker logs without completion checkpoints; a task state alone is not that proof.


Selection deduplicates scoped native material by message ID and text hash. Screening metadata, reflected intent/action/outcome assessment and full material coverage are distinct. Only successful finalization advances applicable progress; limited assessments hold missing grounding rather than claiming full review. `reviewer run --detail-episode EPISODE_ID` requests a bounded manual detail pass subject to the same eligibility and scheduling rules. Direct central episodes remain taskless; linked decisions do not widen central coverage.

Only an operator can request detail, with repeated `--detail-episode` flags for multiple episodes; neither timers nor reviewers initiate it. A current-run native message `resume_cursor` continues after a prior successful partial prefix. Omit it to reread from the start; do not reuse a cursor from another run.

To run a manual review from the distro directory, use `./bin/agd --home . reviewer run`. Changed extensions activate only in a fresh native OMP process; `/reload` is not a guaranteed activation boundary. Do not stop agents or fabricate historical completion boundaries.

Capture bounded UTF-8 command or query output beneath the active run artifact root with `reviewer evidence add RUN_ID --file PATH --label LABEL`; listing returns metadata. Captures are immutable, run-scoped and cannot establish native material coverage or target authority. Unsafe paths and credential-bearing inputs are refused. Native evidence is the fallback when optional scoped Langfuse data is unavailable; do not infer a defect from missing telemetry.

The cumulative input limit counts unique admitted manifest, catalog metadata, target and capture bytes; it does not bound all shell output, model tokens or cost. Responses are bounded to 16 KiB and metadata pages to 20 rows; raw CLI output and the run deadline have separate limits. Project targets use the registered identity and selected task revision; distro file targets use a current observed identity/hash and time. Present, absent and described guards record different proof; current source is not proof of historical effective instructions. Applicability and verified-project, verified-distro, external or unknown authority are separate. Retain private project evidence for authorized triage, not global guidance.

### Run-owned probe workspaces

Use a registered project and an existing local commit for execution-only investigation:

```sh
./bin/agd --home . reviewer workspace create RUN_ID --project PROJECT_ID --commit COMMIT
./bin/agd --home . reviewer workspace list RUN_ID
./bin/agd --home . reviewer workspace cleanup RUN_ID --project PROJECT_ID --commit COMMIT --instruction OPERATOR_INSTRUCTION
```

The durable receipt records the exact run/project/commit, Git common directory, intent-owned CAS base reference and object ID, branch, lease holder/ID, and path. Allocation is not a task: it does not create task or worker metadata, fetch, install dependencies, or run project code, and it never uses stable, active, or ready worktrees. `list` reconciles the exact current lease before disclosing an exposed path. Write bounded output only beneath the run artifact root, then capture it with the evidence workflow above.

Managed reviewer workspace commands set `TREEHOUSE_NO_UPDATE_CHECK=1` to prevent Treehouse's background updater from recreating isolated data during cleanup. This is scoped to reviewer probes; ordinary Treehouse use and installer version policy are unchanged. The flag is supported by [Treehouse v3.1.6](https://github.com/kunchenguid/treehouse/blob/v3.1.6/cmd/root.go).

`workspace cleanup` is not a force-delete operation. An exposed probe requires a closed run and an operator instruction naming that exact run, project, and commit. Dirty or committed work, changed/foreign/ambiguous leases, active or detached users, and unknown process identity are retained or refused; an absent OMP group is not containment proof. Uninstall inventories each owned probe and its work-loss before quiescing/removing it, and existing scoped work-loss consent never bypasses lease or process identity. The process scan aggregates cwd, open descriptors, mapped paths, and uninspectable same-UID processes. On hosts where a protected same-UID user-manager PID cannot be inspected, clean probe cleanup and whole uninstall may remain blocked; AGD does not whitelist a name/PID, weaken inspection, change privileges, or promise positive cleanup in that state.

### Native reviewer runtime and managed endpoints

The public coordinator launches standalone OMP through a dedicated managed Herdr or tmux endpoint, validates the private request, materializes the selection manifest and reserved `agd-reviewer` skill, and records bounded lifecycle evidence. Recovery requires successful exit, binding, report and process/endpoint absence proof; incomplete handoff is not a successful review. Existing historical packet artifacts remain readable.

Native JSONL is the authoritative fallback. Scoped optional local Langfuse queries require the owned loopback stack and readiness; missing or delayed data is a limitation. Selection, captured source versions, receipts, reports and proposal history remain private. Requests carry sanitized query availability rather than credentials or cloud fallback settings.

The model inspects the manifest and scoped evidence with normal native tools under the reviewer policy; this is not filesystem, secret or process containment. Missing or out-of-cutoff material remains a limitation. Report schema 2 records screened, reflected or limited episode assessments and general recommendations with admitted source IDs and guarded target references. See the bundled skill reference for exact commands and report shapes.

The reviewer uses the distro-local OMP settings/auth agent directory and a private per-run working directory containing an explicit empty project plugin registry. Orchestrator plugins, project extensions, skills, bootstrap injection, and command discovery therefore do not enter the reviewer; normal native OMP tools remain available under the reviewer skill and admitted-run policy. The standalone OMP executable is the public runtime; no importable reviewer SDK host or SDK capability prerequisite is required.

`reviewer status`, `reviewer reports list|show ID`, and `reviewer proposals list|show ID` inspect retained data. Reports, evidence and revisions are immutable; human triage is separate. `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded` records disposition only: acknowledgement and acceptance grant no edit, task or publication authority. Recommendations and drafts remain inert. Default selection excludes reviewer sessions; explicit retrospectives admit only closed reviewer runs and exclude current and later runs.

`reviewer timer enable|disable|status` manages the UUID/UID-owned systemd user timer and requires explicit home-scoped approval. Configuration, `init`, central startup, and manual runs do not authorize activation. The installer can obtain that approval through its dedicated default-No reviewer prompt; AGD installs no cron job and does not enable lingering. Calendar scheduling defaults to 02:00 UTC, validates native/IANA agreement, skips a DST gap, and runs once in a fold. Intervals use positive whole hours anchored to UTC hours; persistent catch-up coalesces missed occurrences without drifting from completion. `manual-only` has no timer. Timer status retains lifecycle/checkpoint evidence, not proof of model authentication or completed reviews.

### Install-time Langfuse CLI

Stage `5 / 6  Install optional Langfuse CLI`, or `bin/agd installer langfuse` later, independently offers the official `@langfuse/cli@1.2.4`. The warning before `Install optional Langfuse CLI? [y/N]` discloses the existing Node.js 20+ and npm prerequisite and that credential-capable queries are possible. Blank, No, or EOF performs no runtime, package-manager, network, or Docker probe and writes no optional receipt.

With explicit consent, AGD verifies archive SHA-256 `0fc4e39b38bfa45d77f045a2d3a79e165411e4de0737eb7893bec57b246e574f` before a private npm staging install with lifecycle scripts disabled. It never installs Node/npm, uses sudo, mutates global packages, enables telemetry, chooses a cloud project, or enables a schedule. A reused external CLI must be canonical `@langfuse/cli` exactly `1.2.4`; all package assets, Node identity and offline `--version`/`api schema --json` probes are revalidated before use. The external tree remains unowned; only `data/langfuse-cli/bin/langfuse` and its strict receipt are home-owned.

Different, modified, legacy, foreign, or partial installations are retained and refused rather than overwritten. Managed runtime, package assets, launcher and provenance are promoted atomically under `data/langfuse-cli/runtime`; failure is nonfatal and preserves any previous valid installation. Retry with `bin/agd --home . installer langfuse`. This optional CLI stage does not enable telemetry or scheduled reviews.
When the local stack is independently enabled, a valid CLI receipt yields a loopback-only project query environment only after a bounded read-only check confirms the current owned running web container, native home/project/service identity, configured `127.0.0.1` published web port, and health/project readiness. Persisted `healthy` status alone is insufficient. Missing, disabled, stopped, unreachable, API-incompatible, or damaged optional state is reported as an availability limitation: no `LANGFUSE_*` keys or cloud fallback are supplied, and environment construction does not start Docker or download a replacement. The CLI keys are write-capable by design; AGD's query path is read-only by policy. For the exercised official CLI/API operation, use `--api-version auto api observations list --from-start-time <ISO> --to-start-time <ISO> --trace-id <TRACE_ID> --limit 1 --json`; the deprecated `api traces list` operation is not the proof path.

### Install-time reviewer scheduling

Stage `6 / 6  Configure scheduled reviewer`, or `bin/agd installer reviewer` later, offers daily 02:00 UTC, every 6 hours, every hour, custom daily time/timezone, and a custom whole-hour interval. Explicit opt-in warns about evidence sent to the configured model provider and potential charges; bounded call/byte/runtime limits are not a dollar budget. Review proposals remain inert until separate human action. This feature neither requires nor enables Langfuse.

Custom daily time uses HH:MM and an IANA zone (UTC by default); custom intervals must be positive whole hours. Invalid input can be corrected or cancelled. Blank/No at consent, menu 0, or EOF before a complete valid selection writes no reviewer config or units. A complete choice preserves existing model and limits, writes private validated config, and only then attempts activation. An active reviewer lease or non-disabled timer receipt refuses reconfiguration; inspect and explicitly disable the timer first.

Activation verifies the selected standalone OMP executable, existing distro-private native agent directory, and user-systemd support without a model call. It does not require an importable reviewer SDK or Bun host, and it never installs either. The owned service binds the actual Python interpreter and a minimal executable search path. It starts with only HOME, that PATH, and LANG; unrelated user-manager credentials and ambient OTEL settings are discarded rather than copied into units or inherited. Configure authentication through OMP's distro-private agent directory, not this installer; environment-only provider credentials in the user manager do not reach scheduled runs. Native active state is checked before reporting enabled; that snapshot is not provider/model readiness.

A standalone OMP executable alone does not satisfy full reviewer readiness. Missing executable, model, user-manager, or endpoint prerequisites keep the selected schedule and report configured/not enabled; no runtime is installed. Fix the prerequisite and run `bin/agd reviewer timer enable`. If activation failed after unit creation, keep the home, inspect `bin/agd reviewer timer status`, and use `bin/agd reviewer timer disable` before reconfiguration or deletion. Optional failure does not roll back a successful mandatory installation or change telemetry.

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

Fresh installation offers this lifecycle in stage `4 / 6  Configure observability`, defaulting to No. Consent starts the local stack before the first central launch; declining starts no Docker activity and leaves local native session recording available. The prompt warns about image downloads, resource use, and sensitive captured content. It does not configure Langfuse Cloud or install Docker/Compose.

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
