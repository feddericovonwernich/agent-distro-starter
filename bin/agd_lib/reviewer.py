import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store

INSTRUCTION_FILE = Path(__file__).resolve().parents[2] / 'lib' / 'agd-reviewer-instructions.md'
DEFAULT_LIMITS = {'max_candidates': 20, 'max_input_bytes': 262144, 'max_model_calls': 20,
                  'max_output_bytes': 65536, 'max_runtime_seconds': 1800}
DEFAULT_CONFIG = {'schema': 1,
                  'schedule': {'mode': 'calendar', 'timezone': 'UTC', 'calendar': '02:00', 'interval_hours': None},
                  'limits': DEFAULT_LIMITS, 'review_model': None}


def paths(home):
    root = store.home(home)
    data, state = root / 'data' / 'reviewer', root / 'state' / 'reviewer'
    return {'config': data / 'config.json', 'packets': data / 'packets', 'reports': data / 'reports',
            'proposals': data / 'proposals', 'manifests': data / 'manifests', 'triage': data / 'triage',
            'intents': state / 'intents', 'receipts': state / 'receipts', 'pending': state / 'pending',
            'checkpoint': state / 'checkpoint.json', 'lock': state / 'run.lock'}


def _private(parent):
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise store.Refusal('reviewer path is unsafe')
    return parent


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def publish_immutable(path, value):
    path = Path(path)
    body = _json_bytes(value)
    _private(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise store.Refusal('immutable collision')
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def validate_schedule(schedule):
    if not isinstance(schedule, dict) or set(schedule) != {'mode', 'timezone', 'calendar', 'interval_hours'}:
        raise store.Refusal('schedule schema')
    if schedule['mode'] == 'calendar':
        if (not isinstance(schedule['timezone'], str) or not isinstance(schedule['calendar'], str) or
                not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule['calendar']) or schedule['interval_hours'] is not None):
            raise store.Refusal('calendar schedule')
        try:
            ZoneInfo(schedule['timezone'])
        except ZoneInfoNotFoundError as error:
            raise store.Refusal('timezone') from error
    elif schedule['mode'] == 'interval':
        if type(schedule['interval_hours']) is not int or schedule['interval_hours'] <= 0 or schedule['timezone'] is not None or schedule['calendar'] is not None:
            raise store.Refusal('interval schedule')
    elif schedule['mode'] == 'manual-only':
        if any(schedule[key] is not None for key in ('timezone', 'calendar', 'interval_hours')):
            raise store.Refusal('manual schedule')
    else:
        raise store.Refusal('schedule mode')
    return schedule


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'schedule', 'limits', 'review_model'} or value.get('schema') != 1:
        raise store.Refusal('config schema')
    limits = value.get('limits')
    if (not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS) or
            any(type(number) is not int or number <= 0 for number in limits.values()) or
            (value['review_model'] is not None and (not isinstance(value['review_model'], str) or not value['review_model'].strip()))):
        raise store.Refusal('config values')
    validate_schedule(value['schedule'])
    return json.loads(json.dumps(value))


def read_config(home):
    path = paths(home)['config']
    if not path.exists():
        return {'schema': 1, 'schedule': dict(DEFAULT_CONFIG['schedule']), 'limits': dict(DEFAULT_LIMITS), 'review_model': None}
    try:
        return validate_config(json.loads(path.read_text()))
    except json.JSONDecodeError as error:
        raise store.Refusal('invalid config JSON') from error


def _hex_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise store.Refusal('invalid content id')


def read_packet(home, packet_id):
    _hex_id(packet_id)
    value = json.loads((paths(home)['packets'] / f'{packet_id}.json').read_text())
    value['packet_id'] = packet_id
    return value


