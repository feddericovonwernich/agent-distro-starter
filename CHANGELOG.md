# Changelog

## Unreleased
- Preserve Herdr's native pane marker when starting the central agent so it can launch Herdr workers; continue filtering caller-supplied markers and sanitizing the outer attach client.
- Export the central owner token during OMP session start and switch so agent commands can mutate their own home while foreign commands remain read-only.
- Restored Herdr central startup and worker controls with Herdr 0.9.5 status fields and transient pane-process reporting during initial ownership proof.
- Documented guided Linux x86-64 installation, verified dependency receipts, explicit OMP authentication boundary, backend-aware central start, pane-bound Herdr workers, and retention-first refusal behavior.
- Verified the bounded reviewer against real OMP 18.3.2 public modules: provider-free scripted AgentSession persistence, restricted tools, disabled telemetry, and enabled loopback-only OTLP purpose/run metadata; approved disposable user-systemd lifecycle evidence is recorded.
- Hardened reviewer deadline enforcement across collection, capability discovery, and child dispatch; reviewer immutable records now refuse unsafe symlink and non-regular paths.

- Added reviewer CLI inspection, human triage, manual native-run dispatch, and durable no-work receipts. Timer activation remains an explicit command and is never automatic.
- Added the inert reviewer scheduling kernel and owned per-user systemd unit rendering. Calendar schedules use systemd's native IANA/DST semantics, interval schedules use a fixed UTC-hour anchor, and no configuration or initialization action activates a timer.
- Added immutable reviewer reports and proposal revisions, audited human-only triage, idempotent final batch events, and final-packet-only candidate consumption. New-skill drafts remain stored inertly outside discovery roots.
- Hardened reviewer native JSONL collection to preserve bounded physical offsets and hashes across malformed, non-object, late, and skipped rows; finalized retrospective packets include committed reports.
- Made reviewer report submission crash-recoverable through retained request-keyed prepared records with exact output reservations; identical finalized-request retries return their committed receipt.
- Require an explicitly observed OMP runtime version before a reviewer receipt can begin immutable report production; catalog provenance now hashes raw skill bytes and flags bounded invalid/truncated excerpts.
- Added private, immutable reviewer evidence packets selected from closed worker incarnations, with bounded native JSONL excerpts, frozen config/instructions, provenance and explicit unknown-coverage records. Reviewer model hosting, reports, timers, and CLI commands remain unavailable.
- Added a private project registry, explicit trust/deletion approval, pinned Treehouse task leases, readiness, exact-head landing, publication evidence and guarded cleanup.
- Added private worker skill catalogs, exact profile/curated selection, frozen task snapshots, OMP native materialization, collision/exclusion guards, and lifecycle integrity checks. Cursor nonempty provisioning remains fail-closed pending authenticated live verification.
- Seed a new empty private home with the reviewed default worker-skill profile without changing existing homes.
- Added isolated tmux endpoints, live OMP workers with durable steering/acknowledgement, native turn observations, safe stop and guarded relaunch.
- Added durable task decisions/events, single-owner background monitoring, central OMP idle wake and a one-restart watcher guard; persisted runner/process receipts support conservative recovery.
- Added a one-command installer for named local clones with independent private homes and an unchanged template origin. The user-approved disposable Herdr ship was landed locally; its duplicate tmux ship was discarded. Cursor Agent login and live validation remain external prerequisites.
- Replaced repeated visible OMP supervision cards with hidden agent context and compact footer status; actionable task wakes and unrecoverable watcher failures remain visible.
- Added a distro customization reference skill covering instruction/configuration/code surfaces, central-versus-worker scope, private state, safe activation and upstream update implications.
- Added a scoped `pr-publish` repair workflow: a task-target grant selectively enables one AGD worker bundle, guarded non-force publication, central Relay CI/human-review feedback, and final GitHub verification before readiness. Deployment requires central read-only GitHub credentials; no live GitHub acceptance is claimed.
- Added private observability state records, per-incarnation OMP session bindings, and disabled child-environment sanitization. Managed telemetry commands and services are not enabled by this boundary.
- Added private, immutable-pinned managed observability lifecycle commands with loopback Langfuse/Collector endpoints and preserved stop/disable records.
- Added managed OMP central and worker launch bindings: every OMP worker incarnation has a retained private native session directory, managed launchers sanitize ambient OTEL configuration, and the central launcher refuses conflicting native session controls while failing open on degraded telemetry. Bare OMP and Cursor remain unmanaged.
- Added per-process managed-observability generation receipts, explicit stale-restart status, retained exited receipts, and local Collector diagnostics that keep current queue fullness, historical admission failures, and sanitized disk/file-storage failures distinct from unknown delivery state.
- Documented managed-observability operations, prerequisites, loopback ownership, native-session authority, sensitive full-content handling, queue/delivery limits, degradation and Cursor exclusion; recorded disposable two-home native OMP-to-Collector-to-Langfuse acceptance.
- Hardened managed-observability config provenance, read-only status inspection, native binding visibility, and managed resource identity.
- Verified guided local installation and public central start through isolated tmux and Herdr PTYs, including canonical clone boundary, retained reattachment, conflict refusal, and owned-runtime cleanup.
