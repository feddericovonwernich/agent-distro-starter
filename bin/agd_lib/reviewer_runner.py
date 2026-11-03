"""Reviewer collection lifecycle and the trusted JSONL evidence broker."""
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import observability, omp_runtime, reviewer, store

UNINSTALL_TIMEOUT = 15.0


def assert_runtime_capability(deadline_epoch_ms, *, home=None, omp_binary='omp'):
    """Discover public installed OMP modules from the selected OMP executable."""
    from contextlib import nullcontext
    from .uninstall_state import admission
    remaining = reviewer.deadline_remaining_seconds(deadline_epoch_ms)
    if remaining <= 0: raise store.DependencyError('reviewer deadline exceeded')
    omp = shutil.which(omp_binary)
    if not omp: raise store.DependencyError('omp is unavailable')
    try:
        entry = Path(omp).resolve(strict=True)
        if not entry.is_file(): raise ValueError
        root = next((parent for parent in (entry.parent, *entry.parents)
                     if (parent / 'package.json').is_file() and not (parent / 'package.json').is_symlink()
                     and json.loads((parent / 'package.json').read_text()).get('name') == '@oh-my-pi/pi-coding-agent'), None)
        if root is None: raise ValueError
        manifest = json.loads((root / 'package.json').read_text())
        declared = (root / manifest['bin']['omp']).resolve(strict=True)
        if declared != entry or root not in entry.parents or not manifest.get('version'): raise ValueError
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        raise store.DependencyError('installed OMP package root is unavailable')
    binary = shutil.which('bun')
    if not binary: raise store.DependencyError('bun is unavailable')
    script = """const agent='@oh-my-pi/pi-coding-agent', modules={ai:'@oh-my-pi/pi-ai',core:'@oh-my-pi/pi-agent-core',sdk:agent+'/sdk',registry:agent+'/config/model-registry',manager:agent+'/session/session-manager',session:agent+'/session/agent-session',telemetry:agent+'/telemetry-export',resolver:agent+'/config/model-resolver'};
const [ai,core,sdk,registry,manager,session,telemetry,resolver]=await Promise.all(Object.values(modules).map(name=>import(name)));
for(const [name,value] of Object.entries({Agent:core.Agent,streamSimple:ai.streamSimple,AssistantMessageEventStream:ai.AssistantMessageEventStream,Settings:sdk.Settings,discoverAuthStorage:sdk.discoverAuthStorage,ModelRegistry:registry.ModelRegistry,SessionManager:manager.SessionManager,AgentSession:session.AgentSession,initTelemetryExport:telemetry.initTelemetryExport,isTelemetryExportEnabled:telemetry.isTelemetryExportEnabled,createTelemetryExportConfig:telemetry.createTelemetryExportConfig,flushTelemetryExport:telemetry.flushTelemetryExport,getModelMatchPreferences:resolver.getModelMatchPreferences,resolveAllowedModels:resolver.resolveAllowedModels,resolveModelRoleValue:resolver.resolveModelRoleValue,resolveCliModel:resolver.resolveCliModel,formatModelString:resolver.formatModelString}))if(typeof value!=='function')throw new Error('unsupported OMP export: '+name);
console.log(JSON.stringify({ai_url:import.meta.resolve(modules.ai),core_url:import.meta.resolve(modules.core),sdk_url:import.meta.resolve(modules.sdk),registry_url:import.meta.resolve(modules.registry),manager_url:import.meta.resolve(modules.manager),session_url:import.meta.resolve(modules.session),telemetry_url:import.meta.resolve(modules.telemetry),resolver_url:import.meta.resolve(modules.resolver),omp_version:process.argv[1]}));"""
    base_env = {key: os.environ[key] for key in ('PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME') if key in os.environ}
    env = omp_runtime.environment(home, base_env) if home is not None else base_env
    with admission(home) if home is not None else nullcontext():
        try: result = subprocess.run([binary, '--no-install', '--no-env-file', '-e', script, manifest['version']], capture_output=True, text=True, cwd=root, timeout=min(10, remaining), env=env)
        except subprocess.TimeoutExpired as error: raise store.DependencyError('installed OMP runtime capability timed out') from error
    if result.returncode: raise store.DependencyError(f'installed OMP runtime capability unavailable: {result.stderr.strip()}')
    try: capability = json.loads(result.stdout)
    except json.JSONDecodeError as error: raise store.DependencyError('installed OMP runtime capability malformed') from error
    expected = {'ai_url', 'core_url', 'sdk_url', 'registry_url', 'manager_url', 'session_url', 'telemetry_url', 'resolver_url', 'omp_version'}
    try:
        if set(capability) != expected or not isinstance(capability['omp_version'], str) or not capability['omp_version']: raise ValueError
        for key in expected - {'omp_version'}:
            parsed = urlparse(capability[key])
            location = Path(unquote(parsed.path)).resolve(strict=True)
            if parsed.scheme != 'file' or root.parents[1] not in location.parents: raise ValueError
    except (OSError, TypeError, ValueError):
        raise store.DependencyError('installed OMP runtime capability malformed')
    return capability



def _runtime_boundary(home, review_run_id):
    runtime = reviewer.paths(home)['runtime'] / review_run_id
    plugins = runtime / '.omp' / 'plugins'
    reviewer._private(plugins)
    reviewer.publish_immutable(
        plugins / 'installed_plugins.json', {'version': 2, 'plugins': {}})
    reviewer.publish_immutable(
        plugins / 'omp-plugins.lock.json', {'plugins': {}, 'settings': {}})
    return runtime

