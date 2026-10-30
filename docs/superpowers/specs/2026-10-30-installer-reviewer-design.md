# Scheduled reviewer installer opt-in

Status: approach selected; design pending approval. Base: merged main `2128811` (includes installer observability PR #18). Worktree branch: `agd/installer-reviewer`.

## Intent

Offer automatic skill review during installation, with useful presets and supported customization. Keep model-cost consent explicit, keep scheduling independent of Langfuse, and never label an unusable timer ready. Do not modify any live distro.

## Approaches

- **Selected by the user: reuse installed runtime.** Check the existing native reviewer prerequisites. If missing, retain the selected schedule, explain the missing prerequisite, and do not activate a timer. This matches telemetry's no-Docker-install policy.
- **Not selected: provision a private reviewer SDK/runtime.** Would improve fresh-host readiness but adds dependency download, installation, version, and ownership handling. No such installation is included here.

Implementation choice: a fifth shell-installer stage delegates the interactive schedule flow to `bin/agd installer reviewer`, implemented with the existing Python installer/reviewer modules. Reuse `reviewer.validate_config`, atomic private config persistence, and `reviewer_timer.enable`; do not write JSON with shell interpolation or add a second scheduler. Keep runtime readiness and executable binding in the shared timer activation path so manual enable and installer enable cannot disagree.

## Operator flow

1. Renumber installer stages to five. After observability, show `5 / 5  Configure scheduled reviewer`.
2. Explain before consent: reviews completed worker evidence, may send that evidence to the configured model provider and incur model charges, writes reports/proposals only, never automatically edits skills, and is independent of Langfuse. Existing bounded reviewer limits remain unchanged; they are not a dollar-cost guarantee.
3. Ask `Enable scheduled skill reviews? [y/N]`. Blank, no, and EOF do not inspect model/runtime prerequisites, write reviewer configuration, create units, or activate a timer.
4. After consent, offer this menu:
   - `1` Daily at `02:00 UTC` (default selection).
   - `2` Every 6 hours.
   - `3` Every hour.
   - `4` Custom daily time and timezone.
   - `5` Custom interval in whole hours.
   - `0` Cancel.
5. Custom daily asks `HH:MM` and an IANA timezone, default `UTC`. Custom interval asks for a positive whole number of hours. Invalid entries explain the error and allow correction or cancellation; EOF cancels without writing configuration. No arbitrary cron/systemd expression or weekly scheduler is added: these choices use the existing daily-calendar/whole-hour engine.
6. Show the accepted schedule explicitly. Preserve existing `limits` and `review_model`; validate the complete config before saving it privately and atomically under the existing home lock.
7. Check readiness without a model call: compatible importable OMP SDK, Bun, the existing distro agent directory, and native user-systemd scheduling support. Provider login and model selection remain OMP's responsibility; do not prompt for, copy, change, or print credentials. Do not claim authentication or a successful model review from import/activation checks.
8. If prerequisites are missing, retain valid chosen config, show `configured, not enabled` and the specific prerequisite, then show the later `bin/agd reviewer timer enable` command. Do not silently install runtimes or point production at temporary verification packages.
9. If ready, activate the existing UUID/UID-owned user timer, show its schedule and status, and show `reviewer timer status|disable` recovery commands. Confirm actual native timer activation before reporting enabled. Do not launch a review as part of installation.

## Shared timer/runtime contract

- Validate and retain the actual Python, Bun, and importable OMP runtime paths needed by the service. The native unit must run with those validated executables available, not rely on the installer's interactive PATH or a login shell.
- Preserve distro-private OMP settings/auth lookup, reviewer plugin isolation, native session recording, and observability behavior. Copy no credentials or arbitrary inherited environment into systemd units.
- Existing timer ownership/collision checks, calendar-vs-IANA/DST checks, persistent catch-up, interval anchor/checkpoint behavior, and immutable review intent semantics remain authoritative.
- Interval presets mean the existing UTC-hour-anchored interval behavior, not an elapsed-time timer measured from the installer's current minute. Daily presets explicitly show UTC rather than silently using local time.
- A configured schedule is not enabled status, an enabled timer is not model readiness, and neither proves a review or Langfuse delivery occurred.

## Failure and recovery

This stage runs after the existing mandatory-install rollback boundary. Cancellation, missing prerequisites, validation errors, activation failure, or interruption must not delete the distro, telemetry records, reviewer configuration, or unit ownership evidence. Ordinary optional-stage failure warns and continues to the distro shell. Do not overwrite foreign units, enable lingering, use sudo, install cron jobs, modify other homes, or automatically disable unrelated services.

Persist selected schedule only after a complete valid choice. On repeat invocation, do not silently rewrite the schedule/config underneath a running reviewer or an active/different timer: require an explicit safe disable/reconfiguration boundary and preserve non-schedule settings. Activation errors must retain truthful failed/partial state and usable disable/status recovery, not fabricate success.

## Verification and scope

Behavior regressions cover decline/cancel/EOF, each preset, both custom modes, invalid time/timezone/hours, correction, preserved model/limits, missing runtime/user bus, partial unit activation, safe reconfiguration, and shell-input handoff including spaced paths. Shared tests cover executable paths with spaces, timer ownership, checkpoint idempotency, and runtime availability under a minimal service environment.

Exercise the actual terminal surface. In an explicitly owned disposable home, use the available user-systemd manager to enable, inspect, trigger a no-work service, disable, remove only its owned units, and prove delayed absence before deleting the home. No paid model request, existing live timer, or live distro is used. Scripted-provider reviewer tests establish model-host behavior separately; scheduling proof is not an obedience/authentication claim. Run the existing full suite with the installed official SDK on PATH and report known unavailable integrations explicitly.

No new package installation, general scheduling framework, new schedule syntax, OMP configuration wizard, automatic skill changes, or automatic publishing/merging. Implementation uses Native execution with one final independent review unless the user chooses otherwise.
