# Optional Langfuse CLI Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offer a home-owned official Langfuse CLI during fresh or existing-distro installation and make local-project querying available to managed reviewer runs without enabling telemetry or touching global software.

**Architecture:** Add one focused private package-runtime module, following `relay.ensure_runtime`'s staged npm/provenance pattern rather than extending the account-global mandatory dependency receipt. Reuse `observability.reviewer_environment` for optional local credentials/PATH and preserve native-only review when query access is unavailable. This plan ships independently of the normal-CLI reviewer cutover.

**Tech Stack:** Existing Python 3.12+, Bash installer, installed Node.js >=20 and npm for optional managed package installation, official `@langfuse/cli@1.2.4`; no new Python dependency, MCP server, SDK client, or system runtime installation.

**Spec:** `docs/superpowers/specs/2026-11-02-investigative-reviewer-design.md`

## Global Constraints

- New managed package: `@langfuse/cli@1.2.4`, archive `https://registry.npmjs.org/@langfuse/cli/-/cli-1.2.4.tgz`, SHA-256 `0fc4e39b38bfa45d77f045a2d3a79e165411e4de0737eb7893bec57b246e574f`.
- Require existing Node.js >=20 and npm for new managed installation; never install a system runtime, use sudo, mutate a global npm prefix, or fetch tools during a review.
- Independent default-No consent; blank, No, EOF cause no runtime/package/network/Docker probes and no installation writes. Optional failure never deletes an otherwise usable distro.
- Home paths: `data/langfuse-cli/runtime`, `data/langfuse-cli/bin/langfuse`, `data/langfuse-cli/installation.json`; shared dependencies remain untouched.
- Explicit local LANGFUSE_BASE_URL and LANGFUSE_HOST, project-scoped keys, and API-version auto; never fall back to ambient credentials or Cloud.
- Preserve normal worker/central/interactive-shell environments, mandatory dependency receipts, telemetry and schedule choices.
- No implementation or live rollout is authorized by this planning artifact.

## Review Focus

- Interrupted/foreign/symlinked package staging must be preserved/refused, not overwritten: Task 1 tests atomic promotion and exact ownership.
- Declining the new prompt must not consume subsequent installer input or change telemetry/scheduling: Task 1 PTY scenarios.
- A disabled or broken local stack plus ambient cloud keys must never cause a cloud query: Task 2 environment scenarios.
- Optional CLI receipt damage must degrade reviewer query coverage, not prevent native-only review: Task 2 invalid-installation scenario.
- Uninstall must remove only home-owned package/launcher/state and retain externally reused CLI and Node/npm: Task 2 uninstall scenario.

---

## File map

Create `bin/agd_lib/langfuse_cli.py` for package/runtime ownership, validation, home launcher and optional reviewer query environment. Modify `bin/agd_lib/installer.py`, `bin/agd`, and `install.sh` for consent/dispatch; `observability.py` for the existing reviewer environment integration; `uninstall.py` for recovery-source staging and ownership inventory. Extend existing installer/install/observability/uninstall tests; create one focused `tests/test_langfuse_cli.py` for package and environment behavior. Update README, operations, compatibility and changelog within the owning task after actual proof. Do not create a generic package manager or change Relay semantics.

### Task 1: Optional package lifecycle and complete installer entrypoints

**Files:** Create `bin/agd_lib/langfuse_cli.py`, `tests/test_langfuse_cli.py`; modify `bin/agd_lib/installer.py`, `bin/agd`, `install.sh`, `tests/test_installer.py`, `tests/test_install.py`, existing installer input driver in `tests/test_backends.py`; docs named above.

**Interfaces:**
- `langfuse_cli.read_installation(home: Path) -> dict | None`: strict regular-file receipt read; absent is None, unsafe/malformed is Refusal.
- `langfuse_cli.ensure_installed(home: Path, *, run=store.run, which=shutil.which, fetch_bytes) -> dict`: explicit-consent caller only; validate existing official package or install pinned archive privately; returns sanitized source/path/version status, not credentials.
- `langfuse_cli.validate_installation(home: Path, *, run=store.run) -> dict`: validate recorded real package, executable, Node identity/version, launcher and provenance; never install/upgrade.
- `installer.setup_langfuse(home: Path, input_fn=input, output=print, *, run=store.run, which=shutil.which, fetch_bytes) -> dict`: prompt before probing; return declined/installed/incomplete status and rerun instructions.
- Public `agd --home . installer langfuse`; no arguments that implicitly select a cloud project or alter telemetry.

