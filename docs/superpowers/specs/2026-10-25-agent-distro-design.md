# Agent distro starter — approved design

## Goal
A company can distribute one versioned directory of instructions, skills, executable tools, and harness integration. Users launch omp in its central home and work across managed projects, directly or through supervised workers. Existing company tools remain independent tools composed by the distro, not reimplemented behind a generic plugin framework.

## Approved scope
- Central harness: omp only.
- Worker harnesses: omp and Cursor Agent CLI.
- Worker backends: tmux and Herdr, independently selectable from the harness.
- Stable project clones under projects/ with registration, listing, inspection, guarded refresh, and safe removal.
- Isolated task worktrees for central-agent and worker code changes.
- Background monitoring, durable actionable events, omp wake delivery, and restart reconciliation.
- Completed implementation tasks produce a committed task branch and a report with verification evidence and limitations.
- Local merge and PR publication each require explicit user authorization. Publication does not authorize PR merge.
- Keep work until landing is confirmed or explicit discard is authorized.

## Distribution boundary
Use a standalone template, not a fork of all Firstmate functionality. Preserve license notices for any copied source. Review Herdr's AGPL/commercial distribution terms before bundling its binary; invoking an independently installed CLI is the proposed integration, not a legal conclusion.

Tracked surfaces:
- AGENTS.md: role, routing, authority, completion rules.
- .agents/skills/: project, workspace, and orchestration procedures.
- .omp/extensions/: startup, watcher ownership, actionable wake delivery, turn-end guard.
- bin/: executable lifecycle helpers and backend/harness adapters.
- docs/: setup, operations, supported-version evidence.

Private and gitignored surfaces:
- config/: default worker harness/backend and local tool settings; credentials remain in their existing providers' stores where possible.
- projects/: stable project clones.
- data/: project registry, durable task metadata, briefs, results, steering messages and acknowledgements.
- state/: endpoint observations, supervisor lock, watcher identity, pending events and handling receipts.

The configured home identifies ownership. Never discover owned work solely by matching terminal labels or directory names. Runtime files are reconstructable only where their source is independently retained; unhandled events must not be discarded as cache.

## Task contract
Each task has a stable ID, project identity, selected base branch and starting commit, task branch, canonical worktree path, worktree lease identity, brief, executor type, optional worker harness/backend/endpoint identity, result pointer, and landing evidence.

Lifecycle: prepare -> run -> ready -> land -> clean up.
Blocked, failed, interrupted, and unknown describe execution conditions, not permission to delete. A historical done message alone does not prove current readiness or landing.

Prepare persists intent before launch and records acquired resources as each succeeds. If launch fails, preserve the worktree and an actionable failure record. Recovery must not allocate a second workspace for the same recorded task.

Ready requires a clean committed implementation branch plus reported verification and limitations. Re-read branch/head and working-copy state before accepting readiness. Investigation-only work produces a durable report rather than artificial commits; preserve any unexpected changes for disposition.

Landing initially uses guarded fast-forward local merge or explicitly authorized PR publication using the company's selected forge tooling. Refuse conflicting or dirty local targets; do not force/reset/stash to bypass refusal. PR merge remains separately authorized. No automated multi-forge framework is required by this design.

Cleanup verifies no active owned worker, no uncommitted/untracked task work, and proof that the task's committed work landed. A published PR is not landing proof. When squash/rebase prevents simple ancestry proof, retain work until exact landing evidence is established; ambiguity never authorizes deletion. Explicit discard is a distinct destructive operation scoped to the exact task.

## Worktree provider
Propose Treehouse rather than custom pooling. Its documented noninteractive API supplies durable leases:
`treehouse get --lease --json --lease-holder <task-owner> --base <base> -b <task-branch>`.
Record the returned path and lease_id; prove the path belongs to the registered clone and differs from the stable checkout before launching anything.
Leases protect a workspace even when no worker process remains. Release only after the distro's landing/discard gate, using the recorded lease identity with Treehouse's conditional return interface. Never use blanket return/prune or unconditional force as ordinary task cleanup.
Treehouse's default pool need not live inside projects/. The registry tracks canonical paths; it must not assume a pool layout.

## Execution adapters
Keep backend and harness responsibilities separate without introducing a dynamically extensible plugin system.
Backend operations: create endpoint, inspect endpoint, deliver input/doorbell, capture bounded output, verify lifecycle actions, close owned endpoint. Implement only tmux and Herdr.
Harness operations: construct launch arguments/environment, identify worker process, obtain supported busy/idle evidence, interrupt/exit semantics, and install task status integration. Implement only omp and Cursor.
A configured default is overridden only by an explicit task choice. Dependency, authentication, or backend failure must not silently select another harness/backend.
Cursor must receive the acquired absolute worktree via --workspace; never --worktree/-w, which creates another copy. Firstmate documents transcript-based Cursor busy state and notes that Herdr's native Cursor blocked state is not reliable enough on its own. Unknown remains unknown. An Escape send is not proof a process stopped.
omp uses lifecycle events rather than rendered spinner text where available. Do not load project-discovered extensions again via -e.
Unattended workers need an explicitly documented approval posture. Firstmate's auto-approve/yolo launch flags are reference behavior, not automatic permission for this distro to adopt them. No worktree or prompt is a security sandbox; enforce sensitive boundaries through existing tool/server permissions as well as lifecycle checks.

