# Exact worker skills and retained-worker replies

**Status:** Implemented

## Intent

A task's recorded skill selection must be the complete set exposed to its worker. A retained worker must also support repeated central-to-worker request/reply turns without requiring prompt-specific shell instructions.

The current implementation violates both expectations:

- `smoke-test-task` records `skill-mode: none`, an empty selection, and no skill manifest, but OMP discovers 21 Ponytail and Superpowers skills from the distro's shared XDG plugin cache.
- `worker send` durably queues a follow-up and the worker extension delivers it through OMP's native `sendUserMessage(..., {deliverAs: "followUp"})`, but terminal `agent_end` only records `idle`. Supervision intentionally does not wake central for ordinary idle observations, so the final assistant response remains only in the worker transcript.

Workers are retained until explicit stop/cleanup. A ready task may keep `worker_alive: true` and reuse the same OMP session for read-only follow-ups. Existing readiness-integrity checks continue to reject or warn about workspace changes after readiness.

## Goals

- Expose exactly the task's immutable worker-skill manifest to OMP.
- Prevent installed orchestrator plugins, their extensions, and unrelated cached skills from leaking into workers.
- Keep the central OMP plugin installation unchanged.
- Make every `worker send TASK --file FILE` request produce one correlated durable completion notification containing the worker's final assistant response.
- Keep one retained OMP worker/session for successive follow-ups.
- Preserve queued requests across a worker crash or relaunch and make completion retries idempotent.

## Non-goals

- A blocking command that waits for a model turn.
- A second `worker ask` API alongside `worker send`.
- Parallel turns within one OMP worker session.
- Automatic acceptance of code changes after a task is ready.
- Migrating or mutating already-running worker processes.
- Changing central plugin installation or OMP provider/model configuration.

## Approaches considered

### Upgrade `worker send` — selected

Keep the existing central command and make its queued message a durable request. The worker extension delivers one request at a time and records the terminal response. Central receives the existing supervision event mechanism.

This is the smallest complete API: steering already means asking the retained worker to continue. A separate fire-and-forget path has no demonstrated caller.

### Add `worker ask` — rejected

Keep `worker send` fire-and-forget and add a second request/reply command. This duplicates queueing, authorization, recovery, and documentation while preserving the current ambiguous default.

### Block until the worker replies — rejected

A long-running CLI call would tie model latency to tool timeouts and make central/session restarts harder to recover. Durable asynchronous events already solve notification and recovery.

## Design

### Exact native OMP capability surface

OMP workers launch with native discovery controls:

- Always pass `--no-extensions`. OMP 18.14 treats explicit `-e` paths as authorized under this flag, so `lib/omp-worker.ts` still loads while installed/configured plugin extensions do not.
- Build the allowed skill names from the task's current immutable `worker_skills.skills` manifest, not from the mutable plugin registry.
- If the manifest is empty or absent, pass `--no-skills`.
- Otherwise pass one `--skills` value containing the exact canonical names. AGD's existing ID validation excludes glob metacharacters, so these values are literal allowlist entries.

The worktree's `.omp/skills/NAME` copies remain the authoritative bytes. `--no-extensions` removes installed extension-package roots from OMP's sibling skill discovery; `--skills` excludes unrelated project, user, or managed skills. PR-granted workers naturally allow only the generated `agd-scoped-pr-relay` manifest entry.

Central launch behavior is unchanged.

The native smoke must inspect the surface a real worker session consumes. OMP 18.14's `skill list`, `read`, and `plugin list` management commands do not apply session-only skill controls, so none can prove the worker boundary; the smoke must obtain the session-visible names and source paths from the launched worker itself.

### Durable request/reply lifecycle

`worker send TASK --file FILE` keeps its current syntax and response. It accepts `running` or `ready` tasks only when `worker_alive` is true and the recorded endpoint is presently verified; stopped, missing, and unknown workers refuse rather than accepting a request with no execution path. A request accepted before a later crash remains queued for relaunch.

Under the task lock, send increments a task-local inbox sequence and writes an immutable record containing schema 1, UUID request ID, body, enqueueing incarnation, integer sequence, and enqueue time. Endpoint presence is inspected before taking the lock, then the locked write revalidates that the task incarnation and endpoint identity did not change. `worker inbox` sorts by sequence rather than UUID filename, giving concurrent sends one durable FIFO order.

