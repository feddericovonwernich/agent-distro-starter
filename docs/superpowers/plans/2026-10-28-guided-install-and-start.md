# Guided Install and Start Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install verified runtime dependencies, initialize a selected launcher backend, and safely attach an ordinary terminal to one proven OMP central.

**Architecture:** `install.sh` bootstraps a clone then delegates dependency work to narrow Python installer functions. A durable receipt separates artifact and installed hashes. `agd start` reserves a receipt under short locks, creates/inspects one backend endpoint without locks, correlates the extension owner/watcher to the post-exec OMP argv, and execs a backend attach command only after proof.

**Tech Stack:** Bash, Python 3.12 stdlib, pytest, Bun, tmux, Herdr, OMP.

**Spec:** `docs/superpowers/specs/2026-10-28-guided-install-and-start-design.md`

## Global Constraints

- Linux x86-64; default No never elevates. Dynamic OMP/Herdr release installs require explicit consent; decline reuses a compatible install or refuses. User-local replacements stream to same-directory private temps, fsync, chmod, and replace atomically.
- `dependencies.json` keeps `artifact_sha256` (download) distinct from `installed_sha256` (executable); every entry includes `source`. Existing records contain only `source`, `path`, `version`, `capabilities`; managed records additionally contain `asset`, `tag`, both hashes.
- Never retain an HTTP asset or archive in memory. No generic download/backend abstraction and no production cleanup path. Fixtures raw-clean only uniquely named resources after assertions.
- `managed_command` is the pre-exec `bin/agd ... observability central`; `(expected_process_argv, central_env, warning) = observability.central_argv(home, ())`; receipt stores only argv and endpoint launch merges central env with launch ID/token.

---

## Task 1: Fresh-home launcher configuration

**Files:** Create `bin/agd_lib/launcher.py`, `tests/test_launcher.py`; Modify `bin/agd`; Test `tests/test_projects.py`.

**Interfaces:** `initialize(home: Path, backend: str) -> None`; `launcher.json = {"schema":1,"backend":backend,"herdr_session":"agd-<uuidhex>"}`.

- [ ] **Step 1: Write failing test**
```python
def test_init_writes_safe_selected_config(tmp_path):
    home = tmp_path / "h"; command(home, "init", "--backend", "herdr")
    got = json.loads((home / "data/launcher.json").read_text())
    assert got["schema"] == 1 and got["backend"] == "herdr"
    assert re.fullmatch(r"agd-[0-9a-f]{32}", got["herdr_session"])
```
Add tmux, malformed config, symlink, and nonfresh-home refusal cases.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_projects.py tests/test_launcher.py -q`; expected: missing init backend/config behavior.
- [ ] **Step 3: Implement minimal behavior**
```python
def initialize(home: Path, backend: str) -> None:
    if backend not in {"tmux", "herdr"} or home.is_symlink() or (home / "data/launcher.json").exists():
        raise Refusal("fresh non-symlink launcher home required")
    atomic_json(home / "data/launcher.json", {"schema": 1, "backend": backend,
        "herdr_session": f"agd-{uuid.uuid4().hex}"})
```
Parse `init --backend` only; no worker parser/default change.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_projects.py tests/test_launcher.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd bin/agd_lib/launcher.py tests/test_projects.py tests/test_launcher.py && git commit -m 'feat: save launcher backend configuration'`

## Task 2: Dynamic receipt and OMP/Herdr installs

**Files:** Create `bin/agd_lib/installer.py`, `tests/test_installer.py`; Test `tests/test_installer.py`.

**Interfaces:** `read_dependencies(home) -> dict`, `write_dependencies(home, receipt) -> None`, `validate_dependencies(home, run) -> dict`, `probe_omp(path, run) -> dict`, `probe_herdr(path, session, run) -> dict`, `setup(home, backend, prompt, fetch_json, stream_url, run) -> dict`, `install_omp(home, fetch_json, stream_url, run) -> dict`, `install_herdr(...) -> dict`.

- [ ] **Step 1: Write failing test**
```python
def stream_url(url): yield b"x"
def test_omp_rejects_missing_api_digest(tmp_path):
    release={"draft":False,"prerelease":False,"assets":[{"name":"omp-linux-x64"}]}
    with pytest.raises(Refusal, match="digest"): install_omp(tmp_path, lambda _: release, stream_url, fake_run)
```
Add `herdr-linux-x86_64`, draft/prerelease, malformed digest, existing-hash mismatch, and probe-output cases.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_installer.py -q`; expected: import/function failures.
- [ ] **Step 3: Implement minimal behavior**
```python
def _install(asset, target, stream_url):
    digest = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest): raise Refusal("release digest")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as f:
        h = hashlib.sha256()
        for chunk in stream_url(asset["browser_download_url"]): f.write(chunk); h.update(chunk)
        f.flush(); os.fsync(f.fileno()); name = f.name
    if h.hexdigest()!=digest[7:]: os.unlink(name); raise Refusal("artifact digest")
    os.chmod(name,0o755); os.replace(name,target); return _sha256(target)
