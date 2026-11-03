# Agent distro starter

An OMP central session manages registered project clones, isolated Treehouse task worktrees, and supervised OMP workers. `projects/`, `config/`, `data/`, and `state/` belong to the local operator and are ignored by Git.

## Install and start

On Linux x86-64, review the [installer](install.sh) before running it. From the parent directory where you want the instance:

```sh
bash -c 'installer=$(curl -fsSL https://raw.githubusercontent.com/feddericovonwernich/agent-distro-starter/main/install.sh) && bash -c "$installer"'
```

The interactive installer asks for a lowercase name and central backend (`tmux` or `herdr`), refuses an existing destination, clones the canonical origin into `./NAME`, and preserves that origin. It initializes the clone's ignored private home and records the selected backend plus a generated non-default Herdr session name.

The installer groups setup into numbered stages with terminal colors and a final start command. Set `NO_COLOR` to disable colors; dumb terminals and redirected streams stay plain. `bin/agd` pretty-prints JSON on a terminal and keeps redirected JSON compact for scripts.

It can use APT only for missing Git, Python 3.12+, and selected `tmux`, after one explicit sudo prompt; blank or non-affirmative answers mean no elevation. OMP and Herdr are reused when compatible or installed/updated only after consent to the displayed latest stable release, exact Linux x86-64 asset, and published digest. Treehouse is reused when compatible or installed as **v3.1.6** after SHA-256 verification: `e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1`. Managed downloads are atomically installed in `~/.local/bin`, and their source, version, capabilities, artifact hash, and installed hash are recorded in the private home.

Setup uses OMP's official project marketplace flow to install `ponytail@ponytail` from `DietrichGebert/ponytail` and `superpowers@superpowers-dev` from `obra/superpowers`. Their executable extensions and central skills are trusted third-party code and become active in the next central session. AGD validates native project registrations and keeps their payloads under this distro; it does not auto-update plugins or copy a global installation. Existing distros with vendored profiles are not migrated automatically.

The fourth installer stage asks `Enable local Langfuse telemetry? [y/N]`. Blank, No, or EOF leaves export off without probing Docker; native local session recording remains available. Yes uses the existing observability lifecycle to provision a distro-owned, loopback-only Langfuse + Collector stack before the first central starts. Docker/Compose access is required; the installer does not install Docker or elevate for telemetry. This pulls images, uses disk/memory, and can capture sensitive prompts, instructions, and tool inputs/results. It is not Langfuse Cloud, and healthy services alone do not prove trace delivery.

If optional telemetry setup fails, installation warns and still enters the usable distro shell. The distro and its recovery files are retained, even if services were partially created; mandatory clone/dependency/plugin failures still remove the installer-owned clone. Inspect with `bin/agd observability status`, retry with `bin/agd observability enable`, or stop and disable with `bin/agd observability disable`. Existing installs are unchanged; use those commands explicitly and restart existing managed sessions to pick up a mode change.
The fifth stage independently offers the official `@langfuse/cli@1.2.4`:

```text
Install optional Langfuse CLI? [y/N]
```

The warning immediately before the default-No question discloses the existing Node.js 20+ and npm prerequisite and that credential-capable queries are possible. Blank, No, or EOF performs no Node/npm/package/network probe and writes no optional state. With explicit consent, AGD verifies the pinned archive SHA-256 `0fc4e39b38bfa45d77f045a2d3a79e165411e4de0737eb7893bec57b246e574f` before a private, lifecycle-disabled npm staging install. It never installs Node/npm, uses sudo, changes global packages, enables telemetry, selects a cloud project or configures a schedule.

An already-installed canonical `@langfuse/cli` is reused only when version `1.2.4`, every package asset, Node identity and offline `--version`/`api schema --json` probe match the pinned release. The home-owned launcher and receipt live under `data/langfuse-cli/`; an external package remains unowned and untouched. Different, modified, legacy, foreign or partial installations are retained and never receive credentials. Optional failure is nonfatal; retry with `bin/agd --home . installer langfuse`.
When the CLI is valid and the separately enabled local stack is known healthy, AGD can construct a loopback-only project query environment for the official CLI. Missing, disabled, stopped, unreachable, or damaged optional state is an explicit availability limitation: it supplies no Langfuse keys or cloud-host fallback and never starts Docker or downloads a replacement. The CLI credentials are technically write-capable; AGD's query operation is read-only by policy. Telemetry, CLI installation/query access, and scheduled reviewer activation are three independent opt-ins.

