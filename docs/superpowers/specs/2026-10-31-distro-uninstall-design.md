# Distro uninstall — design

Status: proposed; requires user approval before implementation.

## Intent and agreed scope

Provide one guided `agd` command that fully removes a selected distro without leaving producers able to recreate it. The user selected **full removal after confirmation**, not retained-data uninstall. Dirty or unpublished work requires separate explicit discard approval. Shared tooling, unrelated resources, and remote artifacts are never uninstall targets.

This design does not authorize uninstalling a live distro during development. Verification uses disposable, uniquely owned homes and resources only.

## Approach

Use the existing Python CLI and lifecycle modules. Reuse their receipts, home identity, backend protocols, ownership checks, and private-file conventions; add the missing shutdown/purge operations where those resources are owned. Do not make uninstall a shell script chaining existing `stop` commands: they intentionally retain resources and do not establish complete absence. A separate shell uninstaller would duplicate these guards. A retained-data default was considered; the user instead chose confirmed full removal.

No new dependency, service manager, global resource registry, generic workflow engine, automatic backup, package uninstaller, or unattended uninstall mode is needed.

## Command and confirmation contract

Proposed interface (builder choice, not a previously existing API):

```sh
/path/to/distro/bin/agd --home /absolute/path/to/distro uninstall --dry-run
/path/to/distro/bin/agd --home /absolute/path/to/distro uninstall
```

- Dry-run is read-only and works without a terminal. It returns a pretty-printed JSON inventory with resource identities, planned removals, retained shared resources, data-loss warnings, and blockers. It must not call helpers that create directories, generate secrets, or publish lifecycle records merely to inspect an unconfigured feature.
- Execution requires an interactive terminal, previews the same inventory, and requires the exact canonical home path in a typed deletion confirmation. No generic `--force`, `--yes`, environment-variable authorization, or piped default acceptance.
- A separate typed discard confirmation binds to the displayed work-loss inventory: affected project/worktree paths, relevant heads/refs, and dirty/untracked/ignored file identities. General uninstall confirmation alone does not authorize discarding this work. Unverified publication/merge status is a risk, not proof that work is disposable; do not fetch or publish automatically.
- Read-only validation runs before consent. Declining the initial confirmation or discard question changes nothing. Display path/identity metadata, not file contents, credentials, auth payloads, or process environments. Escape untrusted path text; use subprocess argv rather than shell interpolation.
- Full removal includes distro-local OMP auth/history, custom configuration/persona/skills, project clones and owned worktrees, task/reviewer records, local telemetry archives/queues/credentials, and owned database/object-store volumes. The preview must name these consequences.
- If the home or resource inventory materially changes before execution, do not apply the old approval to the new inventory. Heartbeat timestamps alone are not material changes. Changed work produced while stopping an active task needs a refreshed discard confirmation before data deletion.

## Invocation and home identity

Execute from a terminal whose working directory is outside the target home and its owned worktrees. Refuse when the coordinator is a descendant/member of a central, worker, or backend process group that teardown would stop. This avoids killing the uninstaller or leaving the caller in a deleted working directory; a child cannot change its parent shell's cwd. Print an external-terminal invocation on refusal. Detached self-uninstall is not part of this design.

Require a real initialized home with a regular, current-UID `data/home.json`, valid schema/home ID, and safe directory identity. Bind the operation to canonical path, home ID, UID, and filesystem identity. Reject dangerous targets such as `/`, the account home, malformed/copied conflicting identities, and symlink redirection. Do not follow symlinks or traverse mounts into external storage during deletion; unexpected mount points or foreign ownership block removal. Symlink entries inside approved local data can be unlinked without following their destinations.

Home ID alone is insufficient to delete an external resource. Correlate retained receipts with its canonical home/project paths and actual runtime identity. A copied directory must not gain permission to remove the original home's units or services. Missing, replaced, malformed, inaccessible, or ambiguous evidence is a blocker; it is not permission to adopt or kill a resource.

## Resource inventory and teardown boundaries

