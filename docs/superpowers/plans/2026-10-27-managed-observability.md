# Managed Distro Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retain authoritative native OMP sessions for central and worker execution, and provide opt-in distro-local Langfuse/OpenTelemetry observability without blocking agent work.

**Architecture:** Add one focused `agd_lib.observability` module for private records, complete sanitized OMP child environments, release-locked generated Compose/Collector files, and lifecycle control. Existing CLI, worker, harness, task and extension code consume it. OMP owns native JSONL; Collector receives/transforms/routes OTLP; Langfuse is optional trace storage only.

**Tech Stack:** Python stdlib; existing `bin/agd`, `agd_lib.store`, Python/OMP-extension tests; OMP native JSONL/OTLP; operator-provided Docker Compose; one release-verified Langfuse/Collector lock.

**Spec:** `docs/superpowers/specs/2026-10-27-managed-observability-design.md`

## Global Constraints

- Record `PLAN_BASE=$(git rev-parse HEAD)` from the approved plan commit, create an isolated implementation worktree, verify `a36b60e` is an ancestor, and merge `origin/main` normally before source edits. Preserve both approved documentation commits; never reset, rebase, checkout over, clean, force-push, or pull the stable checkout.
- Reuse `store.home`, `store.lock`, `store.atomic_json`, and `store.require_owner` without broadening/changing them. Do not hold the home flock while Docker/Compose, HTTP, network, process wait, or health polling runs. Mutators retain the existing live-central owner-token guard; bootstrap must not bypass another live owner.
- Private data remains under ignored `AGD_HOME`. `state/observability/collector/queue` and `state/observability/archives` are durable despite their path; stop, disable, relaunch, task cleanup and worktree cleanup never delete them.
- Never show raw Compose/HTTP stdout/stderr or `docker compose config` output; map errors to fixed stage/category/exit-code text. Credentials never enter argv, task records, agent/worker environments, prompts, status, or telemetry attributes.
- Disabled new processes get a complete child environment with all OTEL export/content controls removed and no Collector endpoint. Native session directories continue. Existing processes are stale until restart; failed stop/disable never claims runtime disabled.
- OMP sends traces/logs/metrics to Collector loopback host endpoints. Collector sends traces to Langfuse plus trace archive; OMP-emitted logs/metrics only to archives. No arbitrary stdout/stderr capture claim.
- Use only Collector `file_storage` for persistent Langfuse sending queue; no custom queue/replay/backend, Docker installer/elevation, retention deletion/truncation, or automatic upgrade.
- Select and commit one compatible release lock with immutable digests during implementation. Each distro `enable` validates and copies that exact bundled lock into private config; it never discovers a newer release.
- Receiver acknowledgement/in-memory buffering is not queued/delivered. Preserve IDs on retries; pre-queue loss, permanent reject, disk/queue exhaustion, possible duplicate delivery, attachment limits, and lack of fsync archive guarantee remain accepted limits.
- Docker/Compose absence is an explicit failed real-stack acceptance prerequisite. Real acceptance uses disposable homes only and no paid/model calls.

---

## Code map and interfaces

| Path | Change |
|---|---|
| `bin/agd` | Adds complete `observability enable|start|stop|status|disable|central` command group with current owner gating. |
| `bin/agd_lib/observability.py` | New private paths/config/status, binding, sanitized environment, release lock/render/control/lifecycle functions. |
| `bin/agd_lib/harnesses.py`, `bin/agd_lib/workers.py` | OMP-only session directory and fresh incarnation binding; sanitized `Popen` environment. |
| `bin/agd_lib/tasks.py` | Explicitly preserves observability paths during cleanup. |
| `.omp/extensions/agd-primary.ts` | Bare/unmanaged warning and stale/degraded managed status. |
| `bundled/observability/release-lock.json` | Committed exact selected release/version/digest/component evidence. |
| `bundled/observability/{compose,collector}.yaml.tmpl` | Lock-compatible per-home Compose/Collector configuration. |
| `tests/test_observability.py`, `tests/test_harnesses.py`, `tests/test_workers.py`, `tests/primary.test.ts` | Consumer behavior/lifecycle coverage. |
| `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md` | Operational contract and evidence. |