The sixth stage asks `Enable scheduled skill reviews? [y/N]`, independently of telemetry and CLI installation. 

After consent, choose daily at 02:00 UTC, every 6 hours, every hour, a custom daily HH:MM/IANA timezone, or a custom positive whole-hour interval. Blank/No skips setup; menu `0` or EOF cancels without changing config. Reviews inspect completed worker evidence, can incur model-provider charges, and produce reports/proposals—not automatic skill edits.

Reviewer activation uses the installed standalone OMP executable, the distro's native reviewer skill, and the user-systemd manager; no reviewer SDK/Bun host or runtime is downloaded, no sudo/linger is used, and authentication/model settings remain OMP's responsibility. Each run owns a dedicated managed Herdr or tmux endpoint, proves readiness and exact native-session binding before dispatch, and records bounded startup/runtime failures rather than claiming model work. Missing executable, model, user-manager, or endpoint prerequisites retain the selected schedule and report configured/not enabled. Retry selection with `bin/agd installer reviewer`, or after fixing prerequisites run `bin/agd reviewer timer enable`; inspect/disable with the corresponding timer commands. Existing active or partially configured timers must be explicitly disabled before changing their schedule.

Provider login and model configuration belong to OMP, not this installer: there are no login or model-selection prompts. Read-only dependency compatibility probes do not prove authentication. Managed OMP processes use a distro-private `HOME` at `data/omp/home/`, fix OMP's agent, profile, and XDG storage under `data/omp/`, pin Treehouse worktrees to `data/treehouse/`, and keep project plugin records in this distro's `.omp/`; the caller's effective Git global configuration remains available to Git. The explicit Treehouse root lets central and worker processes resolve the same lease even though their inherited `HOME` values differ. OMP's private home is required because OMP 18.14 uses its home-relative config name for project plugin discovery. The interactive nested shell preserves the caller's real `HOME` and shell configuration; exiting it returns to the caller. Run native commands through `bin/agd omp`, for example `bin/agd omp login`; AGD does not copy or configure credentials.

If the calling shell already defines `AGD_HOME`, unset it to use this clone's private home. Then:

```sh
unset AGD_HOME
bin/agd project add app --source /path/to/repository.git --base main
bin/agd start
```

`bin/agd start [--backend tmux|herdr]` attaches to a proven ready central or starts one after validating private dependency and launcher receipts. The explicit backend applies only to that invocation; otherwise a proven active central takes precedence over the saved backend. A conflicting override, foreign/partial/ambiguous state, or failed process/owner/watcher correlation refuses and preserves receipts, tasks, leases, and backend sessions. An omitted worker `--backend` inherits the proven active central backend; an explicit worker backend is local to that worker.

Use `AGD_HOME` with an absolute private-home path, or `bin/agd --home /absolute/home ...`, to operate another initialized instance. See [operations](docs/operations.md) for the Herdr split and lifecycle constraints, and [compatibility](docs/compatibility.md) for observed compatibility evidence.

## Uninstall a distro

From an independent ordinary terminal, inspect first, then remove the exact initialized home:

```sh
./bin/agd --home . uninstall --dry-run
./bin/agd --home . uninstall && cd ..
```

The command always prints pretty JSON. Destruction requires typing the canonical home path; dirty, unpublished or unverified local work requires a separate displayed `DISCARD` token. Unknown ownership is a blocker, not a force prompt. Shared tools, global account configuration, Docker images, remote repositories and other distros remain untouched.

Uninstall stages its own code and prints an outside `Recovery:` command before stopping owned runtimes. Retain that command/journal after interruption; a fresh process asks for consent again. Verified removal waits at least eight seconds for non-recreation. Python cannot change the parent shell's directory, hence `&& cd ..`. See [uninstall operations](docs/operations.md#uninstall-and-recovery) for owned resources, recovery and legacy blockers.


