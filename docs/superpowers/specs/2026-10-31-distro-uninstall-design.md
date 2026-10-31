# Distro uninstall — design

Status: proposed; requires user approval before implementation.

## Intent and agreed scope

Provide one guided `agd` command that fully removes a selected distro without leaving producers able to recreate it. The user selected **full removal after confirmation**, not retained-data uninstall. Dirty or unpublished work requires separate explicit discard approval. Shared tooling, unrelated resources, and remote artifacts are never uninstall targets.

This design does not authorize uninstalling a live distro during development. Verification uses disposable, uniquely owned homes and resources only.

## Approach

Use the existing Python CLI and lifecycle modules. Reuse their receipts, home identity, backend protocols, ownership checks, and private-file conventions; add the missing shutdown/purge operations where those resources are owned. Do not make uninstall a shell script chaining existing `stop` commands: they intentionally retain resources and do not establish complete absence. A separate shell uninstaller would duplicate these guards. A retained-data default was considered; the user instead chose confirmed full removal.

No new dependency, service manager, global resource registry, generic workflow engine, automatic backup, package uninstaller, or unattended uninstall mode is needed.

## Command and confirmation contract

Proposed interface, including the user's requested in-directory invocation (not a previously existing API):

```sh
./bin/agd --home . uninstall --dry-run
./bin/agd --home . uninstall
```

For this command, resolve a relative `--home` against the invocation cwd before validation, staging, or chdir; pass only the resulting absolute, identity-checked target to existing lifecycle helpers. Do not relax `store.home()` or other commands' path contracts. `./bin/agd` is the distro-local script; `/bin/agd` would mean an unrelated system-wide installation, which this project does not create.

- Dry-run is read-only and works without a terminal. It returns a pretty-printed JSON inventory with resource identities, planned removals, retained shared resources, data-loss warnings, and blockers. It must not call helpers that create directories, generate secrets, or publish lifecycle records merely to inspect an unconfigured feature.
- Print the inventory even when blocked. Dry-run exits 0 when executable after the stated confirmations, 3 for ownership/state refusal, and 4 when an essential inspection dependency is unavailable; malformed arguments retain exit 2. Work-loss warnings awaiting discard approval are not unknown-ownership blockers. Cancellation is a distinct `cancelled` result with exit 0, never an `uninstalled` claim.
- Execution requires an interactive terminal, previews the same inventory, and requires the exact canonical home path in a typed deletion confirmation. No generic `--force`, `--yes`, environment-variable authorization, or piped default acceptance.
- A separate typed discard confirmation binds to the displayed work-loss inventory: affected project/worktree paths, relevant heads/refs, and dirty/untracked/ignored file identities. General uninstall confirmation alone does not authorize discarding this work. Unverified publication/merge status is a risk, not proof that work is disposable; do not fetch or publish automatically.
- Read-only validation runs before consent. Declining the initial confirmation or discard question changes nothing. Display path/identity metadata, not file contents, credentials, auth payloads, or process environments. Escape untrusted path text; use subprocess argv rather than shell interpolation.
- Full removal includes distro-local OMP auth/history, custom configuration/persona/skills, project clones and owned worktrees, task/reviewer records, local telemetry archives/queues/credentials, and owned database/object-store volumes. The preview must name these consequences.
- The main confirmation explicitly authorizes the displayed owned volume IDs/data classes and clean, locally evidenced published clone history; there is no additional per-volume or per-clean-project question. This is a new aggregate uninstall grant bound to that inventory, not a forged normal task/project approval or a fabricated `landed` state. Existing ordinary task/project/central guards remain unchanged. Uninstall is a separately confirmed human management operation; it must not borrow a central token to impersonate its owner.
- If the home or resource inventory materially changes before execution, do not apply the old approval to the new inventory. Heartbeat timestamps alone are not material changes. Changed work produced while stopping an active task needs a refreshed discard confirmation before data deletion.

## Invocation and home identity

Allow invocation from a normal terminal inside the target home. After validation/consent, move only the coordinator's cwd outside every deletion root before foreground handoff. Still refuse when the coordinator is a descendant/member of a central, worker, or backend process group that teardown would stop; being inside the directory is allowed, being part of a process tree being terminated is not. A child cannot change the caller shell's cwd: display the parent-directory `cd` command and recommend `./bin/agd --home . uninstall && cd ..` for an in-home shell. The acknowledged caller shell's cwd alone is not kill authority or an unresolved-runtime blocker; unrelated references retain their normal safety checks. Detached self-uninstall and automatic parent-shell manipulation are not part of this design.

Require a real initialized home with a regular, current-UID `data/home.json`, valid schema/home ID, and safe directory identity. Bind the operation to canonical path, home ID, UID, and filesystem identity. Reject dangerous targets such as `/`, the account home, malformed/copied conflicting identities, and symlink redirection. Within each deletion root, do not follow symlinks or descend into nested mounts (including bind mounts); a root that is itself a mount point, an unexpected nested mount, or foreign ownership blocks removal. Do not unmount anything. A separately inventoried, receipt-proven external worktree may live on another filesystem: treat it as an independent exact deletion root with its own identity, never as permission to traverse or delete its parent pool. Symlink entries inside approved local data can be unlinked without following their destinations.

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