| Interface | Contract |
|---|---|
| `paths(home) -> dict[str, Path]` | Returns private config/secrets/session/status/compose/queue/three archive paths; starts no service. |
| `worker_binding_locked(home, task_id, incarnation)` | Requires existing `store.lock`; atomically records one 0700 UUID-incarnation session directory. |
| `worker_binding(home, task_id, incarnation)` | Takes/releases the lock then calls locked variant; callers already under lock use only locked variant. |
| `managed_environment(home, role, task, incarnation)` | Returns `(complete_child_env, generation, warning)`; strips ambient OTEL first, then adds only enabled Collector/resource/full-capture values. |
| `central_argv(home, omp_args)` | Returns OMP argv, complete sanitized child environment and warning; refuses forwarded cwd/session/resume/no-session flags. |
| `run_control(argv: Sequence[str], stage: str) -> dict[str, object]` | Runs a Docker/HTTP boundary outside the lock and returns only sanitized `stage`, `category`, `exit_code`, and message. |
| `render_collector(lock: dict[str, object], paths: dict[str, Path]) -> dict[str, object]` | Returns the semantic Collector model; `render_stack(home, lock) -> dict[str, Path | str]` writes that model to a private file and returns its file path, Compose path, project/network/volume/port identities. |
| `record_process_generation(home: Path, role: Literal['central', 'worker'], generation: str) -> None`; `record_desired_generation(home: Path, generation: str, mode: Literal['enabled', 'disabled']) -> None`; `enable/start/stop/disable/status(home: Path) -> dict[str, object]` | Short-lock state/lifecycle operations used by managed launch and status. |

### Task 1: Reconcile source and add private observability state

**Files:** Create `bin/agd_lib/observability.py`, `tests/test_observability.py`.

**Interfaces:** Produces paths, locked/unlocked binding, disabled environment, config/status records.

- [ ] **Step 1: Reconcile without rewriting approved work**

```sh
PLAN_BASE=$(git rev-parse HEAD)
git merge-base --is-ancestor a36b60e "$PLAN_BASE"
git merge --no-ff origin/main -m "merge: reconcile origin runtime base"
git merge-base --is-ancestor "$PLAN_BASE" HEAD
git merge-base --is-ancestor origin/main HEAD
git diff --check "$PLAN_BASE"..HEAD
```

Expected: every check exits 0; retain spec/plan commits and keep the normal merge commit.

- [ ] **Step 2: Write failing state tests**

```python
def test_uuid_incarnations_get_distinct_private_directories():
    first_id = '00000000-0000-4000-8000-000000000001'
    second_id = '00000000-0000-4000-8000-000000000002'
    with lab() as (home, _):
        first = observability.worker_binding(home, 'task-a', first_id)
        second = observability.worker_binding(home, 'task-a', second_id)
        assert first['session_dir'] != second['session_dir']
        assert Path(first['session_dir']).is_dir()
        assert (Path(first['session_dir']) / 'binding.json').is_file()


def test_disabled_environment_removes_ambient_export_and_content_controls(monkeypatch):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://outside.invalid')
    monkeypatch.setenv('OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT', 'true')
    with lab() as (home, _):
        env, _generation, _warning = observability.managed_environment(
            home, 'worker', {'id': 'task-a'}, '00000000-0000-4000-8000-000000000001')
        assert 'OTEL_EXPORTER_OTLP_ENDPOINT' not in env
        assert 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT' not in env
```

- [ ] **Step 3: Run failing test, implement, verify, commit**

Run `uv run --python 3.12 --with pytest pytest tests/test_observability.py -q`; expected initial failure for missing module.

