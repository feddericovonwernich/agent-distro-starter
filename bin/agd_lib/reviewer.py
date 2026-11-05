import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import observability, projects, reviewer_episodes, store, worker_skills
INSTRUCTION_FILE = Path(__file__).resolve().parents[2] / 'lib' / 'agd-reviewer-instructions.md'
DEFAULT_LIMITS = {'max_candidates': 20, 'max_input_bytes': 262144,
                  'max_output_bytes': 65536, 'max_runtime_seconds': 1800}
DEFAULT_CONFIG = {'schema': 2, 'schedule': {'mode': 'calendar', 'timezone': 'UTC', 'calendar': '02:00', 'interval_hours': None},
                  'limits': DEFAULT_LIMITS, 'review_model': None}


def paths(home):
    root = store.home(home); data, state = root / 'data' / 'reviewer', root / 'state' / 'reviewer'
    return {'config': data / 'config.json', 'packets': data / 'packets', 'evidence': data / 'evidence',
            'reports': data / 'reports', 'proposals': data / 'proposals', 'revisions': data / 'revisions',
            'manifests': data / 'manifests', 'triage': data / 'triage', 'intents': state / 'intents',
            'selections': data / 'selections', 'catalogs': data / 'catalogs',
            'inspections': state / 'inspections', 'progress': state / 'progress',
            'selection_details': data / 'selection-details',
            'receipts': state / 'receipts', 'prepared': state / 'prepared',
            'pending': state / 'pending', 'checkpoints': state / 'checkpoints',
            'followups': state / 'followups', 'candidates': state / 'candidates',
            'runtime': state / 'runtime', 'checkpoint': state / 'checkpoint.json',
            'lock': state / 'run.lock'}


def _private(path):
    path = Path(path)
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            identity = os.lstat(ancestor)
            if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode): raise store.Refusal('reviewer path is unsafe')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while True:
        identity = os.lstat(current)
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode): raise store.Refusal('reviewer path is unsafe')
        current.chmod(0o700)
        if current.name == 'reviewer': break
        current = current.parent
    return path


def _json_bytes(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
def _digest(value): return hashlib.sha256(_json_bytes(value)).hexdigest()








def publish_immutable(path, value):
    path, body = Path(path), _json_bytes(value); _private(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream: stream.write(body); stream.flush(); os.fsync(stream.fileno())
        try: os.link(temporary, path)
        except FileExistsError:
            try: existing = _read_regular_bytes(path, require_current_uid=True)
            except (OSError, ValueError) as error: raise store.Refusal('unsafe immutable record') from error
            if existing != body: raise store.Refusal('immutable collision')
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def validate_schedule(schedule):
    if not isinstance(schedule, dict) or set(schedule) != {'mode', 'timezone', 'calendar', 'interval_hours'}: raise store.Refusal('schedule schema')
    if schedule['mode'] == 'calendar':
        if not isinstance(schedule['timezone'], str) or not isinstance(schedule['calendar'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule['calendar']) or schedule['interval_hours'] is not None: raise store.Refusal('calendar schedule')
        try: ZoneInfo(schedule['timezone'])
        except ZoneInfoNotFoundError as error: raise store.Refusal('timezone') from error
    elif schedule['mode'] == 'interval':
        if type(schedule['interval_hours']) is not int or schedule['interval_hours'] <= 0 or schedule['timezone'] is not None or schedule['calendar'] is not None: raise store.Refusal('interval schedule')
    elif schedule['mode'] == 'manual-only':
        if any(schedule[key] is not None for key in ('timezone', 'calendar', 'interval_hours')): raise store.Refusal('manual schedule')
    else: raise store.Refusal('schedule mode')
    return schedule


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'schedule', 'limits', 'review_model'}:
        raise store.Refusal('config schema')
    if value.get('schema') != 2:
        raise store.Refusal('config schema')
    limits = value.get('limits')
    if (not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS)
            or any(type(item) is not int or item <= 0 for item in limits.values())
            or (value['review_model'] is not None
                and (not isinstance(value['review_model'], str) or not value['review_model'].strip()))):
        raise store.Refusal('config values')
    validate_schedule(value['schedule'])
    return json.loads(json.dumps(value))


def normalize_config(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'schedule', 'limits', 'review_model'}:
        raise store.Refusal('config schema')
    if value.get('schema') == 1:
        limits = value.get('limits')
        if not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS) | {'max_model_calls'}:
            raise store.Refusal('config schema')
        value = {**value, 'schema': 2, 'limits': {
            key: item for key, item in limits.items() if key != 'max_model_calls'}}
    return validate_config(value)


def read_config(home):
    path = paths(home)['config']
    try:
        os.lstat(path)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        return normalize_config(_read_json(path))
    except (OSError, ValueError, json.JSONDecodeError, store.Refusal) as error:
        if isinstance(error, store.Refusal):
            raise
        raise store.Refusal('invalid config JSON') from error


def _hex_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value): raise store.Refusal('invalid content id')


def read_packet(home, packet_id):
    _hex_id(packet_id)
    try: value = _read_json(paths(home)['packets'] / f'{packet_id}.json')
    except (OSError, ValueError, json.JSONDecodeError) as error: raise store.Refusal('unknown packet') from error
    if not isinstance(value, dict): raise store.Refusal('invalid packet')
    value['packet_id'] = packet_id
    return value


def read_manifest(home, manifest_id):
    _hex_id(manifest_id)
    try:
        value = _read_json(paths(home)['selections'] / f'{manifest_id}.json')
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise store.Refusal('unknown selection manifest') from error
    if not isinstance(value, dict) or _digest(value) != manifest_id:
        raise store.Refusal('invalid selection manifest')
    return {**value, 'manifest_id': manifest_id}


def _coverage(coverage, bucket, value): coverage['complete'] = False; coverage[bucket].append(value)


def _read_regular_bytes(path, *, require_current_uid=False):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        identity = os.fstat(stream.fileno())
        if not stat.S_ISREG(identity.st_mode) or (require_current_uid and identity.st_uid != os.getuid()):
            raise ValueError('unsafe record')
        return stream.read()


def _read_json(path):
    return json.loads(_read_regular_bytes(path))


_EVIDENCE_SECRET_NAME = re.compile(
    r'(?i)(^|[._-])(credentials?|secrets?|tokens?|passwords?|id_rsa|id_dsa|'
    r'private[_-]?key|known_hosts|\.env)([._-]|$)')
_EVIDENCE_SECRET_TEXT = re.compile(
    r'(?i)\b(api[_-]?key|secret(?:[_-]?key)?|token|password|authorization)'
    r'\s*[:=]\s*["\']?[^,\s}"\']+')


def _evidence_identity(review_run_id, origin, text_sha256):
    return {'review_run_id': review_run_id, 'origin': origin,
            'text_sha256': text_sha256}


def _evidence_integrity(record):
    return _digest({key: value for key, value in record.items() if key != 'integrity_sha256'})


def _evidence_records(home, review_run_id):
    _receipt(home, review_run_id)
    root = paths(home)['evidence'] / review_run_id
    if os.path.lexists(root) and (root.is_symlink() or not root.is_dir()):
        raise store.Refusal('evidence directory is unsafe')
    if not os.path.lexists(root):
        return {}
    records = {}
    try:
        children = sorted(root.iterdir())
    except OSError as error:
        raise store.Refusal('evidence directory is unavailable') from error
    for path in children:
        if path.suffix != '.json' or path.is_symlink() or not path.is_file():
            raise store.Refusal('evidence directory is unsafe')
        source_id = path.stem
        _hex_id(source_id)
        try:
            record = json.loads(_read_regular_bytes(path, require_current_uid=True))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise store.Refusal('invalid supplementary evidence') from error
        if (not isinstance(record, dict) or record.get('schema') != 1
                or record.get('source_id') != source_id
                or record.get('review_run_id') != review_run_id
                or not isinstance(record.get('captured_at'), (int, float))
                or not isinstance(record.get('integrity_sha256'), str)
                or record.get('integrity_sha256') != _evidence_integrity(record)
                or not isinstance(record.get('origin'), dict)
                or not isinstance(record.get('label'), str)
                or not isinstance(record.get('text'), str)
                or not isinstance(record.get('text_sha256'), str)
                or record.get('text_sha256') != hashlib.sha256(record['text'].encode()).hexdigest()
                or record.get('bytes') != len(record['text'].encode())):
            raise store.Refusal('invalid supplementary evidence')
        if _digest(_evidence_identity(review_run_id, record['origin'],
                                      record['text_sha256'])) != source_id:
            raise store.Refusal('modified supplementary evidence')
        records[source_id] = record
    return records


