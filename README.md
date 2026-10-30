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

The fifth stage asks `Enable scheduled skill reviews? [y/N]`, independently of telemetry. After consent, choose daily at 02:00 UTC, every 6 hours, every hour, a custom daily HH:MM/IANA timezone, or a custom positive whole-hour interval. Blank/No skips setup; menu `0` or EOF cancels without changing config. Reviews inspect completed worker evidence, can incur model-provider charges, and produce reports/proposals—not automatic skill edits.

Reviewer activation reuses installed Bun, an importable compatible OMP SDK, the distro's native agent directory, and the user-systemd manager; no runtime is downloaded, no sudo/linger is used, and authentication/model settings remain OMP's responsibility. The normal standalone OMP release does not expose that SDK. Missing prerequisites retain the selected schedule and clearly report configured/not enabled, without claiming a review can run. Retry selection with `bin/agd installer reviewer`, or after fixing prerequisites run `bin/agd reviewer timer enable`; inspect/disable with the corresponding timer commands. Existing active or partially configured timers must be explicitly disabled before changing their schedule.

Provider login and model configuration belong to OMP, not this installer: there are no login or model-selection prompts. Read-only dependency compatibility probes do not prove authentication. Managed OMP processes use a distro-private `HOME` at `data/omp/home/`, fix OMP's agent, profile, and XDG storage under `data/omp/`, pin Treehouse worktrees to `data/treehouse/`, and keep project plugin records in this distro's `.omp/`; the caller's effective Git global configuration remains available to Git. The explicit Treehouse root lets central and worker processes resolve the same lease even though their inherited `HOME` values differ. OMP's private home is required because OMP 18.14 uses its home-relative config name for project plugin discovery. The interactive nested shell preserves the caller's real `HOME` and shell configuration; exiting it returns to the caller. Run native commands through `bin/agd omp`, for example `bin/agd omp login`; AGD does not copy or configure credentials.

If the calling shell already defines `AGD_HOME`, unset it to use this clone's private home. Then:

```sh
unset AGD_HOME
bin/agd project add app --source /path/to/repository.git --base main
bin/agd start
```

`bin/agd start [--backend tmux|herdr]` attaches to a proven ready central or starts one after validating private dependency and launcher receipts. The explicit backend applies only to that invocation; otherwise a proven active central takes precedence over the saved backend. A conflicting override, foreign/partial/ambiguous state, or failed process/owner/watcher correlation refuses and preserves receipts, tasks, leases, and backend sessions. An omitted worker `--backend` inherits the proven active central backend; an explicit worker backend is local to that worker.

Use `AGD_HOME` with an absolute private-home path, or `bin/agd --home /absolute/home ...`, to operate another initialized instance. See [operations](docs/operations.md) for the Herdr split and lifecycle constraints, and [compatibility](docs/compatibility.md) for observed compatibility evidence.

## Worker skills

`bin/agd init --backend tmux|herdr` seeds an empty home with a `default` profile naming nine canonical upstream skills. It does not copy skill payloads. After setup installs the official Ponytail and Superpowers project plugins, `worker skills list` resolves those exact upstream names, metadata, resources, versions, and paths from OMP's validated native registry. Existing private-home state is untouched; init never upgrades or overwrites it. Operators may add private custom skills under `config/worker-skills/NAME/` and select them in `config/workers.json`; a custom/plugin name collision refuses rather than overriding either source.

Choose the harness and immutable optional skill set at `task create`: use the configured default, `--profile NAME`, `--skill-mode curated --skill NAME`, or explicit `--skill-mode none`. Every OMP task also receives the mandatory AGD-authored `agd-worker` protocol; `none` disables optional skills, not that protocol. Creation freezes both sets under canonical names in ignored worktree paths before publishing `prepared`. Launch and relaunch use the frozen snapshot, never mutable plugin payloads. A valid `pr-publish` grant on a task created with no optional skills appends `agd-scoped-pr-relay` without replacing the base protocol. Both AGD names are reserved. The five Open Relay skills under `.agents/skills/` remain central-only; provenance is recorded in [`third_party/PROVENANCE.md`](third_party/PROVENANCE.md).