| Surface | Required behavior |
| --- | --- |
| Central OMP and watcher | Correlate launcher/owner/PID-start receipts; quiesce new activity, gracefully stop the exact central, retire its watcher, and verify both absent. Killing only the watcher is insufficient because the extension can rearm it. Add a production shutdown path; the existing smoke helper is not a lifecycle API. |
| Worker runners, model children, and Relay | Cancel/drain active work, prefer supported native quit, then verify recorded runners/model children/connectors/servers and tracked descendants absent. Current idle-only OMP stop and Relay direct-child termination are useful but not complete uninstall proofs. Preserve process records until verification succeeds. |
| Native backend endpoints | Close exact owned tmux/Herdr panes/windows/workspaces. Remove a per-home server only after proving its process/socket identity and that no unrelated endpoint remains. Never stop externally selected/shared Herdr servers. If an untouchable/shared resource still requires the target home, block home deletion and explain the dependency. |
| Scheduled/manual reviewer | Disable future timer activation; stop the owned active service/cgroup and verify exit; drain or safely stop a manual reviewer under its run lease. Clean owned unit files, wants links, persistent timer state, and reload the user manager. Do not delete or replace a held run lock. |
| Treehouse and Git | Enumerate task receipts, native pool state, and every registered project's Git worktrees, including older external roots. Match exact lease ID/holder and Git common directory before release/removal. Remove frozen skill artifacts and only this task's shared Git-exclusion ownership. Return and destroy are distinct operations. Destroy only approved owned slots, verify skips/remaining slots, and remove registrations before deleting their backing repository. |
| Docker observability | Validate configured or rendered-but-unpublished state; stop/remove owned containers and networks, then explicitly approved owned PostgreSQL/ClickHouse/Redis/MinIO volumes. Keep credentials, Compose files, and release-lock evidence until cleanup is verified. Shared images are not targets. |
| Private data and source checkout | After every runtime/external dependency is resolved, remove private OMP plugins/marketplaces/caches/auth/sessions, private Relay runtime/staging, configuration, reports, inboxes, observations, archives, queues, receipts, and project/source directories as part of the approved home removal. |

### Process evidence and cancellation

Never use `pkill`, executable names, cwd matches, session names alone, or PID alone as kill authority. A path match can diagnose a blocker, not authorize termination. Revalidate PID/start and endpoint identity at each stop boundary. Use kernel-bound process handles for new direct signaling where available; if safe signaling is unavailable, refuse rather than introduce a PID-only fallback. Native token/socket-validated close operations remain usable.

Use bounded graceful cancellation/quit, then TERM/KILL only for identities conclusively belonging to the approved uninstall. The original confirmation must warn that active tasks/reviews will be cancelled. Record only additional launch/descendant identity needed for safe teardown in the existing resource-specific receipts, not a parallel process supervisor. Keep the coordinator alive while its children are being identified and retired; recheck for newly spawned descendants before reporting absence.

Legacy receipts remain readable and usable when sufficient. A legacy active manual reviewer or orphan lacking safe identity must finish or be stopped through an independently proven mechanism before retry; never fabricate new ownership or claim complete descendant tracking retroactively. Arbitrary unrecorded/daemonized software launched by user commands is not automatically owned by AGD. Known unresolved references to the home are reported as blockers. This is accidental-misuse protection, not a same-UID OS sandbox.

### Treehouse, Git, and work loss

Use each task's recorded root; older tasks can use account-global pools. Never run a global prune/return/destroy or recursively remove an external pool. Native bulk commands can return success while skipping leased/unmerged slots, so completion requires reconciliation of the exact target set, not only exit status.

A foreign lease or external non-AGD worktree is not discardable by a distro confirmation. Block deleting a backing Git repository while such dependencies remain. Dirty, ignored/untracked files, unmerged/unverified commits, other refs, and retained local history must be represented in the separate work-loss approval. Existing task `phase` alone is not proof of publication or disposability; conversely, a remote PR merge does not silently authorize rewriting stale task lifecycle records.

### Docker and partial setup

Reuse home-bound secrets/release-lock and resource-label checks, but do not equate every failed `inspect` with absence. Confirm Docker reachability/context/engine identity before and after destructive operations, correlate exact project/home labels and retained paths, and distinguish not-found from permission/transport/daemon failure. Enumerate actual project resources rather than assuming only the usual service names exist.

The preview identifies the selected Docker engine/context. Refuse a conflict with any retained engine evidence. Legacy records without engine identity must visibly disclose that gap; confirmation authorizes only the displayed engine, never an undiscovered engine or context. Do not search/delete across remote Docker engines. If resource ownership or the intended engine cannot be established, retain the home and require correction before retry.

An interrupted first enable can have Compose files, a lock and secrets without a completed config. This is a supported discovery path. A never-enabled feature must not be provisioned merely so uninstall can disable it. Stopping containers is not removing them, and retaining volumes without their recovery metadata is not a successful full uninstall.

## Quiescing, recovery, and deletion ordering

Use one home-scoped coordinator lock and a private `state/uninstall.json` journal. This is an operation receipt, not a general workflow framework. It stores canonical identity, stable resource identities, approved inventory digest, work-discard scope, progress, and failures; never secrets.

Publish a retiring fence under the same short admission synchronization used by new runtime/state producers. Start/attach, watcher rearm, worker launch/relaunch, task allocation, Relay service creation, reviewer dispatch/timer activation, installer setup/init, and other mutating paths must not create new work once retirement is admitted. Already admitted creation must settle into discoverable receipts before the final inventory. Do not hold the ordinary store mutation lock while calling shutdown helpers that acquire it themselves. Old already-loaded processes may not honor a new fence; absence verification is still mandatory.

Logical ordering:

