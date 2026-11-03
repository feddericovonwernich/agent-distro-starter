"""Managed native reviewer lifecycle and durable report handoff."""
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import observability, omp_runtime, reviewer, reviewer_workspaces, store

UNINSTALL_TIMEOUT = 15.0





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
            or request.get('report_path') != str(runtime / 'report-draft.json')
            or (request.get('evidence') is not None
                and request.get('evidence') != {'artifact_root': str(runtime), 'capture_run_id': canonical_id})):
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
    from .uninstall_state import admission
    runtime, request = _native_request(home, review_run_id)
    receipt = reviewer._receipt(home, review_run_id)
    target = receipt.get('target')
    if target is not None and target != runtime_home_identity(home):
        raise store.Refusal('reviewer runtime home identity changed')
    if receipt.get('dispatch_state') != 'reserved':
        raise store.Refusal('reviewer run is already dispatched')
    packet = reviewer.read_packet(home, request['packet_id'])
    binary = omp_runtime.executable(home)
    with admission(home):
        _native_runtime_boundary(home, review_run_id)
        _materialize_reviewer_skill(runtime)
        _verify_reviewer_skill(runtime)
    config = reviewer._read_json(reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")
    model_selector = config.get('review_model')
    from . import installer
    dependencies = installer.read_dependencies(home)
    omp_entry = dependencies.get('omp') if isinstance(dependencies, dict) else None
    omp_version = omp_entry.get('version') if isinstance(omp_entry, dict) else None
    with admission(home):
        receipt = _begin_native_run(
            home, receipt, packet, model_selector=model_selector, omp_version=omp_version)
        with store.lock(store.home(home)):
            receipt = reviewer._receipt(home, review_run_id)
            receipt['supervisor'] = process_identity(os.getpid())
            receipt['child_state'] = 'spawning'
            store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
    remaining = reviewer.deadline_remaining_seconds(receipt['deadline_epoch_ms'])
    if remaining <= 0:
        with admission(home):
            _write_exit(runtime, None, False, True, b'', b'', supervisor=receipt['supervisor'])
        return 124
    with admission(home):
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
    env.update(AGD_HOME=str(store.home(home)), AGD_REVIEW_RUN_ID=review_run_id,
               AGD_CLI=str(Path(__file__).resolve().parents[2] / 'bin' / 'agd'))
    started = time.time()
    process = None
    stdout_stream = stderr_stream = None
    child_identity = None
    timed_out = False
    stdout = bytearray()
    stderr = bytearray()
    code = None
    try:
        with admission(home):
            def open_diagnostic(path):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                return os.fdopen(fd, 'wb')
            stdout_stream = open_diagnostic(runtime / 'stdout')
            stderr_stream = open_diagnostic(runtime / 'stderr')
            process = subprocess.Popen(argv, cwd=runtime, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            try:
                child_identity = process_identity(process.pid)
            except (FileNotFoundError, ProcessLookupError):
                child_identity = None
            with store.lock(store.home(home)):
                receipt = reviewer._receipt(home, review_run_id)
                receipt['child'] = child_identity
                receipt['child_state'] = 'spawned'
                store.atomic_json(
                    reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)

        def drain(stream, buffer, sink):
            for chunk in iter(lambda: stream.read(8192), b''):
                if len(buffer) < 65536:
                    chunk = chunk[:65536 - len(buffer)]
                    buffer.extend(chunk)
                    sink.write(chunk)
                    sink.flush()

        readers = [threading.Thread(target=drain, args=(process.stdout, stdout, stdout_stream), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, stderr, stderr_stream), daemon=True)]
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
        for stream in (stdout_stream, stderr_stream):
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        code = process.returncode
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    for stream in (stdout_stream, stderr_stream):
        if stream is not None and not stream.closed:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
    process_absent = _wait_owned_group_absent(
        child_identity, receipt['deadline_epoch_ms']) if child_identity is not None else False
    with admission(home):
        _write_exit(runtime, code, process_absent, timed_out, stdout, stderr, started,
                     child=child_identity, supervisor=receipt['supervisor'])
        with store.lock(store.home(home)):
            receipt = reviewer._receipt(home, review_run_id)
            receipt['child_state'] = 'exited'
            receipt['exit_code'] = code
            receipt['timed_out'] = timed_out
            store.atomic_json(
                reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
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

def _finalize_with_workspace_disclosure(home, review_run_id, outcome):
    probes = reviewer_workspaces.list_workspaces(home, review_run_id)
    result = reviewer.finalize_run(home, review_run_id, outcome)
    result['probe_workspaces'] = probes
    return result


def _collect_native_result(home, review_run_id):
    receipt = reviewer._receipt(home, review_run_id)
    if receipt.get('dispatch_state') == 'final':
        return _finalize_with_workspace_disclosure(home, review_run_id, receipt.get('outcome', 'exited'))
    receipt = reviewer.recover_submissions(home, review_run_id)
    if receipt.get('dispatch_state') == 'final':
        return _finalize_with_workspace_disclosure(home, review_run_id, receipt.get('outcome', 'exited'))
    if receipt.get('dispatch_state') == 'report-committed':
        return _finalize_with_workspace_disclosure(home, review_run_id, 'exited')
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
    return _finalize_with_workspace_disclosure(home, review_run_id, 'exited')


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

def _prepare_native_request(home, run, packet, config, *, langfuse_query=None):
    packet_id = packet.get('packet_id') if isinstance(packet, dict) else None
    reviewer._hex_id(packet_id)
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, run['review_run_id'])
        if (receipt.get('dispatch_state') != 'reserved'
                or receipt.get('config_snapshot_ref') != run['config_snapshot_ref']
                or receipt.get('instruction_snapshot_ref') != run['instruction_snapshot_ref']
                or receipt.get('packet_id') not in (None, packet_id)):
            raise store.Refusal('reviewer request scope is not reservable')
        receipt['packet_id'] = packet_id
        store.atomic_json(
            reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
    runtime = _native_runtime_boundary(home, run['review_run_id'])
    _materialize_reviewer_skill(runtime)
    _verify_reviewer_skill(runtime)
    request = {
        'schema': 1, 'review_run_id': run['review_run_id'],
        'home': str(store.home(home).resolve()), 'cwd': str(runtime),
        'packet_id': packet['packet_id'],
        'config_snapshot_ref': run['config_snapshot_ref'],
        'instruction_snapshot_ref': run['instruction_snapshot_ref'],
        'report_path': str(runtime / 'report-draft.json'),
        'evidence': {'artifact_root': str(runtime), 'capture_run_id': run['review_run_id']},
        'allowed_scope': {'source_ids': sorted(source['source_id'] for source in packet['sources'])},
    }
    if isinstance(langfuse_query, dict):
        request['langfuse_query'] = {
            key: value for key, value in langfuse_query.items()
            if key not in {'secret_key', 'public_key', 'base_url', 'host'}
        }
    else:
        request['langfuse_query'] = {'available': False}
    reviewer.publish_immutable(runtime / 'request.json', request)
    _write_private_bytes(runtime / 'packet.json', json.dumps(
        packet, sort_keys=True, separators=(',', ':')).encode())
    _write_private_bytes(runtime / 'catalog.json', json.dumps(
        packet.get('catalog', []), sort_keys=True, separators=(',', ':')).encode())
    return runtime


def _reviewer_backend(home, name):
    if name not in {'herdr', 'tmux'}:
        raise store.Refusal('reviewer backend is unsupported')
    return __import__(f'agd_lib.{name}', fromlist=['reviewer_create'])


def _wait_backend_absent(home, backend, endpoint, deadline):
    from . import supervision
    while True:
        try:
            observed = backend.reviewer_inspect(home, endpoint)
        except store.Refusal:
            server = endpoint.get('server', {}).get('process')
            try:
                server_live = supervision.process_presence(server) if isinstance(server, dict) else None
            except store.Refusal:
                server_live = None
            if server_live is False and not os.path.lexists(endpoint.get('socket', '')):
                return True
            observed = {'presence': 'unknown'}
        if observed.get('presence') == 'missing':
            return True
        if reviewer.deadline_remaining_seconds(deadline) <= 0:
            return False
        time.sleep(.1)



def _recover_interrupted_run(home):
    from . import supervision
    receipts = reviewer.paths(home)['receipts']
    for path in sorted(receipts.glob('*.json')):
        record = reviewer._read_json(path)
        if not isinstance(record, dict) or record.get('execution') != 'native-cli':
            continue
        claim = record.get('backend_claim')
        if isinstance(claim, dict) and claim.get('state') not in {'absent', 'allocated'}:
            raise store.Refusal('reviewer backend allocation is unresolved')
        if record.get('dispatch_state') == 'final':
            for identity in (record.get('coordinator'), record.get('child')):
                if isinstance(identity, dict) and supervision.process_presence(identity):
                    raise store.Refusal('terminal reviewer resource is still active')
            endpoint, backend_name = record.get('endpoint'), record.get('backend')
            if endpoint is not None:
                if backend_name not in {'herdr', 'tmux'}:
                    raise store.Refusal('terminal reviewer backend is invalid')
                backend = _reviewer_backend(home, backend_name)
                backend.reviewer_inspect(home, endpoint)
                backend.reviewer_close(home, endpoint)
            continue
        run_id = record.get('review_run_id')
        if not isinstance(run_id, str) or path.stem != run_id:
            raise store.Refusal('reviewer recovery receipt identity is invalid')
        target = record.get('target')
        if target is not None and target != supervision.runtime_home_identity(home):
            raise store.Refusal('reviewer recovery home identity changed')
        coordinator = record.get('coordinator')
        if isinstance(coordinator, dict) and supervision.process_presence(coordinator):
            raise store.Refusal('another reviewer coordinator is still active')
        child = record.get('child')
        if isinstance(child, dict) and supervision.process_presence(child):
            raise store.Refusal('another reviewer child is still active')
        endpoint = record.get('endpoint')
        backend_name = record.get('backend')
        if endpoint is not None:
            if backend_name not in {'herdr', 'tmux'}:
                raise store.Refusal('reviewer recovery backend is invalid')
            backend = _reviewer_backend(home, backend_name)
            observed = backend.reviewer_inspect(home, endpoint)
            backend.reviewer_close(home, endpoint)
        try:
            recovered = _collect_native_result(home, run_id)
        except (store.Refusal, OSError, ValueError, KeyError, TypeError):
            recovered = None
        if (recovered is not None and recovered.get('dispatch_state') == 'final'
                and recovered.get('outcome') == 'exited'):
            return recovered
        if recovered is not None and recovered.get('dispatch_state') == 'final':
            continue
        reviewer.close_run(home, run_id, 'failed', error_code='recovery')
    return None

def _record_backend_diagnostic(home, review_run_id, backend, endpoint):
    try:
        output = backend.reviewer_capture(home, endpoint)
    except (store.Refusal, OSError, ValueError, KeyError, TypeError):
        return
    output = reviewer._sanitize_diagnostic(output)
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        receipt['backend_diagnostic'] = {
            'bytes': len(output),
            'sha256': hashlib.sha256(output).hexdigest(),
            'message': output.decode('utf-8', errors='replace'),
        }
        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)

def run_native_reviewer(home, intent, *, omp_binary='omp'):
    """Reserve and run one bounded native reviewer through the selected backend."""
    from contextlib import ExitStack
    from . import launcher, supervision
    from .uninstall_state import admission
    home = store.home(home)
    with ExitStack() as lifetime:
        with admission(home):
            lifetime.enter_context(reviewer.run_lease(home))
            recovered = _recover_interrupted_run(home)
            if recovered is not None:
                return recovered
            config, started = reviewer.read_config(home), time.time()
            run = reviewer.reserve_run(
                home, intent, config_snapshot=config,
                instruction_snapshot=Path(reviewer.INSTRUCTION_FILE).read_bytes(),
                started_at=started,
                deadline_epoch_ms=int((started + config['limits']['max_runtime_seconds']) * 1000))
            with store.lock(home):
                receipt = reviewer._receipt(home, run['review_run_id'])
                receipt['target'] = supervision.runtime_home_identity(home)
                receipt['coordinator'] = supervision.process_identity(os.getpid())
                receipt['coordinator'].pop('state', None)
                receipt['child_state'] = 'not-started'
                store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
        backend = endpoint = None
        try:
            packet = reviewer.select_packet(
                home, cutoff=started, config_snapshot=config,
                retrospective_review_run_ids=tuple(intent.get('retrospective_review_run_ids', ())),
                deadline_epoch_ms=run['deadline_epoch_ms'])
        except store.Refusal:
            if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
                return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout')
            raise
        if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
            return reviewer.close_run(home, run['review_run_id'], 'failed',
                                     error_code='timeout', packet=packet)
        if not packet['excerpts']:
            return reviewer.close_run(home, run['review_run_id'], 'no-work', packet=packet)
        try:
            env, _, _ = observability.reviewer_environment(home, run['review_run_id'])
            query = observability.reviewer_query_status(home, env)
            runtime = _prepare_native_request(
                home, run, packet, config, langfuse_query=query)
            backend_name = launcher.resolve_backend(home, None)
            backend = _reviewer_backend(home, backend_name)
            command = [str(Path(__file__).resolve().parents[1] / 'agd'),
                       '--home', str(home), 'reviewer', 'execute', run['review_run_id']]
            with admission(home):
                claim = {
                    'schema': 1, 'backend': backend_name,
                    'review_run_id': run['review_run_id'],
                    'target': reviewer._receipt(home, run['review_run_id']).get('target'),
                    'state': 'starting',
                }
                if backend_name == 'herdr':
                    claim['session'] = 'agd-reviewer-' + run['review_run_id'].replace('-', '')
                else:
                    claim['session'] = 'agd'
                with store.lock(home):
                    receipt = reviewer._receipt(home, run['review_run_id'])
                    receipt.update(backend=backend_name, backend_claim=claim)
                    store.atomic_json(
                        reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json",
                        receipt)
                endpoint = backend.reviewer_create(
                    home, run['review_run_id'], command, cwd=runtime, env=env)
                with store.lock(home):
                    receipt = reviewer._receipt(home, run['review_run_id'])
                    claim['state'] = 'allocated'
                    receipt.update(backend=backend_name, backend_claim=claim,
                                   endpoint=endpoint, child_state='spawned')
                    store.atomic_json(
                        reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json",
                        receipt)
        except Exception as error:
            if endpoint is None:
                with store.lock(home):
                    receipt = reviewer._receipt(home, run['review_run_id'])
                    claim = receipt.get('backend_claim')
                    if isinstance(claim, dict):
                        claim['state'] = 'unknown'
                        receipt['backend_claim'] = claim
                        store.atomic_json(
                            reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json",
                            receipt)
            if backend is not None and endpoint is not None:
                _record_backend_diagnostic(home, run['review_run_id'], backend, endpoint)
                try:
                    backend.reviewer_close(home, endpoint)
                except Exception:
                    pass
            return reviewer.close_run(home, run['review_run_id'], 'failed',
                                     error_code='preparation',
                                     diagnostic=str(error).encode(), packet=packet)
        try:
            if not _wait_backend_absent(home, backend, endpoint, run['deadline_epoch_ms']):
                _record_backend_diagnostic(home, run['review_run_id'], backend, endpoint)
                try:
                    backend.reviewer_close(home, endpoint)
                except Exception:
                    pass
                return reviewer.close_run(home, run['review_run_id'], 'unknown',
                                         error_code='timeout', packet=packet)
            _record_backend_diagnostic(home, run['review_run_id'], backend, endpoint)
            backend.reviewer_close(home, endpoint)
            return _collect_native_result(home, run['review_run_id'])
        except Exception as error:
            _record_backend_diagnostic(home, run['review_run_id'], backend, endpoint)
            try:
                backend.reviewer_close(home, endpoint)
            except Exception:
                pass
            return reviewer.close_run(home, run['review_run_id'], 'unknown',
                                     error_code='backend',
                                     diagnostic=str(error).encode(), packet=packet)


def launch_timer(home, *, now=None):
    intent = reviewer.timer_intent(home, now=now)
    return None if intent is None else run_native_reviewer(home, intent)


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


def _uninstall_presence(value):
    return 'absent' if value == 'missing' else value


def uninstall_inventory(home, *, expected=None, service=None):
    from . import supervision
    expected = {row['key']: row for row in expected or [] if row['kind'].startswith('reviewer-')}
    lease = _uninstall_lease(home)
    holders = lease['holders'] if lease else []
    rows, accounted = {}, set()
    receipts = reviewer.paths(home)['receipts']
    for path in sorted(receipts.glob('*.json')):
        record = reviewer._read_json(path)
        if (not isinstance(record, dict) or record.get('schema') not in {1, 2}
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
        endpoint = record.get('endpoint')
        backend_name = record.get('backend')
        claim = record.get('backend_claim')
        if endpoint is None and isinstance(claim, dict) and claim.get('state') not in {'absent'}:
            rows[f'reviewer-backend:{run_id}'] = {
                'key': f'reviewer-backend:{run_id}', 'kind': 'reviewer-backend',
                'identity': {'review_run_id': run_id, 'backend': claim.get('backend'),
                             'claim': claim, 'target': record.get('target')},
                'presence': 'unknown', 'ownership': 'unknown',
                'blockers': [{'code': 'reviewer-backend',
                              'reason': 'reviewer backend allocation is unproven'}]}
        if endpoint is not None and backend_name in {'herdr', 'tmux'}:
            backend_identity = {'review_run_id': run_id, 'backend': backend_name,
                                'endpoint': endpoint, 'target': record.get('target')}
            backend_row = {
                'key': f'reviewer-backend:{run_id}', 'kind': 'reviewer-backend',
                'identity': backend_identity, 'presence': 'unknown',
                'ownership': 'unknown', 'blockers': []}
            try:
                observed = _reviewer_backend(home, backend_name).reviewer_inspect(
                    home, endpoint)
                backend_row.update(
                    presence=_uninstall_presence(observed.get('presence', 'unknown')),
                    ownership='owned')
            except (store.Refusal, OSError, ValueError, KeyError, TypeError) as error:
                backend_row['blockers'] = [{
                    'code': 'reviewer-backend',
                    'reason': 'reviewer backend identity or absence is unproven'}]
            rows[backend_row['key']] = backend_row
    for key, saved in expected.items():
        if key in rows:
            continue
        if saved['kind'] == 'reviewer-backend':
            identity = saved['identity']
            try:
                observed = _reviewer_backend(home, identity['backend']).reviewer_inspect(
                    home, identity['endpoint'])
                rows[key] = {**saved,
                             'presence': _uninstall_presence(observed.get('presence', 'unknown')),
                             'ownership': 'owned', 'blockers': []}
            except (store.Refusal, OSError, ValueError, KeyError, TypeError):
                rows[key] = {**saved, 'presence': 'unknown', 'ownership': 'unknown',
                             'blockers': [{'code': 'reviewer-backend',
                                          'reason': 'reviewer backend identity is unproven'}]}
            continue
        if saved['kind'] != 'reviewer-run':
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
    for row in reviewer_workspaces.uninstall_inventory(home, expected=expected.values()):
        rows[row['key']] = row
    return list(rows.values())


def _close_reviewer_backend(home, identity):
    if (not isinstance(identity, dict) or identity.get('backend') not in {'herdr', 'tmux'}
            or not isinstance(identity.get('endpoint'), dict)):
        raise store.Refusal('reviewer backend identity is malformed')
    _reviewer_backend(home, identity['backend']).reviewer_close(home, identity['endpoint'])


def uninstall_quiesce(home, resources, operation):
    from . import supervision, uninstall_state
    for resource in resources:
        if resource['kind'] not in {'reviewer-run', 'reviewer-lease', 'reviewer-backend'}:
            continue
        approved = uninstall_state.require_scope(home, operation, resource['key'])
        current = next((row for row in uninstall_inventory(home, expected=[approved])
                        if row['key'] == approved['key']), None)
        if (current is None or current['identity'] != approved['identity']
                or current['ownership'] != 'owned' or current['presence'] == 'unknown'):
            raise store.Refusal('reviewer uninstall identity changed or remains unknown')
        if current['presence'] == 'present':
            identity = approved['identity']
            if approved['kind'] == 'reviewer-backend':
                _close_reviewer_backend(home, identity)
            else:
                processes = identity['processes'] if approved['kind'] == 'reviewer-lease' else [
                    item for item in (identity['coordinator'], identity['child']) if item]
                supervision.stop_owned_processes(processes, timeout=UNINSTALL_TIMEOUT)
                if approved['kind'] == 'reviewer-run':
                    receipt = reviewer._receipt(home, identity['review_run_id'])
                    live = [item for item in (receipt.get('coordinator'), receipt.get('child'))
                            if isinstance(item, dict) and supervision.process_presence(item)]
                    if receipt.get('dispatch_state') != 'final' and not live:
                        reviewer.close_run(
                            home, identity['review_run_id'], 'unknown',
                            error_code='uninstall')
        remaining = next(row for row in uninstall_inventory(home, expected=[approved])
                         if row['key'] == approved['key'])
        if remaining['presence'] != 'absent':
            raise store.Refusal('reviewer remains active; run lock and receipts retained')
        uninstall_state.checkpoint(home, operation, phase='quiescing', completed=approved['key'])
    reviewer_workspaces.uninstall_quiesce(home, resources, operation)


def uninstall_remove(home, resources, operation):
    reviewer_workspaces.uninstall_remove(home, resources, operation)