def _observation_candidates(home, cutoff):
    result = []
    for task_file in sorted((store.home(home) / 'data' / 'tasks').glob('*/task.json')):
        try:
            task = json.loads(task_file.read_text())
            observations = task_file.parent / 'observations'
            for path in sorted(observations.glob('*.json')):
                event = json.loads(path.read_text())
                if event.get('key') != 'runner-exit' or not isinstance(event.get('observed_at'), (int, float)) or event['observed_at'] > cutoff:
                    continue
                incarnation = event.get('incarnation')
                if not isinstance(incarnation, str):
                    continue
                current = incarnation == task.get('incarnation')
                if current and task.get('worker_alive') is not False:
                    continue
                result.append((event['observed_at'], task, event))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda row: (row[0], row[1].get('id', ''), row[2].get('id', '')))


def _messages(session_path, budget):
    raw = session_path.read_bytes()
    watermark = len(raw)
    lines = raw.splitlines()
    if not lines:
        raise store.Refusal('empty native JSONL')
    header = json.loads(lines[0])
    if header.get('type') != 'session' or header.get('version') != 3 or not isinstance(header.get('id'), str):
        raise store.Refusal('native JSONL header is invalid')
    texts = []
    remaining = budget
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = row.get('message') if isinstance(row, dict) else None
        text = message.get('content') if isinstance(message, dict) else None
        if not isinstance(text, str) or remaining <= 0:
            continue
        encoded = text.encode()
        selected = encoded[:remaining].decode('utf-8', 'ignore')
        if selected:
            texts.append(selected)
            remaining -= len(selected.encode())
    return header['id'], watermark, hashlib.sha256(raw).hexdigest(), texts


def select_packet(home, *, cutoff, config_snapshot, retrospective_review_run_ids=()):
    config = validate_config(config_snapshot)
    if not isinstance(cutoff, (int, float)) or cutoff < 0 or not isinstance(retrospective_review_run_ids, tuple):
        raise store.Refusal('packet scope is invalid')
    root, limit = store.home(home), config['limits']['max_input_bytes']
    episodes, sources, excerpts, provenance, coverage = [], [], [], [], {'complete': True, 'withheld': [], 'unavailable': [], 'unknown': [], 'langfuse_index': 'unused', 'late_basis': []}
    seen = set()
    for observed_at, task, event in _observation_candidates(root, cutoff):
        key = (task.get('id'), event.get('incarnation'))
        if key in seen or len(episodes) >= config['limits']['max_candidates'] or limit <= 0:
            continue
        binding = root / 'data' / 'observability' / 'tasks' / key[0] / 'omp' / key[1] / 'binding.json'
        try:
            bound = json.loads(binding.read_text())
            session_dir = Path(bound['session_dir'])
            if bound.get('task_id') != key[0] or bound.get('incarnation') != key[1] or not session_dir.is_dir():
                raise ValueError
            jsonls = sorted(session_dir.glob('*.jsonl'))
            if not jsonls:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            coverage['complete'] = False; coverage['unavailable'].append({'task_id': key[0], 'incarnation': key[1], 'reason': 'native-jsonl-unavailable'})
            continue
        seen.add(key)
        episode = {'task_id': key[0], 'incarnation': key[1], 'closed_at': observed_at, 'exit_observation_id': event.get('id')}
        episodes.append(episode)
        assigned = [item['id'] for item in task.get('worker_skills', {}).get('skills', []) if isinstance(item, dict) and isinstance(item.get('id'), str)]
        provenance.append({'task_id': key[0], 'assigned': assigned, 'usage': 'unknown'})
        for path in jsonls:
            if limit <= 0:
                coverage['complete'] = False; coverage['late_basis'].append({'task_id': key[0], 'reason': 'input-limit'})
                break
            try:
                session_id, watermark, content_hash, texts = _messages(path, limit)
            except (OSError, store.Refusal, json.JSONDecodeError):
                coverage['complete'] = False; coverage['unavailable'].append({'task_id': key[0], 'incarnation': key[1], 'reason': 'native-jsonl-malformed'})
                continue
            source = {'kind': 'native-jsonl', 'task_id': key[0], 'incarnation': key[1], 'session_id': session_id,
                      'path': str(path), 'byte_watermark': watermark, 'content_sha256': content_hash, 'collected_at': time.time()}
            source['source_id'] = _digest(source)
            sources.append(source)
            for text in texts:
                excerpt = {'source_id': source['source_id'], 'text': text, 'utf8_bytes': len(text.encode())}
                excerpt['excerpt_id'] = _digest(excerpt)
                excerpts.append(excerpt)
                limit -= excerpt['utf8_bytes']
    if not retrospective_review_run_ids:
        coverage['unknown'].append({'category': 'central-feedback', 'reason': 'no explicit retained links'})
    packet = {'schema': 1, 'purpose': 'background-skill-review', 'cutoff': cutoff, 'episodes': episodes, 'sources': sources,
              'excerpts': excerpts, 'skill_provenance': provenance, 'coverage': coverage,
              'retrospective_review_run_ids': list(retrospective_review_run_ids), 'config_snapshot_sha256': _digest(config)}
    packet_id = _digest(packet)
    publish_immutable(paths(root)['packets'] / f'{packet_id}.json', packet)
    packet['packet_id'] = packet_id
    return packet


