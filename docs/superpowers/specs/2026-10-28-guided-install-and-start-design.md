# Guided installation and backend-aware start design

## Status and scope

The conversation has approved this design. This file records that decision and awaits review before an implementation plan is written. It authorizes neither implementation, package installation, privilege elevation, download, account login, backend-session creation, central startup, task mutation, publication, merge, approval, trust change, nor telemetry activation.

The feature retains the published `install.sh` as the remote one-line entry point and adds a narrow `bin/agd start [--backend tmux|herdr]` operation. Scope is Linux x86-64, the current OMP integration, and the existing tmux and Herdr backends. It reuses the managed central command `agd observability central`; it does not introduce a second central supervisor, a generic installer framework, a package framework, or a compatibility alias.

## Existing baseline and evidence

- `install.sh` currently requires an interactive terminal, validates the named destination against the established `[a-z][a-z0-9-]{0,47}` identifier rule, refuses an existing path or symlink, clones canonical `https://github.com/feddericovonwernich/agent-distro-starter.git`, preserves `origin`, and runs `bin/agd --home "$PWD" init`. It currently requires preinstalled Git and Python 3.12+ and does not provision tools.
- `bin/agd init` establishes private `config/`, `projects/`, `data/`, and `state/` directories and a private `data/home.json`; it deliberately does not overwrite an existing home.
- `bin/agd doctor` currently checks Git, Treehouse v3.1.6, OMP (or Cursor), and a selected backend executable. It checks executables, not provider/account authentication.
- `agd observability central` is the existing managed central launcher. It sets the managed OMP working directory and session directory, handles the established observability environment, and `exec`s OMP. It must remain the one command the new launcher starts.
- The existing central extension starts the owner/watcher protocol. `state/owner.json` contains central PID/process-start identity, owner token, watcher PID/process-start identity, generation, and heartbeat; `state/session.lock` belongs to the watcher. A live foreign owner causes normal mutations to refuse. These are accidental-misuse protections, not an OS security boundary.
- Current worker selection is explicit per launch (`--backend tmux|herdr`), defaults to tmux, and is persisted in each task. tmux already uses a per-home socket/server and validates pane ownership tokens. Herdr already requires `HERDR_ENV=1`, an explicitly named non-default `AGD_HERDR_SESSION`, a verified socket device/inode, and a recorded workspace/tab/pane/terminal endpoint.
- The checked compatibility record observed Linux x86-64 with OMP 18.3.2, tmux 3.4, and Herdr 0.8.2/protocol 20. Direct inspection of the verified Treehouse archive [`treehouse-v3.1.6-linux-amd64.tar.gz`](https://github.com/kunchenguid/treehouse/releases/download/v3.1.6/treehouse-v3.1.6-linux-amd64.tar.gz) found exactly one member, `treehouse`; its archive SHA-256 is `e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1`.
- Primary upstream evidence records OMP at [`can1357/oh-my-pi` v18.13.3](https://github.com/can1357/oh-my-pi/releases/tag/v18.13.3), including `omp-linux-x64` and published SHA256SUMS, and Herdr at [`herdrdev/herdr` v0.9.5](https://github.com/herdrdev/herdr/releases/tag/v0.9.5), including `herdr-linux-x86_64` and an API digest. The authoritative OMP v18.13.3 [providers documentation](https://github.com/can1357/oh-my-pi/blob/v18.13.3/docs/providers.md), [CLI reference](https://github.com/can1357/oh-my-pi/blob/v18.13.3/docs/cli-reference.md), [`login` command source](https://github.com/can1357/oh-my-pi/blob/v18.13.3/packages/coding-agent/src/commands/login.ts), and [`models` command source](https://github.com/can1357/oh-my-pi/blob/v18.13.3/packages/coding-agent/src/commands/models.ts) establish `omp login [provider]` and `omp models --json`. The current [Herdr CLI reference](https://herdr.dev/docs/cli-reference/) and [versioned v0.8.2 CLI reference](https://github.com/herdrdev/herdr/blob/master/docs/versions/0.8.2/website/src/content/docs/cli-reference.mdx) establish ordinary-terminal named-session use through `herdr --session NAME` and `herdr session attach NAME`. The release queries are [OMP latest](https://api.github.com/repos/can1357/oh-my-pi/releases/latest) and [Herdr latest](https://api.github.com/repos/herdrdev/herdr/releases/latest). These facts establish sources and observed release capability, not installer pins. Herdr still exposes no server PID or creator identity, so the launcher cannot infer ownership from a same-named server.

## Goals

1. Keep one interactive, reviewable remote installer that creates a new named local clone without overwriting an existing destination and leaves the user in a nested interactive shell rooted at that clone.
2. Guide only supported Linux x86-64 installation, bootstrap only missing Git/Python 3/tmux through APT after a single explicit, default-No sudo consent, and otherwise report precise unmet requirements.
3. Reuse compatible installed tools; offer, but never silently perform, updates to OMP and Herdr; install Treehouse exactly at the approved pinned release and digest.
4. Persist one selected central/worker backend per home and safely start or attach to the existing managed central in that backend.
5. Fail closed around owner, backend, and launcher identity while preserving every existing task, lease, endpoint, session, and unknown record.

## Non-goals and hard boundaries

- No support for non-Linux or non-x86-64 hosts, guessed package managers, non-APT system bootstrap, root/sudo auto-approval, or silent elevation.
- No destination overwrite, clone relocation, global Git configuration change, tool/configuration deletion, package-state rollback, or removal of anything beyond a newly created clone after setup failure.
- No `curl | sh`; release assets are downloaded as files, verified before use, and installed atomically.
- No hard-coded OMP or Herdr version contract based on the currently observed releases, no package manager version pin for them, and no general dependency/package framework.
- No reading, printing, storing, exporting, or validating provider credentials. AGD does not call `omp auth export` and does not make paid model/inference requests.
- No automatic authentication, project trust, task creation/cleanup, worker stop, merge, publish, approval, telemetry opt-in, cleanup, migration, or adoption of external state.
- No reset, kill, deletion, merge, or replacement of unknown, partial, stale, foreign, or interrupted central/backend state. Failure retains evidence for inspection.

## Installer architecture and dependency policy

### Entry point and host gate

The public README one-liner continues to fetch and execute the tracked root `install.sh`; the script remains the reviewable remote entry point. Before changing the filesystem, it requires a terminal, verifies Linux and `x86_64`, and validates the requested distro name and a non-existent non-symlink destination exactly as today. It prompts for the distro name and selected backend (`tmux` or `herdr`). The destination refusal happens before clone creation.

The bootstrap shell remains deliberately small: terminal/host checks, prompts, controlled APT bootstrap, canonical clone, and invocation of a focused Python helper under `bin/agd_lib`. The helper is installer-specific code, imported by the script or an explicit installer entry point; it is not a new Python distribution/package framework. It owns release discovery, download verification, user-local installs, compatibility checks, backend configuration, and durable installer receipts where required.

### System bootstrap and sudo boundary

Only missing `git`, `python3`, and selected `tmux` are eligible for system bootstrap. On a host where one or more of those are missing, show the exact missing package requirements and ask once whether `sudo` may be used. The blank response and every non-affirmative response mean **No**. If declined, do not invoke `sudo`, `apt`, or another package manager; report the missing requirements and exit before clone or helper actions that require them.

If accepted, invoke `sudo` once explicitly to authenticate, then use APT only to install the exact missing system packages. Any failure reports the failed package/action and leaves all package state intact; the installer does not try another privilege path or package manager. Existing compatible Git/Python/tmux are reused. A present but incompatible Python fails with the required version rather than attempting an unapproved system replacement unless it is genuinely missing and the user accepted the APT bootstrap.

For unsupported distributions, absent APT, unsupported architecture, unavailable package names, or missing prerequisites after declined/failed bootstrap, exit with the precise unmet requirement. Do not infer a package manager from `/etc/os-release` or offer guessed commands.

### User-local tools and releases

The helper evaluates each dependency in this order:

1. Discover an existing executable and evaluate its compatibility using its documented local version output and the feature's narrow capability checks.
2. If compatible, retain it and explicitly offer an update where the tool has an AGD-managed update path. A declined update retains the compatible installed executable unchanged.
3. If missing or incompatible, explain the exact unmet requirement and offer the supported user-local install route.
4. Before the final install/update consent for OMP or Herdr, query that upstream repository's GitHub releases API at execution time, choose the latest release that is neither draft nor prerelease, display its tag/version, and identify the exact Linux x86-64 asset plus its published digest. The user must then affirm that exact action; no compatible tool changes without that consent.
5. After consent, download the selected asset, verify the selected exact digest, record the tag and digest, then atomically replace the managed executable in `~/.local/bin`.

Asset selection must use an explicit per-tool exact Linux x86-64 naming/metadata rule verified against the selected release; ambiguous, missing, duplicate, or non-Linux-x86-64 assets are refusals. A release without a trustworthy published digest for the selected exact asset is a refusal. A downloaded asset is written to a private temporary file, digest-checked before it is executable, then atomically renamed into `~/.local/bin`; interrupted downloads/verification never replace the prior executable. Do not use a floating URL, a checksum inferred from HTML, or `curl | sh`.

Treehouse is different: it is fixed at **v3.1.6**, Linux x86-64 only, with SHA-256 **`e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1`**. A compatible existing Treehouse is reused. Otherwise download the exact approved asset, verify that exact digest, and atomically install it into `~/.local/bin`; a mismatched checksum or interruption leaves an existing binary untouched and fails setup. The helper records the selected Treehouse version and digest. No observed OMP/Herdr stable version becomes a pin merely because it was current during design.

`~/.local/bin` is placed at the front of the environment used for dependency discovery, initialization, `agd start`, central launch, and the terminal shell entered after success. The installer does not mutate the parent shell; it ends by `exec`ing an interactive `${SHELL:-/bin/sh}` in the distro cwd, with `~/.local/bin` available. Exiting that nested shell returns to the calling shell.

### Clone, initialization, and failure ownership

After prerequisites sufficient to clone/run Python are confirmed, clone the canonical origin into the newly refused-and-reserved destination, preserving the normal `origin`, then initialize that clone and write its backend configuration. Initialization persists both the selected backend and a generated non-default `herdr_session` for every launcher home, including one whose selected backend is tmux. The helper must distinguish a clone newly created by this invocation from an existing path. On setup failure after clone creation it removes **only that newly created clone**. It never removes or rolls back tools, APT packages, package databases, running backend sessions, credentials/configuration, existing paths, or any pre-existing private home. If deletion of the newly created clone cannot be confirmed, report that fact and retain it; do not broaden deletion.

## Authentication boundary

After OMP installation/selection, offer the user OMP's own `omp login [provider]` command in the interactive installer flow. Skipping login is allowed. AGD merely launches the command in the user's terminal; it never receives, parses, persists, prints, copies, or logs credentials.

`omp models --json` may be used only to display locally configured/selectable models. Its result is not proof that a credential is valid, authorized, or billable. The installer does not issue a model prompt or any paid inference probe and never invokes `omp auth export`.

## Durable configuration and launcher state

All records are under the effective `AGD_HOME`, owner-only, ignored by Git, atomically written using existing `store.atomic_json` semantics, and validated strictly before use. A malformed, symlinked, incomplete, or schema-incompatible record is an uncertain state and must refuse mutation/launch without replacement.

### `data/launcher.json`

`data/launcher.json` is the durable selected-launcher configuration:

```json
{
  "schema": 1,
  "backend": "tmux",
  "herdr_session": "agd-<random-suffix>"
}
```

`backend` is exactly `tmux` or `herdr`. `herdr_session` is required for **every** initialized launcher home, regardless of its selected backend; it is a generated, non-default random-suffixed AGD server name matching the existing safe identifier grammar. This makes a later `agd start --backend herdr` possible even when the saved backend is tmux. The record contains no credential, token, PID, worker lease, endpoint, or mutable runtime assertion. An explicit `agd start --backend` is ephemeral and does not rewrite this file.

### `state/central-launcher.json`

`state/central-launcher.json` is the launcher receipt, separate from the central extension's `state/owner.json`:

```json
{
  "schema": 1,
  "launch_id": "UUID",
  "endpoint_token": "random-secret-like-correlator",
  "backend": "tmux",
  "phase": "reserved",
  "endpoint": {
    "backend-specific verified identity": "..."
  },
  "managed_command": ["bin/agd", "--home", "/absolute/home", "observability", "central"],
  "expected_process_argv": ["omp", "..."],
  "owner": null,
  "watcher": null
}
```

`phase` is one of `reserved`, `spawned`, or `ready` and moves only forward for one launch ID. Before endpoint creation, the launcher generates and records the launch ID and random endpoint token. `endpoint` holds the identity needed to prove backend continuity; `managed_command` is the exact pre-exec managed-central command, never arbitrary user shell text. `(expected_process_argv, central_env, warning) = observability.central_argv(home, ())`; the receipt retains only `expected_process_argv`, while launch merges `central_env` with the launch ID/token. Because `agd observability central` execs OMP, only `expected_process_argv` is used for foreground/owner readiness proof. `owner` and `watcher` are filled only after the extension-created `owner.json` is observed and correlated to this launch.

A crash or interruption between reserve, spawn, ready correlation, or attach leaves the receipt in place. The next invocation inspects it and either proves exact continuity or refuses as ambiguous. It never treats absence of a process, receipt, pane, or watcher as permission to erase state.

## `agd start` selection and lifecycle

### Backend precedence and worker inheritance

`bin/agd start [--backend tmux|herdr]` resolves the central backend in this exact order:

1. explicit `--backend` for this invocation;
2. a proven active `central-launcher.json`/`owner.json` receipt for this home, with a matching inherited launcher environment when present;
3. `data/launcher.json`'s saved backend.

The first matching source wins. An override applies only to that `start`; it never rewrites saved configuration. Thus a central originally started with `--backend herdr` remains reattachable from an ordinary-terminal `agd start` with no override even if saved configuration says tmux. Conversely, an explicit override that differs from a proven active central refuses rather than creating a second central or migrating it.

A worker launch with no explicit `--backend` inherits the proven active central backend; an explicit worker backend remains a worker-only override. The `bin/agd` worker-launch parser must preserve an omitted backend as `None`, and `workers.launch` must resolve that omitted value separately from explicit `tmux`; explicit `--backend tmux` must not be conflated with omission. A non-active home falls back to its saved backend. Existing recorded worker/task backends and endpoint records are never rewritten by selecting a central backend.

### Common start protocol

Under the existing short-lived home mutation lock:

1. Validate `launcher.json`, any `central-launcher.json`, `state/owner.json`, and backend identity record shapes/safe filesystem identities.
2. Inspect the existing owner and launcher receipt. A live owner not correlated to this exact launcher receipt/backend is foreign and produces a read-only refusal. Do not delete its owner file, alter its token, or kill its process.
3. If a ready receipt can prove the exact endpoint identity, tokened endpoint foreground OMP PID/process-start/argv, matching owner PID/process-start, and healthy matching watcher, attach interactively instead of spawning. Every predicate is required; any false/unknown predicate refuses and retains state.
4. If a receipt is `reserved` or `spawned`, or the owner/backend has been replaced, is partial, or cannot be correlated, refuse as interrupted/ambiguous. Do not reset it.
5. With no conflicting/ambiguous state, generate a random endpoint token and new UUID, then reserve their receipt in `reserved` before creating a backend endpoint.
6. Create/verify the selected backend endpoint, mark that exact endpoint through its backend metadata and environment with the recorded launch ID/token, update the receipt to `spawned` with its complete identity, and launch exactly `bin/agd --home <effective-home> observability central` directly there. Do not introduce an abstraction beyond focused launcher/backend methods.
7. Wait boundedly for the existing OMP extension to create `state/owner.json` and its watcher. Read the tokened endpoint's foreground OMP PID, process-start, and argv; require them to equal the owner PID/process-start and receipt `expected_process_argv` from `observability.central_argv(home, ())`, plus matching launch ID/token, endpoint identity, and watcher PID/process-start/heartbeat. The pre-exec `managed_command` is retained for audit only and is never compared to post-exec argv. Only then write `ready` and attach interactively.
8. On any failure after reserve, retain receipt and endpoint identity and return an actionable refusal. Do not close the pane/tab/server, delete state, or start a replacement.

### tmux backend

tmux continues to use the existing deterministic per-home `tmux -L agd-<home-hash>` server and `agd` session. `agd start` creates or reuses that per-home server/session only after the common protocol has proved no conflicting launcher state. It creates a dedicated central pane (or central window/pane identity recorded in the receipt), marks ownership with the recorded launch ID/token, starts the exact central command, and verifies server PID plus pane ID/token plus foreground OMP PID/process-start/argv before readiness/attach.

A repeated start attaches only when the receipt's server PID, pane ID/token, foreground OMP PID/process-start/argv equal receipt `expected_process_argv`, matching `owner.json`, and healthy watcher all match. `managed_command` is audit-only. The existing per-home tmux server/session may contain unrelated or retained task panes; this operation neither kills nor resets them. Detaching from the central pane leaves the central and tmux session running.

### Herdr backend

Every initialized launcher home has the generated `herdr_session` from `launcher.json`, even when its saved backend is tmux. `agd start --backend herdr` uses that recorded non-default name; it must not use `default`. Ordinary-terminal central start/attach is a new, narrow central-launcher path: while `HERDR_ENV` is absent, it invokes documented `herdr --session NAME ...` commands with that explicit saved session and records server socket device/inode, workspace ID, tab ID, pane ID, terminal ID, launch ID/token metadata, foreground OMP process identity, and AGD owner correlation in the central receipt. It does **not** relax or reuse the existing worker Herdr adapter's `HERDR_ENV=1` gate; worker mutation remains pane-bound.

Herdr exposes no server PID or creator. Therefore, the first `agd start` refuses a pre-existing same-name Herdr server unless a matching AGD receipt already proves it was created by this home/launch. It never adopts, stops, or deletes that server. The implementation may create a server only through the controlled first-launch path and must preserve the identity receipt before it creates the workspace/tab/pane. Repeat attachment requires the exact socket device/inode plus all recorded workspace/tab/pane/terminal/foreground-process/owner/endpoint-token continuity and a healthy watcher. A missing/replaced socket, response timeout, missing pane, foreign workspace/tab/pane, or unavailable ordinary-terminal attach is unknown/unsafe and refuses without destructive recovery.
### Backend-session preservation invariant

Every tmux or Herdr backend session that contains an endpoint for any retained task remains alive. `agd start`, interactive attach/detach, failure, refusal, and interrupted launcher state never close that session, its server, or its task endpoints; they perform no implicit cleanup or stop. This holds even when the central endpoint is uncertain or a conflicting launcher request is refused. Only an existing explicit task/session lifecycle operation outside this feature may request endpoint/session teardown, and its existing identity checks remain authoritative.


## Error handling and preservation matrix

| Condition | Required behavior |
| --- | --- |
| Existing destination, including symlink | Refuse before clone; leave it untouched. |
| Unsupported OS/architecture or no supported APT route | Report the exact unmet requirement; do not guess another package manager. |
| Missing APT package and blank/No sudo answer | Do not invoke sudo/APT; report required tools. |
| APT/update/download/digest/initialization failure | Preserve pre-existing tools, packages, sessions, and state; remove only the clone created by this run when safely known. |
| Existing compatible OMP/Herdr/Treehouse | Reuse it. Offer update only where policy permits; decline changes nothing. |
| OMP/Herdr release asset/digest ambiguity | Refuse that action; do not install a guessed asset. |
| Treehouse digest mismatch/interruption | Do not replace any existing binary; fail with version/digest requirement. |
| Login skipped/failed | Preserve credentials/configuration; report that login remains the provider's explicit operation. |
| Live foreign owner | Refuse start read-only; preserve `owner.json`, token, watcher, sessions, leases, and tasks. |
| Reserved/spawned receipt or replaced/partial backend | Refuse fail-closed; preserve receipts and backend resources. |
| Active central with conflicting backend override | Refuse; no second central and no saved-config rewrite. |
| Exact ready central | Attach only after exact tokened endpoint foreground process/argv, owner, and healthy-watcher proof. |
| Any unknown stale/external state | Retain it and refuse; never adopt, kill, reset, merge, or delete it. |

## Supply-chain and security boundaries

The install path is interactive and user-controlled, but downloaded executables remain a supply-chain boundary. The implementation must use HTTPS, GitHub's releases API for runtime OMP/Herdr selection, an explicit exact-platform asset rule, a release-provided digest for the exact bytes, durable tag+digest recording, private temporary files, and atomic `~/.local/bin` replacement. It must not trust a redirect target, release title, asset suffix alone, current observed versions, shell installer, or unverified checksum.

Sudo authorization is visible, opt-in, and bounded to missing Git/Python3/tmux APT installation. It is not reused for OMP, Herdr, Treehouse, project setup, central startup, or any later action. The helper preserves existing binaries/configuration on declined updates and failed replacement.

Provider authentication remains inside OMP. AGD must redact nothing because it must never receive provider secrets in the first place: do not read auth stores, invoke secret-export commands, serialize credentials into JSON receipts, capture them in errors, or pass them to workers. `models --json` is a capability listing only, not an auth check. Launch/endpoint, backend, and owner tokens are accidental-misuse correlators, not credentials or an OS security boundary, and normal output must not expose them.

## Required documentation and verification

Implementation must update README, `docs/operations.md`, `docs/compatibility.md`, and `CHANGELOG.md` together with behavior. Documentation must state the public one-liner, Linux x86-64/APT boundary, default-No sudo prompt, local install/update/digest rules, Treehouse fixed pin/digest, nested-shell behavior, OMP login boundary, saved/override/inherited backend selection, `agd start` attach/refusal behavior, and the split Herdr contract: the new ordinary-terminal central-launcher path uses the saved named session while worker controls remain `HERDR_ENV=1` pane-bound. It must remove obsolete manual setup statements superseded by the guided flow rather than retain a parallel contradictory path.

Focused deterministic checks must include:

1. Installer fakes for GitHub release API/assets, PATH/tool discovery, APT, and sudo: Linux/architecture gates; blank sudo equals No; only missing Git/Python3/selected tmux are APT candidates; no sudo/APT when declined; existing destination refusal; canonical retained origin; selected backend and always-present generated `herdr_session` persistence; nested shell environment/cwd; and cleanup limited to the invocation-created clone.
2. Release tests: compatible tool reuse; explicit update prompt/decline preservation; latest non-draft/non-prerelease filtering; visible selected version before consent; exact Linux x86-64 asset selection; recorded OMP/Herdr tag+digest; digest mismatch; interrupted download; and atomic replacement that preserves the old executable. Test Treehouse v3.1.6 with the approved SHA-256 exactly.
3. Authentication tests: offered `omp login [provider]` remains a terminal command boundary; no auth export; `omp models --json` output is labelled selectable/configured rather than valid authentication; no inference probe.
4. CLI/config tests: saved backend; explicit ephemeral override; saved-tmux `agd start --backend herdr`; no-override ordinary-terminal reattach to that proven active Herdr receipt; and refusal of a conflicting explicit override. The `bin/agd` worker-launch parser must preserve an omitted backend as `None`, not implicit tmux, and `workers.launch` must distinguish it from explicit tmux. Test omitted worker inheritance from the proven active central and explicit `--backend tmux` override without mutation of saved/current central backend.
5. Launcher state tests: fresh reserve/spawn/ready transitions; foreign live owner refusal; exact repeat attach; matching launch ID/token endpoint metadata; endpoint foreground OMP PID/process-start/argv equality with the owner; matching watcher identity; refusal for any mismatched endpoint/process/owner/watcher/token or stale server identity; interrupted reserved/spawned phases; malformed receipts; and preservation of worker leases, task endpoint records, and unknown external state. Assert normal output does not reveal the endpoint token.
6. Backend tests: tmux dedicated central pane/server/launch-token/process validation and detach survival; Herdr first-start refusal for a pre-existing same-name unreceipted server, socket device/inode replacement refusal, workspace/tab/pane/terminal/process continuity, and ordinary-terminal `herdr --session NAME` central attach. Prove the saved-tmux → Herdr override → detach → plain-terminal no-override reattach sequence, and separately prove ordinary worker Herdr controls still refuse outside a Herdr-managed pane. For both backends, retain a task endpoint, then exercise central detach, restart/reattach, and a refused/conflicting launch; assert its backend session/server and task endpoint remain present and unchanged.
7. Disposable real runtime smoke tests for both tmux and Herdr: installer-created/configured home, `agd start`, existing `agd observability central` process, extension-created owner/watcher correlation, verified interactive attach, detach/re-attach, and no competing central. These tests must avoid production credentials and paid model calls.

## Issue acceptance mapping

| Issue #7 acceptance | Design response |
| --- | --- |
| Remote one-liner, origin, no overwrite | Retained `install.sh`, canonical clone/origin, terminal name validation, refusal before destination mutation. |
| Guided dependencies and explicit account auth | APT-only missing-system bootstrap, user-local verified releases, Treehouse pin, explicit OMP login with no secret handling. |
| Saved backend, override, appropriate session, managed central | `launcher.json` persists both backend and an always-present named Herdr session; precedence prefers a proven active receipt after an explicit override, then saved backend; an ordinary-terminal central-only Herdr path launches exact `agd observability central` without relaxing worker controls. |
| Worker inheritance with override | An omitted parser value remains `None` and inherits the proven active central; explicit worker backend, including tmux, remains local. |
| Safe repeat attach / foreign owner refusal | Exact launch-token endpoint foreground process/argv, owner, and watcher proof permits attach; foreign owner is retained and refused. |
| Preserve identities, leases, unknown state | Fail-closed receipt/state model and preservation matrix prohibit reset/adoption/destructive recovery. |
| Retained backend sessions | Every tmux/Herdr session containing a retained task endpoint survives start, attach/detach, failure, refusal, and interrupted state; this feature performs no implicit cleanup or stop. |
| No implicit lifecycle/policy side effects | Non-goals explicitly exclude worker stop/task cleanup/merge/publish/trust/approval/telemetry and backend destruction. |
| Docs, behavior checks, actual terminals | Required README/operations/compatibility/changelog changes; fake-boundary tests and real tmux/Herdr startup smokes, including plain-terminal Herdr reattach and pane-bound worker refusal. |
| Reuse existing lifecycle/managed central | The launcher starts only existing `agd observability central` and waits for the existing extension watcher. |

## Handoff and factual risks

This approved design is ready for user review, not implementation planning. A later implementation plan must preserve the exact state names, precedence, failure retention, and no-adoption rules above and should extend the existing focused `agd_lib`, tmux, Herdr, supervision, worker, installer, and test patterns rather than create an installer or supervisor framework.

The OMP command/runtime facts above are established for the observed releases; they are not unknown. The remaining factual risk is that the latest-release APIs are moving. At execution and acceptance, the helper must revalidate the selected non-draft/non-prerelease release's API schema, exact asset name, published digest format, and required OMP/Herdr capabilities before acting on it. Current evidence—OMP [`can1357/oh-my-pi` v18.13.3](https://github.com/can1357/oh-my-pi/releases/tag/v18.13.3) (`omp-linux-x64`, SHA256SUMS) and Herdr [`herdrdev/herdr` v0.9.5](https://github.com/herdrdev/herdr/releases/tag/v0.9.5) (`herdr-linux-x86_64`, API digest)—is not a pin. Herdr's absence of a server PID/creator remains a design constraint addressed by fail-closed receipt and endpoint continuity checks.