## Messaging and supervision
Persist task steering messages before sending a terminal doorbell. Delivery and processing are separate states: an accepted enqueue is not proof the worker read it. Lifecycle controls are typed adapter actions, not natural-language steer messages.
Run one watcher owned by the active central omp session. Monitoring uses shell/native process and endpoint observations without routine model calls. Persist completion, blockers, unanswered decisions, and suspected stalls before delivering a wake. Handle events at least once with stable event IDs and acknowledgements after handling; never promise exactly-once external effects.
The omp extension owns watcher startup, successor/re-arm, wake delivery, and shutdown. The turn-end guard prevents a blind stop when delegated work exists without healthy monitoring. Startup reconciles pending events, owned endpoints, task worktrees, and leases. A competing central session must remain read-only after ownership refusal.
Distinguish busy, idle, missing, dead, and unknown. An unreachable backend is not proof a worker died. Suspected stalls trigger inspection rather than automatic destructive recovery. Relaunch requires evidence that the prior worker is stopped and reuses the recorded task workspace and brief; native chat-session resume is not promised.

## Minimal operations
Projects: add/register, list, inspect, refresh, unregister. Clone deletion requires explicit intent and active-task checks.
Tasks: create, list/status, inspect result, approved landing/publication, guarded cleanup.
Workers: launch, steer, inspect, interrupt, stop, relaunch.
Supervision: startup/reconcile, monitor, present events, acknowledge handled events.
These are conceptual operations, not a requirement for a new CLI framework or one script per verb.

## Verification acceptance
Before declaring implementation complete:
1. Register a disposable local repository; acquire two tasks and prove edits cannot affect the reference checkout or sibling task.
2. Prove a leased task survives worker exit and central restart without being reused.
3. Exercise all four worker combinations: omp/tmux, Cursor/tmux, omp/Herdr, Cursor/Herdr. Launch a real task, observe progress, steer it, receive its committed result, and stop its endpoint safely.
4. Exercise omp background wake and turn-end behavior on actual extensions, not just unit mocks.
5. Interrupt event handling and restart; show the pending decision/completion is presented again and can be acknowledged without duplicate resource creation.
6. Refuse cleanup for dirty, untracked, unlanded, active, or identity-mismatched work; preserve bytes/commits.
7. Demonstrate approved fast-forward landing and subsequent conditional lease release. Demonstrate PR publication only in an explicitly authorized disposable remote.
8. Exercise missing tools, authentication refusal, lost endpoint access, and failed launch without silent fallback or work loss.
9. Verify private configuration/state/projects do not enter tracked distribution content.
Keep focused deterministic regression checks for lifecycle invariants. Real credentialed harness smoke evidence is separate from portable checks. Report any unavailable runtime matrix entries rather than claiming them verified.

## Excluded
Claude Code; Cursor as central harness; persistent secondmates; remote execution; quota/model auto-routing; public relays; email/voice; autonomous merging; Firstmate's full validation pipeline; fleet dashboards; generic plugin/package management; automatic company-tool installation beyond explicitly approved setup.

## Handoff
The design and lifecycle were approved in conversation. The selected local repository is `projects/agent-distro-starter`; implementation planning is authorized, while this document does not claim implemented or runtime-verified software. Plan delivery in dependency order: project/task workspaces, worker execution, then omp background supervision and recovery; no intermediate slice substitutes for the complete agreed deliverable.

## Sources reviewed
- https://github.com/kunchenguid/firstmate/blob/main/AGENTS.md
- https://github.com/kunchenguid/firstmate/blob/main/docs/architecture.md
- https://github.com/kunchenguid/firstmate/blob/main/docs/scripts.md
- https://github.com/kunchenguid/firstmate/blob/main/.agents/skills/harness-adapters/references/harness/omp.md
- https://github.com/kunchenguid/firstmate/blob/main/.agents/skills/harness-adapters/references/harness/cursor.md
- https://github.com/kunchenguid/firstmate/blob/main/docs/herdr-backend.md
- https://github.com/kunchenguid/treehouse

These sources describe upstream behavior; their reported verification is not verification of this starter. Pin concrete dependency versions and test their actual contracts during implementation rather than claiming compatibility with moving upstream main.
