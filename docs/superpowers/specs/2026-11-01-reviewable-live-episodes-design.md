# Reviewable final-response episodes in live sessions

Status: approved by the user; implementation awaits review of the implementation plan and execution-method selection.

## Intent and user decisions

Make completed worker and central-orchestrator episodes reviewable while their processes remain alive and reusable. Task readiness and process exit are different lifecycle facts; neither a running process nor a later in-progress interaction should prevent review of an earlier completed interaction.

The user explicitly selected **Only final responses**. An intentional handoff/wait for background work is not a completed review episode. Automatic retries, compaction, hidden corrections, queued continuations and other nonterminal settles are also ineligible.

“Final response” means the native terminal response returned to the operator, not that every delegated task in the distro has ended. Use explicit native continuation/wait signals; do not infer intent from prose or gate an independent completed interaction on unrelated active tasks.

The reviewer continues to propose existing-skill improvements or inert new-skill drafts. It does not edit skills, create implementation tasks, change models/auth, enable schedules, or approve its own proposals.

## Observed current behavior

- `reviewer._observation_candidates` admits only `runner-exit` observations and requires the current incarnation to be recorded dead. It therefore excludes the ready, still-alive `worker-smoke` task on jarvis-14.
- The worker extension already handles `agent_end` and skips `willContinue`; its idle observations do not carry an exact native transcript boundary.
- The primary extension has ownership, supervision and session-switch hooks, but no completed-episode capture. Existing reviewer central evidence is only explicitly linked task decisions, not first-class orchestrator episodes.
- Installed official OMP 18.14.0 exposes terminal `agent_end` signals, but its `appendEntry` targets the mutable current session and returns no persisted entry identity. A supported `before_agent_start` custom message can instead carry interaction/session binding in persisted `details`, recoverable from the later end-event messages.
- Native OMP can finish an assistant message with `stopReason: stop` and nevertheless schedule a continuation. Message text, stop reason, idle status and elapsed time are not authoritative final-response boundaries.
- Actual jarvis-14 native JSONL begins with a native `title` record followed by the version-3 session header. The current reader incorrectly requires the session header first. Native title metadata is mutable.
- Existing reviewer packets already retain admitted excerpts, citations, configuration snapshots and bounded evidence. The broker serves those frozen excerpts rather than rereading a live transcript during a model call. Reviewed excerpt identity and transactional report finalization are reusable.

## Goals

1. Admit explicit final-response episodes from living workers and managed central sessions.
2. Keep both agents available for follow-up work; do not stop, relaunch or alter task readiness for eligibility.
3. Exclude later unfinished turns, waits/handoffs and automatic continuations.
4. Support multiple sequential final responses in one native session without duplicate review of unchanged material.
5. Preserve home/task/incarnation/session provenance, byte/message citations, existing budgets, immutable packets and retryability.
6. Read the actual supported native JSONL format, including its title preamble.
7. Keep existing provable exited-worker evidence usable and reviewer-self evidence excluded by default.

## Non-goals

- Inferring a completed episode from prose, `task ready`, idle state, timestamps alone or quiet periods.
- Reviewing an entire live session up to arbitrary EOF or the reviewer's current clock.
- Capturing unmanaged/global OMP conversations or searching Langfuse for eligibility.
- Enabling reviewer timers, changing configured models/auth, auto-applying skills, or adding a second review engine.
- Modifying live jarvis-14 during design or silently hot-reloading its agents.

## Approaches

### Proposed: metadata-bound final-response checkpoints

Use a hidden native start binding and a small home-owned completion record, shared by workers and the managed central. Admit a checkpoint only after its terminal signal can be correlated unambiguously with the persisted native binding and final assistant entry. Select bounded, previously unreviewed evidence through that entry. Reuse the existing broker, packet, report/proposal and budget machinery.

This is the assistant's recommended implementation choice after checking the installed SDK: a delayed `appendEntry` cannot target its original session safely. A scoped immutable record avoids raw JSONL writes, an upstream SDK dependency, and another service. It stores identities and hashes, not a transcript copy.

### Rejected: broaden idle/ready or stop-reason heuristics

This is smaller superficially but cannot distinguish a genuine final response from waiting, retries, hidden corrections or a newly active turn. It would violate the user's final-response-only decision.

### Rejected: retain process-exit gating

