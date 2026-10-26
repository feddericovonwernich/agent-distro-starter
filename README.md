# Agent distro starter

An OMP central session manages registered project clones, isolated Treehouse task worktrees, and supervised OMP workers. `projects/`, `config/`, `data/`, and `state/` belong to the local operator and are ignored by Git.

## Install a named local instance

On Linux, install Bash, curl, Git, Python 3.12+, OMP, tmux and Treehouse **v3.1.6** independently. Review the [installer](install.sh) before running downloaded code. From the parent directory where you want the instance:

```sh
bash -c 'installer=$(curl -fsSL https://raw.githubusercontent.com/feddericovonwernich/agent-distro-starter/main/install.sh) && bash -c "$installer"'
```

Enter a name starting with a lowercase letter, followed by lowercase letters, digits or hyphens (up to 48 characters). The installer refuses existing paths, clones into `./NAME` with its template Git origin intact, and initializes ignored private state in that clone. It does not install dependencies, authenticate Cursor or configure projects.

```sh
cd ./NAME
unset AGD_HOME  # use the clone's private home unless intentionally configured otherwise
bin/agd doctor
bin/agd project add app --source /path/to/repository.git --base main
omp
```

`doctor` checks executable versions, not account authentication. OMP/tmux and OMP/Herdr have been exercised on Linux; Cursor workers still need authentication and live validation.

## Manual setup from a source checkout

Install Git, Python 3.12+, OMP, tmux and **Treehouse v3.1.6** independently. Check the selected tools with `bin/agd doctor`; older Treehouse releases are incompatible. From this checkout:

```sh
bin/agd init
bin/agd project add app --source /path/to/repository.git --base main
omp
```

OMP automatically loads `.omp/extensions/agd-primary.ts` from this checkout. The central process owns the home while running; unrelated shells can inspect but cannot mutate it. Use its tool environment for lifecycle commands. Workers explicitly load `lib/omp-worker.ts`, never the central extension.

For a private home elsewhere, set `AGD_HOME` to its absolute path before `bin/agd init` and `omp`. Keep the pinned Treehouse binary first on `PATH` for both the central process and workers. See [operations](docs/operations.md) for approvals, recovery and commands and [compatibility](docs/compatibility.md) for tested versus deferred combinations.

For the optional Herdr backend, run from a Herdr-managed pane and set `AGD_HERDR_SESSION` to a separately started, non-default named server before launching OMP; see [operations](docs/operations.md). Cursor workers still require account authentication and live transcript validation.

## Optional worker skills

An operator may create private `config/workers.json` profiles and native bundles in `config/worker-skills/agd-ID/`. `worker skills list` and `worker profiles list` inspect the catalog. Launch with the configured default, `--profile NAME`, or the orchestrator-selected `--skill-mode curated --skill agd-ID`; profiles and curated skills cannot be combined. Selected bundles are frozen per task, so relaunch does not adopt catalog edits. Provisioned files are task-owned ignored artifacts and are checked before readiness/delivery and removed only during safe cleanup. OMP 18.3.2 project discovery and relative resources are verified; Cursor nonempty provisioning refuses until authenticated live discovery evidence exists.

`project remove app` only unregisters and preserves the clone. Clone deletion needs a project-scoped approval and refuses retained tasks, uncommitted work and unverifiable local refs. No command publishes or merges without a separate, exact-target approval.

## Customize an instance

Read the [customization skill](.agents/skills/customize/SKILL.md) when changing persona, workflows, tools, worker behavior or UI. It maps each request to its actual consumer and explains activation, approvals and update implications. `config/` is not a generic settings system, and tracked local customizations may require deliberate integration with upstream rather than a fast-forward update.