Implement only owner-only spec paths with short locks and `store.atomic_json`. The existing worker lock calls `worker_binding_locked`; external callers use `worker_binding`:

```python
def worker_binding_locked(home, task_id, incarnation):
    directory = paths(home)['task_sessions'] / task_id / incarnation
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {'task_id': task_id, 'incarnation': incarnation,
             'session_dir': str(directory), 'state': 'directory allocated'}
    store.atomic_json(directory / 'binding.json', value)
    return value
```

Build complete child environments from a copy after clearing all OTEL exporter/protocol/header/content keys; never delete durable paths. Rerun the test (PASS; no Docker invocation), then commit:

```sh
git add bin/agd_lib/observability.py tests/test_observability.py
git commit -m "feat: add private observability state"
```

### Task 2: Commit release lock and deliver complete stack lifecycle

**Files:** Modify `bin/agd`, `bin/agd_lib/observability.py`, `tests/test_observability.py`; create `bundled/observability/release-lock.json`, `bundled/observability/compose.yaml.tmpl`, `bundled/observability/collector.yaml.tmpl`.

**Interfaces:** Produces all public lifecycle commands plus `run_control`, release-lock validation, `render_collector`, and `render_stack`. No `enable` exists before Collector configuration exists.

- [ ] **Step 1: Select one compatible release lock**

Review official Langfuse self-hosted Compose/headless-init docs and selected Collector release schema. Resolve required images by read-only manifest inspection to immutable `image@sha256` references. Verify OTLP receiver, transform processor, `file_storage`, OTLP/HTTP exporter, retry/sending queue, and append-capable file exporter. Commit exact versions, digests, evidence URLs and component names in `release-lock.json`. Templates must only accept that lock; `enable` rejects missing/malformed/unsupported locks and never checks latest.

- [ ] **Step 2: Write failing semantic/lifecycle tests**

```python
def test_compose_config_allows_only_browser_and_collector_ports_on_loopback(tmp_path):
    lock = {'images': {
        'collector': 'registry.example/collector@sha256:' + '0' * 64,
        'langfuse': 'registry.example/langfuse@sha256:' + '1' * 64,
    }}
    home = tmp_path / 'home'; command(home, 'init')
    rendered = observability.render_stack(home, lock)
    result = subprocess.run(['docker', 'compose', '-f', str(rendered['compose']),
                             'config', '--format', 'json'], capture_output=True, text=True, check=True)
    model = json.loads(result.stdout)
    published = {name: service['ports'] for name, service in model['services'].items() if service.get('ports')}
    assert set(published) <= {'langfuse-web', 'collector', 'minio'}
    assert all(port['host_ip'] == '127.0.0.1' for ports in published.values() for port in ports)


def test_control_failure_is_sanitized_not_child_output():
    assert observability.run_control(['missing-command'], 'start') == {
        'stage': 'start', 'category': 'missing-executable', 'exit_code': None,
        'message': 'start failed: executable unavailable'}
```

Skip only the Compose semantic test if its executable is absent. It privately parses captured JSON and prints neither it nor child output.

- [ ] **Step 3: Run test, implement complete config/lifecycle, verify, commit**

Run `uv run --python 3.12 --with pytest pytest tests/test_observability.py -q`; expected initial failure.

`enable` validates bundled lock, copies exact lock privately, generates protected credentials, renders all templates, starts per-home Compose, waits bounded health, headlessly initializes exactly one Langfuse org/project, then records desired enabled generation. `start` requires completed enablement. `stop`/`disable` preserve volumes/records and report `unknown`/`degraded` if absence is unproven. All Docker/HTTP work occurs outside locks; status/errors contain only sanitised receipts. Compose binds only UI/Collector host ports to `127.0.0.1`, leaves DB/cache un-published, and derives distinct project/network/volumes from `home_id`. Generate documented unquoted `LANGFUSE_INIT_ORG_ID`, `LANGFUSE_INIT_PROJECT_ID`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY`, `LANGFUSE_INIT_USER_EMAIL`, and `LANGFUSE_INIT_USER_PASSWORD` values without output.

The generated Collector has its receiver inside container on `0.0.0.0`; Compose publishes host OTLP only on `127.0.0.1`:

```yaml
extensions:
  file_storage/trace_queue:
    directory: "${env:AGD_QUEUE_DIR}"
