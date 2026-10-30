# Worker protocol skill and lifecycle guidance

Status: proposed implementation design; skill-plus-hooks approach approved in conversation, implementation details pending plan review.

## Intent and scope

OMP should advertise available task skills natively, not receive a repeated list of paths and instructions to read every skill. Every managed OMP worker should receive a small AGD protocol skill, with runtime reminders that survive compaction and a bounded check before yielding an unfinished assignment. The model still decides how to do the work; AGD remains authoritative for state and permissions.

This changes the shared distro template, not a running installation. Existing worker sessions are not hot-patched. Central plugin installation, optional upstream skill names/bytes, reviewer tools, and Cursor behavior remain unchanged. Cursor does not gain a claimed protocol-hook capability.

## Current implementation and evidence

Baseline: `origin/main` at `b94415f` after PR #16.

- `bin/agd_lib/harnesses.py:launch_argv` passes an exact native skill allowlist, then repeats skill names, absolute paths, and a requirement to read all skills. It also embeds ordinary and scoped-PR command recipes in the initial prompt.
- `tasks.create` freezes selection before worktree allocation; `_complete_preparation` publishes `prepared` only after selected skill materialization. A `none` task currently has no manifest.
- `worker_skills.prepare/ensure/verify/remove` own snapshots, native-name collisions, source provenance, shared Git exclusions, and artifact cleanup. Reuse that machinery.
- `workers.launch` currently rejects any skill manifest for a PR grant and creates an adapter-only manifest at launch. A mandatory base skill requires changing that check without permitting ordinary task skills in the PR workflow.
- `lib/omp-worker.ts` already owns inbox delivery, busy/idle observations, terminal reply persistence, and ready-worker recovery. The new hooks belong there, not in a second extension or ambient plugin.
- `.omp/extensions/agd-primary.ts` already uses `before_agent_start` and a bounded `session_stop` continuation.
- Inspected OMP 18.14.0 source: `extensibility/extensions/types.ts` exposes `before_agent_start` with `systemPrompt: string[]`, `context` with transformed messages, and `session_stop`. `session/agent-session.ts` invokes the stop hook before `agent_end`, setting `willContinue: true` when the hook continues. These are source observations, not completed native acceptance evidence.
- The merged CLI still marks removed `worker acknowledge`, but not `worker complete`, as worker-scoped in `main`. Fix that adjacent prerequisite and exercise completion under a live central owner, without giving workers the owner token.

## Approaches

1. **Skill only:** removes prompt clutter but leaves discovery, compaction, and forgotten completion to the model.
2. **Skill plus existing worker-extension hooks — selected:** native discoverability, a single protocol source, fresh state reminders, bounded correction, and existing command guards.
3. **Dedicated AGD model tools:** can hide CLI syntax but creates another command surface. Not needed for this change.

## Global constraints

- Target OMP 18.14.0+ native extension APIs; verify the installed version's event ordering before claiming support.
- No new production dependency, plugin installation, generic policy framework, or model-tool API.
- Preserve upstream skill names, bytes, resources, source provenance, and task-frozen optional selections.
- `--skill-mode none` means no optional task skills; every newly created managed OMP task still receives `agd-worker`.
- Ambient extension discovery stays disabled; native skills are exactly the frozen effective manifest.
- Mandatory protocol guidance is not approval, an OS sandbox, or proof the model obeyed it.
- Never infer readiness from assistant prose or a completed turn. Only the existing guarded AGD transition may mark a task ready.
- Never repeat the completed assignment when recovering a ready worker; follow-up reply capture remains extension-owned.
- No hot migration, aliases, overwrite of retained snapshots, or silent addition of permissions to existing tasks.

## 1. Skill ownership and selection

The runner sets `AGD_CLI` to this checkout's absolute `bin/agd` path and sets `AGD_PR_GRANT` only for the task's bound grant, clearing any inherited value for ordinary workers. Recipes use `"$AGD_CLI" --home "$AGD_HOME"` and quote every ID/path. These convenience bindings never substitute for command-side authorization.

Create `bundled/worker-skills/agd-worker/SKILL.md` and `references/commands.md`.