def _native_runtime_boundary(home, review_run_id):
    runtime = reviewer.paths(home)['runtime'] / review_run_id
    reviewer._private(runtime / '.omp' / 'plugins')
    reviewer.publish_immutable(runtime / '.omp' / 'plugins' / 'installed_plugins.json',
                               {'version': 2, 'plugins': {}})
    reviewer.publish_immutable(runtime / '.omp' / 'plugins' / 'omp-plugins.lock.json',
                               {'plugins': {}, 'settings': {}})
    return runtime


_REVIEWER_EXTENSION = Path(__file__).resolve().parents[2] / 'lib' / 'omp-reviewer.ts'
_REVIEWER_SKILL = Path(__file__).resolve().parents[2] / 'bundled' / 'reviewer-skills' / 'agd-reviewer'


def _native_request(home, review_run_id):
    try:
        canonical_id = str(uuid.UUID(str(review_run_id)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error
    runtime = reviewer.paths(home)['runtime'] / canonical_id
    request_path = runtime / 'request.json'
    try:
        request = reviewer._read_json(request_path)
        receipt = reviewer._receipt(home, canonical_id)
    except (OSError, ValueError, json.JSONDecodeError, store.Refusal) as error:
        raise store.Refusal('reviewer request is unavailable') from error
    if (not isinstance(request, dict) or request.get('schema') != 1
            or request.get('review_run_id') != canonical_id
            or request.get('home') != str(store.home(home).resolve())
            or not isinstance(request.get('cwd'), str)
            or Path(request['cwd']).resolve() != runtime.resolve()
            or request.get('packet_id') != receipt.get('packet_id')
            or request.get('config_snapshot_ref') != receipt.get('config_snapshot_ref')
            or request.get('instruction_snapshot_ref') != receipt.get('instruction_snapshot_ref')
            or request.get('report_path') != str(runtime / 'report-draft.json')):
        raise store.Refusal('reviewer request scope is invalid')
    try:
        reviewer._hex_id(request['packet_id'])
        packet = reviewer.read_packet(home, request['packet_id'])
    except (KeyError, store.Refusal) as error:
        raise store.Refusal('reviewer request packet is invalid') from error
    allowed = request.get('allowed_scope')
    if (not isinstance(allowed, dict)
            or allowed.get('source_ids') != sorted(source['source_id'] for source in packet['sources'])):
        raise store.Refusal('reviewer request scope is invalid')
    return runtime, request


def _write_private_bytes(path, content):
    path = Path(path)
    reviewer._private(path.parent)
    body = bytes(content)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if reviewer._read_regular_bytes(path, require_current_uid=True) != body:
            raise store.Refusal('reviewer artifact is immutable')
        return
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _materialize_reviewer_skill(runtime):
    destination = runtime / '.omp' / 'skills' / 'agd-reviewer'
    files = {}
    for relative in ('SKILL.md', 'references/commands.md'):
        source = _REVIEWER_SKILL / relative
        try:
            body = source.read_bytes()
        except OSError as error:
            raise store.DependencyError('bundled reviewer skill is unavailable') from error
        target = destination / relative
        _write_private_bytes(target, body)
        files[relative] = hashlib.sha256(body).hexdigest()
    reviewer.publish_immutable(destination / 'provenance.json', {
        'schema': 1, 'skill': 'agd-reviewer', 'source': str(_REVIEWER_SKILL),
        'files': files,
    })
    return destination

def _verify_reviewer_skill(runtime):
    manifest = reviewer._read_json(runtime / '.omp' / 'skills' / 'agd-reviewer' / 'provenance.json')
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1
            or manifest.get('skill') != 'agd-reviewer'
            or manifest.get('source') != str(_REVIEWER_SKILL)
            or not isinstance(manifest.get('files'), dict)):
        raise store.Refusal('reviewer skill provenance is invalid')
    for relative, digest in manifest['files'].items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise store.Refusal('reviewer skill provenance is invalid')
        body = reviewer._read_regular_bytes(runtime / '.omp' / 'skills' / 'agd-reviewer' / relative,
                                             require_current_uid=True)
        if hashlib.sha256(body).hexdigest() != digest:
            raise store.Refusal('reviewer skill bytes changed')


def _native_request_uuid(review_run_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'agd-reviewer-report:{review_run_id}'))