receivers:
  otlp:
    protocols:
      http:
        endpoint: "0.0.0.0:${env:AGD_OTLP_HTTP_PORT}"
processors:
  transform/langfuse:
    trace_statements:
      - context: span
        statements:
          - set(attributes["langfuse.session.id"], attributes["gen_ai.conversation.id"]) where attributes["gen_ai.conversation.id"] != nil
          - set(attributes["langfuse.trace.metadata.agd.home_id"], resource.attributes["agd.home_id"]) where resource.attributes["agd.home_id"] != nil
          - set(attributes["langfuse.trace.metadata.agd.distro_id"], resource.attributes["agd.distro_id"]) where resource.attributes["agd.distro_id"] != nil
          - set(attributes["langfuse.trace.metadata.agd.role"], resource.attributes["agd.role"]) where resource.attributes["agd.role"] != nil
          - set(attributes["langfuse.trace.metadata.agd.task_id"], resource.attributes["agd.task_id"]) where resource.attributes["agd.task_id"] != nil
          - set(attributes["langfuse.trace.metadata.agd.incarnation"], resource.attributes["agd.incarnation"]) where resource.attributes["agd.incarnation"] != nil
exporters:
  file/traces:
    path: "${env:AGD_TRACE_ARCHIVE}"
    append: true
  file/logs:
    path: "${env:AGD_LOG_ARCHIVE}"
    append: true
  file/metrics:
    path: "${env:AGD_METRIC_ARCHIVE}"
    append: true
  otlphttp/langfuse:
    traces_endpoint: "${env:LANGFUSE_OTLP_TRACES_ENDPOINT}"
    headers:
      Authorization: "${env:LANGFUSE_OTLP_AUTHORIZATION}"
      x-langfuse-ingestion-version: "4"
    sending_queue:
      enabled: true
      storage: file_storage/trace_queue
      queue_size: 1000
    retry_on_failure:
      enabled: true
      initial_interval: 1s
      max_interval: 30s
      max_elapsed_time: 5m
service:
  extensions: [file_storage/trace_queue]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [transform/langfuse]
      exporters: [file/traces, otlphttp/langfuse]
    logs:
      receivers: [otlp]
      exporters: [file/logs]
    metrics:
      receivers: [otlp]
      exporters: [file/metrics]
```

Add parser/dispatch only after methods exist; mutators use existing owner gate and status stays read-only. When the selected pinned Langfuse topology uses MinIO for attachment/blob media, publish that media endpoint on `127.0.0.1` and set Langfuse's generated media URL to the same loopback address; database/cache remain un-published. At this milestone run a real disposable `enable`, assert healthy status and private named volumes/loopback ports, then `stop` it while retaining records:

```sh
TMP_HOME=$(mktemp -d)
bin/agd --home "$TMP_HOME" init
bin/agd --home "$TMP_HOME" observability enable
bin/agd --home "$TMP_HOME" observability status
bin/agd --home "$TMP_HOME" observability stop
```

Rerun tests and commit the listed code/templates/tests.

### Task 3: Bind fresh native sessions to managed central/workers

**Files:** Modify `bin/agd`, `bin/agd_lib/harnesses.py`, `bin/agd_lib/workers.py`, `bin/agd_lib/observability.py`, `.omp/extensions/agd-primary.ts`, `tests/test_workers.py`, `tests/test_observability.py`, `tests/primary.test.ts`; create `tests/test_harnesses.py`.

**Interfaces:** Consumes Task 1 binding and Task 2 lifecycle. Produces `central_argv` and OMP-only session binding.

- [ ] **Step 1: Write failing behavior tests**

```python
def test_omp_launch_persists_uuid_binding_before_endpoint_creation():
    from unittest.mock import patch
    from agd_lib import workers
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'native', '--project', 'sample', '--brief', str(brief), '--kind', 'scout')
        class Endpoint:
            def create(self, _home, task_id, incarnation, _work, _argv):
                return {'task': task_id, 'incarnation': incarnation}
        with patch.object(workers.shutil, 'which', return_value='/bin/true'), \
             patch.object(workers, 'backend', return_value=Endpoint()):
            launched = workers.launch(home, task['id'])
        saved = tasks.read(home, task['id'])
        assert uuid.UUID(saved['observability']['incarnation'])
        assert Path(saved['observability']['session_dir']).is_dir()
        assert saved['incarnation'] == launched['incarnation']