A foreign lease or external non-AGD worktree is not discardable by a distro confirmation. Block deleting a backing Git repository while such dependencies remain. Existing task `phase` alone is not proof of publication or disposability; conversely, a remote PR merge does not silently authorize rewriting stale task lifecycle records.

Inspect the distro source checkout itself, not only registered projects. If it is a linked Git worktree, correlate the exact external common-directory registration and remove that checkout through the narrow native Git/Treehouse operation; verify the registration is gone while preserving sibling worktrees and external refs/history. A foreign source-checkout lease blocks removal. Do not recursively delete an external common directory or run global Git prune. Ordinary source repositories whose common directory would be deleted must also be checked for external worktree dependencies.

Separate discard approval is required for tracked modifications; untracked/ignored project or source-work files not already proven to be AGD-generated private artifacts; and local refs, tags or reflog commits that would be lost and are not evidenced in recorded upstream history. Include the source checkout in this check. Clean clone history evidenced in the existing remote-tracking snapshot is covered by main confirmation; deleting an instance does not require another prompt simply because the clone has history. Local-only ref names and uncertain history are listed as at-risk. Do not require network credentials or fetch to resolve uncertainty: typed discard may authorize owned but unverified work. It cannot authorize foreign ownership. The preview must not describe a recorded upstream snapshot as a live publication guarantee.

### Docker and partial setup

Reuse home-bound secrets/release-lock and resource-label checks, but do not equate every failed `inspect` with absence. Confirm Docker reachability/context/engine identity before and after destructive operations, correlate exact project/home labels and retained paths, and distinguish not-found from permission/transport/daemon failure. Enumerate actual project resources rather than assuming only the usual service names exist.

The preview identifies the selected Docker engine/context. Refuse a conflict with any retained engine evidence. Legacy records without engine identity must visibly disclose that gap; confirmation authorizes only the displayed engine, never an undiscovered engine or context. Do not search/delete across remote Docker engines. If resource ownership or the intended engine cannot be established, retain the home and require correction before retry.

An interrupted first enable can have Compose files, a lock and secrets without a completed config. This is a supported discovery path. A never-enabled feature must not be provisioned merely so uninstall can disable it. Stopping containers is not removing them, and retaining volumes without their recovery metadata is not a successful full uninstall.

## Foreground executor and durable recovery

The checkout-home case means the original command and modules will be deleted. After consent, create a private, deterministic sibling operation directory keyed by the canonical target path, outside every deletion root. Stage only the trusted CLI/Python modules and immutable assets needed for uninstall, using an explicit allowlist; do not copy `.git`, private configuration, project data, sessions, or auth. Use an interpreter outside the deletion roots and foreground exec into this staged CLI with the same terminal/process lineage. This is temporary execution staging, not a detached job, downloaded uninstaller, backup, or new service. If a safe writable parent/interpreter is unavailable, fail before quiescing anything.

The sibling `operation.json` is the authoritative non-secret journal and owns the coordinator lock. It records the original home and filesystem identity, stable resource/engine identities, approval scopes, stage integrity, progress, and verification outcomes. The in-home `state/uninstall.json` is only a retirement pointer/status. Validate both; neither permits following a symlink or adopting another operation. Active external retirement evidence must also gate init/new producers after the original home becomes absent. Claiming retirement and admitting producers share short synchronization; two uninstallers must not create competing operations.

Print the exact staged recovery command before teardown. On interruption, leave this executor/journal intact. Resume from that command or discover the owned sibling receipt when running an intact matching CLI; obtain a fresh preview/confirmation and reuse only valid scopes. A missing home ID file is tolerable only when the journal proves it was removed during this exact operation and any remaining target still has the original filesystem identity. A newly created replacement directory is not the original target and must not be removed on retry.

Keep the staged executor/journal through partial home deletion and the full delayed absence check. After a crash with the home already absent, resume external-resource and delayed verification from this receipt without recreating the home. Remove only this operation's staged files/directory after verified completion. Report any remaining staging artifact explicitly rather than claiming complete removal.

Malformed, corrupt, conflicting, or identity-mismatched journals block automatic execution/resume. Do not overwrite, repair, clear, or trust their approvals automatically; preserve evidence and give manual recovery guidance. A valid cancelled operation permits a new confirmed attempt; a valid interrupted operation resumes rather than becoming a second operation. A recorded terminal outcome never authorizes deleting a new home at the same path. No adoption rule exists for unreceipted runtime resources: reconcile a supported partial setup from sufficient retained evidence, otherwise report a blocker.

## Quiescing, recovery, and deletion ordering