The root skill is the short mandatory core: work only on the assigned task/worktree; inspect authoritative state; use relevant optional skills through `skill://NAME`; report readiness through guarded AGD commands; report blockers/decisions explicitly; answer ready-task follow-ups without modifying the workspace; never approve, merge, clean up, or publish outside a grant. The commands reference carries exact shell recipes, including quoting, report paths, task/incarnation flags, and error handling.

The core is both natively loadable and automatically supplied by the worker extension. It tells the model the core is already loaded; command details remain on demand. Do not store a second handwritten copy of the rules in TypeScript or the launch prompt.

Keep `agd-scoped-pr-relay` separate. Replace its dependency on commands copied from the launch prompt with self-contained guarded recipes using the runtime binding. Never expose it without a validated PR grant. Its procedure overrides the ordinary completion route: granted workers use `verify-pr`, not a direct `task ready` shortcut.

### Data contract

Extend the existing schema-2 selection with `system_skills: list[str]`. `skills` continues to mean optional task skill IDs; `sources` covers the union of optional and system IDs. For new OMP tasks, `system_skills` initially equals `["agd-worker"]`. The existing manifest's `skills` records cover that complete union. No parallel journal or second snapshot store.

- Profile/curated selection resolves ordinary skills unchanged; protocol source identity and hashes are captured before allocation too.
- `none` does not require orchestrator plugin registrations just to provision the AGD bundle.
- Reserve both AGD-owned names: reject optional selection, custom/plugin collisions, and conflicting native project/ancestor skills instead of selecting a shadow copy.
- At valid PR launch, append `agd-scoped-pr-relay` to the system set through a narrowly scoped journal extension. Preserve every existing record and hash. Do not resnapshot or replace `agd-worker`.
- Record grant identity in that append transaction. Retry only the same grant; an incomplete or conflicting transaction retains the task and refuses endpoint creation. Recovery can finish an exact complete snapshot or refuse changed/partial bytes; it must never allocate a second worktree or silently adopt new source bytes.
- Inspection reports optional and system IDs separately from their actual manifest records. Launch and relaunch use only that manifest.
- Existing schema-2 records without `system_skills` remain readable/cleanable. A new-code OMP launch/relaunch of such a task refuses with explicit recreate-task guidance; do not manufacture a protocol snapshot for retained tasks. Existing live processes continue on their loaded code.
- Cursor remains on its existing selection/provisioning path; do not turn `none` into an unsupported Cursor skill request.

## 2. A small worker-scoped context query

Add `worker context TASK --incarnation TOKEN`, implemented by `workers.context(home, task_id, incarnation)`.

It is read-only and does not need a backend inspection or the central owner token. Validate task ID, current incarnation, worker executor, OMP harness, and matching worker environment when present. Refuse foreign/stale bindings, missing protocol artifacts, or corrupt state; never return another task's details to a worker.

Return schema 1 with these fields:

- `task_id`, `incarnation`, `phase`, `kind`, `worktree`, `brief`, `report`;
- `agd_argv`: absolute executable and `--home` arguments as an array, not an interpolated shell command;
- `pr_grant`: the bound grant ID or null (not a new authorization);
- `task_skills`, `system_skills`: the frozen ID lists;
- `protocol`: the frozen core file path and SHA-256;
- `outcome`: `unfinished`, `ready`, `blocked`, `decision`, or `closed`.

Compute the outcome from task state and durable current-incarnation observations, not assistant messages or the watcher's delayed reconciliation. Ready/landed/cleaned states take precedence. For a running task, fold `blocked`, `decision`, and `resolved` by key, ordered by `(observed_at, id)`; the latest unresolved blocker/decision qualifies, with decisions taking precedence. Respect a persisted resolved decision receipt even if its old observation still exists. Ignore old incarnations, normal busy/idle/done traffic, and the extension's reserved `agd-contract-incomplete` diagnostic when computing a legitimate blocking outcome.

The context exposes bindings, not credentials, central tokens, all-task records, reports, or full observation bodies. Hash the small core and validate its expected manifest identity when loaded; existing lifecycle verification still validates the full skill tree.

## 3. Clean prompt and injected context

For OMP, the visible startup prompt becomes `Read the assigned task brief and complete the assignment in its workspace.` The runtime context supplies the brief and workspace bindings. Remove all optional skill path/name enumeration and ordinary/PR CLI recipes from the OMP prompt. A brief may still require a particular procedure by its native name. Leave Cursor's existing instructions intact until that harness has its own verified support.