def test_central_refuses_conflicting_native_session_flags():
    with lab() as (home, _):
        argv, _env, _warning = observability.central_argv(home, [])
        assert argv[argv.index('--session-dir') + 1] == str(observability.paths(home)['central_sessions'])
        with pytest.raises(store.Refusal): observability.central_argv(home, ['--no-session'])
```


- [ ] **Step 2: Run failing test, implement, verify, commit**

Run `uv run --python 3.12 --with pytest pytest tests/test_harnesses.py tests/test_workers.py tests/test_observability.py -q && bun test tests/primary.test.ts`; expected initial failure.

Inside existing worker locks call only `worker_binding_locked`, save fresh UUID/directory before endpoint creation, and preserve old binding on relaunch—never `--continue`/`--resume`. OMP gets `--session-dir`; Cursor remains unsupported/unconfigured. Pass the complete environment directly, never overlay `os.environ` or inject `env` argv:

```python
child_env, _generation, warning = observability.managed_environment(home, 'worker', task, incarnation)
args = harnesses.launch_argv(task, brief, task['unattended'], session_dir=Path(task['observability']['session_dir']))
child = subprocess.Popen(args, cwd=task['worktree'], env=child_env)
```

Managed central rejects forwarded cwd/session/resume/no-session flags; enabled mode boundedly ensures stack health then execs OMP even after degraded warning; disabled mode starts no stack. Extension warns bare OMP without relocation. Worker never runs Docker/gets Langfuse credentials. Rerun tests and a disposable `/bin/true` final-binary smoke proving relaunch retains two binding directories; commit this slice.

### Task 4: Truthful degradation, stale generations, cleanup retention, and full suite

**Files:** Modify `bin/agd_lib/observability.py`, `bin/agd_lib/workers.py`, `bin/agd_lib/tasks.py`, `tests/test_observability.py`, `tests/test_workers.py`.

**Interfaces:** Produces `healthy`, `degraded`, `disabled`, `stopped`, `unknown`, `stale-restart-required` status.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_changed_generation_marks_running_central_stale_until_restart():
    with lab() as (home, _):
        observability.record_process_generation(home, 'central', '1')
        observability.record_desired_generation(home, '2', 'enabled')
        assert observability.status(home)['central']['state'] == 'stale-restart-required'


def test_scout_cleanup_keeps_binding_queue_and_archive():
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'scout', '--project', 'sample', '--brief', str(brief), '--kind', 'scout')
        old = observability.worker_binding(home, 'scout', '00000000-0000-4000-8000-000000000001')
        queue = observability.paths(home)['queue']; queue.mkdir(parents=True); (queue / 'item').write_text('x')
        archive = observability.paths(home)['trace_archive']; archive.parent.mkdir(parents=True); archive.write_text('x')
        report = home / 'report.md'; report.write_text('verified\n')
        command(home, 'task', 'ready', 'scout', '--report', str(report)); command(home, 'task', 'cleanup', 'scout')
        assert Path(old['session_dir']).is_dir() and (queue / 'item').is_file() and archive.is_file()
```