## Worker skills

`bin/agd init --backend tmux|herdr` seeds an empty home with a `default` profile naming nine canonical upstream skills. It does not copy skill payloads. After setup installs the official Ponytail and Superpowers project plugins, `worker skills list` resolves those exact upstream names, metadata, resources, versions, and paths from OMP's validated native registry. Existing private-home state is untouched; init never upgrades or overwrites it. Operators may add private custom skills under `config/worker-skills/NAME/` and select them in `config/workers.json`; a custom/plugin name collision refuses rather than overriding either source.

Choose the harness and immutable optional skill set at `task create`: use the configured default, `--profile NAME`, `--skill-mode curated --skill NAME`, or explicit `--skill-mode none`. Every OMP task also receives the mandatory AGD-authored `agd-worker` protocol; `none` disables optional skills, not that protocol. Creation freezes both sets under canonical names in ignored worktree paths before publishing `prepared`. Launch and relaunch use the frozen snapshot, never mutable plugin payloads. A valid `pr-publish` grant on a task created with no optional skills appends `agd-scoped-pr-relay` without replacing the base protocol. Both AGD names are reserved. The five Open Relay skills under `.agents/skills/` remain central-only; provenance is recorded in [`third_party/PROVENANCE.md`](third_party/PROVENANCE.md).

OMP workers are retained task executors, not one-shot agents. Native launch controls disable ambient extensions and expose exactly the task's optional and system skill names. Startup prompts no longer enumerate skills or filesystem paths: native discovery makes optional skills available on demand, while the worker extension injects the verified AGD core and fresh task context. An unfinished initial assignment gets at most one corrective turn, then a durable blocked diagnostic—not fabricated readiness. CLI approvals and state checks remain authoritative. Existing OMP tasks without a frozen protocol must be recreated; Cursor does not receive these hooks.

`worker send NAME --file FILE` queues asynchronous follow-ups in the same live session; terminal replies become durable correlated events. Queued requests survive crash/relaunch with at-least-once delivery, but new sends refuse stopped, missing, or unverified workers. A live `ready` worker answers read-only follow-ups. `worker relaunch NAME` verifies process absence, the ready head and workspace, then consumes retained requests without replaying the completed brief.

`project remove app` only unregisters and preserves the clone. Clone deletion needs a project-scoped approval and refuses retained tasks, uncommitted work and unverifiable local refs. No command publishes or merges without a separate, exact-target approval.

## Background skill review

`./bin/agd --home . reviewer run` creates one bounded manual review from the distro directory. It reviews a frozen packet of completed worker and central evidence for existing-skill improvements or inert new-skill drafts. It writes private schema-2 receipts, reports, proposal revisions, triage, events, and native reviewer session data under the effective home; it never edits skills, creates tasks, approves a proposal, or reviews itself by default. The reviewer uses normal native OMP read/bash/write tools under the reviewer skill's admitted run-directory policy; this model-tool boundary is not an OS sandbox. Every run uses a dedicated managed endpoint and native session binding; failed startup, timeout, process-group, endpoint, or model handoff evidence remains explicit and retryable.