Use two native extension surfaces:

1. `before_agent_start`: load/cache the validated frozen protocol core and add one marked AGD segment to the event's `systemPrompt` array. Preserve all unrelated native policy. Replace, rather than append to, any previous AGD segment. Do not create a separate model turn or force a redundant skill read.
2. `context`: query current task context and include exactly one hidden `agd:worker-context` message in the outgoing messages. Remove only the previous message of that custom type. Include task bindings, phase/outcome, current FIFO request ID when present, and a phase-appropriate next-action reminder. Do not put skill bodies, repeated skill inventories, or recipes in that message.

The native context hook runs for requests within a turn as well, so an agent which just marked ready receives a read-only reminder on its next model request. Compaction must not remove the system core; subsequent context processing regenerates the dynamic message. Verify this against the real harness rather than adding redundant compaction hooks by assumption. On session switch, revalidate the binding and clear cached policy/state before use.

If the query or protocol load fails, do not invent a normal task state or proceed without mandatory guidance. Refuse startup where possible; at runtime surface a visible diagnostic, try to emit a durable blocked observation, and never request an automatic continuation on unknown state. Failure to persist a diagnostic remains visible, not silently suppressed.

## 4. Bounded completion guard

At `session_stop`, reread context rather than trust the last injected state.

- For an initial assignment turn with no active FIFO follow-up, `phase=running` and `outcome=unfinished` requests one corrective continuation: submit a verified report through the appropriate route or record a concrete blocker/decision.
- Track the correction budget across the continuation's `agent_start`; do not reset it on that event. Respect `stop_hook_active` and never continue after an aborted signal, provider-error terminal result, or explicit user interruption/shutdown.
- If the correction still leaves the assignment unfinished, emit one `blocked` observation with reserved key `agd-contract-incomplete` for that stop episode, then allow idle. Retain the worktree and original phase. No infinite retry and no fabricated ready result.
- Ready/closed tasks, an actual unresolved blocker/decision, and active FIFO follow-up turns do not trigger assignment-completion correction.
- `agent_end(willContinue=true)` must not complete a request, mark idle, or consume the next inbox message. Only the eventual terminal event may do so.
- Do not automatically call `task ready` or `verify-pr` from a hook. Do not block process teardown waiting for the model.

The extension writes its fixed diagnostic under the task data directory and uses existing `worker event` semantics. A local latch prevents repeated emission for the same episode; this is not a new exactly-once event guarantee across crashes.

## Verification and acceptance

1. New none/profile/curated tasks expose the mandatory base plus exactly their selected optional skills. Granted PR workers expose base plus scoped adapter only. Verify actual native resolution paths, not assistant testimony.
2. Source mutation/removal after completed snapshots does not affect existing tasks; tampering, collisions, failed append, or stale grants never launch an endpoint.
3. The core reaches actual provider input without being a visible launch wall; native `skill://agd-worker` and supporting reference reads resolve from the frozen worktree copy.
4. The core and exactly one fresh dynamic message survive compaction and a readiness transition in the same turn. Do not drop unrelated system instructions.
5. An unfinished initial assignment gets one corrective turn, then a durable central blocked event if unresolved. Ready, blocked, decision, interrupted, errored, and FIFO-follow-up paths never spin or fabricate readiness.
6. Two follow-ups retain worker identity and produce two correlated replies. A stop-hook continuation cannot cause early completion. Completion works with a live central owner and no worker owner token.
7. Ready-worker recovery retains its no-initial-prompt behavior. Cleanup still verifies/removes the effective owned skill set and preserves unrelated files.
8. Exercise native OMP with scripted provider output for deterministic hooks/resolution, then one disposable backend lifecycle with the actual CLI. Preserve backend ownership gates and report missing prerequisites instead of narrowing acceptance.

## Deliberate limits

No new required-skills configuration: explicitly required optional skills remain instructions in the brief. No generic event-policy engine or raw-shell command blocker. No security claim from prompts/hooks. No new native conversation-resume promise across incarnations. Native OMP probes are an implementation acceptance gate, not evidence already obtained during planning.