def enqueue_intent(home, *, kind, retrospective_review_run_ids=()):
    if kind not in ('default', 'manual', 'timer', 'retrospective') or not isinstance(retrospective_review_run_ids, tuple):
        raise store.Refusal('invalid reviewer intent')
    if kind == 'retrospective' and not retrospective_review_run_ids:
        raise store.Refusal('retrospective intent needs explicit closed runs')
    intent = {'schema': 1, 'intent_id': str(uuid.uuid4()), 'kind': kind,
              'retrospective_review_run_ids': list(retrospective_review_run_ids), 'created_at': time.time()}
    with store.lock(store.home(home)):
        publish_immutable(paths(home)['intents'] / f'{intent["intent_id"]}.json', intent)
    return intent


@contextlib.contextmanager
def run_lease(home):
    lock = paths(home)['lock']
    _private(lock.parent)
    with lock.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise store.Refusal('another reviewer run owns this home') from error
        yield


def reserve_run(home, intent, *, config_snapshot, instruction_snapshot, started_at, deadline_epoch_ms):
    config = validate_config(config_snapshot)
    if not isinstance(intent, dict) or not isinstance(intent.get('intent_id'), str) or not isinstance(instruction_snapshot, bytes) or not isinstance(started_at, (int, float)) or type(deadline_epoch_ms) is not int:
        raise store.Refusal('invalid reviewer reservation')
    run_id = str(uuid.uuid4())
    config_ref, instruction_ref = _digest(config), hashlib.sha256(instruction_snapshot).hexdigest()
    publish_immutable(paths(home)['manifests'] / f'{config_ref}.json', config)
    publish_immutable(paths(home)['manifests'] / f'{instruction_ref}.json', {'text': instruction_snapshot.decode('utf-8'), 'raw_utf8_sha256': instruction_ref})
    run = {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': run_id, 'intent': intent,
           'config_snapshot_ref': config_ref, 'instruction_snapshot_ref': instruction_ref, 'started_at': started_at,
           'deadline_epoch_ms': deadline_epoch_ms, 'counters': {'model_calls': 0}, 'dispatch_state': 'reserved'}
    with store.lock(store.home(home)):
        publish_immutable(paths(home)['receipts'] / f'{run_id}.json', run)
    return run


def begin_run(home, run, packet, *, model):
    if not isinstance(run, dict) or not isinstance(run.get('review_run_id'), str) or not isinstance(model, str) or not model.strip():
        raise store.Refusal('invalid reviewer dispatch')
    packet_id = packet.get('packet_id') if isinstance(packet, dict) else None
    _hex_id(packet_id)
    receipt = paths(home)['receipts'] / f'{run["review_run_id"]}.json'
    with store.lock(store.home(home)):
        stored = json.loads(receipt.read_text())
        if stored != run or stored.get('dispatch_state') != 'reserved':
            raise store.Refusal('reviewer receipt is not reservable')
        stored.update(packet_id=packet_id, model=model, dispatch_state='begun')
        store.atomic_json(receipt, stored)
    return stored
