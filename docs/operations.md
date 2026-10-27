# Local operations

All commands below are `bin/agd` from the distro checkout. Set `AGD_HOME` before launching OMP if the private home is elsewhere. An active central exports its owner token to tool commands; another shell receives a read-only refusal. This is an accidental-misuse guard, **not** an OS sandbox.

## Projects and task workspaces

Run `init`, then `project add ID --source URL_OR_PATH --base main` or register an existing clone at `projects/ID` with `project register ID --path ABSOLUTE_PATH`. `project refresh ID` requires a clean base branch and fast-forward. `project trust ID --instruction FILE` records explicit Cursor trust for that exact clone; registration alone does not grant it. `project remove ID` only unregisters. For irreversible deletion, obtain user instruction, run `project approve ID --action delete-clone --instruction FILE`, then `project remove ID --delete-clone --approval APPROVAL_ID`; tasks, dirty work, extra refs and unpublished reflog commits prevent deletion.

`task create NAME --project ID --brief FILE [--kind ship|scout]` obtains a distinct Treehouse v3.1.6 lease and worktree. `task status NAME` gives the path. Work and commits go **only** there, never in the stable clone. A task stuck `preparing` uses `task reconcile NAME`, not a second allocation. Scout tasks report unchanged work; ship tasks need committed changes. `task ready NAME --report FILE` checks the branch, lease, clean state and report. Readiness is not landing or publication approval.

## Worker orchestration

For tmux, `worker launch NAME --harness omp --backend tmux` starts a live worker in its recorded worktree. `worker inspect NAME`, `worker send NAME --file FILE` and `worker inbox NAME` expose state and durable steering; an OMP extension signals the queued message and the worker calls `worker acknowledge NAME MESSAGE_ID`. Enqueue/doorbell is **not** proof the instruction was processed. `worker interrupt NAME` sends Escape only for a confirmed busy OMP turn and does not assert process death. `worker stop NAME` submits `/quit` only after a native idle receipt and waits for process exit and endpoint absence; refusal retains the lease. `worker relaunch NAME` reuses the existing worktree only if the previous process and endpoint are positively absent. It preserves unacknowledged inbox messages and uncommitted files. An unknown state never triggers force-kill, blind restart or lease return.

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

For Herdr, run the operator/central CLI inside a Herdr-managed pane (`HERDR_ENV=1`). Start a **separate named, non-default** server, for example `herdr --session agd-distro-lab server` in its own terminal, and set `AGD_HERDR_SESSION=agd-distro-lab` plus the pinned Treehouse v3.1.6 path in `PATH` before starting the central OMP process. Then select `worker launch NAME --harness omp --backend herdr`. The adapter creates a home-owned workspace and one unfocused tab per worker; all reads and controls use that recorded server, workspace, tab, pane, terminal and socket identity. It never uses the focused/default session. Keep the named server alive until every retained task that used it has been cleaned: even a stopped worker's endpoint must remain verifiably absent before releasing its lease. An unavailable or replaced server is **unknown**, not proof a worker is gone.

The central extension starts one watcher per session and re-arms it once after unexpected exit. `session status`, `session reconcile`, `events list` and `events acknowledge EVENT_ID` inspect and handle durable wakes. Acknowledging a wake does **not** answer a decision: use `task status NAME` to inspect `open_decisions`, obtain the requested answer, and `decision resolve NAME KEY --answer FILE` to persist and enqueue it. Event receipt survives acknowledgement separately. A competing central must not take over while the first owner is alive. Worker phase `ready` can still mean its process is alive; stop it before cleanup.

Routine supervision is hidden from the conversation but stays available to the agent. Interactive OMP shows `Distro · watching · N pending` in the status footer; actionable task events still wake the agent visibly, and an unrecoverable watcher exit raises an error notification. Clients that do not render extension status requests can inspect `session status` and `events list`.

Cursor Agent still needs user authentication and a live transcript bound to its runner before either Cursor/backend combination can be claimed operational. Herdr 0.8.2 with protocol 20 has live OMP worker evidence only inside an explicitly named session. `doctor --harness cursor --backend herdr` diagnoses executable versions; it does not authenticate Cursor or provision a Herdr session.

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