- [ ] **Step 2: Run failing test, implement, verify, commit**

Run `uv run --python 3.12 --with pytest pytest tests/test_observability.py tests/test_workers.py -q`; expected initial failure.

Increment desired generation at config mutation and record launch generation. Separate transient retryable, permanent/rejected, queue-full and disk-full status; receiver/in-memory state is never queued/delivered, and queue counts come only from post-`file_storage` evidence. Degradation never blocks OMP or auto-deletes queue data. Cleanup must refuse/traverse no observability path; stop/disable/relaunch/cleanup retain JSONL/artifacts, queue/archives and volumes. Rerun focused tests, then the full project suite with documented private Treehouse v3.1.6 prerequisite:

```sh
uv run --python 3.12 --with pytest pytest tests -q
```

Commit lifecycle changes.

### Task 5: Real two-distro acceptance and operations documentation

**Files:** Modify `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`; modify/add acceptance tests only for behavior discovered during this run.

- [ ] **Step 1: Check prerequisites**

Run `docker version`, `docker compose version`, and ensure documented private Treehouse v3.1.6 is on `PATH`. If any fails, record the exact external prerequisite failure and do not claim real-stack acceptance or install/elevate anything.

- [ ] **Step 2: Run end-to-end two-home acceptance when prerequisites pass**

Create two disposable homes, run `init` and `observability enable`, and verify unique project/network/volumes/Langfuse project credentials/loopback ports plus cross-home refusal. First use direct synthetic OTLP spans only to validate Collector routing, archive separation, queue behavior and absent/present conversation-ID transform behavior; they are not evidence of native OMP capture.

For native-content acceptance, invoke the installed OMP core session/telemetry serializer with a synthetic no-network provider that returns deterministic system/prompt/assistant/tool-call/tool-result/error/usage/timing/cost data and attachment/reference shapes. Persist/reopen its actual native JSONL/artifact output, then send the OMP-produced OTLP through Collector. Query pinned Langfuse through its documented API/SDK using generated local project credentials and exact markers; assert original trace/span IDs, `langfuse.session.id`, metadata, and observed full-content values/limits. Native JSONL/artifacts remain the separate authority.

For recovery, stop Langfuse **before** emitting known marker spans; wait until Collector status proves persistent `file_storage` enqueue; restart Collector while Langfuse remains unavailable; restore Langfuse; query for the original IDs/markers and record any duplicates. Separately exercise permanent rejection and queue/disk-full; verify degraded status while agent process continues. Verify logs/metrics local archives only, no Langfuse log/metric ingestion. Stop/disable/relaunch/task cleanup and prove native/queue/archive/volume preservation.

- [ ] **Step 3: Document, verify, commit**

Document exact commands, Docker/Compose and Treehouse prerequisite, loopback ownership, sensitive full content, bare-OMP warning, disabled/stale/degraded behavior, durable preservation, accepted queue/delivery limits, and Cursor exclusion. Never publish credentials or payloads.

```sh
uv run --python 3.12 --with pytest pytest tests -q
git diff --check
git add docs/operations.md docs/compatibility.md CHANGELOG.md tests/test_observability.py
git commit -m "docs: describe managed observability operations"
```

## Plan self-review

- **Coverage:** Task 1 preserves base/history and durable state; Task 2 commits lock-compatible Compose/Collector before any public enablement; Task 3 provides native central/worker retention and sanitized startup; Task 4 provides stale/error/retention correctness; Task 5 proves two-distro, queue-recovery, Langfuse-visible end-to-end behavior and documents it.
- **Type/placeholder review:** Interfaces are declared above. Tests use valid UUIDs, disposable real records/config semantics, or final process boundaries; no incidental generation literal, source-text assertion, undefined Python transform mirror, or fake lifecycle success remains.
- **Limits:** The plan preserves accepted pre-queue/permanent rejection/queue-disk/duplicate/attachment/archive limits and adds no custom queue, provider, or fabricated release pin.
