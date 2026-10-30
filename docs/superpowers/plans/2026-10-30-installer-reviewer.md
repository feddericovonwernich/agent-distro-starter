# Scheduled reviewer installer opt-in implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add default-No reviewer onboarding with presets/customization and safe, runnable native timer activation.

**Architecture:** The fifth shell stage invokes a focused Python `installer reviewer` flow. Shared timer activation validates the installed SDK/Bun and binds a minimal runtime environment into owned systemd units; onboarding saves an accepted config before attempting activation. Reuse existing schedule/config validators, ownership, checkpoints, and reviewer execution.

**Tech Stack:** Existing Bash/Python CLI, OMP native SDK/Bun, systemd user timers, pytest/PTY checks.

**Spec:** `docs/superpowers/specs/2026-10-30-installer-reviewer-design.md`, approved through Approve and implement. Native execution selected in that design; no further implementation handoff is required.

## Global Constraints

- Default No; decline/cancel/EOF performs no configuration write or timer activation.
- No runtime downloads, sudo, linger, cron, OMP login/model changes, live-distro changes, or paid review during installation/verification.
- Missing prerequisites retain the validated selected schedule and report configured/not enabled.
- Preserve existing reviewer limits/model, private credentials, plugin isolation, timer ownership, interval checkpoints, DST checks, and telemetry behavior.
- Daily 02:00 UTC, 6-hour interval, 1-hour interval, custom daily HH:MM/IANA zone, custom positive whole-hour interval; no new schedule syntax.
- Confirm actual native timer activation before reporting enabled; distinguish timer/config state from authentication/model-review success.
- Optional failure/interruption retains the distro and recovery metadata; never delete or alter foreign units.

## Review Focus

1. Installer shell PATH and systemd PATH differ: the service must resolve the validated SDK and Bun without inheriting unrelated credentials. Task 1 minimal-environment regression and native service smoke.
2. SDK missing on the standalone default installation: save a complete chosen schedule, do not create/arm units, and show a usable retry path. Task 2 missing-runtime regression.
3. Invalid/custom/EOF input must not write partial config or consume shell input as consent; declined reviewer must not undo opted-in telemetry. Task 2 PTY and config-preservation regressions.
4. Existing active/failed timer or reviewer run must not have its config silently rewritten. Task 2 safe-reconfiguration regression; Task 1 partial-activation/disable recovery.
5. Native activation can fail after units exist: retain ownership evidence, do not report enabled, and make status/disable usable. Task 1 failure recovery and Task 2 warning/shell-entry checks.

## File map

- `bin/agd_lib/reviewer_timer.py`: runtime readiness/binding and verified native activation; preserve existing scheduling logic.
- `bin/agd_lib/reviewer.py`: reuse validators; fix invalid user timezone exceptions at the shared validator only if required by boundary tests.
- `bin/agd_lib/installer.py`, `bin/agd`: interactive onboarding and CLI dispatch; no generic configuration framework.
- `install.sh`: fifth stage, guarded optional call, numbered stages.
- `tests/test_reviewer_timer.py`, `tests/test_installer.py`, `tests/test_install.py`, `tests/test_backends.py`: behavior and updated input drivers.
- `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`: choices, costs, prerequisites, activation/recovery, exercised evidence.

### Task 1: Make timer activation runtime-safe

**Interfaces:** Add `reviewer_timer.runtime_environment(home) -> dict` returning `python` (absolute interpreter) and `path` (minimal executable search path). It verifies the current selected importable OMP runtime through `reviewer_runner.assert_runtime_capability`, Bun availability, and existing native agent directory; no model call or package installation. Make `render_units(home, config, *, runtime, source_cli=None)` consume that binding; migrate every caller. `enable` obtains this binding, checks the user manager, and verifies native active state after starting. Existing disable/status interfaces remain.