Use `reviewer status`, `reviewer reports list|show ID`, and `reviewer proposals list|show ID` to inspect retained data. `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded` records human triage only: acknowledgement, report access, and disposition are separate from authority to edit. Historical schema-1 receipts and reports remain readable; new runs use truthful schema-2 receipts without legacy model-call/provider-inflight counters. See [operations](docs/operations.md#background-reviewer) for limits, privacy, timer approval, managed endpoint lifecycle, recovery and owned uninstall, and [compatibility](docs/compatibility.md#background-reviewer-acceptance) for exact evidence and external blockers.

Reviewer evidence is bounded by a proved **final response**, not by the lifetime of an OMP process or task. Native capture is part of the managed session lifecycle and does not depend on the optional telemetry stack. A guarded source binding and durable completion proof admit a response only when its native boundary is confirmed; an active tail, pending proof, malformed row, or other partial coverage remains explicit and eligible for a later fresh-cutoff packet. Incremental selection deduplicates material by canonical home, role, native session (and worker task/incarnation), native message ID, and exact text hash rather than title or physical coordinates. Only material retained in a successfully finalized report is consumed.

Investigation outputs may be deliberately captured as run-scoped supplementary evidence with `./bin/agd --home . reviewer evidence add RUN_ID --file PATH --label LABEL` and inspected as sanitized metadata with `reviewer evidence list RUN_ID`. The file must be a same-run owned regular UTF-8 artifact beneath the private run directory; symlinks, special files, hardlinks, known credential paths, and secret-bearing inputs are refused. Stored supplementary text shares the frozen seed packet's cumulative input-byte bound, while late investigation never widens seed eligibility or reviewed-material coverage. Failed or unknown runs' evidence is diagnostic-only until deliberately recaptured under a new run, which receives a new source ID and provenance. Reports may cite only frozen seed IDs and evidence captured for that run. Langfuse remains optional and query/read-only by policy; unavailable or delayed query data is an evidence limitation, not a negative finding, and no credentials are copied into requests or reports.

For execution-only investigation of a registered project, allocate a run-owned probe from an existing local commit:

```sh
./bin/agd --home . reviewer workspace create RUN_ID --project PROJECT_ID --commit COMMIT
./bin/agd --home . reviewer workspace list RUN_ID
./bin/agd --home . reviewer workspace cleanup RUN_ID --project PROJECT_ID --commit COMMIT --instruction OPERATOR_INSTRUCTION
```

The receipt binds the run, project, commit, Git common directory, base reference, branch, lease holder, lease ID, and path. Allocation uses an intent-owned local CAS base reference and does not fetch, create task metadata, install dependencies, or run project code. A returned path is exposed only after its receipt is durable. Cleanup is conservative: dirty or committed work, exposed probes without the exact operator instruction, active or detached users, and incomplete process inspection are retained or refused. A protected same-UID process (for example, an uninspectable user-manager process) can therefore block clean probe cleanup and the whole uninstall; AGD does not whitelist names/PIDs, bypass this with force, or change privileges. `uninstall --dry-run` inventories probe work-loss before any removal.
For an existing live OMP session, reload the installed extension through the official harness's supported command (`/reload` where that command is provided), then obtain a subsequent proved response; do not stop agents or invent historical boundaries. Direct central sessions are taskless reviewer episodes, while explicitly linked decisions remain contextual to their worker task. Packet citations and source hashes are frozen at selection time, so later native-session changes do not rewrite an earlier packet.


## Scoped PR repair workflow

Existing `publish` records one approved ready head; it is not an iterative PR workflow. A `ship` task intended for scoped PR repair must be created with `--skill-mode none`, then receive a target-specific `pr-publish` grant, for example `bin/agd task approve NAME --action pr-publish --target REMOTE:agd/NAME --instruction FILE`, followed by `bin/agd worker launch NAME --pr-grant UUID`. The granted worker uses only guarded `publish-pr`, `relay-poll`, `relay-reply`, and `verify-pr` commands with its task ID, incarnation, and grant. They non-force publish only the assigned branch; never merge, tag, or change the base branch.

Relay is privately installed on demand from pinned Open Relay commit `dac2865837bcab9444a8636053f4f1406a3f9291` and requires Node >=22.19. Deployment also needs central `AGD_RELAY_GITHUB_READ_TOKEN` with fine-grained Checks, Pull requests, and Metadata read access for the `generic-check-runs` connector and human-review watcher; producer/admin tokens are never given to workers. A task becomes ready only after AGD verifies its live GitHub current head, required checks/reviews, and `CLEAN` merge state. Failures stay durably blocked. The same-UID CLI checks are not an OS sandbox. See [operations](docs/operations.md#landing-publication-and-retention) for the exact command/file contract. No live GitHub publication or acceptance is claimed by this repository documentation.

## Customize an instance

Read the [customization skill](.agents/skills/customize/SKILL.md) when changing persona, workflows, tools, worker behavior or UI. It maps each request to its actual consumer and explains activation, approvals and update implications. `config/` is not a generic settings system, and tracked local customizations may require deliberate integration with upstream rather than a fast-forward update.