def launch_argv(home: Path, review_run_id: str, *, omp_binary: str = 'omp',
                remaining_seconds: float) -> list[str]:
    runtime, request = _native_request(home, review_run_id)
    if not isinstance(remaining_seconds, (int, float)) or isinstance(remaining_seconds, bool) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise store.Refusal('reviewer runtime is invalid')
    receipt = reviewer._receipt(home, review_run_id)
    config = reviewer._read_json(reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")
    session_dir = observability.reviewer_session_dir(home, review_run_id)
    argv = [str(omp_binary), '--print', '--mode', 'json', '--no-extensions',
            '--skills', 'agd-reviewer', '--cwd', str(runtime), '--session-dir', session_dir]
    model = config.get('review_model')
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise store.Refusal('review model is invalid')
        argv.extend(['--model', model])
    argv.extend(['-e', str(_REVIEWER_EXTENSION), '--max-time',
                 str(max(1, math.ceil(remaining_seconds)))])
    return argv


def _begin_native_run(home, receipt, packet, *, model_selector, omp_version):
    packet_id = packet.get('packet_id') if isinstance(packet, dict) else None
    reviewer._hex_id(packet_id)
    with store.lock(store.home(home)):
        stored = reviewer._receipt(home, receipt['review_run_id'])
        if stored.get('dispatch_state') != 'reserved' or stored.get('packet_id') not in (None, packet_id):
            raise store.Refusal('reviewer receipt is not reservable')
        stored.update(packet_id=packet_id, model=None, model_selector=model_selector,
                      omp_version=omp_version, dispatch_state='begun')
        store.atomic_json(reviewer.paths(home)['receipts'] / f"{receipt['review_run_id']}.json", stored)
    return stored
def _wait_owned_group_absent(identity, deadline_epoch_ms):
    """Wait for the child process and its owned group to disappear before handoff."""
    if not isinstance(identity, dict):
        return False
    from . import supervision
    while True:
        try:
            process_live = supervision.process_presence(identity)
        except (KeyError, TypeError, store.Refusal):
            return False
        group_live = _process_group_present(identity)
        if process_live is False and group_live is False:
            return True
        remaining = reviewer.deadline_remaining_seconds(deadline_epoch_ms)
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def execute(home: Path, review_run_id: str) -> int:
    """Run one already-reserved native reviewer and leave only durable handoff state."""
    from .supervision import process_identity, runtime_home_identity
    runtime, request = _native_request(home, review_run_id)
    receipt = reviewer._receipt(home, review_run_id)
    target = receipt.get('target')
    if target is not None and target != runtime_home_identity(home):
        raise store.Refusal('reviewer runtime home identity changed')
    if receipt.get('dispatch_state') != 'reserved':
        raise store.Refusal('reviewer run is already dispatched')
    packet = reviewer.read_packet(home, request['packet_id'])
    binary = omp_runtime.executable(home)
    _native_runtime_boundary(home, review_run_id)
    _materialize_reviewer_skill(runtime)
    _verify_reviewer_skill(runtime)
    config = reviewer._read_json(reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")
    model_selector = config.get('review_model')
    from . import installer
    dependencies = installer.read_dependencies(home)
    omp_entry = dependencies.get('omp') if isinstance(dependencies, dict) else None
    omp_version = omp_entry.get('version') if isinstance(omp_entry, dict) else None
    receipt = _begin_native_run(
        home, receipt, packet, model_selector=model_selector, omp_version=omp_version)
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        receipt['supervisor'] = process_identity(os.getpid())
        receipt['child_state'] = 'spawning'
        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
    remaining = reviewer.deadline_remaining_seconds(receipt['deadline_epoch_ms'])
    if remaining <= 0:
        _write_exit(runtime, None, False, True, b'', b'', supervisor=receipt['supervisor'])
        return 124
    _write_private_bytes(runtime / 'packet.json', json.dumps(
        packet, sort_keys=True, separators=(',', ':')).encode())
    _write_private_bytes(runtime / 'catalog.json', json.dumps(
        packet.get('catalog', []), sort_keys=True, separators=(',', ':')).encode())
    instruction = reviewer._read_json(
        reviewer.paths(home)['manifests'] / f"{receipt['instruction_snapshot_ref']}.json")['text']
    prompt = (f"{instruction}\n\nReview run {review_run_id}. First read the trusted request.json, "
              "then read the admitted packet.json and catalog.json with native tools before inspecting "
              f"evidence for packet {request['packet_id']}; write report-draft.json only after those results.")
    argv = launch_argv(home, review_run_id, omp_binary=binary, remaining_seconds=remaining)
    inherited = {key: value for key, value in os.environ.items() if not key.startswith('LANGFUSE_')}
    env, _, _ = observability.reviewer_environment(home, review_run_id)
    env.update({key: value for key, value in inherited.items() if key not in env})
    env.update(AGD_HOME=str(store.home(home)), AGD_REVIEW_RUN_ID=review_run_id)
    started = time.time()
    process = None
    child_identity = None
    timed_out = False
    stdout = bytearray()
    stderr = bytearray()
    code = None
    try:
        process = subprocess.Popen(argv, cwd=runtime, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            child_identity = process_identity(process.pid)
        except (FileNotFoundError, ProcessLookupError):
            child_identity = None
        with store.lock(store.home(home)):
            receipt = reviewer._receipt(home, review_run_id)
            receipt['child'] = child_identity
            receipt['child_state'] = 'spawned'
            store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)

        def drain(stream, buffer):
            for chunk in iter(lambda: stream.read(8192), b''):
                if len(buffer) < 65536:
                    buffer.extend(chunk[:65536 - len(buffer)])

        readers = [threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
        except (BrokenPipeError, OSError):
            process.stdin.close()
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for reader in readers:
            reader.join(timeout=1)
        code = process.returncode
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            code = process.returncode
    process_absent = _wait_owned_group_absent(
        child_identity, receipt['deadline_epoch_ms']) if child_identity is not None else False
    _write_exit(runtime, code, process_absent, timed_out, stdout, stderr, started,
                 child=child_identity, supervisor=receipt['supervisor'])
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        receipt['child_state'] = 'exited'
        receipt['exit_code'] = code
        receipt['timed_out'] = timed_out
        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
    return int(code if code is not None else 1)


def _write_exit(runtime, code, process_absent, timed_out, stdout, stderr, started=None,
                *, child=None, supervisor=None):
    limit = 65536
    stdout, stderr = bytes(stdout)[:limit], bytes(stderr)[:limit]
    _write_private_bytes(runtime / 'stdout', stdout)
    _write_private_bytes(runtime / 'stderr', stderr)
    reviewer.publish_immutable(runtime / 'exit.json', {
        'schema': 1, 'code': code, 'process_absent': bool(process_absent),
        'child': child, 'supervisor': supervisor, 'timed_out': bool(timed_out),
        'started_at': started or time.time(), 'ended_at': time.time(),
        'stdout_bytes': len(stdout), 'stderr_bytes': len(stderr),
    })


def _same_uid_file(path, limit):
    path = Path(path)
    if type(limit) is not int or limit < 0:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            identity = os.fstat(stream.fileno())
            if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.getuid() or identity.st_size > limit:
                return None
            data = stream.read(limit + 1)
            return data if len(data) <= limit else None
    except OSError:
        return None


def _process_group_present(identity):
    if not isinstance(identity, dict) or type(identity.get('pgid')) is not int or type(identity.get('sid')) is not int:
        return None
    from . import supervision
    try:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                current = supervision._process_stat(int(entry.name))
            except (OSError, ValueError, IndexError):
                continue
            if (current['uid'] == os.getuid() and current['pgid'] == identity['pgid']
                    and current['sid'] == identity['sid'] and current['state'] not in {'Z', 'X'}):
                return True
    except OSError:
        return None
    return False


def _lifecycle_proved_absent(receipt, exit_record):
    if exit_record.get('process_absent') is not True:
        return False
    from . import supervision
    supervisor = receipt.get('supervisor')
    child = exit_record.get('child') or receipt.get('child')
    for identity in (supervisor, child):
        if not isinstance(identity, dict) or not isinstance(identity.get('start'), str):
            return False
        try:
            if supervision.process_presence(identity):
                return False
        except store.Refusal:
            return False
    return _process_group_present(child) is False

def _collect_native_result(home, review_run_id):
    receipt = reviewer._receipt(home, review_run_id)
    if receipt.get('dispatch_state') == 'final':
        return receipt
    receipt = reviewer.recover_submissions(home, review_run_id)
    if receipt.get('dispatch_state') == 'final':
        return receipt
    if receipt.get('dispatch_state') == 'report-committed':
        return reviewer.finalize_run(home, review_run_id, 'exited')
    runtime, request = _native_request(home, review_run_id)
    exit_bytes = _same_uid_file(runtime / 'exit.json', 8192)
    if exit_bytes is None:
        return reviewer.close_run(home, review_run_id, 'failed', error_code='exit-proof', packet=reviewer.read_packet(home, receipt['packet_id']))
    try:
        exit_record = json.loads(exit_bytes)
    except (TypeError, ValueError, json.JSONDecodeError):
        return reviewer.close_run(home, review_run_id, 'failed', error_code='exit-proof', packet=reviewer.read_packet(home, receipt['packet_id']))
    packet = reviewer.read_packet(home, receipt.get('packet_id') or request['packet_id'])
    outcome = 'unknown' if exit_record.get('timed_out') else 'failed'
    if (exit_record.get('schema') != 1 or exit_record.get('code') != 0
            or not _lifecycle_proved_absent(receipt, exit_record)):
        return reviewer.close_run(home, review_run_id, outcome, error_code='nonzero', packet=packet)
    binding_path = store.home(home) / 'data' / 'observability' / 'reviewer' / review_run_id / 'binding.json'
    binding = None
    try:
        binding = reviewer._read_json(binding_path)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if (not isinstance(binding, dict) or binding.get('review_run_id') != review_run_id
            or binding.get('session_dir') != str(observability.reviewer_session_dir(home, review_run_id))):
        return reviewer.close_run(home, review_run_id, 'failed', error_code='binding', packet=packet)
    bind_bytes = _same_uid_file(runtime / 'session-bind.json', 8192)
    if bind_bytes is None:
        return reviewer.close_run(home, review_run_id, 'failed', error_code='binding', packet=packet)
    try:
        bind_record = json.loads(bind_bytes)
    except (TypeError, ValueError, json.JSONDecodeError):
        return reviewer.close_run(home, review_run_id, 'failed', error_code='binding', packet=packet)
    try:
        bind_file_parent = Path(bind_record['session_file']).resolve().parent
    except (KeyError, OSError, TypeError):
        bind_file_parent = None
    if (not isinstance(bind_record, dict) or bind_record.get('session_id') != binding.get('native_session_id')
            or bind_file_parent != Path(binding['session_dir']).resolve()):
        return reviewer.close_run(home, review_run_id, 'failed', error_code='binding', packet=packet)
    limits = reviewer._read_json(
        reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")['limits']
    max_output = limits.get('max_output_bytes', 65536)
    draft_bytes = _same_uid_file(runtime / 'report-draft.json', max_output)
    if draft_bytes is None:
        return reviewer.close_run(home, review_run_id, 'failed', error_code='report', packet=packet)
    try:
        draft = json.loads(draft_bytes)
    except (TypeError, ValueError, json.JSONDecodeError):
        return reviewer.close_run(home, review_run_id, 'failed', error_code='report', packet=packet)
    request_id = _native_request_uuid(review_run_id)
    try:
        reviewer.submit_report(home, review_run_id, request_id, draft)
    except (store.Refusal, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return reviewer.close_run(home, review_run_id, 'failed', error_code='report', packet=packet)
    return reviewer.finalize_run(home, review_run_id, 'exited')


def _native_session_header(raw: str) -> dict:
    for line in raw.splitlines()[:8]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, json.JSONDecodeError) as error:
            raise store.Refusal('reviewer session header is malformed') from error
        if not isinstance(record, dict):
            raise store.Refusal('reviewer session header is malformed')
        if record.get('type') == 'session':
            return record
        if record.get('type') != 'title':
            break
    raise store.Refusal('reviewer session header is malformed')


def bind_session(home: Path, review_run_id: str, session_id: str, session_file: Path) -> dict:
    try:
        canonical_id = str(uuid.UUID(str(review_run_id)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error
    receipt = reviewer._receipt(home, canonical_id)
    if receipt.get('dispatch_state') != 'begun':
        raise store.Refusal('reviewer receipt cannot bind a session')
    child = receipt.get('child')
    from . import supervision
    if (not isinstance(child, dict) or not isinstance(child.get('start'), str)
            or not supervision.process_presence(child)):
        raise store.Refusal('reviewer child is not active')
    try:
        parent = supervision._process_stat(os.getppid())
    except (OSError, ValueError, IndexError) as error:
        raise store.Refusal('reviewer session caller is unavailable') from error
    if parent['pid'] != child.get('pid') or parent['start'] != child.get('start'):
        raise store.Refusal('reviewer session caller is outside child scope')
    expected = Path(observability.reviewer_session_dir(home, canonical_id)).resolve()
    candidate = Path(session_file)
    if not isinstance(session_id, str) or not session_id or '/' in session_id or candidate.is_symlink():
        raise store.Refusal('reviewer session binding is invalid')
    try:
        if candidate.resolve().parent != expected:
            raise store.Refusal('reviewer session file is outside its directory')
    except OSError as error:
        raise store.Refusal('reviewer session file is unavailable') from error
    raw = _same_uid_file(candidate, 65536)
    if raw is None:
        raise store.Refusal('reviewer session file is unavailable')
    header = _native_session_header(raw)
    if header.get('id') != session_id:
        raise store.Refusal('reviewer session header is foreign')
    result = observability.reviewer_binding(home, canonical_id, session_id)
    runtime = reviewer.paths(home)['runtime'] / canonical_id
    reviewer.publish_immutable(runtime / 'session-bind.json', {
        'schema': 1, 'session_id': session_id, 'session_file': str(candidate.resolve()),
    })
    return result

def run_native_reviewer(home, intent, *, omp_binary='omp'):
    """Run one selector-only native child while retaining the private lease."""
    from contextlib import ExitStack
    from .uninstall_state import admission
    from .supervision import process_identity, runtime_home_identity
    with ExitStack() as lifetime:
        with admission(home):
            lifetime.enter_context(reviewer.run_lease(home))
            config, started = reviewer.read_config(home), time.time()
            run = reviewer.reserve_run(home, intent, config_snapshot=config, instruction_snapshot=Path(reviewer.INSTRUCTION_FILE).read_bytes(),
                                       started_at=started, deadline_epoch_ms=int((started + config['limits']['max_runtime_seconds']) * 1000))
            with store.lock(store.home(home)):
                receipt = reviewer._receipt(home, run['review_run_id'])
                receipt['target'] = runtime_home_identity(home)
                receipt['coordinator'] = process_identity(os.getpid())
                receipt['coordinator'].pop('state')
                receipt['child_state'] = 'not-started'
                store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
        try:
            packet = reviewer.select_packet(home, cutoff=started, config_snapshot=config,
                                            retrospective_review_run_ids=tuple(intent.get('retrospective_review_run_ids', ())),
                                            deadline_epoch_ms=run['deadline_epoch_ms'])
        except store.Refusal:
            if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
                return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout')
            raise
        if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
            return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout', packet=packet)
        if not packet['excerpts']:
            return reviewer.close_run(home, run['review_run_id'], 'no-work', packet=packet)
        directory = omp_runtime.agent_dir(home)
        if directory.is_symlink() or not directory.is_dir(): return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='agent-dir', packet=packet)
        try: capability = assert_runtime_capability(run['deadline_epoch_ms'], home=home, omp_binary=omp_binary)
        except Exception:
            return reviewer.close_run(home, run['review_run_id'], 'failed',
                                      error_code='timeout' if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0 else 'capability',
                                      packet=packet)
        if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
            return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout', packet=packet)
        try:
            with store.lock(store.home(home)):
                receipt = reviewer._receipt(home, run['review_run_id'])
                receipt.update(packet_id=packet['packet_id'], omp_version=capability['omp_version'])
                store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
            env, _, _ = observability.reviewer_environment(home, run['review_run_id'])
            langfuse_query = observability.reviewer_query_status(home, env)
            runtime = _runtime_boundary(home, run['review_run_id'])
            payload = {'urls': {key: capability[f'{key}_url'] for key in ('ai', 'core', 'sdk', 'registry', 'manager', 'session', 'telemetry', 'resolver')},
                       'input': {'cwd': str(runtime), 'agentDir': str(directory.resolve()), 'sessionDir': observability.reviewer_session_dir(home, run['review_run_id']),
                                 'reviewRunId': run['review_run_id'], 'langfuse_query': langfuse_query,
                                 'receipt': {'deadline_epoch_ms': run['deadline_epoch_ms'], 'omp_version': capability['omp_version']},
                                 'modelSelector': config['review_model'], 'reviewPrompt': reviewer._read_json(reviewer.paths(home)['manifests'] / f"{run['instruction_snapshot_ref']}.json")['text']}}
        except Exception:
            return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='preparation', packet=packet)
        bun = shutil.which('bun')
        if not bun: return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='bun', packet=packet)
        stderr = bytearray()
        try:
            with admission(home):
                with store.lock(store.home(home)):
                    receipt = reviewer._receipt(home, run['review_run_id'])
                    receipt['child_state'] = 'spawning'
                    store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
                process = subprocess.Popen([bun, '--no-install', '--no-env-file', str(Path(__file__).resolve().parents[2] / 'lib' / 'agd-reviewer-host.ts')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=runtime, start_new_session=True)
                try:
                    with store.lock(store.home(home)):
                        receipt = reviewer._receipt(home, run['review_run_id'])
                        receipt['child'] = process_identity(process.pid)
                        receipt['child'].pop('state')
                        receipt['child_state'] = 'spawned'
                        store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
        except OSError: return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='spawn', packet=packet)
        def drain():
            for chunk in iter(lambda: process.stderr.read(256), ''):
                if len(stderr) < 4096: stderr.extend(chunk.encode(errors='replace')[:4096-len(stderr)])
        thread = threading.Thread(target=drain, daemon=True); thread.start()
        timed_out, completion, code = threading.Event(), None, None
        def expire():
            timed_out.set()
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        timer = threading.Timer(max(.001, run['deadline_epoch_ms']/1000-time.time()), expire); timer.start()
        try:
            process.stdin.write(json.dumps(payload) + '\n'); process.stdin.flush()
            completion = serve_broker(home, run['review_run_id'], process.stdout, process.stdin)
            code = process.wait()
        except Exception as error:
            failure = error
        else:
            failure = None
        finally:
            timer.cancel()
            if process.poll() is None: os.killpg(process.pid, signal.SIGKILL); process.wait()
            process.stdout.close(); process.stdin.close(); thread.join(timeout=1); process.stderr.close()
        receipt = reviewer._receipt(home, run['review_run_id'])
        outcome = 'unknown' if receipt['counters'].get('provider_inflight') else 'failed'
        if timed_out.is_set() or failure or code or not isinstance(completion, dict):
            return reviewer.close_run(home, run['review_run_id'], outcome, error_code='timeout' if timed_out.is_set() else ('nonzero' if code else 'ipc'), diagnostic=bytes(stderr), packet=packet)
        try: result = _durable_result(home, run['review_run_id'])
        except Exception: return reviewer.close_run(home, run['review_run_id'], outcome, error_code='completion', diagnostic=bytes(stderr), packet=packet)
        if completion.get('reportId') != result['reportId']:
            return reviewer.close_run(home, run['review_run_id'], outcome, error_code='completion', diagnostic=bytes(stderr), packet=packet)
        return reviewer.finalize_run(home, run['review_run_id'], 'exited')

def launch_timer(home, *, now=None):
    intent = reviewer.timer_intent(home, now=now)
    return None if intent is None else run_native_reviewer(home, intent)


def _request_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('invalid broker request ID') from error


def _broker_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _replay_or_reserve_input(home, review_run_id, request_id, op, args, value, limit, *, evidence_limit=None):
    digest = reviewer._digest({'op': op, 'args': args})
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        replies = receipt.setdefault('broker_replies', {})
        prior = replies.get(request_id)
        if prior:
            if prior.get('digest') != digest: raise store.Refusal('conflicting broker request reuse')
            return prior['value']
        encoded = value if isinstance(value, str) else _broker_text(value)
        counters = receipt.setdefault('counters', {})
        used = counters.get('input_bytes', 0)
        if not isinstance(used, int) or used + len(encoded.encode()) > limit: raise store.Refusal('cumulative broker input limit exceeded')
        if evidence_limit is not None and counters.get('evidence_calls', 0) >= evidence_limit: raise store.Refusal('evidence-call limit exceeded')
        counters['input_bytes'] = used + len(encoded.encode())
        if evidence_limit is not None: counters['evidence_calls'] = counters.get('evidence_calls', 0) + 1
        replies[request_id] = {'digest': digest, 'value': value}
        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
    return value


def _durable_result(home, review_run_id):
    reviewer.recover_submissions(home, review_run_id)
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        chosen = receipt.get('broker_result')
        if chosen is None:
            results = receipt.get('report_results', {})
            if len(results) != 1: raise store.Refusal('report result is ambiguous')
            request_id, result = next(iter(results.items()))
            if not isinstance(result, dict) or not isinstance(result.get('report_id'), str): raise store.Refusal('report result is malformed')
            chosen = {'request_id': request_id, 'report_id': result['report_id']}
            receipt['broker_result'] = chosen
            store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
        if not isinstance(chosen, dict) or not isinstance(chosen.get('request_id'), str) or not isinstance(chosen.get('report_id'), str): raise store.Refusal('report result is malformed')
        return {'reportId': chosen['report_id']}


def serve_broker(home, review_run_id, stdin, stdout):
    """Serve only frozen packet evidence and receipt transitions over JSONL."""
    receipt = reviewer._receipt(home, review_run_id)
    try: config = reviewer.validate_config(reviewer._read_json(reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json"))
    except (OSError, ValueError, json.JSONDecodeError, store.Refusal) as error: raise store.Refusal('invalid frozen config manifest') from error
    if reviewer._digest(config) != receipt['config_snapshot_ref']: raise store.Refusal('frozen config manifest digest mismatch')
    limit, packet, sources, excerpts = config['limits']['max_input_bytes'], None, set(), []
    if receipt.get('dispatch_state') in ('begun', 'report-committed'):
        packet = reviewer.read_packet(home, receipt['packet_id']); sources = {item['source_id'] for item in packet['sources']}; excerpts = [item for item in packet['excerpts'] if item['source_id'] in sources]
    completion = None
    for line in stdin:
        if not line.strip(): continue
        if completion is not None: raise store.Refusal('reviewer protocol after completion')
        response = {'id': None, 'ok': False}
        try:
            request = json.loads(line)
            if set(request) == {'complete', 'result'}:
                result = request['result']; receipt = reviewer._receipt(home, review_run_id)
                if receipt.get('dispatch_state') != 'report-committed' or request['complete'] is not True or not isinstance(result, dict) or set(result) != {'reportId'} or not isinstance(result['reportId'], str) or result != _durable_result(home, review_run_id):
                    raise store.Refusal('invalid reviewer completion')
                completion = result
                continue
            request_id = _request_id(request.get('id')); response['id'] = request_id
            op, args = request.get('op'), request.get('args')
            receipt = reviewer._receipt(home, review_run_id)
            if not isinstance(op, str) or not isinstance(args, dict): raise store.Refusal('invalid broker request')
            if receipt.get('dispatch_state') == 'reserved' and op != 'model': raise store.Refusal('reviewer model is not persisted')
            if receipt.get('dispatch_state') not in ('reserved', 'begun', 'report-committed'): raise store.Refusal('reviewer receipt is not dispatchable')
            if op == 'packet':
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text(packet), limit)
            elif op == 'reviewer_excerpt':
                source_id = args.get('source_id')
                if source_id not in sources: raise store.Refusal('source is outside packet')
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text([item for item in excerpts if item['source_id'] == source_id]), limit, evidence_limit=config['limits']['max_model_calls'])
            elif op == 'reviewer_search':
                query = args.get('query')
                if not isinstance(query, str) or not query or len(query.encode()) > 256: raise store.Refusal('invalid packet search')
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text([item for item in excerpts if query.casefold() in item['text'].casefold()]), limit, evidence_limit=config['limits']['max_model_calls'])
            elif op == 'reviewer_submit':
                report = args.get('report')
                result = reviewer.submit_report(home, review_run_id, request_id, report)
                with store.lock(store.home(home)):
                    receipt = reviewer._receipt(home, review_run_id)
                    receipt['broker_result'] = {'request_id': request_id, 'report_id': result['report_id']}
                    store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text({'reportId': result['report_id']}), limit)
            elif op == 'reserve':
                if receipt.get('dispatch_state') not in ('begun', 'report-committed') or not receipt.get('model'): raise store.Refusal('reviewer model is not persisted')
                if time.time() * 1000 >= receipt['deadline_epoch_ms']: raise store.Refusal('review deadline exceeded')
                with store.lock(store.home(home)):
                    current = reviewer._receipt(home, review_run_id); counters = current.setdefault('counters', {}); replies = current.setdefault('broker_replies', {})
                    digest = reviewer._digest({'op': op, 'args': args}); prior = replies.get(request_id)
                    if prior:
                        if prior.get('digest') != digest: raise store.Refusal('conflicting broker request reuse')
                        value = prior['value']
                    else:
                        if counters.get('model_calls', 0) >= config['limits']['max_model_calls']: raise store.Refusal('model-call limit exceeded')
                        counters['model_calls'] = counters.get('model_calls', 0) + 1; counters['provider_inflight'] = counters.get('provider_inflight', 0) + 1
                        value = {'model_calls': counters['model_calls']}; replies[request_id] = {'digest': digest, 'value': value}
                        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', current)
            elif op == 'settle':
                with store.lock(store.home(home)):
                    current = reviewer._receipt(home, review_run_id); counters = current.setdefault('counters', {}); replies = current.setdefault('broker_replies', {})
                    digest = reviewer._digest({'op': op, 'args': args}); prior = replies.get(request_id)
                    if prior:
                        if prior.get('digest') != digest: raise store.Refusal('conflicting broker request reuse')
                        value = prior['value']
                    else:
                        counters['provider_inflight'] = max(0, counters.get('provider_inflight', 0) - 1)
                        value = {'provider_inflight': counters['provider_inflight']}; replies[request_id] = {'digest': digest, 'value': value}
                        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', current)
            elif op == 'model':
                model = args.get('id')
                if not isinstance(model, str) or not __import__('re').fullmatch(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+', model):
                    raise store.Refusal('invalid resolved reviewer model')
                receipt = reviewer._receipt(home, review_run_id)
                if receipt.get('dispatch_state') == 'reserved':
                    packet = reviewer.read_packet(home, receipt['packet_id']); sources = {item['source_id'] for item in packet['sources']}; excerpts = [item for item in packet['excerpts'] if item['source_id'] in sources]
                    value = reviewer.begin_run(home, receipt, packet, model=model, observed_omp_version=receipt.get('omp_version'))
                elif receipt.get('dispatch_state') in ('begun', 'report-committed') and receipt.get('model') == model:
                    value = receipt
                else:
                    raise store.Refusal('reviewer model transition is unavailable')
            elif op == 'session':
                native_session_id = args.get('id')
                value = observability.reviewer_binding(home, review_run_id, native_session_id)
            elif op == 'result':
                value = _durable_result(home, review_run_id)
            else:
                raise store.Refusal('broker operation is denied')
            response.update(ok=True, value=value)
        except (json.JSONDecodeError, store.Refusal, ValueError, TypeError) as error:
            response['error'] = str(error)
        stdout.write(json.dumps(response, sort_keys=True) + '\n'); stdout.flush()
    return completion


def _uninstall_lease(home):
    """Inspect Linux flock ownership without creating or taking the run lock."""
    import stat
    path = reviewer.paths(home)['lock']
    if not os.path.lexists(path):
        return None
    mode = path.lstat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid():
        raise store.Refusal('reviewer run lock identity is unsafe')
    identity = {'path': str(path), 'device': mode.st_dev, 'inode': mode.st_ino,
                'uid': mode.st_uid}
    holders = []
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != 'FLOCK':
            continue
        major, minor, inode = fields[5].split(':')
        if (int(major, 16), int(minor, 16), int(inode)) == (
                os.major(mode.st_dev), os.minor(mode.st_dev), mode.st_ino):
            holders.append(int(fields[4]))
    return {'identity': identity, 'holders': holders}


def uninstall_inventory(home, *, expected=None, service=None):
    from . import supervision
    expected = {row['key']: row for row in expected or [] if row['kind'].startswith('reviewer-')}
    lease = _uninstall_lease(home)
    holders = lease['holders'] if lease else []
    rows, accounted = {}, set()
    receipts = reviewer.paths(home)['receipts']
    for path in sorted(receipts.glob('*.json')):
        record = reviewer._read_json(path)
        if (not isinstance(record, dict) or record.get('schema') != 1
                or not isinstance(record.get('review_run_id'), str)):
            raise store.Refusal('reviewer run receipt is malformed')
        run_id = record['review_run_id']
        if path.stem != run_id or str(uuid.UUID(run_id)) != run_id:
            raise store.Refusal('reviewer receipt path differs from its run identity')
        key = f'reviewer-run:{run_id}'
        coordinator, child = record.get('coordinator'), record.get('child')
        processes = [item for item in (coordinator, child) if item is not None]
        for item in processes:
            if (not isinstance(item, dict) or type(item.get('pid')) is not int
                    or not isinstance(item.get('start'), str)
                    or type(item.get('uid')) is not int):
                raise store.Refusal('reviewer process receipt is malformed')
        identity = {'target': record.get('target'), 'review_run_id': run_id,
                    'coordinator': coordinator, 'child': child}
        blockers = []
        busy = bool(coordinator and coordinator['pid'] in holders
                    and supervision.process_presence(coordinator))
        child_live = bool(child and supervision.process_presence(child))
        active = record.get('dispatch_state') != 'final' or busy or child_live
        if not active:
            presence, ownership = 'absent', 'owned'
        elif (identity['target'] != supervision.runtime_home_identity(home)
              or not coordinator or any(not all(name in item for name in
                  ('pid', 'start', 'uid', 'ppid', 'pgid', 'sid', 'argv_sha256')) for item in processes)):
            presence, ownership = 'unknown', 'unknown'
            blockers.append({'code': 'reviewer-identity', 'reason': 'active reviewer lacks bound launch identity'})
        else:
            live = [item for item in processes if supervision.process_presence(item)]
            if (not live and record.get('dispatch_state') != 'final' and child is None
                    and record.get('child_state') not in {'not-started', 'absent'}):
                presence, ownership = 'unknown', 'unknown'
                blockers.append({'code': 'reviewer-child-gap', 'reason': 'interrupted reviewer has no proved child outcome'})
            else:
                presence, ownership = ('present' if live or busy else 'absent'), 'owned'
                if busy:
                    accounted.add(coordinator['pid'])
        rows[key] = {'key': key, 'kind': 'reviewer-run', 'identity': identity,
                     'presence': presence, 'ownership': ownership, 'blockers': blockers}
    for key, saved in expected.items():
        if key in rows or saved['kind'] != 'reviewer-run':
            continue
        identity = saved['identity']
        processes = [item for item in (identity['coordinator'], identity['child']) if item]
        live = any(supervision.process_presence(item) for item in processes)
        rows[key] = {**saved, 'presence': 'present' if live else 'absent', 'blockers': []}
    unresolved = set(holders) - accounted
    if unresolved:
        proof = service.get('identity', {}) if service and service.get('ownership') == 'owned' else {}
        owned = {item['pid']: item for item in proof.get('processes', [])}
        proved = (proof.get('run_lock') == lease['identity'] and unresolved <= owned.keys())
        identity = {'lease': lease['identity'], 'processes': [owned[pid] for pid in sorted(unresolved)]
                    if proved else [], 'service_key': service['key'] if proved else None}
        rows['reviewer-lease:legacy'] = {
            'key': 'reviewer-lease:legacy', 'kind': 'reviewer-lease', 'identity': identity,
            'presence': 'present' if proved else 'unknown',
            'ownership': 'owned' if proved else 'unknown',
            'blockers': [] if proved else [{'code': 'reviewer-lease-owner',
                'reason': 'busy reviewer lease lacks coordinator or owned-service proof'}]}
    elif 'reviewer-lease:legacy' in expected:
        saved = expected['reviewer-lease:legacy']
        live = any(supervision.process_presence(item) for item in saved['identity']['processes'])
        rows[saved['key']] = {**saved, 'presence': 'present' if live else 'absent', 'blockers': []}
    return list(rows.values())


def uninstall_quiesce(home, resources, operation):
    from . import supervision, uninstall_state
    for resource in resources:
        if resource['kind'] not in {'reviewer-run', 'reviewer-lease'}:
            continue
        approved = uninstall_state.require_scope(home, operation, resource['key'])
        current = next((row for row in uninstall_inventory(home, expected=[approved])
                        if row['key'] == approved['key']), None)
        if (current is None or current['identity'] != approved['identity']
                or current['ownership'] != 'owned' or current['presence'] == 'unknown'):
            raise store.Refusal('reviewer uninstall identity changed or remains unknown')
        if current['presence'] == 'present':
            identity = approved['identity']
            processes = identity['processes'] if approved['kind'] == 'reviewer-lease' else [
                item for item in (identity['coordinator'], identity['child']) if item]
            supervision.stop_owned_processes(processes, timeout=UNINSTALL_TIMEOUT)
        remaining = next(row for row in uninstall_inventory(home, expected=[approved])
                         if row['key'] == approved['key'])
        if remaining['presence'] != 'absent':
            raise store.Refusal('reviewer remains active; run lock and receipts retained')
        uninstall_state.checkpoint(home, operation, phase='quiescing', completed=approved['key'])
