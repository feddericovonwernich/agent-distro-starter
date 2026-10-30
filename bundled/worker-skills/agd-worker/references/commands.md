# AGD worker commands

The runner sets `AGD_CLI`, `AGD_HOME`, `AGD_TASK_ID`, and `AGD_INCARNATION`. A PR worker additionally receives `AGD_PR_GRANT`. Keep the supplied values; the CLI checks the actual task and current incarnation. Never copy a central owner token.

## Inspect

```sh
"$AGD_CLI" --home "$AGD_HOME" task status "$AGD_TASK_ID"
```

Read the brief at `$AGD_HOME/data/tasks/$AGD_TASK_ID/brief.md`. The injected worker context supplies the authoritative worktree and phase. Skill names resolve through `skill://NAME`; no filesystem search is needed.

## Complete an ordinary assignment

Verify the brief's acceptance criteria first. Write a nonempty report to `$AGD_HOME/data/tasks/$AGD_TASK_ID/report-draft.md` with results, checks actually run, and remaining limitations. A scout must keep the original HEAD; a ship must commit its work. Both require a clean, intact leased workspace and unchanged generated skill artifacts.

Only when there is no PR grant:

```sh
"$AGD_CLI" --home "$AGD_HOME" task ready "$AGD_TASK_ID" \
  --report "$AGD_HOME/data/tasks/$AGD_TASK_ID/report-draft.md"
```

Check the exit status. On refusal, inspect the cause and fix only what is authorized; do not claim ready. A granted PR worker uses `skill://agd-scoped-pr-relay` and `worker verify-pr`, not this shortcut. Readiness does not authorize publication, landing, cleanup, or further workspace changes.

## Record a blocker or decision

Write the concrete obstacle or question to `$AGD_HOME/data/tasks/$AGD_TASK_ID/worker-message.md`. Choose a stable key for this obstacle, such as `missing-credential`.

```sh
"$AGD_CLI" --home "$AGD_HOME" worker event "$AGD_TASK_ID" \
  --incarnation "$AGD_INCARNATION" --kind blocked --key missing-credential \
  --message "$AGD_HOME/data/tasks/$AGD_TASK_ID/worker-message.md"
```

Use `--kind decision` for a question requiring central/user judgment. Do not choose a privileged action on their behalf. The orchestrator answers through `decision resolve`; the answer arrives in the existing inbox. When a blocker is genuinely cleared, update the message file with evidence and emit `--kind resolved` using the same key. Do not use the reserved `agd-contract-incomplete` runtime key.

## Retained follow-ups

Answer the delivered request normally. The extension handles FIFO delivery, durable reply persistence and central notification; do not call `worker complete`, acknowledge, or emit a second done event manually. After `ready`, follow-ups are read-only. Request a new task for more code changes.

## Common mistakes

A final chat message is not readiness. A skill or environment variable is not approval. A blocked command is not permission to edit task JSON. Never return a lease or stop other processes from a worker.
