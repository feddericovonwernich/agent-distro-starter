# Installer Observability Opt-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask about distro-local telemetry during installation and apply the user's choice before the first central starts.

**Architecture:** Add one optional final stage to `install.sh`, calling the existing `bin/agd observability enable` lifecycle. Keep dependency/plugin setup separate and end clone rollback ownership before any optional Docker provisioning. No second settings schema or exporter implementation.

**Tech Stack:** Existing Bash installer, Python AGD CLI/observability lifecycle, Docker Compose, pytest/PTY installer fixtures.

**Spec:** Embedded design below, approved by the user on 2026-10-30. Implementation is authorized; enabling jarvis-13, publishing, and merging remain separate actions.

## Proposed design

### Intent and approaches

Installation should make the difference between native local session recording and optional Langfuse export explicit. The next normal central/worker launch should inherit the selected telemetry configuration without a second enablement step.

Recommended: a fourth shell-installer stage that reuses the public observability command. Alternative: move optional provisioning into `installer.setup`; rejected because setup currently verifies dependencies/plugins and is used separately, and the outer shell would still need a new rollback boundary. External/Cloud Langfuse configuration is a separate feature, not an alternative implementation of this local-stack opt-in.

### Operator interaction

- Renumber the existing stages to `1 / 4`, `2 / 4`, `3 / 4`; add `4 / 4  Configure observability` after successful dependency/plugin setup.
- Explain before consent: this starts a distro-owned local Langfuse + Collector Docker stack, pulls images/uses disk and memory, and captures potentially sensitive prompts, instructions, and tool inputs/results. Services are loopback-only; this is not Langfuse Cloud. Local native session files are retained even when export is off.
- Ask `Enable local Langfuse telemetry? [y/N]` using existing color/NO_COLOR/TERM and `/dev/tty` conventions. Accept y/yes case-insensitively; blank, no, or EOF does not enable anything.
- Decline: no observability lifecycle command, Docker probe, image pull, service startup, credentials, or resource allocation. Report `Telemetry: disabled (local session recording remains available)` for this fresh installation, plus the later `bin/agd observability enable` command.
- Consent: invoke the installed clone's CLI with an explicit home and existing dependency PATH. Reuse its lifecycle prerequisite failures, pinned stack, generated project credentials, private paths, loopback allocation, health checks, and sanitized errors. Do not install Docker, change group membership, elevate, or edit native OMP preferences.
- Preserve the existing pretty terminal JSON output. Show the safe public `observability status` result after an attempt; do not expose secrets, parse private credentials, or claim trace delivery from a healthy stack. Before the first agent request, delivery may legitimately be unknown.
- Successful installation still enters the distro shell and offers `bin/agd start`; do not automatically start a central. Since consent precedes the first central launch, no initial session restart is required.

### Failure and ownership boundary

The current EXIT/signal trap recursively removes an installer-owned clone on any failure. After mandatory dependency/plugin setup succeeds, set `owned=false` before the new stage. Existing mandatory failure cleanup remains unchanged. Keep signal/exit behavior correct; later shell entry already clears the trap.

Run optional enablement in a guarded conditional so `set -e` cannot delete or abort the usable distro on an ordinary telemetry failure. On failure, show a visible warning that telemetry setup is incomplete, retain all compose/config/identity/credential/queue files, and print exact retry/status/disable commands. Status failure must not hide the original error or prevent shell entry. Do not label an uncertain partial stack disabled or clean, auto-delete containers/volumes, or silently switch configuration. Interruption after this boundary also preserves the home; the operator can inspect and stop partial resources.

### Non-goals and activation

No changes to the live jarvis-13 instance, automatic upgrade of existing installs, external Langfuse endpoints, API-key wizard, history backfill, OMP provider/model/login configuration, global observability defaults, Docker installation, or exporter redesign. Existing homes continue using `observability enable|disable|status`. No claim that enabled/healthy means a specific trace reached Langfuse.

## Global Constraints

- Telemetry requires explicit opt-in; the default is No.
- Reuse the public observability lifecycle; no duplicate settings, secret generation, resource ownership, health, or export code.
- Do not install Docker, elevate, change user groups, or alter OMP configuration.
- Once optional external resources can exist, preserve their owning home and recovery records.
- Retain current rollback on mandatory clone/init/dependency/plugin failure.
- Preserve loopback binding, private generated secrets, pinned stack, unrelated homes, and containers.
- Native session recording, stack health, and confirmed Langfuse delivery remain distinct facts.

## Review Focus

1. Blank/EOF/decline must not start Docker or allocate telemetry resources; install still enters a usable shell. Covered by Task 1 decline/EOF scenarios.
2. An enable attempt can create external resources before failing; its home/compose identity must survive both failure and interruption. Covered by Task 1 partial-stack and signal scenarios.
3. A healthy stack with no traffic is not confirmed Langfuse delivery; no misleading sent-data success message. Checked in Task 1 actual PTY/status smoke.
4. New prompt input must not consume the first command intended for the installed shell. Covered by Task 1 queued-answer/marker scenario and backend fixture updates.
5. Consent must affect first central/worker launches while leaving native OMP settings/auth unchanged. Checked in Task 1 isolated enabled/disabled environment smoke and existing OMP-configuration regression.

---

## File map

