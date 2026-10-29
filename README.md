# Agent distro starter

An OMP central session manages registered project clones, isolated Treehouse task worktrees, and supervised OMP workers. `projects/`, `config/`, `data/`, and `state/` belong to the local operator and are ignored by Git.

## Install and start

On Linux x86-64, review the [installer](install.sh) before running it. From the parent directory where you want the instance:

```sh
bash -c 'installer=$(curl -fsSL https://raw.githubusercontent.com/feddericovonwernich/agent-distro-starter/main/install.sh) && bash -c "$installer"'
```

The interactive installer asks for a lowercase name and central backend (`tmux` or `herdr`), refuses an existing destination, clones the canonical origin into `./NAME`, and preserves that origin. It initializes the clone's ignored private home and records the selected backend plus a generated non-default Herdr session name.

It can use APT only for missing Git, Python 3.12+, and selected `tmux`, after one explicit sudo prompt; blank or non-affirmative answers mean no elevation. OMP and Herdr are reused when compatible or installed/updated only after consent to the displayed latest stable release, exact Linux x86-64 asset, and published digest. Treehouse is reused when compatible or installed as **v3.1.6** after SHA-256 verification: `e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1`. Managed downloads are atomically installed in `~/.local/bin`, and their source, version, capabilities, artifact hash, and installed hash are recorded in the private home.

The installer optionally runs `omp login [provider]` in your terminal. It does not receive or store provider secrets; `omp models --json` only lists configured/selectable models and is not authentication proof. It then starts an interactive nested shell in the clone with `~/.local/bin` first on `PATH`; exiting that shell returns to the caller.

If the calling shell already defines `AGD_HOME`, unset it to use this clone's private home. Then:

```sh
unset AGD_HOME
bin/agd project add app --source /path/to/repository.git --base main
bin/agd start
```

`bin/agd start [--backend tmux|herdr]` attaches to a proven ready central or starts one after validating private dependency and launcher receipts. The explicit backend applies only to that invocation; otherwise a proven active central takes precedence over the saved backend. A conflicting override, foreign/partial/ambiguous state, or failed process/owner/watcher correlation refuses and preserves receipts, tasks, leases, and backend sessions. An omitted worker `--backend` inherits the proven active central backend; an explicit worker backend is local to that worker.

Use `AGD_HOME` with an absolute private-home path, or `bin/agd --home /absolute/home ...`, to operate another initialized instance. See [operations](docs/operations.md) for the Herdr split and lifecycle constraints, and [compatibility](docs/compatibility.md) for observed compatibility evidence.

## Worker skills

`bin/agd init` seeds an empty external private home, or a checkout home with no private `config`, `projects`, `data`, or `state`, with a `default` profile and nine reviewed bundles copied from tracked `bundled/worker-skills/`. Existing private-home state is untouched; init never upgrades or overwrites it. An operator may instead create private `config/workers.json` profiles and native bundles in `config/worker-skills/agd-ID/`. `worker skills list` and `worker profiles list` inspect the catalog. Launch with the configured default, `--profile NAME`, or the orchestrator-selected `--skill-mode curated --skill agd-ID`; profiles and curated skills cannot be combined. Selected bundles are frozen per task, so relaunch does not adopt catalog edits. Provisioned files are task-owned ignored artifacts and are checked before readiness/delivery and removed only during safe cleanup. OMP 18.3.2 project discovery and relative resources are verified; Cursor nonempty provisioning refuses until authenticated live discovery evidence exists.

The central agent has pinned upstream skills under `.agents/skills/`; the Relay integration adds five central-only Open Relay skills. Workers receive only the reviewed nine default bundles, plus one grant-only AGD-authored `agd-scoped-pr-relay` bundle for a valid `pr-publish` task, not every central skill. Pinned upstream SHAs and license notices are recorded in [`third_party/PROVENANCE.md`](third_party/PROVENANCE.md).

`project remove app` only unregisters and preserves the clone. Clone deletion needs a project-scoped approval and refuses retained tasks, uncommitted work and unverifiable local refs. No command publishes or merges without a separate, exact-target approval.

## Background skill review

`bin/agd reviewer run` manually reviews a bounded frozen packet of completed worker evidence for existing-skill improvements or inert new-skill drafts. It writes private receipts, reports, proposal revisions, triage, events, and native reviewer session data under the effective home; it never edits skills, creates tasks, approves a proposal, or reviews itself by default. The reviewer model has exactly `reviewer_excerpt`, `reviewer_search`, and `reviewer_submit`; this model-tool boundary is not an OS sandbox.

Use `reviewer status`, `reviewer reports list|show ID`, and `reviewer proposals list|show ID` to inspect retained data. `reviewer proposals dispose ID --status accepted|rejected|deferred|superseded` records human triage only: acknowledgement, report access, and disposition are separate from authority to edit. See [operations](docs/operations.md#background-reviewer) for limits, privacy, timer approval, and failure states, and [compatibility](docs/compatibility.md#background-reviewer-acceptance) for the exact acceptance evidence and external blockers.


## Scoped PR repair workflow

Existing `publish` records one approved ready head; it is not an iterative PR workflow. A running `ship` task instead needs a target-specific `pr-publish` grant, for example `bin/agd task approve NAME --action pr-publish --target REMOTE:agd/NAME --instruction FILE`, then `bin/agd worker launch NAME --pr-grant UUID`. The granted worker uses only guarded `publish-pr`, `relay-poll`, `relay-reply`, and `verify-pr` commands with its task ID, incarnation, and grant. They non-force publish only the assigned branch; never merge, tag, or change the base branch.

Relay is privately installed on demand from pinned Open Relay commit `dac2865837bcab9444a8636053f4f1406a3f9291` and requires Node >=22.19. Deployment also needs central `AGD_RELAY_GITHUB_READ_TOKEN` with fine-grained Checks, Pull requests, and Metadata read access for the `generic-check-runs` connector and human-review watcher; producer/admin tokens are never given to workers. A task becomes ready only after AGD verifies its live GitHub current head, required checks/reviews, and `CLEAN` merge state. Failures stay durably blocked. The same-UID CLI checks are not an OS sandbox. See [operations](docs/operations.md#landing-publication-and-retention) for the exact command/file contract. No live GitHub publication or acceptance is claimed by this repository documentation.

## Customize an instance

Read the [customization skill](.agents/skills/customize/SKILL.md) when changing persona, workflows, tools, worker behavior or UI. It maps each request to its actual consumer and explains activation, approvals and update implications. `config/` is not a generic settings system, and tracked local customizations may require deliberate integration with upstream rather than a fast-forward update.
