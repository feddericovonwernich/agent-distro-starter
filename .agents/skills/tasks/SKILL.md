---
name: distro-tasks
description: Create isolated tasks, report readiness, approve exact-head delivery and preserve work.
---

# Task delivery

Create `bin/agd task create ID --project PROJECT --brief FILE [--kind ship|scout]`; read the returned worktree and lease. Direct central edits are allowed, but only in that worktree. If allocation was interrupted, use `task reconcile ID`, never blindly create another lease. Ship tasks require committed changes and a clean worktree; scout tasks require the original commit and a written finding. `task ready ID --report FILE` verifies these conditions without authorizing a merge or publication.

Landing needs an explicit user instruction: `task approve ID --action local-merge --target PROJECT:BASE --instruction FILE`, then `task land ID --approval APPROVAL_ID`. It fast-forwards only a clean stable clone to the exact approved head. Publication is separate: `task approve ID --action publish --target REMOTE:BRANCH --instruction FILE`; use only the user's existing authorized forge tooling to push/create the review; `task publication-record ID --approval APPROVAL_ID --remote REMOTE --branch BRANCH --url URL --evidence FILE` verifies its remote head. Neither readiness nor publication means the work landed.

If a worker was used, `worker stop ID` must prove its process and endpoint absent first. Then `task cleanup ID` returns a clean landed/scout lease conditionally. For destructive discard, inventory and obtain an exact instruction, `task approve ID --action discard --target ID --instruction FILE`, then `task cleanup ID --discard --approval APPROVAL_ID`; refusal preserves bytes. Never remove worktrees by hand or silently grant approval.