The OMP worker extension:

1. Polls only while OMP is idle and no request or unsaved response is active.
2. Selects the first uncompleted inbox record by sequence.
3. Delivers that record's body directly through `sendUserMessage(body, {deliverAs: "followUp"})` and remembers its request ID.
4. Does not acknowledge the request before the turn finishes. A crash therefore leaves it queued for at-least-once redelivery.
5. On terminal `agent_end` (`willContinue` is false), reads the supported OMP 18.14 `AgentEndEvent.messages` snapshot, selects its last assistant message, and concatenates that message's text blocks in order. Thinking, tool-call, and tool-result blocks are not reply text.
6. Invokes an internal native completion command with that response on stdin. It clears the active request only after the command succeeds, then checks for the next queued request.

If completion fails transiently, the extension retains both request ID and extracted response and retries the same completion every inbox polling interval while OMP remains idle. A process exit loses only that in-memory response: the unacknowledged request is redelivered after restart, preserving at-least-once execution rather than silent loss.

The internal command is:

```text
bin/agd --home HOME worker complete TASK REQUEST_ID --incarnation INCARNATION --status done|failed
```

It is worker-scoped, reads the response from stdin, and refuses a foreign task/current incarnation or unknown request. The inbox record may come from a prior incarnation after relaunch. Under the task lock it:

- writes `data/tasks/TASK/replies/REQUEST_ID.json` with schema 1, request ID, enqueueing and completing incarnations, `done` or `failed` status, response body, and completion time;
- writes a deterministic `done` observation for `done`, or `blocked` for `failed`, keyed by the request ID;
- writes the inbox acknowledgement last.

Retries return the identical completion record. A partial write is repaired using the same deterministic paths and observation identity, so supervision emits at most one central event for the request. On worker restart, `worker inbox` uses any reply journal missing its observation or final acknowledgement to finish that repair before selecting another model turn.

If terminal `agent_end` has no assistant text, the extension completes it as `failed` with the exact diagnostic `steering turn ended without assistant text`. Central is still notified; silence is never reported as success.

`worker inbox` returns only requests without a final acknowledgement. Pending requests from a prior incarnation remain eligible after relaunch; a completed reply records the current incarnation while retaining the original inbox record for provenance. Because acknowledgement is written last, the inbox path can finish an interrupted completion from its reply journal without repeating the model turn.

The existing model-operated `worker acknowledge` command and prompt instruction become obsolete and are removed. Completion, not mere receipt, is the durable boundary.

### Central notification

`supervision.reconcile` already turns `done` and `blocked` observations into durable central events. The event details contain the persisted final response or fixed failure diagnostic. The healthy watcher wakes the central OMP session, which can inspect the event and issue another `worker send` against the same live worker.

No idle event is promoted globally. Initial task turns and ordinary idle transitions remain quiet; only correlated requests create reply notifications.

## Safety and recovery invariants

- One active request per OMP session; a task-local sequence orders additional sends.
- A request is never removed before a durable reply/observation exists.
- Completion requires the current task process environment and current incarnation.
- Duplicate `agent_end`, extension restart, or CLI retry cannot create duplicate central events.
- A crash before completion may redeliver a request, but cannot silently lose it.
- Ready-task workspace verification remains authoritative; the reply protocol does not relax Git or lease checks.
- Replies and inbox records remain private distro data and are not committed to task worktrees.

## Verification

Permanent checks will cover:

- OMP launch arguments use `--no-extensions` and `--no-skills` for an empty manifest.
- A selected manifest uses an exact `--skills` allowlist and still loads only the explicit worker extension.
- PR workers expose only `agd-scoped-pr-relay`.
- The extension delivers one queued body at a time, correlates terminal `agent_end`, and advances only after durable completion.
- Completion validates ownership, is idempotent, repairs partial state, and produces one supervision event.
- Relaunch preserves uncompleted requests.
- The native plugin smoke checks exact real-session-visible skill names and sources rather than management-command plugin enablement.
- A real retained worker receives two sequential `worker send` requests and produces two correlated central events without changing incarnation.

Documentation will describe workers as retained task executors, `worker send` as asynchronous request/reply, the exact skill boundary, queue recovery, and ready-worktree constraints.