- [ ] **Step 1: Add behavior regressions.** Use current installer fixture conventions. Pin tests `test_langfuse_opt_out_has_no_probes_or_writes`, `test_langfuse_reuses_external_official_cli_without_ownership`, `test_langfuse_verified_private_install_is_atomic`, `test_langfuse_bad_digest_or_probe_preserves_previous_install`, `test_langfuse_foreign_or_partial_runtime_is_retained`, `test_langfuse_setup_does_not_install_node_or_npm`, `test_mandatory_setup_preserves_optional_cli`. Assertions concern destination bytes, ownership/provenance, network/process admission, and resulting usable state—not command forwarding echoes.
- [ ] **Step 2: Run those failing tests.** `python3 -m pytest -q tests/test_langfuse_cli.py tests/test_installer.py -k langfuse`; expect failures for missing new behavior, not host prerequisites.
- [ ] **Step 3: Implement package validation/staging.** Before executing an external candidate or giving it keys, require canonical `@langfuse/cli` exactly1.2.4 and compare every runtime/executable package asset against the verified pinned archive. Record/revalidate that file-hash map; a version string alone is insufficient. Keep different/legacy/modified/opaque installations untouched and install a separate private pinned copy after consent. For managed installation, verify the archive before `npm install --global --prefix STAGE --ignore-scripts --no-audit --no-fund LOCAL_ARCHIVE`; validate metadata, package assets, runtime/provenance and executable before atomic promotion. Then prove offline `--version`/`api schema --json`. Use home-scoped exclusive installation locking and existing regular-path/atomic JSON patterns. Store receipt schema1 with source, version, canonical entry/package/Node paths and hashes; managed entries additionally contain pinned archive and owned runtime identity. Add regressions `test_modified_external_cli_never_receives_keys` and `test_other_version_is_retained_not_reused`. Never extend the account-global mandatory dependency receipt.
- [ ] **Step 4: Implement the scoped launcher and consent entrypoint.** Create an owned regular executable `data/langfuse-cli/bin/langfuse` that execs the validated absolute Node path and original package entry; this works for managed and externally reused packages without owning the latter. Quote paths safely. Disclose Node >=20/npm and credential-capable querying before the default-No question. Decline before probing. Preserve staged evidence and previous valid installation on any failure; output sanitized retry information, not an exception/env dump. Add the new Python module to uninstall recovery source staging when its functions become reachable there.
- [ ] **Step 5: Wire fresh and existing installation.** Add the argparse/owner-admission/dispatch path. Make `install.sh` six stages, with independent CLI consent between observability and scheduled reviewer. Keep `owned=false` before optional stages. Add PTY scenarios `test_langfuse_optional_answers_do_not_change_telemetry_or_reviewer`, `test_langfuse_failure_retains_home_and_shell_handoff`, `test_langfuse_eof_does_not_invoke_package_tools`, and preserve trailing shell input. Remove obsolete exact-stage/wording assertions instead of re-pinning incidental text.
- [ ] **Step 6: Run affected checks and real CLI smoke.** `python3 -m pytest -q tests/test_langfuse_cli.py tests/test_installer.py tests/test_install.py`; then use `agd --home ABSOLUTE_THROWAWAY_HOME installer langfuse` with actual explicit consent and existing Node/npm, run the resulting `langfuse --version` and `api schema --json` without credentials/network API calls, verify global installations unchanged and default-No produces no package tree. Exercise the PTY installation surface, not only Python functions. Record exact package/version/digest and any unavailable host prerequisite; no claim of a real server query yet.
- [ ] **Step 7: Update user docs and commit.** Document consent, six stages, Node prerequisite, external vs home ownership, retry command, optional failure, and no implicit auth/telemetry/schedule setup. Commit this complete installer deliverable with its tests/docs; do not publish or merge without later approval.

### Task 2: Optional local-project query environment and ownership-safe removal

