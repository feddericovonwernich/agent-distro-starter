---
name: distro-orchestrate
description: Launch, steer and supervise recorded task workers without losing leases or decisions.
---

# Worker orchestration

The live-verified combination is `bin/agd worker launch ID --harness omp --backend tmux`; inspect `docs/compatibility.md` before choosing others. `--unattended` requires a separate `task approve ID --action unattended --target ID --instruction FILE` and the returned approval ID. Do not grant a tool permission or bypass a prompt merely because a worker requests it. OMP workers run in their leased worktrees with their own extension; the central owns the watcher.

`worker inspect ID` reports endpoint evidence. `worker send ID --file FILE` atomically queues a steering message; `worker inbox ID` shows pending messages until that incarnation acknowledges them. Queued or submitted does not mean processed. `session status`, `events list`, and `task status ID` show watcher health, wakes and open decisions. Handle a wake, then `events acknowledge EVENT_ID` with exact IDs. To answer a named decision after obtaining the user's answer, `decision resolve ID KEY --answer FILE` persists it and queues it for the worker; acknowledgement alone never resolves it.

`worker interrupt ID` sends Escape only on a native busy OMP turn; inspect again rather than assuming the process ended. `worker stop ID` needs native idle evidence and verifies exit and pane absence before reporting success. `worker relaunch ID` requires positive previous-process and endpoint absence and reuses its exact workspace; unresolved state must remain for inspection. On watcher restart use `session reconcile`, inspect pending events/leases, and do not allocate a replacement task or kill unknown endpoints. Herdr control is unavailable outside its verified named session; Cursor Agent requires user authentication and live transcript binding before operational claims.