- [ ] Write RED regressions: missing SDK/Bun/agent directory refuses without unit activation; rendered service executes a helper from paths containing spaces/percent/quotes under a minimal environment; startup failure leaves failed state with owned-unit disable recovery; unchanged enable retains interval anchor/checkpoint and does not rewrite identical units. Retain existing native DST and foreign-unit tests. Delete incidental source-string/call-count assertions rather than re-pinning them. Use real file/config effects and lower-level command runners, not mock echoes.
- [ ] Run `env PATH=/tmp/agd-omp-reviewer-18.14.0/node_modules/.bin:$PATH python3 -m pytest tests/test_reviewer_timer.py -q`; observe failures at missing readiness/binding/active verification.
- [ ] Implement the shared path. Preserve the original precedence of the selected OMP and Bun command directories, add standard system command directories, and verify they still resolve the exact validated executables. Bind the actual Python interpreter explicitly in ExecStart. Escape systemd path/environment values without shells or credential forwarding. Existing clock/ownership/checkpoint semantics remain unchanged; preflight and active checks occur outside the home mutation lock.
- [ ] Run GREEN plus actual `systemd-analyze verify` on generated owned units in scratch. Do not activate a user timer before the explicitly scoped disposable smoke in Task 2.
- [ ] Commit `fix: bind scheduled reviewer to validated native runtime`; run task-done for the affected timer/lifecycle tests using the installed official SDK, with output retained in this plan's workspace.

### Task 2: Add schedule onboarding and prove installed lifecycle

**Interfaces:** Add `installer.setup_reviewer(home, ask, report) -> dict`. `ask(message)` returns stripped input or None on EOF; `report(message)` writes human progress/warnings. Return state `skipped`, `cancelled`, `configured` (not enabled), or `enabled`, with the validated schedule/timer details where applicable. Register `bin/agd installer reviewer`; invoke it from stage 5 with current terminal styling. Preserve existing dependency `setup` API.

- [ ] Add failing tests: No/blank/EOF and menu cancel leave no reviewer config/units; presets yield literal native schedule dictionaries (02:00/UTC; interval 6; interval 1); custom daily 03:45/Europe/Berlin and interval 9 validate; malformed time/timezone/hours can be corrected or cancelled without partial writes; custom EOF cancels. Preserve supplied model/limits. Missing runtime/user manager retains selected config but no enabled receipt. Refuse active/failed timer reconfiguration and acquire the existing reviewer run lease while changing config. Partial activation failure keeps home/unit evidence and usable disable. PTY proves next shell marker in a spaced parent path executes after reviewer answers, and telemetry selection is independent.
- [ ] Run RED in `tests/test_installer.py tests/test_install.py`. Extend fixtures to route the new installer command through the real Python implementation. Fake only SDK discovery where specifically testing a missing prerequisite, external systemctl responses, or other unavoidable OS boundaries; do not ask a fake function to return the asserted config.
- [ ] Implement the menu/validation loop in `installer.py`, sharing `reviewer.validate_config`, existing safe private/atomic config persistence, store lock, and reviewer run lease. Reject non-disabled existing timer receipts before changing config; partial state needs explicit disable. Save only a complete valid selection, release locks before native activation, and catch sanitized prerequisite/activation errors into configured/not-enabled results. No installer-triggered review.
- [ ] Add CLI dispatch using the existing interactive prompt/color conventions and distinguish blank from EOF. Add the fifth shell stage after telemetry, guarded like its optional predecessor. Update all PTY drivers with an explicit new decline answer before shell commands. Update docs after smoke, preserving existing telemetry failure rollback boundary.
- [ ] Run focused GREEN: `env PATH=/tmp/agd-omp-reviewer-18.14.0/node_modules/.bin:$PATH python3 -m pytest tests/test_reviewer_timer.py tests/test_installer.py tests/test_install.py -q`; `bash -n install.sh`; Python compile for changed modules/CLI.
- [ ] Exercise actual terminal presets, a corrected custom schedule, missing-runtime warning, cancellation, and shell handoff. In an owned disposable home, use the official installed SDK, valid private native agent directory, and available real user-systemd manager to activate/inspect the generated timer and a no-work service; disable, stop any exact owned service, remove only owned units, reload, and verify delayed process/unit/home absence. No paid call or live distro. Report any unavailable acceptance row rather than faking it.
- [ ] Run full Python suite with official SDK on PATH; exclude only the three previously documented missing-tmux tests by exact name. Run existing native reviewer Bun checks if runtime/service changes affect their assumptions. Commit `feat: offer scheduled reviewer setup during installation`; task-done records the final integrated run. Request one whole-branch read-only review; one RED→GREEN fix pass for important findings. No push/merge without separate authorization.

## Self-review

The design's two shared interfaces have producers in Task 1 and consumers in Task 2. Each Review Focus failure mode has owning regression/smoke proof. Runtime and schedule authority remain in their existing modules; no dependency installer, model configuration, or scheduler syntax is introduced. The current systemd user bus and native calendar parser are available, but actual unit lifecycle acceptance will be observed only in the scoped smoke. The existing importable SDK path is verification infrastructure, not a production fallback.