```
Require latest object `draft is False` and `prerelease is False`; select only exact named asset. `probe_omp` executes `[path,"--version"]`, `[path,"models","--json"]` (JSON list/object collection), `[path,"login","--help"]` exit zero. `probe_herdr` executes version, `session list --json`, and `--session NAME status server`, accepting only documented running/not-running output. Probe the chmodded temporary executable before `os.replace`; on failure unlink it and preserve old bytes. Existing entries re-probe; managed entries rehash installed bytes then re-probe. `setup` probes PATH, fetches/shows latest before consent, reuses compatible bytes unchanged on decline, raises unmet requirement otherwise, and atomically writes only accepted installs.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_installer.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd_lib/installer.py tests/test_installer.py && git commit -m 'feat: verify dynamic runtime dependencies'`

## Task 3: Pinned streamed Treehouse archive

**Files:** Modify `bin/agd_lib/installer.py`; Test `tests/test_installer.py`.

**Interfaces:** `install_treehouse(home, stream_url) -> dict`; `_extract_treehouse(archive_path: Path, expected_digest: str, destination: Path) -> None`.

- [ ] **Step 1: Write failing test**
```python
def make_archive(path, members):
    with tarfile.open(path,"w:gz") as t:
        for name, body in members: info=tarfile.TarInfo(name); info.size=len(body); t.addfile(info, io.BytesIO(body))
def test_extract_accepts_only_one_treehouse(tmp_path):
    archive=tmp_path/"a.tgz"; make_archive(archive, [("treehouse",b"x")])
    _extract_treehouse(archive, sha256_file(archive), tmp_path/"treehouse")
```
Add two-member, link, absolute/component, digest mismatch, and public fixed URL/digest assertions.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_installer.py -q`; expected: missing helper.
- [ ] **Step 3: Implement minimal behavior** Stream public URL to private archive path while hashing fixed `e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1`; helper rehashes, requires exactly one regular non-link normalized `treehouse`, streams `extractfile` to private executable temp, fsyncs/chmods 0755, probes its exact `--version` before replacement (unlink on failure, preserve old binary), then replaces and records fixed artifact plus executable installed hash.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_installer.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd_lib/installer.py tests/test_installer.py && git commit -m 'feat: install pinned treehouse archive'`

## Task 4: Guided installation

**Files:** Modify `install.sh`, `bin/agd`, `bin/agd_lib/installer.py`; Test `tests/test_install.py`.

**Interfaces:** `bin/agd --home HOME installer setup --backend BACKEND`; exact post-clone order `init --backend BACKEND`, then internal setup.

- [ ] **Step 1: Write failing test** Define PATH fake `sudo`, `apt`, `git`, Python, shell in `tests/test_install.py`; PTY assertions cover both backends, argv order, receipt, blank sudo no elevation, only missing APT tools, owned clone-trap/disarm before shell exec, incompatible present refusal, and Python 3.11 exact 3.12 error. Assert optional `omp login [provider]` and models view neither export secrets nor infer a provider.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_install.py -q`; expected: missing ordered flow.
- [ ] **Step 3: Implement minimal behavior** Bash performs host/TTY/Python bootstrap before Python code, requests update/install consent, clones under an owned trap, runs `bin/agd --home "$clone" init --backend "$backend"` then installer setup, offers login/models as child commands only, clears trap, and `exec`s `${SHELL:-/bin/sh} -i` in clone with local PATH.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_install.py -q && bash -n install.sh`; expected: PASS.
- [ ] **Step 5: Commit** `git add install.sh bin/agd bin/agd_lib/installer.py tests/test_install.py && git commit -m 'feat: guide verified installation'`

## Task 5: Common launcher state and exec

**Files:** Modify `bin/agd`, `bin/agd_lib/launcher.py`, `.omp/extensions/agd-primary.ts`, `tests/primary.test.ts`, `tests/test_launcher.py`; Test those two files.

