---
name: customize-distro
description: Use when customizing a distro instance's persona, instructions, skills, models, tools, worker behavior, UI, environment, or defaults, or when maintaining customizations across template updates.
---

# Customize the distro

Choose the smallest surface that actually consumes the change. Instructions guide an agent; settings configure a supported consumer; code changes behavior; runtime records preserve operational evidence. They are not interchangeable.

Default to the current instance, not the shared template or every project. Publishing a customization upstream is a separate explicit choice. This reference does not grant permission to edit, publish, land, discard, or run unattended.

## Establish scope

Before editing, inspect the checkout, existing instructions, relevant skill, actual consumer and installed harness version. Establish:

- **Scope:** one task, one project, this instance, or the shared template?
- **Audience:** central agent, workers, or the human-facing UI?
- **Mechanism:** supported setting, instruction, skill, tool, or code?
- **Authority:** does it affect trust, approvals, credentials or resource ownership?
- **Activation:** next task/turn, harness reload, or session restart?
- **Updates:** private operational data, local tracked customization, or upstream contribution?

Resolve these from the request and repository first; ask only where materially different choices remain. Propose the affected paths and activation/verification plan before changing behavior.

## Surface map

Code and instruction paths below are relative to the installed checkout. Private `config/`, `projects/`, `data/` and `state/` paths are relative to the selected home (`--home`, then `AGD_HOME`, then the checkout). With an external home, the instructions, skills, extensions, `bin/` and `lib/` stay in the checkout; creating copies under the private home does not make them load.

| Need | Surface and consumer | Boundary |
|---|---|---|
| Central identity, tone, routing, report style | Root `AGENTS.md`, consumed by the central agent | Personality does not change approval authority. |
| Reusable procedures and domain knowledge | `.agents/skills/<name>/SKILL.md` | A skill guides judgment; it does not implement a missing setting or enforce a permission. |
| Project coding standards, architecture and test commands | Instructions/skills in that managed project's task worktree | Keep project-specific rules out of global distro instructions. Follow the project/task procedures for edits. |
| One task's acceptance criteria, review checklist or report format | Initial task brief; post-launch OMP steering through `bin/agd worker send` | Only the OMP worker extension currently consumes the steering inbox automatically. A queued message is not proof of processing. Put Cursor requirements in the initial brief until a verified steering consumer exists. Do not hand-edit active runtime records. |
| Provider, model, reasoning preferences and native integrations | The selected harness's supported configuration | Inspect installed harness documentation and setting scope. Central settings need not propagate to workers; keep secrets in existing credential stores. |
| Worker harness, backend and task kind | `worker launch --harness omp\|cursor --backend tmux\|herdr`; `task create --kind scout\|ship` | These are explicit per-operation choices. Check `docs/compatibility.md`; do not claim Cursor is live-verified. |
| Worker skill profiles and curated bundles | Private-home `config/workers.json` and `config/worker-skills/<agd-id>/`; `worker profiles list`, `worker skills list`, and `worker launch` consume them | These are the only supported `config/` consumers. Profiles select deterministic catalog sets; curated launch selects an explicit catalog subset of provisioned skills, while native project/user skills may also remain visible. Bundles are frozen per task and materialized into the worker worktree, not loaded from the private home at runtime. |
| Private home, pool and executable selection | `AGD_HOME`, `TREEHOUSE_ROOT`, `PATH`, `AGD_HERDR_SESSION` | Changing a home is not migrating it. Herdr needs a managed pane and explicit non-default named server. Preserve existing leases and identity records. |
| Status, notifications and central session hooks | `.omp/extensions/agd-primary.ts` | Keep model context separate from UI display. Preserve actionable wakes, failure visibility and watcher ownership. Verify the actual client renders the chosen UI API. |
| Shared worker startup instructions and harness arguments | `bin/agd_lib/harnesses.py`; OMP lifecycle/steering in `lib/omp-worker.ts` | Workers run in project worktrees, not the central home. Trace what each harness actually loads. Never auto-load the central watcher in workers. |
| Company tools, issue trackers or deployment procedures | Existing external CLI/integration first; focused scripts/extensions only if needed | Document prerequisites and scope credentials. Do not build a parallel integration framework. |
| Backend adapters, readiness, supervision or lifecycle checks | `bin/agd`, `bin/agd_lib/` | Software development, not configuration: regression checks and real changed-path smoke evidence required. |
| Template installation and onboarding | `install.sh`, `README.md`, `docs/` | Preserve origin/update expectations and the tracked/private boundary. |

**Current limitation:** `config/workers.json` and `config/worker-skills/<agd-id>/` are the specifically supported worker-skill configuration paths; other `config/` paths have no generic settings reader. CLI defaults such as OMP/tmux are currently defined in code. Writing `config/distro.yaml` does nothing unless a consumer is implemented and documented. Check code again before relying on this statement in a later release.

`projects/`, `config/`, `data/`, `state/` and `.env` are private/ignored. `data/` and `state/` contain registries, approvals, leases, events and ownership evidence—not editable preferences or disposable caches. Use `bin/agd` operations; do not fabricate approvals or modify receipts to bypass a refusal.

## Change and verify

Prefer the narrowest applicable surface: task brief → project instructions → distro instructions/skill → supported settings → code. Trace the reader before writing a new file. Do not add a second convention where an existing procedure or native tool already works.

Follow the repository's workspace and approval rules. Do not modify the live central/worker checkout in place. Work in isolation, preserve private homes, and arrange activation at a safe restart boundary. A documentation change is not evidence that a running session has reloaded it.

Example: “Make every worker use our review checklist.” Editing central `AGENTS.md` alone is insufficient. For one task, include the checklist in its brief. For a permanent policy, trace the shared launch prompt and both harnesses' instruction discovery; make the checklist reachable from the worker's worktree and explicitly required by its input. Exercise an actual worker and inspect its report. Do not claim Cursor coverage without authenticated live evidence.

Keep exact-target publication/landing approvals, explicit trust and unattended approval, isolated task workspaces, durable event acknowledgement, and verified worker absence before lease cleanup. A request to remove approval noise is an authority-design discussion, not permission to delete checks. UI changes must not suppress the underlying decision or failure.

## Preserve updates

The installed clone retains template `origin`; it has no automatic customization overlay. Inspect remotes, branch, clean/dirty state and local commits before recommending an update.

- An unmodified compatible branch can fast-forward after safely stopping affected sessions.
- Tracked customization commits can diverge from upstream; `git pull --ff-only` then correctly refuses.
- Preserve local work and propose deliberate integration on a customization branch. Merge versus rebase has a history-sharing tradeoff; do not automatically choose a history rewrite or reset local `main`.
- Never use reset, clean, forced checkout/push, automatic stash, or delete private state to make an update succeed. Keep unrelated work intact.
- Update the distro separately from managed projects; `agd project refresh` is not distro self-update. Do not change global harness settings or credentials as a side effect.

## Common mistakes

- Inventing a configuration file without a reader.
- Assuming central instructions govern every worker or every project.
- Treating ignored files as safe to delete or safe places to commit credentials.
- Treating a worker report, event receipt or user-interface preference as approval.
- Claiming a new setting, skill or widget works because its source text exists.

Finish with the changed surface, affected audience, exercised evidence, activation steps, and update implications. For skills, exercise realistic requests with and without the guidance; for UI, inspect the actual surface; for code, run the affected behavior and its relevant checks. Report unavailable harness/client verification explicitly.