This leaves the reported problem intact and makes a reusable orchestrator effectively unreviewable until shutdown.

## Completion capture

Use one small shared capture helper in the worker and primary extensions. At an ordinary prompt start, capture the native session identity/file, obtain its authorized source binding, and return a hidden `before_agent_start` custom message carrying an interaction UUID and source-binding identity in `details`. Native OMP persists that metadata and retains it in active messages.

The marker is bookkeeping, not another agent instruction. Set `display: false` and remove the marker from model-facing messages through the supported `context` transformation. That transformation works on a separate message array; it must not remove the binding from native history or the active state used for completion notification. Do not add skill lists, filesystem instructions or identifier tokens to the model prompt.

A checkpoint may be published only when the harness has settled a final response: `willContinue` is not true, `awaitingAsyncWork` is not true, and the snapshot contains a successful, nonempty final assistant response. An aborted, errored, tool-only or truncated response is not relabelled final. Stop reason alone remains insufficient; existing exit/failure evidence is separate.

At the start of the end callback, synchronously copy the relevant binding and final-response identity fields before any asynchronous work. Recover the latest applicable interaction binding from the event's messages, not from the mutable current context. Historical markers may exist, but missing, conflicting, already-closed or otherwise ambiguous current bindings must not fall back to an older interaction.

The logical completion record binds:

- owning home and role (`worker` or `central`), through an authorized native source receipt;
- captured native session identity/file and interaction UUID;
- task ID and incarnation for a worker; no invented task ID for a central;
- the authoritative terminal-response signal and an exact final-message fingerprint using timestamp, content and other stable native identity fields where available.

Publish this small record atomically through a guarded internal AGD recording operation. It must target the captured source rather than the current native session. Repeated identical publication is idempotent; conflicting data for an interaction is refused. Do not persist owner tokens, provider credentials, raw environment or duplicate transcript bodies.

Publication alone does not make evidence eligible. The reader must find the matching persisted native start marker and exactly one matching final assistant entry in the captured session/branch, before a later interaction's binding. It recovers the actual native entry ID and parent chain from those records. Zero matches, multiple matches, source mismatch or an incompatible branch cannot be resolved by guessing the current leaf.

Native message persistence and detached extension callbacks are not publicly fenced. A completion whose native records have not arrived remains pending and retryable; it is never admitted early or marked reviewed. A stale callback after a session switch can still name its original source, but cannot attach to the new session or its later active turn. Unprovable associations remain visible coverage gaps.

Automatic continuation/retry and deliberate `awaitingAsyncWork` yields produce no completion record. Their open binding may close at a subsequent genuine final response when that same binding is still provable. A revive without a provable open binding does not invent one or reopen a completed interaction. Existing supervision and inbox lifecycle semantics stay unchanged.

Capture failure closes eligibility, not the agent's work: retain a scoped diagnostic/coverage gap without aborting the interaction or changing existing task, inbox or supervision state. Capture remains independent of Langfuse/telemetry enablement and does not introduce another installation option.

Durability means complete records visible after a software-process crash, consistent with the native JSONL evidence contract; it does not claim power-loss durability for native session writes. A valid persisted source and boundary remain required after restart.

## Discovery and evidence windows

Source role comes from its authorized location/binding, not merely a role string inside transcript content:

- Workers: the exact home/task/incarnation observability binding, extended to identify the captured native session/header/file.
- Central: only this home's dedicated managed-central directory, with a persisted native source receipt registered against a matching managed launch process receipt and the live owner PID/start identity and token. `AGD_MANAGED_CENTRAL`, a role string or a directory path alone is insufficient authorization. Never store the bearer token. Missing or mismatched authorization is a visible refusal/coverage gap, not path-only admission.
- Reviewer: remains outside normal discovery; existing explicit retrospective selection is unchanged. A valid historical source receipt remains usable after its process exits or ownership changes; discovery does not require its owner to remain alive.

For each proved terminal checkpoint, construct a stable logical episode identity from role, home, native session and completion boundary, plus task/incarnation where applicable. Multiple completed episodes in one process/session are independent candidates. Preserve deterministic ordering and the existing candidate/input limits across both roles.

Admit only complete native records belonging to the completed episode's branch/prefix. The first checkpoint can admit the retained unreviewed prefix up to its final boundary; later checkpoints admit only new material since earlier coverage. A later in-progress prompt, tool result or partial JSONL tail must never enter an earlier episode's packet, even if it exists before reviewer startup.