**Files:** Modify `bin/agd_lib/langfuse_cli.py`, `bin/agd_lib/observability.py`, `bin/agd_lib/uninstall.py`, `tests/test_langfuse_cli.py`, `tests/test_observability.py`, `tests/test_uninstall.py`, `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`.

**Interfaces:**
- Consumes Task 1 `validate_installation` and installation receipt.
- `langfuse_cli.reviewer_environment(home: Path, inherited: Mapping[str, str]) -> tuple[dict[str, str], dict]`: remove ambient LANGFUSE_*; return an environment plus sanitized `{available: bool, reason: str | None}` query status. Add the scoped launcher PATH only when valid. Use a narrow observability helper for validated local project credentials/URL; never duplicate secret parsing.
- `observability.langfuse_query_context(home: Path) -> dict | None`: private internal result containing validated local base URL/project/key pair only for an owned configured stack; never print/persist its result. Not an arbitrary endpoint client and no Docker lifecycle call.
- Existing `observability.reviewer_environment` keeps its public tuple shape and adds this environment internally; attach sanitized query availability to reviewer request/coverage through a separate status read where needed. Do not add a new mandatory cross-plan import to reviewer_runner.

- [ ] **Step 1: Add privacy and availability regressions.** `test_reviewer_langfuse_env_uses_only_owned_local_project`, `test_ambient_cloud_keys_removed_when_query_unavailable`, `test_optional_cli_damage_does_not_block_native_review_environment`, `test_local_query_env_does_not_start_docker`, `test_langfuse_keys_absent_from_status_receipts_and_errors`, `test_uninstall_retains_external_cli_and_node`. Check both host aliases point to the validated loopback web port and LANGFUSE_API_VERSION=auto; disabled/unconfigured local export supplies no keys/host fallback. Ordinary worker/central/provider/Git environments remain unchanged.
- [ ] **Step 2: Verify failures.** `python3 -m pytest -q tests/test_langfuse_cli.py tests/test_observability.py tests/test_uninstall.py -k langfuse`.
- [ ] **Step 3: Implement environment integration and graceful limits.** Reuse observability `_read_secrets`, config/project/home identity and private path validation. Never put credentials in argv, prompts, receipts, schema probes, or diagnostic output. Strip inherited LANGFUSE_* first. No CLI, disabled/unconfigured export, invalid optional receipt, stopped/unreachable stack, or API mismatch remains an explicit optional limitation; do not start services or download replacements. Supply no third-party MCP integration or homemade API client. Do not claim project keys are read-only.
- [ ] **Step 4: Integrate inventory/uninstall.** Inventory the home-owned package, staging, launcher and provenance as private files, with no extra background process. Add the Python module to the explicit recovery executor source list. External package/entry and shared Node/npm remain retained. Home deletion removes managed files; do not add global npm uninstall commands. Refuse symlink/foreign path substitution.
- [ ] **Step 5: Run actual local query proof and affected suites.** With a separately owned disposable observability home running the repository's pinned Langfuse 4.70.1 stack, invoke the actual official CLI with the generated environment, `--api-version auto api traces list --limit 1 --json`. Verify loopback destination, correct project, valid response and no credential output; stop/disable export and prove native environment construction still works without a cloud fallback. Do not use jarvis-14 or inspect its secret contents without separate operational approval. Run `python3 -m pytest -q tests/test_langfuse_cli.py tests/test_observability.py tests/test_uninstall.py tests/test_install.py tests/test_omp_runtime.py`. A fake HTTP server is useful for negative routing tests, not proof of pinned Langfuse compatibility.
- [ ] **Step 6: Document and commit.** Explain that CLI installation, telemetry ingestion, and reviewer schedule are three independent opt-ins; no data-query success is implied by stack health. Record exact exercised server/CLI versions and limitations. Commit the independently usable package/query feature. No reviewer SDK migration is necessary for this plan to ship.

## Handoff and implementation checks

Before implementation, read both the shared spec and this plan, use a new native leased task/worktree, and confirm planning approval. Retain the observed package metadata/checksum references in the spec. Do not run a product build/test suite during planning or claim these future checks have passed.

After implementation, use verification-before-completion and an independent whole-change review. Follow the companion runtime plan only after approval; absence of that plan's code must not make this installer feature unusable.
