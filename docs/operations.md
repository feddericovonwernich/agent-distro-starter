# Local project and task operations

Run `bin/agd init` once per home. Register a clone under `projects/` with `project add` or `project register`; `project refresh` refuses a dirty clone or a branch other than its registered base. `project remove` unregisters without deleting the checkout.

Create a task with `bin/agd task create NAME --project PROJECT --brief FILE`. The returned worktree is leased from Treehouse, independent of the stable clone. Code changes belong in that worktree. `task status NAME` returns the exact path and lease ID after successful allocation. A task left `preparing` after a crash is reconciled through `task reconcile NAME`; it will not allocate a second slot automatically.

When a ship task has committed clean changes, write a report naming the work and checks, then run `task ready NAME --report FILE`. Readiness does not authorize publication or merge. Record an explicit, task/head/target-scoped instruction with `task approve NAME --action local-merge --target PROJECT:BASE --instruction FILE`; pass the returned approval ID to `task land NAME --approval ID`. Landing requires a clean stable clone and fast-forward to the exact approved commit. Then `task cleanup NAME` conditionally returns the Treehouse lease. It refuses dirty or unlanded work.

For PR publication, approval action is `publish` with target `REMOTE:BRANCH`. The user-approved agent uses its existing forge tool to create the review; `task publication-record NAME --approval ID --remote REMOTE --branch BRANCH --url URL --evidence FILE` records the review URL and verifies the remote branch matches the ready commit. This does **not** merge a PR or authorize task cleanup. The distro does not select a company's forge.

An explicit `discard` approval must target the exact task ID and current file inventory; `task cleanup NAME --discard --approval ID` destroys that task's unlanded work, so only execute after explaining the irreversible loss. Never approve on behalf of a user.

Unattended worker execution, tmux/Herdr orchestration, and background supervision are not yet available in this implementation slice.