1. Read-only inventory and both required confirmations.
2. Lock, revalidate identity/approval, journal intent, and fence new producers.
3. Disable scheduled activation and quiesce the central/watcher's restart/event paths. Stop/drain workers, reviewers, and Relay while preserving their evidence; finish central/watcher shutdown and exact backend cleanup. Do not terminate the watcher while a still-active central can rearm it.
4. Reconcile all owned process/endpoint absence and any work changes; obtain refreshed discard approval if necessary.
5. Remove owned external resources, worktrees/Git registrations and approved volumes/units, recording each outcome. Preserve enough configuration/identity to retry every incomplete step.
6. Verify no owned trigger/process/resource remains and no foreign dependency would be orphaned. Revalidate target filesystem identity; remove the home last.
7. From outside the removed home, perform a bounded delayed absence check across the known watcher/recreation interval and emit the final result. No post-delete helper may import or write inside the removed tree. Unexpected recreation is a failed uninstall and must be reported; do not blindly delete a newly created directory again.

A successful result means the approved owned resources and original home are absent, with shared exclusions listed. Unknown state, skipped resources, loss of tool/engine access, timeouts, or changed identity are failures: return nonzero, retain remaining home/evidence, and give exact retry/blocker information. Do not proceed to final deletion just because a stop/disable command returned zero.

Rerunning the same command resumes the retained operation after a fresh preview and confirmation, reusing only still-valid approvals/identities. A confirmed already-missing resource is idempotent success. An entirely missing target is reported already absent without recreating it; it cannot claim to have verified unknown external resources.

If the user cancels after quiescing but before any irreversible removal, clear retirement admission safely, retain an audit outcome, and leave stopped services stopped; do not automatically restart anything. Once irreversible removal has begun, cancellation/failure preserves the retiring fence and the remaining home for retry rather than advertising it as usable. Clearly distinguish these outcomes. No automatic rollback or backup is promised.

## Shared exclusions and non-goals

Never remove APT packages; account-global OMP/Herdr/Treehouse/Bun/Node/Relay or Docker/Compose installations; shared Docker images; account XDG/Git/OMP/provider configuration; another home's services/data; user-systemd manager/lingering; or remote repositories, branches, PRs, provider accounts or credentials. A distro-local `managed` dependency receipt does not imply exclusive ownership of a shared executable.

Do not vacuum shared journals/caches, revoke shared provider tokens, securely erase physical media, uninstall arbitrary user applications, add a backup/archive feature, or add detached/unattended uninstall. No automatic publication or merge is part of this work.

## Verification and acceptance

Behavioral regressions must cover target/identity refusal, no-write dry-run and declined confirmation, separate changed-work approval, quiescing/restart races, active reviewer versus disabled timer, exact process/endpoint absence, partial/unavailable Docker/systemd/backend state, external/foreign leases, repeated cleanup, shared-resource preservation, and failure before final home deletion. Avoid source-text, argv-order, mocked-forwarding, or incidental output wording assertions.

Use disposable native smoke resources to prove actual central/backend shutdown, active reviewer service/timer cleanup including persistent state, owned Docker container/network/volume removal, native Treehouse release/destruction and Git registration cleanup, and delayed non-recreation. Exercise the actual interactive command from outside its target; verify a second home and shared tools/sentinel resources remain untouched. No paid model call or real user-distro deletion is required. Name unavailable prerequisites and distinguish exercised proof from inference; mocks do not replace native lifecycle acceptance.

## Technical calls left to the builder

Exact helper names, receipt-field additions, prompt wording, bounded wait constants derived from current lifecycle behavior, and the minimal admission-lock implementation are builder choices. Preserve current API conventions and migrate every affected creation/teardown path; no aliases or parallel lifecycle framework. No product code changes are authorized until this document is approved.

## Source anchors

- `bin/agd_lib/reviewer_timer.py:308-324`: timer disable removes units but does not explicitly stop the active service.
- `bin/agd_lib/reviewer.py:527-532`: manual-run non-overlap lease.
- `bin/agd_lib/workers.py:507-532`: guarded idle OMP quit and retained uncertain state.
- `.omp/extensions/agd-primary.ts:143-153,182-187`: watcher rearm and session shutdown.
- `bin/agd_lib/relay.py:413-454`: direct process stop; descendants need additional proof.
- `bin/agd_lib/tasks.py:437-490`: guarded task cleanup returns the lease, not worktree destruction.
- `bin/agd_lib/worker_skills.py:737-790`: generated skill removal and shared Git exclusion ownership.
- `bin/agd_lib/projects.py:84-119`: clone/work/ref/ignored-file deletion checks.
- `bin/agd_lib/observability.py:654-710`: retention-first lifecycle and partial-enable recovery.
- `bin/agd_lib/omp_runtime.py:24-89`: private runtime roots and external account configuration boundary.
- `bin/agd_lib/installer.py:256-257`: shared executable installation location.
