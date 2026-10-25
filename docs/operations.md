# Local operations

All commands below are `bin/agd` from the distro checkout. Set `AGD_HOME` before launching OMP if the private home is elsewhere. An active central exports its owner token to tool commands; another shell receives a read-only refusal. This is an accidental-misuse guard, **not** an OS sandbox.

## Projects and task workspaces

Run `init`, then `project add ID --source URL_OR_PATH --base main` or register an existing clone at `projects/ID` with `project register ID --path ABSOLUTE_PATH`. `project refresh ID` requires a clean base branch and fast-forward. `project trust ID --instruction FILE` records explicit Cursor trust for that exact clone; registration alone does not grant it. `project remove ID` only unregisters. For irreversible deletion, obtain user instruction, run `project approve ID --action delete-clone --instruction FILE`, then `project remove ID --delete-clone --approval APPROVAL_ID`; tasks, dirty work, extra refs and unpublished reflog commits prevent deletion.

`task create NAME --project ID --brief FILE [--kind ship|scout]` obtains a distinct Treehouse v3.1.6 lease and worktree. `task status NAME` gives the path. Work and commits go **only** there, never in the stable clone. A task stuck `preparing` uses `task reconcile NAME`, not a second allocation. Scout tasks report unchanged work; ship tasks need committed changes. `task ready NAME --report FILE` checks the branch, lease, clean state and report. Readiness is not landing or publication approval.

## Worker orchestration

For the verified transport, `worker launch NAME --harness omp --backend tmux` starts a live worker in its recorded worktree. `worker inspect NAME`, `worker send NAME --file FILE` and `worker inbox NAME` expose state and durable steering; an OMP extension signals the queued message and the worker calls `worker acknowledge NAME MESSAGE_ID`. Enqueue/doorbell is **not** proof the instruction was processed. `worker interrupt NAME` sends Escape only for a confirmed busy OMP turn and does not assert process death. `worker stop NAME` submits `/quit` only after a native idle receipt and waits for process exit and endpoint absence; refusal retains the lease. `worker relaunch NAME` reuses the existing worktree only if the previous process and endpoint are positively absent. It preserves unacknowledged inbox messages and uncommitted files. An unknown state never triggers force-kill, blind restart or lease return.

The central extension starts one watcher per session and re-arms it once after unexpected exit. `session status`, `session reconcile`, `events list` and `events acknowledge EVENT_ID` inspect and handle durable wakes. Acknowledging a wake does **not** answer a decision: use `task status NAME` to inspect `open_decisions`, obtain the requested answer, and `decision resolve NAME KEY --answer FILE` to persist and enqueue it. Event receipt survives acknowledgement separately. A competing central must not take over while the first owner is alive. Worker phase `ready` can still mean its process is alive; stop it before cleanup.

Cursor Agent cannot be live-tested without user authentication, and Herdr controls cannot be run from this non-Herdr session. Do not claim their worker/backend combinations operational; Herdr selection currently lacks an adapter and refuses. `doctor --harness cursor --backend herdr` only diagnoses executable versions, not authentication or backend session access.

## Landing, publication, and retention

For local landing obtain a concrete user instruction, `task approve NAME --action local-merge --target PROJECT:BASE --instruction FILE`, then `task land NAME --approval ID`. The exact ready head must fast-forward a clean stable clone. After a stopped worker, `task cleanup NAME` conditionally returns the lease. Dirty, unlanded or identity-uncertain work stays intact.

For a PR, `task approve NAME --action publish --target REMOTE:BRANCH --instruction FILE`; use the organization's **existing** forge tool for the approved push/review; `task publication-record NAME --approval ID --remote REMOTE --branch BRANCH --url URL --evidence FILE` checks the remote head and requires the URL in the supplied forge output. It does not independently query the forge review object, publish, merge or clean up. No PR publication has been authorized in this session.

Discard is irreversible: inventory the task and obtain explicit user instruction, then `task approve NAME --action discard --target NAME --instruction FILE`; `task cleanup NAME --discard --approval ID` applies only that approval and only after the worker is proved stopped. Keep an uncertain lease rather than risk losing work.