Use the external authoritative journal/lock described above and the in-home retirement pointer. These are scoped operation receipts, not a general workflow framework, and must contain no credentials or payload contents.

Publish a retiring fence under the same short admission synchronization used by new runtime/state producers. Start/attach, watcher rearm, worker launch/relaunch, task allocation, Relay service creation, reviewer dispatch/timer activation, installer setup/init, and other mutating paths must not create new work once retirement is admitted. Already admitted creation must settle into discoverable receipts before the final inventory. Do not hold the ordinary store mutation lock while calling shutdown helpers that acquire it themselves. Old already-loaded processes may not honor a new fence; absence verification is still mandatory.

Logical ordering:

1. Read-only inventory and both required confirmations.
2. Stage the foreground executor, lock, revalidate identity/approval, journal intent, and fence new producers.
3. Disable scheduled activation and stop the exact central session early so it cannot rearm its watcher or introduce work; finish watcher retirement, including its best-effort Relay cleanup. Then independently stop/drain workers, reviewers, remaining Relay processes, and exact backend endpoints. Never equate central exit with worker/Relay absence, and never kill only the watcher while a still-active central can rearm it.
4. Reconcile all owned process/endpoint absence and any work changes; obtain refreshed discard approval if necessary.
5. Remove owned external resources, worktrees/Git registrations and approved volumes/units, recording each outcome. Preserve enough configuration/identity to retry every incomplete step.
6. Verify no owned trigger/process/resource remains and no foreign dependency would be orphaned. Revalidate target filesystem identity; remove the home last (via exact worktree removal when it is a linked source checkout). The staged executor/journal remain outside it.
7. Perform a bounded delayed absence check across the known watcher/recreation interval and persist the result externally. No post-delete helper may import or write inside the removed tree. Unexpected recreation is a failed uninstall; preserve the external evidence and do not blindly delete a newly created directory. On verified completion, clean the stage and emit the final result.

An `uninstalled` result means the approved owned resources and original home are absent, with shared exclusions listed. Unknown state, skipped resources, loss of tool/engine access, timeouts, or changed identity are failures: return nonzero, retain remaining home plus external evidence, and give exact retry/blocker information. Do not proceed to final deletion just because a stop/disable command returned zero.

Rerunning the operation resumes from the external receipt after fresh preview/confirmation, reusing only still-valid approvals/identities. A confirmed already-missing resource is idempotent success. With no pending receipt, an entirely missing target is only `already-absent`: never recreate it or claim unknown external resources were verified.

If the user cancels after quiescing but before any irreversible removal, clear retirement admission safely, record the cancelled outcome, and leave stopped services stopped; do not automatically restart anything. Once irreversible removal has begun, cancellation/failure preserves external retirement evidence, any remaining in-home pointer, and the staged executor for retry rather than advertising the home as usable. Clearly distinguish these outcomes. No automatic rollback or backup is promised.

## Shared exclusions and non-goals

Never remove APT packages; account-global OMP/Herdr/Treehouse/Bun/Node/Relay or Docker/Compose installations; shared Docker images; account XDG/Git/OMP/provider configuration; another home's services/data; user-systemd manager/lingering; or remote repositories, branches, PRs, provider accounts or credentials. A distro-local `managed` dependency receipt does not imply exclusive ownership of a shared executable.

Do not vacuum shared journals/caches, revoke shared provider tokens, securely erase physical media, uninstall arbitrary user applications, add a backup/archive feature, or add detached/unattended uninstall. No automatic publication or merge is part of this work.

## Verification and acceptance

Behavioral regressions must cover target/identity refusal, no-write dry-run and declined confirmation, separate changed-work approval, quiescing/restart races, active reviewer versus disabled timer, exact process/endpoint absence, partial/unavailable Docker/systemd/backend state, external/foreign leases, repeated cleanup, shared-resource preservation, and failure before final home deletion. Avoid source-text, argv-order, mocked-forwarding, or incidental output wording assertions.

Also cover source-checkout linked-worktree registration removal, lost/corrupt journal refusal, cancellation before versus after irreversible removal, safe independent external filesystem roots versus nested mounts, and crashes during home deletion or after deletion but before delayed verification. Those recovery cases must work from the external executor without the original CLI, home marker, or modules.

Invocation acceptance includes relative-home normalization before any cwd change, a normal in-home terminal, coordinator cwd relocation, and refusal from a managed process tree being stopped. Do not claim to have changed the parent shell's cwd.

Use disposable native smoke resources to prove actual central/backend shutdown, active reviewer service/timer cleanup including persistent state, owned Docker container/network/volume removal, native Treehouse release/destruction and Git registration cleanup, and delayed non-recreation. Exercise the actual interactive command from normal terminals both inside and outside its target; verify a second home and shared tools/sentinel resources remain untouched. No paid model call or real user-distro deletion is required. Name unavailable prerequisites and distinguish exercised proof from inference; mocks do not replace native lifecycle acceptance.

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
