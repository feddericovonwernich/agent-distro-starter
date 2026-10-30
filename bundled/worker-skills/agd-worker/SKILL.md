---
name: agd-worker
description: Use when an AGD-managed OMP worker starts an assignment, reports a result or blocker, or answers a retained-task follow-up.
---

# AGD worker protocol

This core is automatically loaded by the worker extension; do not reload it. AGD state, not assistant prose, determines task completion and authority.

- Read the brief named in `agd:worker-context`. Work only on that task in its assigned worktree.
- Optional skills are available through native discovery. Load `skill://NAME` when relevant or explicitly required by the brief; do not read every available skill automatically.
- Before finishing an assignment, verify its acceptance criteria and write a concrete report. Without a PR grant, submit it through `task ready`. With a bound PR grant, load `skill://agd-scoped-pr-relay` and use its guarded verification route instead.
- If blocked or awaiting a decision, persist an AGD observation with the exact obstacle/question. Resolve a cleared blocker using its original key. A chat answer alone does not record it.
- A ready task accepts read-only follow-ups. Do not modify its workspace or repeat initial completion. The extension persists FIFO replies automatically.
- Never approve, merge, clean up, or publish outside the bound grant. Do not bypass refused commands or alter runtime records.

Exact quoted commands: `skill://agd-worker/references/commands.md`. Runtime bindings identify the CLI, home, task and incarnation; they do not grant permissions.