OMP workers are retained task executors, not one-shot agents. Native launch controls disable ambient extensions and expose exactly the task's optional and system skill names. Startup prompts no longer enumerate skills or filesystem paths: native discovery makes optional skills available on demand, while the worker extension injects the verified AGD core and fresh task context. An unfinished initial assignment gets at most one corrective turn, then a durable blocked diagnostic—not fabricated readiness. CLI approvals and state checks remain authoritative. Existing OMP tasks without a frozen protocol must be recreated; Cursor does not receive these hooks.

`worker send NAME --file FILE` queues asynchronous follow-ups in the same live session; terminal replies become durable correlated events. Queued requests survive crash/relaunch with at-least-once delivery, but new sends refuse stopped, missing, or unverified workers. A live `ready` worker answers read-only follow-ups. `worker relaunch NAME` verifies process absence, the ready head and workspace, then consumes retained requests without replaying the completed brief.

`project remove app` only unregisters and preserves the clone. Clone deletion needs a project-scoped approval and refuses retained tasks, uncommitted work and unverifiable local refs. No command publishes or merges without a separate, exact-target approval.

## Background skill review

`bin/agd reviewer run` manually reviews a bounded frozen packet of completed worker evidence for existing-skill improvements or inert new-skill drafts. It writes private receipts, reports, proposal revisions, triage, events, and native reviewer session data under the effective home; it never edits skills, creates tasks, approves a proposal, or reviews itself by default. The reviewer model has exactly `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit`; this model-tool boundary is not an OS sandbox.

Use `reviewer status`, `reviewer reports list|show ID`, and `reviewer proposals list|show ID` to inspect retained data. `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded` records human triage only: acknowledgement, report access, and disposition are separate from authority to edit. See [operations](docs/operations.md#background-reviewer) for limits, privacy, timer approval, and failure states, and [compatibility](docs/compatibility.md#background-reviewer-acceptance) for the exact acceptance evidence and external blockers.


## Scoped PR repair workflow

Existing `publish` records one approved ready head; it is not an iterative PR workflow. A `ship` task intended for scoped PR repair must be created with `--skill-mode none`, then receive a target-specific `pr-publish` grant, for example `bin/agd task approve NAME --action pr-publish --target REMOTE:agd/NAME --instruction FILE`, followed by `bin/agd worker launch NAME --pr-grant UUID`. The granted worker uses only guarded `publish-pr`, `relay-poll`, `relay-reply`, and `verify-pr` commands with its task ID, incarnation, and grant. They non-force publish only the assigned branch; never merge, tag, or change the base branch.

Relay is privately installed on demand from pinned Open Relay commit `dac2865837bcab9444a8636053f4f1406a3f9291` and requires Node >=22.19. Deployment also needs central `AGD_RELAY_GITHUB_READ_TOKEN` with fine-grained Checks, Pull requests, and Metadata read access for the `generic-check-runs` connector and human-review watcher; producer/admin tokens are never given to workers. A task becomes ready only after AGD verifies its live GitHub current head, required checks/reviews, and `CLEAN` merge state. Failures stay durably blocked. The same-UID CLI checks are not an OS sandbox. See [operations](docs/operations.md#landing-publication-and-retention) for the exact command/file contract. No live GitHub publication or acceptance is claimed by this repository documentation.

## Customize an instance

Read the [customization skill](.agents/skills/customize/SKILL.md) when changing persona, workflows, tools, worker behavior or UI. It maps each request to its actual consumer and explains activation, approvals and update implications. `config/` is not a generic settings system, and tracked local customizations may require deliberate integration with upstream rather than a fast-forward update.
