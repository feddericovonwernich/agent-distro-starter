# Agent distro starter

An OMP central session manages registered project clones, isolated Treehouse task worktrees, and supervised OMP workers. `projects/`, `config/`, `data/`, and `state/` belong to the local operator and are ignored by Git.

## Start

Install Git, OMP, tmux and **Treehouse v3.1.6** independently. Check the selected tools with `bin/agd doctor`; the globally installed Treehouse v2.3.0 is incompatible. From this checkout:

```sh
bin/agd init
bin/agd project add app --source /path/to/repository.git --base main
omp
```

OMP automatically loads `.omp/extensions/agd-primary.ts` from this checkout. The central process owns the home while running; unrelated shells can inspect but cannot mutate it. Use its tool environment for lifecycle commands. Workers explicitly load `lib/omp-worker.ts`, never the central extension.

For a private home elsewhere, set `AGD_HOME` to its absolute path before `bin/agd init` and `omp`. Keep the pinned Treehouse binary first on `PATH` for both the central process and workers. See [operations](docs/operations.md) for approvals, recovery and commands and [compatibility](docs/compatibility.md) for tested versus deferred combinations.

For the optional Herdr backend, run from a Herdr-managed pane and set `AGD_HERDR_SESSION` to a separately started, non-default named server before launching OMP; see [operations](docs/operations.md). Cursor workers still require account authentication and live transcript validation.

`project remove app` only unregisters and preserves the clone. Clone deletion needs a project-scoped approval and refuses retained tasks, uncommitted work and unverifiable local refs. No command publishes or merges without a separate, exact-target approval.