| File | Change |
|---|---|
| `install.sh` | Fourth stage, consent, rollback boundary, guarded lifecycle invocation, warning and recovery instructions. |
| `tests/test_install.py` | Existing PTY fixture support for optional provisioning and durable failure artifacts; behavioral regressions. |
| `tests/test_backends.py:_install_start` | Supply an explicit declined telemetry answer before queued `exit`; preserve owned real-backend acceptance. |
| `README.md`, `docs/operations.md` | Installer choice, privacy/cost, default, prerequisites, failure retention, and later enablement. |
| `docs/compatibility.md`, `CHANGELOG.md` | Actual exercised outcomes/prerequisites and user-visible installer behavior. |

No production Python API change is expected. Read `installer.setup`, its callers, and observability lifecycle at execution to verify this remains true. No new permanent smoke script or dependency is needed.

### Task 1: Add safe optional observability onboarding

**Interfaces:** Consume the existing `bin/agd --home HOME observability enable|status|disable` commands and their existing exit/output contract. `installer.setup(...)` and dependency receipts remain unchanged. Shell ownership changes only after mandatory setup returns successfully.

- [ ] **Step 1: Add failing behavior cases in `tests/test_install.py`.** Extend existing process-boundary fixtures only enough to model partial external resources and failure. Cover both backends for default decline; explicit blank/no/EOF; consent success; consent failure before resources; partial provisioning failure; signal after partial provisioning; and a home path containing spaces. Assert resulting home/resource/config preservation or absence, shell marker execution, and truthful failure reporting—not command-count, forwarding, prose-copy, or source-text assertions. Preserve current tests proving mandatory setup failure removes the owned clone and existing destinations remain untouched. Update queued PTY input with an explicit telemetry answer so shell commands are not consumed by the new prompt.
  - `test_declined_telemetry_keeps_install_usable`: parameterize blank/no/EOF and backend; home exists, shell marker observed where terminal remains open, no telemetry config or owned external resources.
  - `test_enabled_telemetry_applies_before_first_launch`: consent, enabled config and owned resource artifact exist before entering the shell; preserve existing OMP settings/auth.
  - `test_failed_telemetry_preserves_home_and_recovery_state`: parameterize missing executable/daemon failure/partial-start failure; successful installation shell entry, visible warning, retained identity/config for any created resources, no false disabled/delivered claim.
  - `test_interrupted_telemetry_preserves_resource_owner`: interrupt after provisioning artifact exists; home and recovery files remain, child process is reaped, no recursive deletion.
  - `test_telemetry_prompt_preserves_next_shell_command`: supply a consent answer followed by a marker command and exit; marker executes inside the installed home, including a spaced parent path.
- [ ] **Step 2: Run RED.** Run `python3 -m pytest tests/test_install.py -q`. New cases must fail because no consent/provisioning stage or safe optional boundary exists, not because the fixture cannot start or hangs. Read all failure output.
- [ ] **Step 3: Implement the shell stage.** Follow the proposed design in `install.sh`, reusing `section`, color variables, current PATH and explicit-home CLI. Set `owned=false` after the mandatory subshell succeeds and before the new stage. Guard enable/status failures; preserve the original failure warning and recovery records. Keep launch and OMP configuration unchanged. Do not add a new Python installer API just to wrap the lifecycle command.
- [ ] **Step 4: Update affected callers and docs.** Find all `install.sh` PTY drivers, including `tests/test_backends.py`, and insert the additional decline answer before shell input. Update the active docs listed above. Describe enabled/disabled/incomplete status separately from actual trace delivery; do not claim runtime verification before it occurs.
- [ ] **Step 5: Run GREEN and actual installer smoke.** Run `python3 -m pytest tests/test_install.py tests/test_installer.py tests/test_observability.py -q` and `bash -n install.sh`. Exercise the actual terminal installer in an isolated disposable home through decline and induced enablement failure; observe the fourth stage, input handoff, warning, surviving home, and shell entry. Use existing PTY helpers, not a source-text check. Inspect colored and NO_COLOR/dumb-terminal rendering without pinning exact wording.
- [ ] **Step 6: Prove real enabled behavior where prerequisites permit.** In an explicitly owned disposable installation with Docker/Compose access, consent to the real existing lifecycle; inspect `observability status`, loopback stack health, private resource ownership, and generation. Confirm the next managed central/worker environment has the Collector endpoint, while the declined install has no OTEL export controls and native session files remain usable. If generating one synthetic non-sensitive native trace, require readback from that installation's Langfuse project before claiming delivery. Do not use jarvis-13, paid model calls, or unrelated stacks. Stop/remove only disposable owned resources using existing ownership helpers and verify no live process can recreate the directory. Report missing prerequisites as unavailable rows, never as passed acceptance.
- [ ] **Step 7: Integrated check and commit.** Run the existing full Python suite once, with only individually documented environmental exclusions; record results and any existing warnings. Run the backend install/start smoke only where that backend's ownership prerequisites hold; never fake `HERDR_ENV`. Obtain one final review of the installer diff. Commit only this task's installer/tests/docs with `feat: offer local Langfuse telemetry during installation`; push/create PR only after separate user authorization.

## Plan self-review

Consent, privacy, activation, failure retention, existing rollback, and operator recovery each have a producing change and a behavior check in Task 1. There are no inter-task interfaces or new settings to drift. This is one small, sequential installer change; Native execution is recommended, with one final fresh-context review. Docker-backed delivery and backend startup acceptance are explicitly conditional on verified disposable ownership and prerequisites, not replaced by fixture success.