Support the known native title preamble before the required session header. Title/UI metadata is not review evidence and must not make an unchanged completed episode appear new when renamed. Keep physical line/byte coordinates as selection-time citations, together with native entry identity and frozen content proof. Citations resolve to the immutable packet's captured material, not a promise that those offsets still address a subsequently rewritten live file. Do not retain another raw transcript prefix or add offset-translation storage. Reject malformed or redirected sources rather than guessing another session.

Existing confirmed runner-exit evidence can still define a closed worker range when no final-response checkpoints exist. Existing living histories without a proved checkpoint must not be retroactively declared final from `ready`, idle or prose. After capture is loaded, a subsequent genuine final-response checkpoint may close the retained unreviewed prefix; this is not a fabricated set of historical episode boundaries. Missing historical boundaries must be explicit in coverage rather than silently claimed complete.

## Packet, deduplication and review finalization

Generalize episode metadata so a central episode is a first-class source, not a fake worker task or a fabricated linked decision. Keep worker assignment provenance where available; central skill availability/usage must not be presented as a frozen worker assignment or proof of actual skill use.

Reuse existing frozen excerpts and citations. The reviewer must see the packet that was selected, not newly appended native bytes. Keep existing linked central decisions as contextual evidence without treating them as proof that every central conversation was reviewed.

Track reviewed material under role/session/completion scope and retain the existing message/content identity protections. A new final response should expose only unreviewed evidence; unchanged prior responses must not cause another model call. Renames, repeated scans and reader restarts must not change that result.

Advance coverage only for retained material after a committed report and successful existing finalization. Failure, timeout, interruption and an empty/no-work run must not consume new episodes. Input/candidate truncation must remain explicitly partial so omitted evidence stays eligible later.

The report/proposal schemas and citation validation remain shared. No second worker-only/central-only model dispatch or alternate authority path is introduced.

## Rollout and scope boundaries

Implementation belongs in the new leased `reviewable-live-episodes` worktree. Existing reports, candidate coverage and explicit retrospective behavior must remain intact; do not delete old private data to simplify migration.

Completion capture becomes available when the relevant native extension is loaded/reloaded through supported harness behavior. Do not kill an agent or alter a ready worktree merely to make a reviewer smoke check pass. Native disposable sessions are the first integration proof; updating/reloading a live distro is a separate explicitly scoped operation.

No remote push/PR/merge, unrelated worktree cleanup, schedule change or skill application is authorized by this design alone.

## Acceptance and verification

- A worker completes a final response, stays alive, and its episode becomes eligible.
- A managed central completes a final response, stays alive, and its episode becomes eligible without any worker task.
- A review while a later turn is running contains only the earlier completed range.
- Deliberate background-work waits, automatic retries, compaction and hidden continuation settles are not selected.
- Two sequential final responses are reviewable incrementally; repeated successful review does not repeat unchanged evidence.
- A session switch, stale callback, branch/rewind, partial tail or unsafe source cannot attach evidence to the wrong episode/session/home.
- Missing or mismatched managed-central authorization, copied foreign transcripts and forged role strings cannot make a source eligible.
- A persisted completion signal without its native start marker/final entry stays pending; a later successful scan can admit it without duplicate publication. Metadata markers never reach model-facing context.
- Actual title-prefixed native transcripts are accepted; title changes and late appends do not alter frozen evidence or cause duplicate review.
- Failed reviews leave material retryable, and budget-truncated material remains explicitly partial.
- Existing exited-worker selection and explicit reviewer retrospectives continue to work; default selection never captures reviewer-self sessions or unrelated homes.
- Retain deterministic behavior regressions and run the existing Python/Bun suites with documented host prerequisite limits. Also exercise actual native OMP session persistence with final responses while the process remains alive; assertions over fake forwarding or copied wiring do not count as proof.

## Builder-owned details

Choose minimal internal function names and the exact binding/completion schemas using the verified public SDK contracts. Reuse existing module layout, private atomic storage, ownership/process receipts and fixtures. Do not add dependencies or abstractions beyond the shared completion boundary and source-selection needs. Settle mechanical implementation choices in the plan; bring back only user-policy conflicts or a newly demonstrated missing native capability that materially changes this design.