**Interfaces:** `active_backend(home: Path, inherited_env: Mapping[str,str]|None=None) -> str|None`; `prepare_start(home, explicit_backend, inherited_env, create, inspect, attach) -> AttachTarget`; `AttachTarget(attach_argv, env)`. Receipt has schema, launch_id, `endpoint_token`, backend, phase, endpoint, `managed_command`, `expected_process_argv`, owner, watcher.

- [ ] **Step 1: Write failing test** Define `fake_create`, `fake_inspect`, `fake_attach` in `tests/test_launcher.py`; assert inherited environment must match proven receipt, reserved/spawned/changed snapshot refuses, and ready requires tokened foreground `{pid,start,argv}` equal owner and complete expected argv element-for-element, including absolute OMP executable, `--cwd`, absolute home, and `--session-dir`; add mismatched-argv cases.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_launcher.py -q && bun test tests/primary.test.ts`; expected: missing correlation.
- [ ] **Step 3: Implement minimal behavior** Call `argv, central_env, warning = observability.central_argv(home, ())`; receipt stores the complete returned argv (already absolute PATH-resolved OMP, cwd/home/session-dir arguments) and launch env `{**central_env, "AGD_LAUNCH_ID": launch_id, "AGD_ENDPOINT_TOKEN": endpoint_token}` does not alter it. `_advance(home, expected, replacement)` locks, requires strict parsed receipt equality with exact prior record and nonforeign owner snapshot, then atomically writes replacement. Reserve locked; create unlocked; advance spawned; launch/poll unlocked; final advance requires exact spawned bytes and observed owner/watcher. `active_backend` invokes selected backend inspection from the ready receipt and returns only after continuity proof; inherited home/backend/launch-id/token-hash mismatch refuses. No lock spans external work or exec.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_launcher.py -q && bun test tests/primary.test.ts`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd bin/agd_lib/launcher.py .omp/extensions/agd-primary.ts tests/test_launcher.py tests/primary.test.ts && git commit -m 'feat: prepare safe central attachment'`

## Task 6: tmux central

**Files:** Modify `bin/agd_lib/tmux.py`, `bin/agd_lib/launcher.py`; Create `tests/test_central_backends.py`; Test it.

- [ ] **Step 1: Write failing test** Fake `store.run` and process inspection; assert use of existing `tmux.prefix(home)`, a dedicated central window, recorded server PID/window ID/pane ID/token, selection and exact attach `[*tmux.prefix(home),"attach-session","-t","agd"]`; compare stable retained IDs/receipt/task bytes.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_central_backends.py -q`; expected: no central adapter.
- [ ] **Step 3: Implement minimal behavior** Add `create_central(home, launch_id, endpoint_token, managed_argv, launch_env, expected_omp_argv)`, `inspect_central(home, receipt)`, `attach_argv(home, receipt)`; record/verify window/pane, immediately before returning exec target run `[*tmux.prefix(home),"select-window","-t",window_id]`, `[*tmux.prefix(home),"select-pane","-t",pane_id]`, returning `[*tmux.prefix(home),"attach-session","-t","agd"]`.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_central_backends.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd_lib/tmux.py bin/agd_lib/launcher.py tests/test_central_backends.py && git commit -m 'feat: attach tmux central window'`

## Task 7: Herdr central

**Files:** Modify `bin/agd_lib/launcher.py`; Modify `tests/test_central_backends.py`; Test it.

- [ ] **Step 1: Write failing test** Fake exact `herdr.py` response forms and socket stat. Assert named status refusal for unreceipted server; workspace get/create, `tab create --no-focus`, pane run, ordered `["herdr","--session",name,"workspace","focus",workspace]`, `["herdr","--session",name,"tab","focus",tab]`, and `["herdr","session","attach",name]`; assert attach sees recorded current focus and external env removes stale `HERDR_ENV` while setting `AGD_HERDR_SESSION`.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_central_backends.py -q`; expected: unsupported named central operations.
- [ ] **Step 3: Implement minimal behavior** After reserved receipt, `herdr session list --json` must prove random saved name absent; then launch `["herdr","--session",name,"server"]` detached with `start_new_session=True` and no inherited pipe, poll named status/socket, and record continuity. A pre-existing same-name server without receipt refuses; never stop it. Only after socket identity exists may central-only `_herdr_call(endpoint, *args)` validate session/socket and use existing `response()` JSON shape, never worker adapter. Central pane env has `AGD_HERDR_SESSION`, `AGD_LAUNCH_ID`, endpoint token, PATH; Herdr supplies `HERDR_ENV=1`. External attach env removes stale `HERDR_ENV`, sets session; after socket/workspace/tab/pane/terminal/process proof call workspace focus then tab focus, verify success, re-inspect pane continuity, then exec `herdr session attach NAME`. Per the [authoritative CLI](https://herdr.dev/docs/cli-reference/), this attaches the named server at saved current focus; no pane-target attach exists.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_central_backends.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd_lib/launcher.py tests/test_central_backends.py && git commit -m 'feat: attach named herdr central session'`

## Task 8: Worker omission inheritance

**Files:** Modify `bin/agd`, `bin/agd_lib/workers.py`; Modify `tests/test_workers.py`, `tests/test_projects.py`; Test both.

- [ ] **Step 1: Write failing test** Through existing CLI/tests patch `os.environ` with matching/mismatching AGD launcher fields; assert omitted parser value reaches `workers.launch(..., selected_backend=None)`, whose implementation calls `launcher.active_backend(home, dict(os.environ))` then saved fallback, while explicit tmux skips inheritance and task/lease/endpoint records remain unchanged. Delete implicit-tmux implementation tests.
- [ ] **Step 2: Verify RED** Run `uv run --python 3.12 --with pytest pytest tests/test_workers.py tests/test_projects.py -q`; expected: default tmux behavior.
- [ ] **Step 3: Implement minimal behavior** Set parser default `None`; before preflight/backend selection, if omitted call `launcher.active_backend(home, dict(os.environ))`, otherwise saved config; explicit backend skips inheritance and remains local.
- [ ] **Step 4: Verify GREEN** Run `uv run --python 3.12 --with pytest pytest tests/test_workers.py tests/test_projects.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `git add bin/agd bin/agd_lib/workers.py tests/test_workers.py tests/test_projects.py && git commit -m 'feat: inherit central backend for workers'`

## Task 9: Documentation cutover

**Files:** Modify `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`; Test existing contract tests.

- [ ] **Step 1: Capture verified baseline** Documentation wording tests are prohibited. Run implemented contracts and identify obsolete manual setup text.
- [ ] **Step 2: Verify baseline** Run `uv run --python 3.12 --with pytest pytest tests/test_install.py tests/test_installer.py tests/test_launcher.py tests/test_central_backends.py tests/test_workers.py tests/test_projects.py -q`; expected: PASS implementation baseline, documentation still stale.
- [ ] **Step 3: Implement minimal behavior** Replace contradictory manual path with installer, receipt/probes, auth/no-secret boundary, start precedence/refusal, central versus pane-worker Herdr, and retention; do not claim smoke evidence.
- [ ] **Step 4: Verify GREEN** Run the Step 2 command plus `git diff --check`; expected: PASS.
- [ ] **Step 5: Commit** `git add README.md docs/operations.md docs/compatibility.md CHANGELOG.md && git commit -m 'docs: document guided installation and start'`

## Task 10: Full acceptance and evidence

**Files:** Modify `tests/test_backends.py`, `docs/compatibility.md`, `CHANGELOG.md`; Test `tests/test_backends.py`.

- [ ] **Step 1: Write failing test** Extend `tests/test_backends.py` with `install-start-tmux` and `install-start-herdr`: run tracked local `install.sh` in a PTY through fake Git/network/release boundaries, clone the current test revision while asserting canonical URL argument, decline compatible updates/use real installed tools, exit nested shell, then run installed clone public `bin/agd start` in a PTY and prove owner/watcher, attach/detach/reattach, retained endpoint and conflict refusal. `tests/test_install.py` remains installer PTY proof. A live remote one-liner cannot test an unmerged revision; the local script plus canonical URL assertion is the pre-merge proof.
- [ ] **Step 2: Verify baseline** Run `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests -q && PATH=/tmp/agd-treehouse-v3.1.6:$PATH bun test tests/primary.test.ts && bash -n install.sh && python3 -m py_compile bin/agd bin/agd_lib/installer.py bin/agd_lib/launcher.py bin/agd_lib/tmux.py bin/agd_lib/workers.py && git diff --check && PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_backends.py install-start-tmux && PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_backends.py install-start-herdr`; expected: PASS.
- [ ] **Step 3: Record observed evidence** Append only observed results.
- [ ] **Step 4: Verify GREEN** Rerun the exact Step 2 command after docs change; expected: PASS.
- [ ] **Step 5: Commit** `git add tests/test_backends.py docs/compatibility.md CHANGELOG.md && git commit -m 'test: verify guided installation and start'`

## Plan self-review

The ten tasks have isolated RED/GREEN scopes, all changed test files in each verification command, exact receipt/argv boundaries, and no product cleanup.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-28-guided-install-and-start.md`. Two execution options:

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task and review between tasks.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, with checkpoints for review.

Which approach?