def read_evidence(home, review_run_id):
    try:
        review_run_id = str(uuid.UUID(str(review_run_id)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error
    return {'schema': 1, 'review_run_id': review_run_id,
            'sources': _evidence_records(home, review_run_id)}


def _artifact_path(home, review_run_id, path):
    root = paths(home)['runtime'] / review_run_id
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise store.Refusal('supplementary artifact is outside run') from error
    if not relative.parts or any(part in ('', '.', '..') for part in relative.parts):
        raise store.Refusal('supplementary artifact path')
    if (_EVIDENCE_SECRET_NAME.search(path.name)
            or path.name.lower() in {'.env', 'credentials', 'credentials.json', 'secrets.json',
                                     'token', 'token.txt', 'password', 'id_rsa', 'id_dsa'}):
        raise store.Refusal('supplementary artifact is credential-bearing')
    return root, relative


def _open_owned_artifact(home, review_run_id, path):
    root, relative = _artifact_path(home, review_run_id, path)
    home_root = store.home(home)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
    file_flags = (os.O_RDONLY | os.O_NONBLOCK
                  | getattr(os, 'O_NOFOLLOW', 0))
    home_fd = current_fd = final_fd = None
    try:
        home_fd = os.open(home_root, directory_flags)
        home_stat = os.fstat(home_fd)
        if not stat.S_ISDIR(home_stat.st_mode) or home_stat.st_uid != os.getuid():
            raise store.Refusal('supplementary artifact home is not owned')
        current_fd = home_fd
        home_fd = None
        components = root.relative_to(home_root).parts + relative.parts
        for part in components[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            identity = os.fstat(next_fd)
            if not stat.S_ISDIR(identity.st_mode) or identity.st_uid != os.getuid():
                os.close(next_fd)
                raise store.Refusal('supplementary artifact parent is unsafe')
            os.close(current_fd)
            current_fd = next_fd
        final_fd = os.open(components[-1], file_flags, dir_fd=current_fd)
        identity = os.fstat(final_fd)
        if (not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.getuid()
                or identity.st_nlink != 1):
            raise store.Refusal('supplementary artifact is not owned')
        os.close(current_fd)
        current_fd = None
        return final_fd, relative, identity
    except (OSError, ValueError) as error:
        if isinstance(error, store.Refusal):
            raise
        raise store.Refusal('supplementary artifact is unavailable') from error
    finally:
        if home_fd is not None:
            os.close(home_fd)
        if current_fd is not None:
            os.close(current_fd)
        if final_fd is not None and current_fd is not None:
            os.close(final_fd)


def _read_owned_artifact(home, review_run_id, path, max_bytes, duplicate_sizes):
    fd, relative, initial = _open_owned_artifact(home, review_run_id, path)
    if initial.st_size > max_bytes:
        if initial.st_size not in duplicate_sizes:
            os.close(fd)
            raise store.Refusal('cumulative reviewer input exceeds limit')
        max_bytes = initial.st_size
    stream = None
    try:
        stream = os.fdopen(fd, 'rb')
        body = stream.read(max_bytes + 1)
        final = os.fstat(stream.fileno())
        if (initial.st_dev != final.st_dev or initial.st_ino != final.st_ino
                or initial.st_size != final.st_size or len(body) != final.st_size
                or initial.st_mtime_ns != final.st_mtime_ns or initial.st_ctime_ns != final.st_ctime_ns
                or not stat.S_ISREG(final.st_mode) or final.st_uid != os.getuid()
                or final.st_nlink != 1):
            raise store.Refusal('supplementary artifact changed during capture')
        if len(body) > max_bytes:
            raise store.Refusal('cumulative reviewer input exceeds limit')
        try:
            text = body.decode('utf-8')
        except UnicodeDecodeError as error:
            raise store.Refusal('supplementary artifact is not UTF-8') from error
    except OSError as error:
        raise store.Refusal('supplementary artifact is unavailable') from error
    finally:
        if stream is None:
            os.close(fd)
        elif not stream.closed:
            stream.close()
    text_sha256 = hashlib.sha256(body).hexdigest()
    return relative.as_posix(), body, text, text_sha256, initial


def _capture_evidence_v2(root, run_id, path, label, initial_receipt):
    from . import reviewer_inspection
    deadline = initial_receipt.get('deadline_epoch_ms')
    _check_deadline(deadline)
    with store.lock(root, deadline_epoch_ms=deadline):
        receipt = _receipt(root, run_id)
        reviewer_inspection._active_receipt(root, run_id, deadline)
        if (receipt.get('dispatch_state') != 'begun' or receipt.get('manifest_id') != initial_receipt['manifest_id']
                or receipt.get('deadline_epoch_ms') != deadline):
            raise store.Refusal('review run is not active')
        _, config = reviewer_inspection._manifest_locked(root, receipt)
        used = reviewer_inspection._input_used_locked(root, receipt)
        records = _evidence_records(root, run_id)
        _, relative = _artifact_path(root, run_id, path)
        origin = {'run_id': run_id, 'path': relative.as_posix()}
        duplicate_sizes = {item['bytes'] for item in records.values() if item['origin'] == origin}
        remaining = max(0, config['limits']['max_input_bytes'] - used)
    origin_path, body, text, text_sha256, initial = _read_owned_artifact(root, run_id, path, remaining, duplicate_sizes)
    _check_deadline(deadline)
    source_id = _digest(_evidence_identity(run_id, origin, text_sha256))
    if _EVIDENCE_SECRET_TEXT.search(text):
        raise store.Refusal('supplementary artifact is credential-bearing')
    with store.lock(root, deadline_epoch_ms=deadline):
        current = _receipt(root, run_id)
        reviewer_inspection._active_receipt(root, run_id, deadline)
        if (current.get('dispatch_state') != 'begun' or current.get('manifest_id') != receipt['manifest_id']
                or current.get('deadline_epoch_ms') != deadline
                or current.get('config_snapshot_ref') != receipt.get('config_snapshot_ref')
                or current.get('instruction_snapshot_ref') != receipt.get('instruction_snapshot_ref')):
            raise store.Refusal('review run changed during capture')
        _check_deadline(deadline)
        fd, _, final = _open_owned_artifact(root, run_id, path)
        os.close(fd)
        if ((initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) !=
                (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)):
            raise store.Refusal('supplementary artifact changed before publication')
        records = _evidence_records(root, run_id)
        if source_id in records:
            return records[source_id]
        _, config = reviewer_inspection._manifest_locked(root, current)
        if reviewer_inspection._input_used_locked(root, current) + len(body) > config['limits']['max_input_bytes']:
            raise store.Refusal('cumulative reviewer input exceeds limit')
        record = {'schema': 1, 'source_id': source_id, 'review_run_id': run_id,
                  'origin': {'run_id': run_id, 'path': origin_path}, 'label': label.strip(),
                  'captured_at': time.time(), 'bytes': len(body), 'text': text, 'text_sha256': text_sha256}
        record['integrity_sha256'] = _evidence_integrity(record)
        publish_immutable(paths(root)['evidence'] / run_id / f'{source_id}.json', record)
        return record


def capture_evidence(home, review_run_id, path, label):
    from .uninstall_state import admission
    with admission(home):
        return _capture_evidence(home, review_run_id, path, label)


def _capture_evidence(home, review_run_id, path, label):
    try:
        review_run_id = str(uuid.UUID(str(review_run_id)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error
    if not isinstance(label, str) or not label.strip() or len(label) > 256:
        raise store.Refusal('supplementary evidence label')
    if _EVIDENCE_SECRET_TEXT.search(label):
        raise store.Refusal('supplementary evidence label is credential-bearing')
    root = store.home(home)
    initial_receipt = _receipt(root, review_run_id)
    if initial_receipt.get('manifest_id') is None:
        raise store.Refusal('legacy reviewer runs cannot capture new evidence')
    return _capture_evidence_v2(root, review_run_id, path, label, initial_receipt)


def deadline_remaining_seconds(deadline_epoch_ms):
    return deadline_epoch_ms / 1000 - time.time()


def _check_deadline(deadline_epoch_ms):
    if deadline_epoch_ms is not None and deadline_remaining_seconds(deadline_epoch_ms) <= 0:
        raise store.Refusal('reviewer deadline exceeded')


def _observation_candidates(home, cutoff, coverage, deadline_epoch_ms=None):
    result = []; home = store.home(home)
    _check_deadline(deadline_epoch_ms)
    root = observability._existing_private_directory(home, 'data', 'tasks')
    if root is None: return result
    for directory in sorted(root.iterdir()):
        _check_deadline(deadline_epoch_ms)
        task_file = directory / 'task.json'
        try:
            if observability._existing_private_directory(home, 'data', 'tasks', directory.name) != directory:
                raise ValueError('unsafe task directory')
            task = _read_json(task_file)
            _check_deadline(deadline_epoch_ms)
            task_id = store.validate_id(task.get('id'))
            if task_id != directory.name: raise ValueError('task directory mismatch')
            observations = directory / 'observations'
            if observations.exists() and (observations.is_symlink() or not observations.is_dir()): raise ValueError('unsafe observations')
            for path in sorted(observations.glob('*.json')) if observations.exists() else ():
                _check_deadline(deadline_epoch_ms)
                try:
                    event = _read_json(path)
                    _check_deadline(deadline_epoch_ms)
                except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'path': str(path), 'reason': 'observation-malformed'}); continue
                if event.get('key') != 'runner-exit' or not isinstance(event.get('observed_at'), (int, float)) or event['observed_at'] > cutoff: continue
                incarnation = observability._incarnation(event.get('incarnation'))
                if incarnation == task.get('incarnation') and task.get('worker_alive') is not False: continue
                result.append((event['observed_at'], task, event, directory))
        except store.Refusal:
            if deadline_epoch_ms is not None and deadline_remaining_seconds(deadline_epoch_ms) <= 0: raise
            _coverage(coverage, 'unavailable', {'path': str(task_file), 'reason': 'task-record-malformed'})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            _coverage(coverage, 'unavailable', {'path': str(task_file), 'reason': 'task-record-malformed'})
    _check_deadline(deadline_epoch_ms)
    return sorted(result, key=lambda row: (row[0], row[1].get('id', ''), row[2].get('id', '')))




def _fixed_session(root, task_id, incarnation, binding):
    try:
        task_id = store.validate_id(task_id); incarnation = observability._incarnation(incarnation)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid native session identity') from error
    expected = observability._existing_private_directory(
        root, 'data', 'observability', 'tasks', task_id, 'omp', incarnation)
    if expected is None: raise ValueError('missing native session')
    if binding.get('task_id') != task_id or binding.get('incarnation') != incarnation or binding.get('session_dir') != str(expected): raise ValueError('binding mismatch')
    return expected



def _manifest(home, task_dir, task, coverage, deadline_epoch_ms=None):
    _check_deadline(deadline_epoch_ms)
    path = task_dir / 'worker-skills.json'
    if not path.exists(): return []
    try:
        manifest = worker_skills._manifest(home, task)
        result = []
        for item in manifest['skills']:
            _check_deadline(deadline_epoch_ms)
            snap = Path(item['snapshot'])
            raw = (snap / 'SKILL.md').read_bytes()
            _check_deadline(deadline_epoch_ms)
            result.append({'id': item['id'], 'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
                           'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return result
    except store.Refusal:
        if deadline_epoch_ms is not None and deadline_remaining_seconds(deadline_epoch_ms) <= 0: raise
        _coverage(coverage, 'unavailable', {'task_id': task.get('id'), 'reason': 'assigned-manifest-unavailable'})
        return []
    except (OSError, ValueError, json.JSONDecodeError):
        _coverage(coverage, 'unavailable', {'task_id': task.get('id'), 'reason': 'assigned-manifest-unavailable'})
        return []






def _source_scope(root, source):
    scope = {'home': str(Path(root).resolve()), 'role': source.get('role', 'worker'),
             'session_id': source.get('session_id')}
    if scope['role'] == 'worker':
        scope.update(task_id=source.get('task_id'), incarnation=source.get('incarnation'))
    return scope


def _source_candidate_id(scope):
    """Stable bucket for one authorized native source, independent of physical proof."""
    if not isinstance(scope, dict):
        raise store.Refusal('candidate scope is invalid')
    required = {'home', 'role', 'session_id'}
    expected = required | ({'task_id', 'incarnation'} if scope.get('role') == 'worker' else set())
    if set(scope) != expected or scope['role'] not in ('worker', 'central'):
        raise store.Refusal('candidate scope is invalid')
    return _digest({key: scope[key] for key in sorted(expected)})


def _material_id(scope, row):
    return _digest({'scope': scope, 'message_id': row['message_id'],
                    'text_sha256': hashlib.sha256(row['text'].encode()).hexdigest()})


def _legacy_material_id(row):
    return f"{row['message_id']}:{hashlib.sha256(row['text'].encode()).hexdigest()}"


def _reviewed_material(home, scope):
    """Collect reviewed semantic IDs, retaining migration support for old packets."""
    root = store.home(home)
    candidate_id = _source_candidate_id(scope)
    reviewed = set()
    followups = paths(root)['followups']
    for path in sorted(followups.glob('*.json')) if followups.exists() else ():
        try:
            state = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if state.get('state') != 'reviewed':
            continue
        for candidate in state.get('candidates', ()):
            if candidate.get('candidate_id') != candidate_id:
                continue
            reviewed.update(candidate.get('material_ids', ()))
            reviewed.update(candidate.get('excerpt_ids', ()))
    candidate_path = paths(root)['candidates'] / f'{candidate_id}.json'
    if candidate_path.exists():
        try:
            state = _read_json(candidate_path)
        except (OSError, ValueError, json.JSONDecodeError):
            state = {}
        if state.get('state') == 'reviewed':
            reviewed.update(state.get('reviewed_material_ids', ()))
            reviewed.update(state.get('reviewed_excerpt_ids', ()))
    # Old packets did not persist semantic IDs. Rebuild them from retained packet rows.
    checkpoints = paths(root)['checkpoints']
    for checkpoint in sorted(checkpoints.glob('*.json')) if checkpoints.exists() else ():
        try:
            saved = _read_json(checkpoint)
            packet = read_packet(root, saved.get('packet_id'))
            followup = packet.get('coverage', {}).get('followup_id')
            if followup and material_state(root, followup).get('state') != 'reviewed':
                continue
        except (OSError, ValueError, json.JSONDecodeError, store.Refusal):
            continue
        source_ids = {source['source_id'] for source in packet.get('sources', ())
                      if source.get('kind') == 'native-jsonl' and _source_scope(root, source) == scope}
        for row in packet.get('excerpts', ()):
            if row.get('source_id') not in source_ids:
                continue
            if isinstance(row.get('material_id'), str):
                reviewed.add(row['material_id'])
            else:
                # Reconstruct legacy rows only after their packet source matched this scope.
                reviewed.add(_material_id(scope, row))
    return reviewed





def _completion_candidates(root, cutoff, coverage, deadline_epoch_ms=None):
    for completion in reviewer_episodes.completion_records(root, cutoff, coverage, deadline_epoch_ms):
        source = completion['source']
        native = root / source['path']
        scope = _source_scope(root, source)
        task = None
        task_dir = None
        event = {'incarnation': source.get('incarnation')}
        if source.get('role') == 'worker':
            task_dir = root / 'data' / 'tasks' / source['task_id']
            try:
                task = _read_json(task_dir / 'task.json')
            except (OSError, ValueError, json.JSONDecodeError):
                _coverage(coverage, 'unavailable', {'task_id': source.get('task_id'), 'reason': 'task-record-malformed'})
                continue
        closed_at = max(completion['recorded_at'], completion['final']['timestamp'] / 1000)
        yield {'closed_at': closed_at,
               'sort': (closed_at, source.get('role', ''), source.get('session_id', ''),
                        completion.get('interaction_id', '')),
               'source_scope': scope, 'completion': completion, 'task': task,
               'task_dir': task_dir, 'event': event, 'jsonls': [native]}


def _project_binding(home, task, *, deadline_epoch_ms=None):
    project_id = store.validate_id(task['project'])
    record = (projects.read(home, project_id) if deadline_epoch_ms is None else
              projects.read(home, project_id, deadline_epoch_ms=deadline_epoch_ms))
    if record.get('id') != project_id:
        raise store.Refusal('project registry identity mismatch')
    revision = task.get('start')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40,64}', revision):
        raise store.Refusal('task start revision invalid')
    resolved = (store.git(record['path'], 'rev-parse', '--verify', revision + '^{commit}')
                if deadline_epoch_ms is None else
                store.git(record['path'], 'rev-parse', '--verify', revision + '^{commit}',
                          deadline_epoch_ms=deadline_epoch_ms))
    if resolved != revision:
        raise store.Refusal('task start revision unavailable')
    identity = {key: record[key] for key in ('id', 'path', 'common_dir', 'source')}
    home_id = _read_json(home / 'data' / 'home.json')['home_id']
    return {'home_id': home_id, 'project_id': project_id,
            'identity_sha256': _digest(identity), 'task_revision': revision}


def _validate_project_binding(home, episode, *, deadline_epoch_ms=None):
    """Revalidate the frozen worker project and revision before project retrieval."""
    root = store.home(home)
    task_id = store.validate_id(episode['task_id'])
    directory = observability._existing_private_directory(root, 'data', 'tasks', task_id)
    if directory is None:
        raise store.Refusal('selected task unavailable')
    task = _read_json(directory / 'task.json')
    if task.get('id') != task_id or _project_binding(root, task, deadline_epoch_ms=deadline_epoch_ms) != episode.get('project_binding'):
        raise store.Refusal('selected project identity changed')
    return episode['project_binding']


def _artifact_locator(home, path, kind):
    path = Path(path)
    relative = path.relative_to(home)
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise store.Refusal('unsafe episode artifact')
    raw = _read_regular_bytes(path)
    return {'kind': kind, 'path': relative.as_posix(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}


def _episode_artifacts(home, task, task_dir, incarnation, cutoff, coverage):
    locators = []
    try:
        locators.append(_artifact_locator(home, task_dir / 'brief.md', 'intent'))
    except (OSError, ValueError, store.Refusal):
        _coverage(coverage, 'unavailable', {'task_id': task['id'], 'reason': 'intent-unavailable'})
    folder = task_dir / 'decisions'
    if folder.is_symlink():
        _coverage(coverage, 'unavailable', {'task_id': task['id'], 'reason': 'feedback-unavailable'})
        return locators
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else ():
        if path.name.endswith('.answer.json'):
            continue
        try:
            decision = _read_json(path)
            at = decision.get('resolved_at')
            if (decision.get('incarnation') != incarnation or decision.get('status') != 'resolved'
                    or not isinstance(at, (int, float)) or at > cutoff):
                continue
            answer_name = decision.get('answer')
            if (not isinstance(answer_name, str) or Path(answer_name).is_absolute()
                    or '..' in Path(answer_name).parts or Path(answer_name).parts[:1] != ('decisions',)):
                raise ValueError('answer path')
            answer = task_dir / answer_name
            link = _artifact_locator(home, path, 'feedback-link')
            artifact = _artifact_locator(home, answer, 'feedback')
            value = _read_json(answer)
            if (value.get('task_id') != task['id'] or value.get('incarnation') != incarnation
                    or value.get('resolved_at') != at or not isinstance(value.get('body'), str)):
                raise ValueError('answer provenance')
            artifact.update(link_sha256=link['sha256'], link_path=link['path'], resolved_at=at)
            locators.append(artifact)
        except (OSError, ValueError, TypeError, store.Refusal):
            _coverage(coverage, 'unavailable', {'task_id': task['id'], 'reason': 'feedback-unavailable'})
    return locators


def _progress_path(home, episode_id, basis):
    _hex_id(episode_id); _hex_id(basis)
    return paths(home)['progress'] / episode_id / f'{basis}.json'


def _episode_progress(home, episode_id, basis):
    path = _progress_path(home, episode_id, basis)
    if not path.exists():
        return {'screened': False, 'reflected': False, 'ranges': [], 'hold': None, 'last_assessed': 0}
    try:
        value = _read_json(path)
        if value['episode_id'] != episode_id or value['basis'] != basis:
            raise ValueError('progress identity')
        return value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise store.Refusal('episode progress invalid') from error


def _has_new_exposure(existing, incoming):
    """Compare validated half-open byte ranges within the same versioned source."""
    for item in incoming:
        if not isinstance(item, dict):
            continue
        start, end = item.get('start'), item.get('end')
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            continue
        identity = {key: value for key, value in item.items() if key not in ('start', 'end')}
        version = next((identity[key] for key in ('text_sha256', 'source_sha256', 'version_sha256')
                        if isinstance(identity.get(key), str)
                        and re.fullmatch(r'[0-9a-f]{64}', identity[key])), None)
        if not version or not isinstance(identity.get('message_id', identity.get('source_id')), str):
            continue
        intervals = sorted((row['start'], row['end']) for row in existing
                           if isinstance(row, dict) and type(row.get('start')) is int
                           and type(row.get('end')) is int
                           and {k: v for k, v in row.items() if k not in ('start', 'end')} == identity)
        covered = start
        for left, right in intervals:
            if left > covered:
                break
            covered = max(covered, right)
            if covered >= end:
                break
        if covered < end:
            return True
    return False


def _covered_prefix(ranges, message_id, version):
    spans = sorted((row['start'], row['end']) for row in ranges
                   if isinstance(row, dict) and row.get('message_id') == message_id
                   and row.get('text_sha256') == version
                   and type(row.get('start')) is int and type(row.get('end')) is int
                   and 0 <= row['start'] < row['end'])
    end = 0
    for left, right in spans:
        if left > end:
            break
        end = max(end, right)
    return end


def _record_episode_progress(home, *, episode, assessment, limitations=(), ranges=(), submission_id):
    """Apply a validated successful submission while the caller holds store.lock."""
    if assessment not in ('screened', 'reflected', 'limited') or not isinstance(submission_id, str) or not submission_id:
        raise store.Refusal('invalid episode progress')
    if assessment == 'limited' and not limitations:
        raise store.Refusal('limited assessment requires a reason')
    episode_id, basis = episode['episode_id'], episode['basis']
    state = _episode_progress(home, episode_id, basis)
    applied = state.get('submissions', [])
    if submission_id in applied:
        return state
    state.update(episode_id=episode_id, basis=basis, screened=True,
                 reflected=state['reflected'] or assessment == 'reflected',
                 last_assessed=time.time(), submissions=[*applied, submission_id])
    previous_ranges = state.get('ranges', [])
    advanced = _has_new_exposure(previous_ranges, ranges)
    state['ranges'] = list(previous_ranges) + [item for item in ranges if item not in previous_ranges]
    if assessment == 'limited':
        state['hold'] = {'limits_sha256': episode['limits_sha256'], 'limitations': list(limitations)}
    elif assessment == 'reflected' or advanced:
        state['hold'] = None
    path = _progress_path(home, episode_id, basis)
    _private(path.parent)
    store.atomic_json(path, state)
    return state


def _selection_catalog(home, coverage, deadline_epoch_ms, candidates):
    try:
        items = []
        for skill in worker_skills.catalog(home):
            _check_deadline(deadline_epoch_ms)
            raw = _read_regular_bytes(Path(skill['source']) / 'SKILL.md')
            items.append({'id': skill['id'], 'sha256': hashlib.sha256(raw).hexdigest()})
        assigned = []
        tasks_seen = set()
        for candidate in candidates:
            task = candidate.get('task')
            if task and task['id'] not in tasks_seen:
                tasks_seen.add(task['id'])
                assigned.append({'task_id': task['id'],
                                 'skills': _manifest(home, candidate['task_dir'], task, coverage, deadline_epoch_ms),
                                 'usage': 'unknown'})
        return {'schema': 1, 'skills': items, 'assigned': assigned}
    except (OSError, ValueError, store.Refusal):
        _check_deadline(deadline_epoch_ms)
        _coverage(coverage, 'unavailable', {'reason': 'catalog-unavailable'})
        return None


def _selection_episode(home, item, native, read, config, cutoff, coverage):
    completion = item.get('completion')
    task, event = item.get('task'), item.get('event', {})
    role = completion['source']['role'] if completion else item.get('role', 'worker')
    scope = {'home_id': _read_json(home / 'data' / 'home.json')['home_id'],
             'role': role, 'session_id': read['session_id']}
    if task:
        task_id = store.validate_id(task['id'])
        directory = observability._existing_private_directory(home, 'data', 'tasks', task_id)
        if directory is None or directory != item['task_dir']:
            raise store.Refusal('selected task directory invalid')
        observability._incarnation(event['incarnation'])
        if completion and (completion['source'].get('task_id') != task_id or
                           completion['source'].get('incarnation') != event['incarnation']):
            raise store.Refusal('selected task source mismatch')
        scope.update(task_id=task['id'], incarnation=event['incarnation'])
    if completion:
        scope['interaction_id'] = completion['interaction_id']
    elif role == 'reviewer':
        scope['review_run_id'] = item['review_run_id']
    else:
        scope['exit_observation_id'] = event.get('id')
    episode_id = _digest(scope)
    locators = [{'kind': 'native', 'path': str(native.relative_to(home)),
                 'session_id': read['session_id'], 'projection_sha256': read['material_sha256'],
                 'source_id': completion['source_id'] if completion else None,
                 'completion': {k: v for k, v in completion.items() if k != 'source'} if completion else None}]
    binding = None
    if task:
        binding = _project_binding(home, task)
        locators.extend(_episode_artifacts(home, task, item['task_dir'], event['incarnation'], cutoff, coverage))
        if not completion:
            if (event.get('task') != task_id or event.get('key') != 'runner-exit'
                    or str(uuid.UUID(event['id'])) != event['id']):
                raise store.Refusal('runner exit identity invalid')
            locators.append(_artifact_locator(home, item['task_dir'] / 'observations' /
                                              (event['id'] + '.json'), 'runner-exit'))
    if role == 'reviewer':
        for report_id in item.get('report_ids', ()):
            locators.append(_artifact_locator(home, report_path(home, report_id), 'reviewer-report'))
    basis = _digest({'scope': scope, 'projection_sha256': read['material_sha256'],
                     'boundary': read['boundary'], 'project_binding': binding,
                     'artifacts': locators[1:]})
    limits_sha256 = _digest(config['limits'])
    progress = _episode_progress(home, episode_id, basis)
    if role in ('worker', 'central'):
        material_scope = _source_scope(home, {'role': role, 'session_id': read['session_id'],
                                              'task_id': scope.get('task_id'),
                                              'incarnation': scope.get('incarnation')})
        reviewed = _reviewed_material(home, material_scope)
        progress['reviewed_message_ids'] = [
            row['message_id'] for row in read['projection']
            if _digest({'scope': material_scope, 'message_id': row['message_id'],
                        'text_sha256': row['text_sha256']}) in reviewed]
    episode = {**scope, 'episode_id': episode_id, 'basis': basis,
               'closed_at': item['closed_at'], 'boundary': read['boundary'],
               'projection': read['projection'], 'locators': locators,
               'limits_sha256': limits_sha256,
               'progress': {key: value for key, value in progress.items() if key != 'submissions'},
               'limitations': [row['reason'] for row in read['withheld']]}
    if binding:
        episode['project_binding'] = binding
    else:
        episode['project_bindings'] = []
    return episode


def _bounded_notice(value):
    if isinstance(value, str):
        return value if len(value) <= 256 else value[:256] + '…'
    if isinstance(value, list):
        return [_bounded_notice(item) for item in value[:4]]
    if isinstance(value, dict):
        return {key: _bounded_notice(item) for key, item in value.items()}
    return value


def _selection_disclosures(held, coverage):
    details = {'schema': 1, 'kind': 'disclosures', 'held': held,
               'unavailable': coverage['unavailable'], 'withheld': coverage['withheld']}
    summary = {'complete': coverage['complete'],
               'unavailable_count': len(coverage['unavailable']),
               'withheld_count': len(coverage['withheld']),
               'unavailable': [_bounded_notice(item) for item in coverage['unavailable'][:3]],
               'withheld': [_bounded_notice(item) for item in coverage['withheld'][:3]]}
    return details, summary


def _compact_episode(episode):
    locators = {'schema': 1, 'kind': 'locators', 'episode_id': episode['episode_id'],
                'basis': episode['basis'], 'locators': episode['locators']}
    progress = episode['progress']
    compact = {key: value for key, value in episode.items()
               if key not in ('projection', 'locators', 'progress', 'limitations')}
    compact.update(projection={'sha256': episode['locators'][0]['projection_sha256'],
                               'message_count': len(episode['projection'])},
                   locators=episode['locators'][:4], locators_id=_digest(locators),
                   locator_count=len(episode['locators']),
                   progress={'screened': progress['screened'], 'reflected': progress['reflected'],
                             'last_assessed': progress.get('last_assessed', 0),
                             'hold': _bounded_notice(progress.get('hold')),
                             'range_count': len(progress.get('ranges', [])),
                             'reviewed_message_count': len(progress.get('reviewed_message_ids', []))},
                   limitations=[_bounded_notice(item) for item in episode['limitations'][:4]],
                   limitation_count=len(episode['limitations']))
    return compact, locators


def _read_selection_detail(home, detail_id, kind):
    _hex_id(detail_id)
    try:
        value = _read_json(paths(home)['selection_details'] / f'{detail_id}.json')
        if not isinstance(value, dict) or value.get('kind') != kind or _digest(value) != detail_id:
            raise ValueError('detail identity')
        return value
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise store.Refusal('invalid selection details') from error


def _selection_page(rows, cursor, limit, *, overflow=None):
    if type(cursor) is not int or cursor < 0 or cursor > len(rows) or type(limit) is not int or not 1 <= limit <= 20:
        raise store.Refusal('selection detail cursor invalid')
    result = {'rows': [], 'next_cursor': None, 'total': len(rows)}
    for index in range(cursor, min(cursor + limit, len(rows))):
        result['rows'].append(rows[index])
        result['next_cursor'] = index + 1 if index + 1 < len(rows) else None
        if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) >= 16384:
            result['rows'].pop()
            if not result['rows'] and overflow is not None:
                source = rows[index]
                reference = {**overflow, 'index': index}
                summary = {'artifact_ref': reference}
                if isinstance(source, dict):
                    for key in ('episode_id', 'basis'):
                        if key in source:
                            summary[key] = source[key]
                result['rows'].append(summary)
                result['next_cursor'] = index + 1 if index + 1 < len(rows) else None
                if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) >= 16384:
                    raise store.Refusal('selection detail reference exceeds limit')
            else:
                result['next_cursor'] = index
            break
    if cursor < len(rows) and not result['rows']:
        raise store.Refusal('selection detail row exceeds limit')
    return result


def read_selection_disclosures(home, manifest_id, category, *, cursor=0, limit=20):
    if category not in ('held', 'unavailable', 'withheld'):
        raise store.Refusal('selection disclosure category invalid')
    manifest = read_manifest(home, manifest_id)
    details = _read_selection_detail(home, manifest['details_id'], 'disclosures')
    reference = {'path': str((paths(home)['selection_details'] / f"{manifest['details_id']}.json").absolute()),
                 'detail_id': manifest['details_id'], 'category': category}
    if len(json.dumps(reference, ensure_ascii=False).encode()) > 8192:
        reference['path'] = str(Path(reference['path']).relative_to(store.home(home)))
        reference['path_base'] = 'home'
    return _selection_page(details[category], cursor, limit, overflow=reference)


def read_selection_locators(home, manifest_id, episode_id, *, cursor=0, limit=20):
    _hex_id(episode_id)
    manifest = read_manifest(home, manifest_id)
    episode = next((row for row in manifest['episodes'] if row['episode_id'] == episode_id), None)
    if episode is None:
        raise store.Refusal('episode is not selected')
    details = _read_selection_detail(home, episode['locators_id'], 'locators')
    if details['episode_id'] != episode_id or details['basis'] != episode['basis']:
        raise store.Refusal('selection locator identity mismatch')
    return _selection_page(details['locators'], cursor, limit)


def _selection_schedule(home, actionable, requested):
    """Caller holds store.lock; schedule is saved only after successful admission."""
    path = paths(home)['progress'] / 'schedule.json'
    if path.exists():
        try:
            state = _read_json(path)
            if (state.get('schema') != 1 or type(state.get('sequence')) is not int
                    or state['sequence'] < 0 or not isinstance(state.get('order'), dict)
                    or state.get('turn') not in ('new', 'pending')
                    or any(not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key)
                           or type(value) is not int or not 0 <= value <= state['sequence']
                           for key, value in state['order'].items())):
                raise ValueError('schedule')
        except (OSError, ValueError, TypeError) as error:
            raise store.Refusal('selection schedule invalid') from error
    else:
        state = {'schema': 1, 'sequence': 0, 'order': {}, 'turn': 'new'}
    by_id = {episode['episode_id']: episode for episode in actionable}
    state['order'] = {key: value for key, value in state['order'].items() if key in by_id}
    for episode in sorted(actionable, key=lambda row: (row['closed_at'], row['episode_id'])):
        ident = episode['episode_id']
        if ident not in state['order']:
            state['sequence'] += 1
            state['order'][ident] = state['sequence']
    explicit = [by_id[ident] for ident in requested]
    groups = {'new': [], 'pending': []}
    for episode in actionable:
        if episode['episode_id'] not in requested:
            groups['pending' if episode['progress']['screened'] else 'new'].append(episode)
    for group in groups.values():
        group.sort(key=lambda row: (state['order'][row['episode_id']], row['closed_at'], row['episode_id']))
    ordered = [(episode, None) for episode in explicit]
    turn = state['turn']
    while groups['new'] or groups['pending']:
        both = bool(groups['new'] and groups['pending'])
        chosen = turn if both else ('new' if groups['new'] else 'pending')
        next_turn = ('pending' if chosen == 'new' else 'new') if both else None
        ordered.append((groups[chosen].pop(0), next_turn))
        if both:
            turn = 'pending' if chosen == 'new' else 'new'
    return path, state, ordered


def select_manifest(home, *, cutoff, config_snapshot, retrospective_review_run_ids=(),
                    detail_episode_ids=(), deadline_epoch_ms=None):
    config = validate_config(config_snapshot)
    if (isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or cutoff < 0
            or not isinstance(retrospective_review_run_ids, tuple) or not isinstance(detail_episode_ids, tuple)):
        raise store.Refusal('manifest scope is invalid')
    if config['limits']['max_input_bytes'] < 512:
        raise store.Refusal('selection limit cannot encode required metadata')
    for episode_id in detail_episode_ids:
        _hex_id(episode_id)
    _check_deadline(deadline_epoch_ms)
    root = store.home(home)
    coverage = {'complete': True, 'unavailable': [], 'withheld': []}
    items = list(_completion_candidates(root, cutoff, coverage, deadline_epoch_ms))
    seen_exit = set()
    for observed_at, task, event, task_dir in _observation_candidates(root, cutoff, coverage, deadline_epoch_ms):
        key = (task['id'], event['incarnation'])
        if key in seen_exit:
            continue
        seen_exit.add(key)
        try:
            binding = _read_json(root / 'data' / 'observability' / 'tasks' / key[0] / 'omp' / key[1] / 'binding.json')
            directory = _fixed_session(root, *key, binding)
            files = sorted(directory.glob('*.jsonl'))
            if not files:
                raise ValueError('missing session')
            for native in files:
                items.append({'closed_at': observed_at, 'sort': (observed_at, 'worker~exit', '', event.get('id', '')),
                              'task': task, 'task_dir': task_dir, 'event': event, 'jsonls': [native]})
        except (OSError, ValueError, store.Refusal):
            _check_deadline(deadline_epoch_ms)
            _coverage(coverage, 'unavailable', {'task_id': key[0], 'reason': 'native-jsonl-unavailable'})
    requested = set()
    for rid in retrospective_review_run_ids:
        try:
            if str(uuid.UUID(rid)) != rid or rid in requested:
                raise ValueError('review run ID')
        except (ValueError, TypeError, AttributeError) as error:
            raise store.Refusal('invalid retrospective run') from error
        requested.add(rid)
        receipt = _receipt(root, rid)
        closed = receipt.get('closed_at')
        if receipt.get('dispatch_state') != 'final' or not isinstance(closed, (int, float)) or closed > cutoff:
            raise store.Refusal('retrospective run is not finalized before cutoff')
        directory = observability._existing_private_directory(root, 'data', 'observability', 'reviewer', rid, 'omp')
        if directory is None:
            raise store.Refusal('retrospective native evidence unavailable')
        binding = _read_json(directory.parent / 'binding.json')
        if binding.get('review_run_id') != rid or binding.get('session_dir') != str(directory):
            raise store.Refusal('retrospective binding invalid')
        files = sorted(directory.glob('*.jsonl'))
        if not files:
            raise store.Refusal('retrospective native evidence unavailable')
        for native in files:
            items.append({'closed_at': closed, 'sort': (closed, 'reviewer', rid, native.name),
                          'role': 'reviewer', 'review_run_id': rid, 'jsonls': [native],
                          'report_ids': receipt.get('report_ids', [])})
    candidates = []
    for item in sorted(items, key=lambda row: row['sort']):
        _check_deadline(deadline_epoch_ms)
        native = item['jsonls'][0]
        try:
            native_cutoff = cutoff if item.get('completion') else min(cutoff, item['closed_at'])
            read = reviewer_episodes.read_native(native, native_cutoff, config['limits']['max_input_bytes'],
                                                 deadline_epoch_ms, completion=item.get('completion'), metadata_only=True)
            if read['status'] != 'ready':
                _coverage(coverage, 'unavailable', {'reason': 'native-proof-pending'})
                continue
            if read['limited'] or read['withheld']:
                _coverage(coverage, 'withheld', {'reason': 'native-proof-incomplete', 'session_id': read['session_id']})
            if not read['projection'] and not (item.get('role') == 'reviewer' and item.get('report_ids')):
                continue
            candidates.append(_selection_episode(root, item, native, read, config, cutoff, coverage))
        except (OSError, ValueError, KeyError, TypeError, store.Refusal):
            _check_deadline(deadline_epoch_ms)
            _coverage(coverage, 'unavailable', {'reason': 'native-or-scope-unavailable'})
    proved = {}
    for episode in candidates:
        if episode.get('interaction_id'):
            key = (episode['role'], episode['session_id'], episode.get('task_id'), episode.get('incarnation'))
            proved.setdefault(key, set()).update((row['message_id'], row['text_sha256']) for row in episode['projection'])
    candidates = [episode for episode in candidates
                  if not episode.get('exit_observation_id') or
                  not set((row['message_id'], row['text_sha256']) for row in episode['projection']).issubset(
                      proved.get((episode['role'], episode['session_id'], episode.get('task_id'), episode.get('incarnation')), set()))]
    if set(detail_episode_ids) - {episode['episode_id'] for episode in candidates}:
        raise store.Refusal('detail episode is not eligible')
    if len(set(detail_episode_ids)) != len(detail_episode_ids):
        raise store.Refusal('duplicate detail episode')
    if len(detail_episode_ids) > config['limits']['max_candidates']:
        raise store.Refusal('detail episodes exceed candidate limit')
    task_items = {item['task']['id']: item for item in items if item.get('task')}
    instruction_sha256 = hashlib.sha256(_read_regular_bytes(INSTRUCTION_FILE)).hexdigest()
    with store.lock(root):
        _check_deadline(deadline_epoch_ms)
        held, actionable = [], []
        for episode in candidates:
            reviewed = episode['progress'].get('reviewed_message_ids')
            progress = _episode_progress(root, episode['episode_id'], episode['basis'])
            if reviewed is not None:
                progress['reviewed_message_ids'] = reviewed
            episode['progress'] = {key: value for key, value in progress.items() if key != 'submissions'}
            explicit = episode['episode_id'] in detail_episode_ids
            hold = progress.get('hold')
            if not explicit and hold and hold.get('limits_sha256') == episode['limits_sha256']:
                held.append({'episode_id': episode['episode_id'], 'basis': episode['basis'],
                             'limitations': hold['limitations']})
            elif explicit or not progress.get('reflected'):
                actionable.append(episode)
        schedule_path, schedule, ordered = _selection_schedule(root, actionable, detail_episode_ids)
        selected, locator_bundles = [], []
        catalog = _selection_catalog(root, coverage, deadline_epoch_ms, []) if actionable else None

        def envelope(episodes, catalog_value, current_coverage):
            details, summary = _selection_disclosures(held, current_coverage)
            value = {'schema': 2, 'purpose': 'bounded-behavioral-reflection',
                     'home_id': _read_json(root / 'data' / 'home.json')['home_id'],
                     'cutoff': cutoff, 'config_snapshot_sha256': _digest(config),
                     'catalog_id': _digest(catalog_value) if catalog_value is not None else None,
                     'catalog_bytes': len(_json_bytes(catalog_value)) if catalog_value is not None else 0,
                     'instruction_sha256': instruction_sha256, 'episodes': episodes,
                     'held': [_bounded_notice(item) for item in held[:3]], 'held_count': len(held),
                     'details_id': _digest(details), 'omitted_pending': len(actionable) - len(episodes),
                     'coverage': summary, 'retrospective_review_run_ids': list(retrospective_review_run_ids)}
            return value, details

        def fits(value, catalog_value):
            size = len(json.dumps({**value, 'manifest_id': '0' * 64}, ensure_ascii=False, indent=2).encode())
            catalog_size = len(_json_bytes(catalog_value)) if catalog_value is not None else 0
            return size <= 16384 and size + catalog_size <= config['limits']['max_input_bytes']

        result, disclosures = envelope(selected, catalog, coverage)
        if not fits(result, catalog) and catalog is not None:
            _coverage(coverage, 'unavailable', {'reason': 'catalog-input-limit'})
            catalog = None
            result, disclosures = envelope(selected, catalog, coverage)
        if not fits(result, catalog):
            raise store.Refusal('selection metadata exceeds limit')
        for episode, next_turn in ordered:
            if len(selected) >= config['limits']['max_candidates']:
                break
            _check_deadline(deadline_epoch_ms)
            compact, bundle = _compact_episode(episode)
            trial_coverage = {**coverage, 'unavailable': list(coverage['unavailable']),
                              'withheld': list(coverage['withheld'])}
            trial_catalog = catalog
            task_id = episode.get('task_id')
            if catalog is not None and task_id and not any(row['task_id'] == task_id for row in catalog['assigned']):
                item = task_items[task_id]
                assigned = {'task_id': task_id,
                            'skills': _manifest(root, item['task_dir'], item['task'], trial_coverage, deadline_epoch_ms),
                            'usage': 'unknown'}
                trial_catalog = {**catalog, 'assigned': [*catalog['assigned'], assigned]}
            trial, trial_disclosures = envelope([*selected, compact], trial_catalog, trial_coverage)
            if not fits(trial, trial_catalog) and not selected and trial_catalog is not None:
                _coverage(trial_coverage, 'unavailable', {'reason': 'catalog-input-limit'})
                trial_catalog = None
                trial, trial_disclosures = envelope([*selected, compact], trial_catalog, trial_coverage)
            if not fits(trial, trial_catalog):
                break
            selected.append(compact)
            locator_bundles.append(bundle)
            result, disclosures, catalog, coverage = trial, trial_disclosures, trial_catalog, trial_coverage
            schedule['sequence'] += 1
            schedule['order'][episode['episode_id']] = schedule['sequence']
            if next_turn is not None:
                schedule['turn'] = next_turn
        if actionable and not selected:
            raise store.Refusal('selection limit cannot represent an eligible episode')
        if set(detail_episode_ids) - {episode['episode_id'] for episode in selected}:
            raise store.Refusal('selection limit cannot represent requested detail episodes')
        _check_deadline(deadline_epoch_ms)
        if catalog is not None:
            publish_immutable(paths(root)['catalogs'] / f"{result['catalog_id']}.json", catalog)
        publish_immutable(paths(root)['selection_details'] / f"{result['details_id']}.json", disclosures)
        for bundle in locator_bundles:
            publish_immutable(paths(root)['selection_details'] / f'{_digest(bundle)}.json', bundle)
        manifest_id = _digest(result)
        publish_immutable(paths(root)['selections'] / f'{manifest_id}.json', result)
        _private(schedule_path.parent)
        store.atomic_json(schedule_path, schedule)
        return {**result, 'manifest_id': manifest_id}




def material_state(home, material_basis):
    _hex_id(material_basis)
    path = paths(home)['followups'] / f'{material_basis}.json'
    try:
        state = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise store.Refusal('unknown reviewer material basis') from error
    if state.get('schema') != 1 or state.get('material_basis') != material_basis or state.get('state') not in ('pending', 'reviewed'):
        raise store.Refusal('invalid reviewer material state')
    return state


def mark_material_reviewed(home, material_basis):
    with store.lock(store.home(home)):
        state = material_state(home, material_basis)
        if state['state'] == 'pending':
            state = {**state, 'state': 'reviewed'}
            store.atomic_json(paths(home)['followups'] / f'{material_basis}.json', state)
        for candidate in state.get('candidates', []):
            if isinstance(candidate.get('material_basis'), str):
                path = paths(home)['candidates'] / f"{candidate['candidate_id']}.json"
                try:
                    prior = _read_json(path)
                except (OSError, ValueError, json.JSONDecodeError):
                    prior = {}
                material_ids = sorted(set(prior.get('reviewed_material_ids', ()))
                                      | set(prior.get('reviewed_excerpt_ids', ()))
                                      | set(candidate.get('material_ids', ()))
                                      | set(candidate.get('excerpt_ids', ())))
                store.atomic_json(path, {'schema': 1, 'candidate_id': candidate['candidate_id'],
                                         'state': 'reviewed', 'material_basis': candidate['material_basis'],
                                         'reviewed_material_ids': material_ids,
                                         'reviewed_excerpt_ids': material_ids})
        return state


def _enqueue_intent_locked(home, *, kind, retrospective_review_run_ids=(), detail_episode_ids=()):
    if (kind not in ('default', 'manual', 'timer', 'retrospective')
            or not isinstance(retrospective_review_run_ids, tuple)
            or not isinstance(detail_episode_ids, tuple)):
        raise store.Refusal('invalid reviewer intent')
    if kind == 'retrospective' and not retrospective_review_run_ids: raise store.Refusal('retrospective intent needs explicit closed runs')
    if detail_episode_ids and kind != 'manual':
        raise store.Refusal('detail episodes require a manual run')
    for episode_id in detail_episode_ids:
        _hex_id(episode_id)
    if len(set(detail_episode_ids)) != len(detail_episode_ids):
        raise store.Refusal('duplicate detail episode')
    scope = {'kind': kind, 'retrospective_review_run_ids': list(retrospective_review_run_ids),
             'detail_episode_ids': list(detail_episode_ids)}
    if kind in ('default', 'timer'):
        for path in paths(home)['intents'].glob('*.json') if paths(home)['intents'].exists() else ():
            existing = _read_json(path); pending = paths(home)['pending'] / f'{existing["intent_id"]}.json'
            if {key: existing.get(key) for key in scope} == scope and pending.exists() and _read_json(pending).get('state') == 'pending': return existing
    intent = {'schema': 1, 'intent_id': str(uuid.uuid4()), **scope, 'created_at': time.time()}
    publish_immutable(paths(home)['intents'] / f'{intent["intent_id"]}.json', intent)
    store.atomic_json(paths(home)['pending'] / f'{intent["intent_id"]}.json', {'intent_id': intent['intent_id'], 'state': 'pending'})
    return intent


def enqueue_intent(home, *, kind, retrospective_review_run_ids=(), detail_episode_ids=()):
    from .uninstall_state import admission
    with admission(home):
        with store.lock(store.home(home)):
            return _enqueue_intent_locked(store.home(home), kind=kind,
                                          retrospective_review_run_ids=retrospective_review_run_ids,
                                          detail_episode_ids=detail_episode_ids)


def timer_intent(home, *, now=None):
    """Create a timer intent only when its persisted schedule is eligible."""
    from . import reviewer_timer
    return reviewer_timer.timer_tick(home, now=now)


@contextlib.contextmanager
def run_lease(home):
    lock = paths(home)['lock']; _private(lock.parent)
    with lock.open('a+') as stream:
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise store.Refusal('another reviewer run owns this home') from error
        yield


def reserve_run(home, intent, *, config_snapshot, instruction_snapshot, started_at, deadline_epoch_ms):
    config = validate_config(config_snapshot)
    if (not isinstance(intent, dict) or not isinstance(intent.get('intent_id'), str)
            or not isinstance(instruction_snapshot, bytes)
            or not isinstance(started_at, (int, float)) or type(deadline_epoch_ms) is not int):
        raise store.Refusal('invalid reviewer reservation')
    run_id = str(uuid.uuid4())
    config_ref = _digest(config)
    instruction_ref = hashlib.sha256(instruction_snapshot).hexdigest()
    publish_immutable(paths(home)['manifests'] / f'{config_ref}.json', config)
    publish_immutable(paths(home)['manifests'] / f'{instruction_ref}.json', {
        'text': instruction_snapshot.decode('utf-8'), 'raw_utf8_sha256': instruction_ref})
    run = {'schema': 2, 'purpose': 'background-review', 'review_run_id': run_id,
           'intent': intent, 'config_snapshot_ref': config_ref,
           'instruction_snapshot_ref': instruction_ref, 'started_at': started_at,
           'deadline_epoch_ms': deadline_epoch_ms, 'execution': 'native-cli',
           'backend': None, 'dispatch_state': 'reserved'}
    with store.lock(store.home(home)):
        publish_immutable(paths(home)['receipts'] / f'{run_id}.json', run)
        pending = paths(home)['pending'] / f'{intent["intent_id"]}.json'
        if pending.exists():
            store.atomic_json(pending, {'intent_id': intent['intent_id'], 'state': 'consumed',
                                        'review_run_id': run_id})
    return run




def _sanitize_diagnostic(value):
    if isinstance(value, bytes):
        text = value.decode('utf-8', errors='replace')
    else:
        text = str(value)
    text = re.sub(
        r'(?i)\b(api[_-]?key|secret(?:[_-]?key)?|token|password|authorization)'
        r'\s*[:=]\s*["\']?[^,\s}"\']+',
        lambda match: f'{match.group(1)}=<redacted>',
        text)
    return text[:4096].encode('utf-8', errors='replace')

def close_run(home, review_run_id, outcome, *, error_code=None, diagnostic=b''):
    diagnostic = _sanitize_diagnostic(diagnostic)
    if outcome not in ('no-work', 'failed', 'unknown'): raise store.Refusal('reviewer outcome')
    needs_finalize = False
    with store.lock(store.home(home)):
        receipt = _receipt(home, review_run_id)
        if receipt.get('dispatch_state') == 'final': return receipt
        if (receipt.get('dispatch_state') == 'report-committed' or
                (receipt.get('manifest_id') is not None and
                 any(paths(home)['prepared'].glob(f"{receipt['review_run_id']}-*.json")))):
            needs_finalize = True
        else:
            receipt.update(dispatch_state='final', outcome=outcome, closed_at=time.time())
            if outcome == 'no-work': receipt['state'] = 'no-work'
            if error_code:
                receipt['error'] = {
                    'code': error_code, 'bytes': len(diagnostic),
                    'sha256': hashlib.sha256(diagnostic).hexdigest(),
                    'message': diagnostic.decode('utf-8', errors='replace'),
                }
            store.atomic_json(paths(home)['receipts'] / f"{receipt['review_run_id']}.json", receipt)
    return finalize_run(home, review_run_id, outcome) if needs_finalize else _receipt(home, review_run_id)


def normalize(value):
    if not isinstance(value, str): raise store.Refusal('text')
    return ' '.join(value.split()).casefold()


def proposal_path(home, proposal_id):
    try: proposal_id = str(uuid.UUID(str(proposal_id)))
    except (ValueError, TypeError, AttributeError) as error: raise store.Refusal('proposal ID') from error
    return paths(home)['proposals'] / f'{proposal_id}.json'


def triage_path(home, proposal_id):
    try: proposal_id = str(uuid.UUID(str(proposal_id)))
    except (ValueError, TypeError, AttributeError) as error: raise store.Refusal('proposal ID') from error
    return paths(home)['triage'] / f'{proposal_id}.json'


def report_path(home, report_id):
    _hex_id(report_id)
    return paths(home)['reports'] / f'{report_id}.json'


def read_proposal(home, proposal_id):
    try: return _read_json(proposal_path(home, proposal_id))
    except (OSError, ValueError, json.JSONDecodeError) as error: raise store.Refusal('unknown proposal') from error


def read_triage(home, proposal_id):
    proposal = read_proposal(home, proposal_id); path = triage_path(home, proposal['proposal_id'])
    if not path.exists(): return {'schema': 1, 'proposal_id': proposal['proposal_id'], 'current': 'pending', 'history': []}
    try: return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error: raise store.Refusal('invalid triage') from error


def list_proposals(home):
    folder = paths(home)['proposals']
    return [read_proposal(home, path.stem) for path in sorted(folder.glob('*.json'))] if folder.exists() else []


def _finding_fingerprint(finding, target):
    return _digest([2, target['identity'], target['operation'],
                    normalize(finding['finding']), normalize(finding['applicability'])])




def _receipt(home, review_run_id):
    try: review_run_id = str(uuid.UUID(str(review_run_id)))
    except (ValueError, TypeError, AttributeError) as error: raise store.Refusal('review run ID') from error
    try: return _read_json(paths(home)['receipts'] / f'{review_run_id}.json')
    except (OSError, ValueError, json.JSONDecodeError) as error: raise store.Refusal('unknown reviewer run') from error


def _validate_report(home, receipt, packet, report, supplementary=None):
    if not isinstance(report, dict) or set(report) != {'schema', 'summary', 'findings'} or report['schema'] != 1 or not isinstance(report['summary'], str) or not isinstance(report['findings'], list):
        raise store.Refusal('report schema')
    config = _read_json(paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")
    if len(_json_bytes(report)) > config['limits']['max_output_bytes']: raise store.Refusal('report exceeds output limit')
    admitted = {source['source_id'] for source in packet['sources'] if any(excerpt['source_id'] == source['source_id'] for excerpt in packet['excerpts'])}
    if supplementary is not None:
        if (not isinstance(supplementary, dict)
                or supplementary.get('review_run_id') != receipt.get('review_run_id')
                or not isinstance(supplementary.get('sources'), dict)):
            raise store.Refusal('supplementary evidence')
        admitted.update(supplementary['sources'])
    inventory = {row['id']: row['catalog_sha256'] for row in packet.get('catalog', [])}
    required = {'kind', 'target', 'finding', 'applicability', 'citations', 'counterexamples', 'uncertainty', 'resolution', 'validation', 'validation_limits'}
    for finding in report['findings']:
        if not isinstance(finding, dict) or not required <= set(finding) or finding['kind'] not in ('new-skill', 'existing-skill'):
            raise store.Refusal('finding schema')
        if not all(isinstance(finding[key], str) and finding[key].strip() for key in ('target', 'finding', 'applicability', 'uncertainty', 'resolution', 'validation')):
            raise store.Refusal('finding text')
        if not isinstance(finding['citations'], list) or not finding['citations'] or not set(finding['citations']) <= admitted:
            raise store.Refusal('citation')
        if not isinstance(finding['counterexamples'], list) or not set(finding['counterexamples']) <= admitted or not isinstance(finding['validation_limits'], list) or not all(isinstance(row, str) for row in finding['validation_limits']):
            raise store.Refusal('finding evidence')
        if finding['kind'] == 'existing-skill':
            version = finding.get('target_source_version')
            if set(finding) != required | {'target_source_version'} or not isinstance(version, str) or not version or inventory.get(finding['target']) != version:
                raise store.Refusal('frozen skill target')
        else:
            draft = finding.get('new_skill')
            if (set(finding) != required | {'new_skill'} or not isinstance(draft, dict) or set(draft) != {'id', 'draft', 'triggers', 'scope', 'prerequisites'}
                or not isinstance(draft['id'], str) or not draft['id'].strip() or not isinstance(draft['draft'], str)
                or not isinstance(draft['scope'], str) or not draft['scope'].strip() or not isinstance(draft['triggers'], list)
                or not draft['triggers'] or not all(isinstance(row, str) and row.strip() for row in draft['triggers'])
                or not isinstance(draft['prerequisites'], list) or not all(isinstance(row, str) for row in draft['prerequisites'])
                or not re.match(r'\A---\nname: [^\n]+\ndescription: [^\n]+\n---\n', draft['draft'])):
                raise store.Refusal('new skill draft')
    return report


def _string_list(value, *, unique=False):
    return (isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
            and (not unique or len(set(value)) == len(value)))


def _validate_report_v2(home, receipt, manifest, report, evidence, targets, journal):
    if (not isinstance(report, dict) or set(report) != {'schema', 'summary', 'episode_reviews', 'findings'}
            or type(report['schema']) is not int or report['schema'] != 2
            or not isinstance(report['summary'], str) or not isinstance(report['episode_reviews'], list)
            or not isinstance(report['findings'], list)):
        raise store.Refusal('report schema')
    config = validate_config(_read_json(paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json"))
    if len(_json_bytes(report)) > config['limits']['max_output_bytes']:
        raise store.Refusal('report exceeds output limit')
    episodes = {row['episode_id']: row for row in manifest['episodes']}
    sources = journal['sources']
    supplementary = evidence['sources']
    for source in supplementary.values():
        origin = source.get('origin', {})
        if origin.get('run_id') != receipt['review_run_id']:
            raise store.Refusal('evidence origin')
        _artifact_path(home, receipt['review_run_id'],
                       paths(home)['runtime'] / receipt['review_run_id'] / str(origin.get('path', '')))
        if _EVIDENCE_SECRET_TEXT.search(source['text']):
            raise store.Refusal('evidence contains credentials')
    reviews = {}
    row_keys = {'episode_id', 'assessment', 'intent_sources', 'action_sources', 'outcome_sources', 'limitations'}
    for row in report['episode_reviews']:
        if (not isinstance(row, dict) or set(row) != row_keys or not isinstance(row['episode_id'], str)
                or row['episode_id'] not in episodes
                or row['episode_id'] in reviews or row['assessment'] not in ('screened', 'reflected', 'limited')
                or not _string_list(row['limitations'])):
            raise store.Refusal('episode review schema')
        if row['assessment'] == 'limited' and not row['limitations']:
            raise store.Refusal('limited assessment requires a reason')
        used = []
        artifacts = {}
        cursor = 0
        while cursor is not None:
            page = read_selection_locators(home, manifest['manifest_id'], row['episode_id'], cursor=cursor)
            artifacts.update({item['sha256']: item['kind'] for item in page['rows']
                              if item.get('kind') != 'native' and 'sha256' in item})
            cursor = page['next_cursor']
        for key in ('intent_sources', 'action_sources', 'outcome_sources'):
            ids = row[key]
            if not _string_list(ids, unique=True):
                raise store.Refusal('episode sources')
            for source_id in ids:
                source = sources.get(source_id)
                if source is None or source['provenance'].get('episode_id') != row['episode_id']:
                    raise store.Refusal('episode source scope')
                if source['provenance'].get('view') not in ('native-content', 'native-metadata',
                                                              'langfuse-input', 'langfuse-output', 'langfuse-metadata'):
                    raise store.Refusal('episode source provenance')
                if source['provenance'].get('view') == 'native-content':
                    kind = artifacts.get(source['provenance'].get('record_id'))
                    if kind is not None and (
                            (key == 'intent_sources' and kind != 'intent') or
                            key == 'action_sources' or
                            (key == 'outcome_sources' and kind not in ('feedback', 'runner-exit', 'reviewer-report'))):
                        raise store.Refusal('artifact incompatible with episode role')
            if row['assessment'] == 'reflected' and not any(
                    sources[ident]['provenance']['view'] in ('native-content', 'langfuse-input', 'langfuse-output')
                    for ident in ids):
                raise store.Refusal('reflection requires content in each role')
            used.extend(ids)
        if row['assessment'] in ('limited', 'screened') and not used:
            raise store.Refusal('assessment requires exposed evidence')
        reviews[row['episode_id']] = row
    finding_keys = {'episode_ids', 'target_ref', 'finding', 'proposed_change', 'expected_benefit',
                    'applicability', 'scope_rationale', 'citations', 'counterexamples', 'alternatives',
                    'uncertainty', 'validation', 'validation_limits', 'risks', 'draft'}
    admitted = set(sources) | set(supplementary)
    for finding in report['findings']:
        if not isinstance(finding, dict) or set(finding) != finding_keys:
            raise store.Refusal('finding schema')
        ids = finding['episode_ids']
        if (not _string_list(ids, unique=True) or not ids
                or any(ident not in reviews or reviews[ident]['assessment'] != 'reflected' for ident in ids)):
            raise store.Refusal('finding episode grounding')
        target = targets.get(finding['target_ref']) if isinstance(finding['target_ref'], str) else None
        if target is None:
            raise store.Refusal('target not admitted')
        identity = target['identity']
        if finding['applicability'] != identity['scope']:
            raise store.Refusal('target applicability mismatch')
        if identity['scope'] == 'project':
            for ident in ids:
                episode = episodes[ident]
                bindings = [episode.get('project_binding')] + episode.get('project_bindings', [])
                if not any(isinstance(binding, dict)
                           and binding.get('project_id') == identity['context']['project_id']
                           and binding.get('identity_sha256') == identity['context']['identity_sha256']
                           for binding in bindings):
                    raise store.Refusal('target project not linked to finding episode')
        if not all(isinstance(finding[key], str) and finding[key].strip() for key in
                   ('finding', 'proposed_change', 'expected_benefit', 'applicability', 'scope_rationale',
                    'alternatives', 'uncertainty', 'validation')):
            raise store.Refusal('finding text')
        for key in ('citations', 'counterexamples'):
            if not _string_list(finding[key], unique=True) or not set(finding[key]) <= admitted:
                raise store.Refusal('finding evidence')
        if not finding['citations'] or not _string_list(finding['risks']) or not _string_list(finding['validation_limits']):
            raise store.Refusal('finding evidence or risks')
        if finding['draft'] is not None and not isinstance(finding['draft'], str):
            raise store.Refusal('finding draft')
        if (finding['draft'] is not None and target['operation'] == 'create'
                and identity['kind'] == 'file' and identity['locator'].endswith('/SKILL.md')
                and not re.match(r'\A---\nname: [^\n]+\ndescription: [^\n]+\n---\n', finding['draft'])):
            raise store.Refusal('skill draft frontmatter')
    return report


def _validate_v2_sources(home, receipt, manifest, report, journal):
    """Reprove issued episode content against selected native scope; caller holds validation watches."""
    from . import reviewer_inspection as inspection
    assessed = {row['episode_id'] for row in report['episode_reviews']}
    episodes = {row['episode_id']: row for row in manifest['episodes']}
    native = {}
    ranges = {ident: [] for ident in assessed}
    overlap = {}
    for source_id, source in journal['sources'].items():
        provenance = source['provenance']
        eid = provenance.get('episode_id')
        if eid is None:
            continue
        if eid not in episodes:
            raise store.Refusal('source episode outside selection')
        episode = episodes[eid]
        if eid not in native:
            if episode.get('project_binding'):
                inspection._watch(home, projects.record(home, episode['project_binding']['project_id']))
                inspection._watch(home, home / 'data' / 'tasks' / episode['task_id'] / 'task.json')
                _validate_project_binding(home, episode, deadline_epoch_ms=receipt['deadline_epoch_ms'])
            native[eid] = inspection._native(home, receipt, manifest, episode)
        locator, locators, path, completion, meta = native[eid]
        if completion is None:
            for item in locators:
                if item.get('kind') == 'runner-exit':
                    inspection._artifact(home, item, offset=0, deadline=receipt['deadline_epoch_ms'])
        view, record_id = provenance.get('view'), provenance.get('record_id')
        if view == 'native-content':
            if provenance.get('projection_sha256') != meta['material_sha256']:
                raise store.Refusal('native source projection mismatch')
            start, end = provenance['start'], provenance['end']
            artifact = next((item for item in locators if item.get('kind') != 'native'
                             and item.get('sha256') == record_id), None)
            if artifact:
                scanned = inspection._artifact(home, artifact, offset=start, deadline=receipt['deadline_epoch_ms'])
                expected = scanned['text'].encode()[:end - start]
                if (scanned['sha256'] != provenance.get('version_sha256') or
                        expected != source['text'].encode() or scanned['end'] != end or
                        provenance['complete'] != (end == scanned['size'])):
                    raise store.Refusal('artifact range changed')
                stable = {'episode_id': eid, 'artifact': artifact['kind'], 'sha256': artifact['sha256'],
                          'start': start, 'end': end}
            else:
                expected_row = next((item for item in meta['projection'] if item['message_id'] == record_id), None)
                if not expected_row or expected_row['text_sha256'] != provenance.get('version_sha256'):
                    raise store.Refusal('native range outside projection')
                read = reviewer_episodes.read_native(path, manifest['cutoff'], inspection.PAGE_BYTES,
                                                     receipt['deadline_epoch_ms'], completion=completion,
                                                     content_range=(record_id, start, inspection.PAGE_BYTES),
                                                     reject_text=_EVIDENCE_SECRET_TEXT.search)
                result = next((item for item in read['rows'] if item['message_id'] == record_id), None)
                if (result is None or result['text_sha256'] != provenance['version_sha256']
                        or result['start'] != start or result['end'] != end or result['text'] != source['text']
                        or provenance['complete'] != (end == result['size'])):
                    raise store.Refusal('native range changed')
                if eid in ranges:
                    ranges[eid].append({'message_id': record_id, 'text_sha256': result['text_sha256'],
                                        'start': start, 'end': end, 'size': result['size']})
                stable = {'episode_id': eid, 'message_id': record_id, 'text_sha256': result['text_sha256'],
                          'start': start, 'end': end}
            overlap[source_id] = stable
        elif view == 'native-metadata':
            if provenance.get('version_sha256') != meta['material_sha256']:
                raise store.Refusal('native metadata version changed')
            artifacts = [{key: item[key] for key in ('kind', 'sha256', 'size') if key in item}
                         for item in locators if item.get('kind') != 'native' and 'sha256' in item]
            expected = [*meta['projection'], *artifacts][provenance['start']:provenance['end']]
            try:
                disclosed = [{key: value for key, value in item.items() if key != 'resume_cursor'}
                             for item in json.loads(source['text'])]
            except (TypeError, ValueError, AttributeError) as error:
                raise store.Refusal('native metadata invalid') from error
            if disclosed != expected:
                raise store.Refusal('native metadata outside selection')
            overlap[source_id] = {'episode_id': eid, 'view': view, 'version_sha256': meta['material_sha256'],
                                  'start': provenance['start'], 'end': provenance['end']}
        elif view in ('langfuse-metadata', 'langfuse-input', 'langfuse-output'):
            scope = provenance.get('query_scope', {})
            if scope.get('session_id') != episode['session_id'] or scope.get('window') != inspection._telemetry_window(home, receipt, manifest, episode, meta):
                raise store.Refusal('telemetry scope mismatch')
            if view == 'langfuse-metadata':
                if provenance.get('version_sha256') != _digest({'query_scope': scope, 'projection': meta['material_sha256']}):
                    raise store.Refusal('telemetry metadata version mismatch')
                try:
                    observations = json.loads(source['text'])
                    if (not isinstance(observations, list)
                            or len(observations) != provenance['end'] - provenance['start']):
                        raise ValueError('metadata page')
                    for observation in observations:
                        if (not isinstance(observation, dict) or not isinstance(observation.get('id'), str)
                                or observation.get('sessionId') != episode['session_id']):
                            raise ValueError('observation scope')
                        start = reviewer_episodes._epoch(observation.get('startTime'))
                        end = reviewer_episodes._epoch(observation.get('endTime'))
                        if not scope['window']['start'] <= start <= end <= scope['window']['end']:
                            raise ValueError('observation window')
                        digest = _digest(observation)
                        if eid in ranges:
                            ranges[eid].append({'provider': 'langfuse', 'record_id': observation['id'],
                                                'metadata_sha256': digest, 'source_id': observation['id'],
                                                'version_sha256': digest, 'start': 0, 'end': 1})
                except (TypeError, ValueError, KeyError, OverflowError) as error:
                    raise store.Refusal('telemetry metadata invalid') from error
            if view != 'langfuse-metadata':
                matching = []
                for metadata in journal['sources'].values():
                    p = metadata.get('provenance', {})
                    if p.get('view') == 'langfuse-metadata' and p.get('episode_id') == eid and p.get('query_scope') == scope:
                        matching.extend(item for item in json.loads(metadata['text']) if item.get('id') == record_id)
                if len(matching) != 1:
                    raise store.Refusal('telemetry record not admitted')
                overlap[source_id] = {'episode_id': eid, 'provider': 'langfuse', 'record_id': record_id,
                                      'metadata_sha256': _digest(matching[0]), 'view': view,
                                      'version_sha256': provenance['version_sha256'],
                                      'start': provenance['start'], 'end': provenance['end']}
        else:
            raise store.Refusal('unsupported episode provenance')
    return ranges, overlap

def _prepared_path(home, run_id, request_id):
    return paths(home)['prepared'] / f'{run_id}-{request_id}.json'


def _apply_prepared_locked(home, receipt, prepared):
    for row in prepared['revisions']:
        publish_immutable(paths(home)['revisions'] / f"{row['revision_hash']}.json", row)
    for planned in prepared['proposals']:
        path = proposal_path(home, planned['proposal_id'])
        current = _read_json(path) if path.exists() else planned
        known = {row['revision_hash'] for row in current['revisions']}
        current['revisions'].extend(row for row in planned['revisions'] if row['revision_hash'] not in known)
        current['head_revision'] = planned['head_revision']
        store.atomic_json(path, current)
    publish_immutable(report_path(home, prepared['result']['report_id']), prepared['record'])
    request_id, result = prepared['request_id'], prepared['result']
    receipt.setdefault('report_requests', {})[request_id] = prepared['input_digest']
    receipt.setdefault('report_results', {})[request_id] = result
    receipt['report_ids'] = list(dict.fromkeys([*receipt.get('report_ids', []), result['report_id']]))
    receipt['proposal_ids'] = sorted(set([*receipt.get('proposal_ids', []), *result['proposal_ids']]))
    receipt['output_bytes'] = sum(_read_json(path)['output_bytes'] for path in paths(home)['prepared'].glob(f"{receipt['review_run_id']}-*.json"))
    receipt['dispatch_state'] = 'report-committed'
    store.atomic_json(paths(home)['receipts'] / f"{receipt['review_run_id']}.json", receipt)
    return result


def _evidence_overlap(left, right):
    for first in left:
        for second in right:
            if (first.get('start') is not None and second.get('start') is not None
                    and {k: v for k, v in first.items() if k not in ('start', 'end')} ==
                    {k: v for k, v in second.items() if k not in ('start', 'end')}
                    and first['start'] < second['end'] and second['start'] < first['end']):
                return True
            if first == second:
                return True
    return False


def _target_display(target):
    authority = target['identity']['authority']
    routing = ('Upstream routing or owner confirmation required; no local edit authority.'
               if authority['status'] in ('external', 'unknown')
               else 'Verified destination; acceptance grants no edit authority.')
    return {'identity': target['identity'], 'operation': target['operation'],
            'guard': target['guard'], 'surface': target['surface'], 'routing': routing}


def _submit_report_v2(home, receipt, request_id, report):
    from . import reviewer_inspection as inspection
    from .uninstall_state import admission
    home = store.home(home)
    run_id, deadline = receipt['review_run_id'], receipt.get('deadline_epoch_ms')
    with admission(home):
        journal = inspection.read_journal(home, run_id)
        manifest = read_manifest(home, receipt['manifest_id'])
        with inspection._validation_scope() as watched:
            for target in journal['targets'].values():
                inspection._validate_target_context(home, manifest, target, deadline=deadline)
            evidence = read_evidence(home, run_id)
            for source_id in evidence['sources']:
                inspection._watch(home, paths(home)['evidence'] / run_id / (source_id + '.json'))
            _validate_report_v2(home, receipt, manifest, report, evidence, journal['targets'], journal)
            ranges, overlap = _validate_v2_sources(home, receipt, manifest, report, journal)
            with store.lock(home, deadline_epoch_ms=deadline):
                current = _receipt(home, run_id)
                if (current.get('dispatch_state') != 'begun'
                        or any(current.get(key) != receipt.get(key) for key in
                               ('manifest_id', 'deadline_epoch_ms', 'config_snapshot_ref', 'instruction_snapshot_ref'))):
                    raise store.Refusal('review run changed during report validation')
                _check_deadline(deadline)
                inspection._check_watches(watched)
                if (inspection._read_journal_locked(home, run_id)['journal_sha256'] != journal['journal_sha256']
                        or read_evidence(home, run_id) != evidence):
                    raise store.Refusal('review evidence changed during validation')
                if list(paths(home)['prepared'].glob(f'{run_id}-*.json')):
                    raise store.Refusal('report submission already prepared')
                report_id = _digest({'review_run_id': run_id, 'request_id': request_id, 'report': report})
                proposals, revisions, proposal_ids, displays = list_proposals(home), [], [], []
                for finding in report['findings']:
                    target = journal['targets'][finding['target_ref']]
                    display = _target_display(target)
                    descriptors = [overlap.get(ident, {'source_id': ident}) for ident in finding['citations']]
                    fingerprint = _finding_fingerprint(finding, target)
                    matches = [item for item in proposals if item.get('schema') == 2 and item['fingerprint'] == fingerprint]
                    overlapping = [item for item in matches if any(
                        _evidence_overlap(row.get('evidence_overlap', []), descriptors)
                        for row in item['revisions'])]
                    proposal = overlapping[0] if len(overlapping) == 1 else None
                    if proposal is None:
                        proposal = {'schema': 2, 'proposal_id': str(uuid.uuid4()), 'fingerprint': fingerprint,
                                    'head_revision': None, 'revisions': [], 'uncertain_distinct': bool(matches)}
                        proposals.append(proposal)
                    revision_hash = _digest([2, proposal['proposal_id'], finding, target, descriptors])
                    if revision_hash not in {row['revision_hash'] for row in proposal['revisions']}:
                        revision = {'schema': 2, 'proposal_id': proposal['proposal_id'], 'revision_hash': revision_hash,
                                    'report_id': report_id, 'manifest_id': manifest['manifest_id'],
                                    'inspection_sha256': journal['journal_sha256'], 'basis_source_ids': sorted(finding['citations']),
                                    'evidence_overlap': descriptors, 'proposed_change': finding, 'target': display,
                                    'observer': {'model': receipt['model'], 'omp_version': receipt.get('omp_version'),
                                                 'instruction_snapshot_ref': receipt['instruction_snapshot_ref']},
                                    'resurfaced': bool(proposal['revisions'])}
                        revisions.append(revision)
                        proposal['revisions'].append(revision)
                        proposal['head_revision'] = revision_hash
                    proposal_ids.append(proposal['proposal_id'])
                    displays.append(display)
                result = {'report_id': report_id, 'proposal_ids': proposal_ids}
                record = {'schema': 2, 'review_run_id': run_id, 'request_id': request_id, 'report': report,
                          'manifest_id': manifest['manifest_id'], 'inspection_sha256': journal['journal_sha256'],
                          'targets': displays, 'proposal_ids': proposal_ids,
                          'revision_ids': [row['revision_hash'] for row in revisions],
                          'observer': {'model': receipt['model'], 'omp_version': receipt.get('omp_version'),
                                       'instruction_snapshot_ref': receipt['instruction_snapshot_ref']}}
                output_bytes = len(_json_bytes(record))
                config = validate_config(_read_json(paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json"))
                if output_bytes > config['limits']['max_output_bytes']:
                    raise store.Refusal('report output exceeds limit')
                by_id = {episode['episode_id']: episode for episode in manifest['episodes']}
                coverage = [{'episode': by_id[row['episode_id']], 'assessment': row['assessment'],
                             'limitations': row['limitations'], 'ranges': ranges[row['episode_id']]}
                            for row in report['episode_reviews']]
                prepared = {'schema': 2, 'run_id': run_id, 'request_id': request_id,
                            'input_digest': _digest(report), 'result': result, 'record': record,
                            'output_bytes': output_bytes, 'revisions': revisions,
                            'proposals': [item for item in proposals if item['proposal_id'] in proposal_ids],
                            'journal_sha256': journal['journal_sha256'],
                            'evidence_sha256': _digest(evidence), 'coverage': coverage}
                publish_immutable(_prepared_path(home, run_id, request_id), prepared)
                return _apply_prepared_locked(home, current, prepared)


def submit_report(home, review_run_id, request_id, report):
    try: request_id = str(uuid.UUID(str(request_id)))
    except (ValueError, TypeError, AttributeError) as error: raise store.Refusal('request ID') from error
    receipt = _receipt(home, review_run_id)
    if receipt.get('dispatch_state') == 'final' and request_id in receipt.get('report_requests', {}):
        if receipt['report_requests'][request_id] != _digest(report): raise store.Refusal('conflicting request reuse')
        return receipt['report_results'][request_id]
    if receipt.get('manifest_id') is not None:
        path = _prepared_path(home, receipt['review_run_id'], request_id)
        if path.exists():
            prepared = _read_json(path)
            if prepared.get('input_digest') != _digest(report):
                raise store.Refusal('conflicting request reuse')
            recover_submissions(home, review_run_id)
            return _receipt(home, review_run_id)['report_results'][request_id]
        if receipt.get('dispatch_state') != 'begun':
            raise store.Refusal('reviewer report already frozen')
        return _submit_report_v2(home, receipt, request_id, report)
    path = _prepared_path(home, receipt['review_run_id'], request_id)
    if not path.exists():
        raise store.Refusal('legacy reviewer runs cannot accept new reports')
    prepared = _read_json(path)
    if prepared.get('input_digest') != _digest(report):
        raise store.Refusal('conflicting request reuse')
    recover_submissions(home, review_run_id)
    return _receipt(home, review_run_id)['report_results'][request_id]

def recover_submissions(home, review_run_id):
    with store.lock(store.home(home)):
        receipt = _receipt(home, review_run_id)
        for path in paths(home)['prepared'].glob(f"{receipt['review_run_id']}-*.json"):
            prepared = _read_json(path)
            if prepared['request_id'] not in receipt.get('report_requests', {}):
                if prepared.get('schema') == 2:
                    if (receipt.get('manifest_id') != prepared['record'].get('manifest_id')
                            or prepared.get('run_id') != receipt['review_run_id']
                            or prepared.get('input_digest') != _digest(prepared['record']['report'])
                            or receipt.get('dispatch_state') != 'begun'):
                        raise store.Refusal('prepared reviewer submission invalid')
                    from . import reviewer_inspection as inspection
                    run_id = receipt['review_run_id']
                    frozen = {'schema': 1, 'run_id': run_id, 'manifest_id': receipt['manifest_id'],
                              'sources': inspection._read_sources(home, receipt),
                              'cursors': inspection._records(home, run_id, 'cursors'),
                              'targets': inspection._records(home, run_id, 'targets')}
                    if (_digest(frozen) != prepared['journal_sha256']
                            or _digest(read_evidence(home, run_id)) != prepared['evidence_sha256']):
                        raise store.Refusal('prepared reviewer evidence changed')
                else:
                    packet = read_packet(home, receipt.get('packet_id'))
                    _validate_report(
                        home, receipt, packet, prepared['record']['report'],
                        supplementary=read_evidence(home, receipt['review_run_id']))
                receipt = _receipt(home, review_run_id)
                _apply_prepared_locked(home, receipt, prepared)
    return _receipt(home, review_run_id)


def dispose(home, proposal_id, status, reason, replacement_proposal_id, operator):
    if status not in {'accepted', 'rejected', 'deferred', 'superseded'} or (reason is not None and not isinstance(reason, str)) or not isinstance(operator, dict) or type(operator.get('uid')) is not int or not isinstance(operator.get('identity'), str):
        raise store.Refusal('disposition')
    with store.lock(store.home(home)):
        proposal = read_proposal(home, proposal_id); replacement = None
        if replacement_proposal_id is not None:
            candidate, separator, revision = str(replacement_proposal_id).partition('@'); row = read_proposal(home, candidate)
            if separator and revision not in {item['revision_hash'] for item in row['revisions']}: raise store.Refusal('unknown revision')
            replacement = str(replacement_proposal_id)
        if (status == 'superseded') != bool(replacement): raise store.Refusal('replacement')
        prior = read_triage(home, proposal['proposal_id'])
        item = {'status': status, 'reason': reason, 'replacement': replacement, 'operator': operator, 'at': time.time(), 'basis_revision': proposal['head_revision']}
        store.atomic_json(triage_path(home, proposal['proposal_id']), {'schema': 1, 'proposal_id': proposal['proposal_id'], 'current': status, 'history': [*prior['history'], item]})
    return read_triage(home, proposal_id)


def _review_event(home, receipt):
    proposal_ids = sorted(set(receipt.get('proposal_ids', [])))
    if not proposal_ids: return None
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _digest([receipt['review_run_id'], receipt.get('report_ids', []), proposal_ids])))
    pending, handled = store.home(home) / 'state' / 'events' / f'{event_id}.json', store.home(home) / 'state' / 'events-handled' / f'{event_id}.json'
    if not pending.exists() and not handled.exists():
        kind = 'background-review' if receipt.get('manifest_id') is not None else 'background-skill-review'
        details = {'purpose': kind, 'review_run_id': receipt['review_run_id'],
                   'report_ids': receipt.get('report_ids', []), 'proposal_ids': proposal_ids,
                   'report_count': len(receipt.get('report_ids', [])), 'proposal_count': len(proposal_ids)}
        if receipt.get('manifest_id') is not None:
            details['manifest_id'] = receipt['manifest_id']
            details['targets'] = [target for report_id in receipt.get('report_ids', [])
                                  for target in _read_json(report_path(home, report_id)).get('targets', [])]
        store.atomic_json(pending, {'schema': 1, 'id': event_id, 'kind': kind,
                                    'key': receipt['review_run_id'], 'observed_at': time.time(), 'details': details})
    return event_id


def _commit_v2_coverage_locked(home, receipt):
    for path in paths(home)['prepared'].glob(f"{receipt['review_run_id']}-*.json"):
        prepared = _read_json(path)
        if prepared.get('schema') != 2:
            continue
        for item in prepared['coverage']:
            episode = item['episode']
            state = _record_episode_progress(home, episode=episode, assessment=item['assessment'],
                                             limitations=item['limitations'], ranges=item['ranges'],
                                             submission_id=f"{receipt['review_run_id']}:{prepared['request_id']}")
            if episode.get('role') not in ('worker', 'central'):
                continue
            scope = _source_scope(home, episode)
            candidate_id = _source_candidate_id(scope)
            candidate_path = paths(home)['candidates'] / f'{candidate_id}.json'
            prior = _read_json(candidate_path) if candidate_path.exists() else {}
            reviewed = set(prior.get('reviewed_material_ids', ())) | set(prior.get('reviewed_excerpt_ids', ()))
            for span in state['ranges']:
                if (not isinstance(span, dict) or type(span.get('size')) is not int
                        or not isinstance(span.get('message_id'), str)
                        or not isinstance(span.get('text_sha256'), str)):
                    continue
                if _covered_prefix(state['ranges'], span['message_id'], span['text_sha256']) >= span['size']:
                    reviewed.add(_digest({'scope': scope, 'message_id': span['message_id'],
                                          'text_sha256': span['text_sha256']}))
            if reviewed:
                store.atomic_json(candidate_path, {**prior, 'schema': 1, 'candidate_id': candidate_id,
                                                  'state': 'reviewed',
                                                  'reviewed_material_ids': sorted(reviewed),
                                                  'reviewed_excerpt_ids': sorted(reviewed)})


def _finalize_v2(home, review_run_id, outcome):
    with store.lock(store.home(home)):
        receipt = _receipt(home, review_run_id)
        if receipt.get('dispatch_state') == 'final':
            outcome = receipt.get('outcome')
        elif receipt.get('finalization_outcome') is not None:
            outcome = receipt['finalization_outcome']
        else:
            receipt['finalization_outcome'] = outcome
            store.atomic_json(paths(home)['receipts'] / f'{review_run_id}.json', receipt)
        if outcome == 'exited':
            _commit_v2_coverage_locked(home, receipt)
            event_id = _review_event(home, receipt)
        else:
            event_id = None
        if receipt.get('dispatch_state') != 'final':
            receipt.update(dispatch_state='final', outcome=outcome, state=outcome,
                           closed_at=time.time(), event_id=event_id)
            store.atomic_json(paths(home)['receipts'] / f'{review_run_id}.json', receipt)
    return _receipt(home, review_run_id)


def finalize_run(home, review_run_id, outcome):
    if not isinstance(outcome, str) or not outcome: raise store.Refusal('outcome')
    receipt = recover_submissions(home, review_run_id)
    if receipt.get('dispatch_state') not in ('report-committed', 'final'): raise store.Refusal('report is not committed')
    if receipt.get('manifest_id') is not None:
        return _finalize_v2(home, review_run_id, outcome)
    with store.lock(store.home(home)):
        receipt = _receipt(home, review_run_id)
        event_id = _review_event(home, receipt)
        if receipt.get('dispatch_state') != 'final':
            receipt.update(dispatch_state='final', outcome=outcome, state=outcome, closed_at=time.time(), event_id=event_id)
            store.atomic_json(paths(home)['receipts'] / f"{receipt['review_run_id']}.json", receipt)
    mark_material_reviewed(home, read_packet(home, receipt['packet_id'])['coverage']['followup_id'])
    return _receipt(home, review_run_id)
