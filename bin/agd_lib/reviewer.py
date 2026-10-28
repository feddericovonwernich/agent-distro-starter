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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store, worker_skills

INSTRUCTION_FILE = Path(__file__).resolve().parents[2] / 'lib' / 'agd-reviewer-instructions.md'
DEFAULT_LIMITS = {'max_candidates': 20, 'max_input_bytes': 262144, 'max_model_calls': 20,
                  'max_output_bytes': 65536, 'max_runtime_seconds': 1800}
DEFAULT_CONFIG = {'schema': 1, 'schedule': {'mode': 'calendar', 'timezone': 'UTC', 'calendar': '02:00', 'interval_hours': None},
                  'limits': DEFAULT_LIMITS, 'review_model': None}


def paths(home):
    root = store.home(home); data, state = root / 'data' / 'reviewer', root / 'state' / 'reviewer'
    return {'config': data / 'config.json', 'packets': data / 'packets', 'reports': data / 'reports', 'proposals': data / 'proposals',
            'manifests': data / 'manifests', 'triage': data / 'triage', 'intents': state / 'intents', 'receipts': state / 'receipts',
            'pending': state / 'pending', 'checkpoints': state / 'checkpoints', 'followups': state / 'followups', 'candidates': state / 'candidates',
            'checkpoint': state / 'checkpoint.json', 'lock': state / 'run.lock'}


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
            if path.read_bytes() != body: raise store.Refusal('immutable collision')
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
    if not isinstance(value, dict) or set(value) != {'schema', 'schedule', 'limits', 'review_model'} or value.get('schema') != 1: raise store.Refusal('config schema')
    limits = value.get('limits')
    if not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS) or any(type(item) is not int or item <= 0 for item in limits.values()) or (value['review_model'] is not None and (not isinstance(value['review_model'], str) or not value['review_model'].strip())): raise store.Refusal('config values')
    validate_schedule(value['schedule']); return json.loads(json.dumps(value))


def read_config(home):
    path = paths(home)['config']
    if not path.exists(): return {'schema': 1, 'schedule': dict(DEFAULT_CONFIG['schedule']), 'limits': dict(DEFAULT_LIMITS), 'review_model': None}
    try: return validate_config(json.loads(path.read_text()))
    except json.JSONDecodeError as error: raise store.Refusal('invalid config JSON') from error


def _hex_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value): raise store.Refusal('invalid content id')


def read_packet(home, packet_id):
    _hex_id(packet_id); value = json.loads((paths(home)['packets'] / f'{packet_id}.json').read_text()); value['packet_id'] = packet_id; return value


def _coverage(coverage, bucket, value): coverage['complete'] = False; coverage[bucket].append(value)


def _read_json(path):
    if path.is_symlink() or not path.is_file(): raise ValueError('unsafe record')
    return json.loads(path.read_text())


def _observation_candidates(home, cutoff, coverage):
    result = []; root = store.home(home) / 'data' / 'tasks'
    if not root.exists(): return result
    for directory in sorted(root.iterdir()):
        task_file = directory / 'task.json'
        try:
            task = _read_json(task_file)
            observations = directory / 'observations'
            if observations.exists() and (observations.is_symlink() or not observations.is_dir()): raise ValueError('unsafe observations')
            for path in sorted(observations.glob('*.json')) if observations.exists() else ():
                try: event = _read_json(path)
                except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'path': str(path), 'reason': 'observation-malformed'}); continue
                if event.get('key') != 'runner-exit' or not isinstance(event.get('observed_at'), (int, float)) or event['observed_at'] > cutoff: continue
                incarnation = event.get('incarnation')
                if not isinstance(incarnation, str): _coverage(coverage, 'unavailable', {'path': str(path), 'reason': 'observation-malformed'}); continue
                if incarnation == task.get('incarnation') and task.get('worker_alive') is not False: continue
                result.append((event['observed_at'], task, event, directory))
        except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'path': str(task_file), 'reason': 'task-record-malformed'})
    return sorted(result, key=lambda row: (row[0], row[1].get('id', ''), row[2].get('id', '')))


def _epoch(value):
    if isinstance(value, (int, float)): return float(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError('naive timestamp')
        return parsed.astimezone(timezone.utc).timestamp()
    raise ValueError('timestamp')


def _render_content(content):
    if isinstance(content, str): return content, False
    if not isinstance(content, list): return None, True
    texts, unsupported = [], False
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str): texts.append(block['text'])
        else: unsupported = True
    return ''.join(texts), unsupported


def _messages(session_path, cutoff, budget):
    if session_path.is_symlink() or not session_path.is_file(): raise store.Refusal('unsafe native JSONL')
    with session_path.open('rb') as stream:
        header_line = stream.readline()
        if not header_line: raise store.Refusal('empty native JSONL')
        header = json.loads(header_line); hasher = hashlib.sha256(header_line); watermark = len(header_line)
        if header.get('type') != 'session' or header.get('version') != 3 or not isinstance(header.get('id'), str): raise store.Refusal('native JSONL header is invalid')
        rows, late, limited, withheld, number, retained = [], False, False, [], 1, 0
        while line := stream.readline():
            number += 1; start = watermark
            if len(line) > budget:
                limited = True
                break
            try: row = json.loads(line)
            except json.JSONDecodeError: withheld.append({'reason': 'native-row-malformed', 'line_start': number}); continue
            try: at = _epoch(row.get('timestamp'))
            except (TypeError, ValueError): withheld.append({'reason': 'native-row-missing-timestamp', 'line_start': number}); continue
            if at > cutoff:
                late = True
                break
            message = row.get('message') if isinstance(row, dict) else None
            text, unsupported = _render_content(message.get('content')) if isinstance(message, dict) else (None, False)
            if text and retained + len(text.encode()) > budget:
                limited = True
                break
            watermark += len(line); hasher.update(line)
            if not isinstance(message, dict) or not isinstance(row.get('id'), str): continue
            if unsupported: withheld.append({'reason': 'unsupported-content-block', 'line_start': number, 'message_id': row['id']})
            if text:
                retained += len(text.encode())
                rows.append({'message_id': row['id'], 'text': text, 'line_start': number, 'line_end': number, 'byte_start': start, 'byte_end': watermark})
    return header['id'], watermark, hasher.hexdigest(), rows, late, limited, withheld


def _fixed_session(root, task_id, incarnation, binding):
    expected = root / 'data' / 'observability' / 'tasks' / task_id / 'omp' / incarnation
    for parent in (root / 'data', root / 'data' / 'observability', root / 'data' / 'observability' / 'tasks', expected.parent, expected):
        if parent.is_symlink() or not parent.is_dir(): raise ValueError('unsafe session path')
    if binding.get('task_id') != task_id or binding.get('incarnation') != incarnation or binding.get('session_dir') != str(expected): raise ValueError('binding mismatch')
    return expected


def _catalog(home, coverage):
    try:
        entries = worker_skills.catalog(home)
        return [{'id': item['id'], 'description': item['description'], 'catalog_sha256': hashlib.sha256((store.home(home) / 'config' / 'worker-skills' / item['id'] / 'SKILL.md').read_bytes()).hexdigest()} for item in entries]
    except (OSError, store.Refusal): _coverage(coverage, 'unavailable', {'category': 'skill-catalog', 'reason': 'catalog-unavailable'}); return []


def _manifest(home, task_dir, task, coverage):
    path = task_dir / 'worker-skills.json'
    if not path.exists(): return []
    try:
        manifest = worker_skills._manifest(home, task)
        result = []
        for item in manifest['skills']:
            snap = Path(item['snapshot'])
            result.append({'id': item['id'], 'snapshot_sha256': hashlib.sha256((snap / 'SKILL.md').read_bytes()).hexdigest(),
                           'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return result
    except (OSError, ValueError, json.JSONDecodeError, store.Refusal):
        _coverage(coverage, 'unavailable', {'task_id': task.get('id'), 'reason': 'assigned-manifest-unavailable'})
        return []


def _central(task_dir, task, cutoff, coverage):
    sources, excerpts = [], []; folder = task_dir / 'decisions'
    if not folder.exists(): return sources, excerpts
    for path in sorted(folder.glob('*.json')):
        if path.name.endswith('.answer.json'): continue
        try:
            decision = _read_json(path); at = decision.get('resolved_at')
            if decision.get('incarnation') != task.get('incarnation') or decision.get('status') != 'resolved' or not isinstance(at, (int, float)) or at > cutoff or not isinstance(decision.get('answer'), str): continue
            answer = task_dir / decision['answer']
            answer_value = _read_json(answer); body = answer_value.get('body')
            if answer_value.get('task_id') != task['id'] or answer_value.get('incarnation') != task['incarnation'] or answer_value.get('resolved_at') != at or not isinstance(body, str): raise ValueError('answer')
            source = {'kind': 'central-decision', 'linked_task_id': task['id'], 'incarnation': task['incarnation'], 'decision_key': decision.get('key'), 'resolved_at': at, 'decision_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'answer_sha256': hashlib.sha256(answer.read_bytes()).hexdigest(), 'byte_watermark': answer.stat().st_size}
            source['source_id'] = _digest(source); sources.append(source)
            excerpt = {'source_id': source['source_id'], 'message_id': decision.get('message_id', decision.get('key')), 'text': body, 'line_start': 1, 'line_end': 1, 'byte_start': 0, 'byte_end': len(body.encode())}; excerpt['excerpt_id'] = _digest(excerpt); excerpts.append(excerpt)
        except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'task_id': task.get('id'), 'reason': 'linked-central-decision-unavailable'})
    return sources, excerpts


def _candidate_id(task_id, incarnation): return _digest({'task_id': task_id, 'incarnation': incarnation})


def _candidate_is_reviewed(root, task_id, incarnation, session_dir):
    try:
        state = _read_json(paths(root)['candidates'] / f'{_candidate_id(task_id, incarnation)}.json')
        if state.get('state') != 'reviewed': return False
        return all(path.stat().st_size <= state['watermarks'][path.name] for path in session_dir.glob('*.jsonl'))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _cap(packet, limit):
    # Remove newest evidence until the complete broker object—not merely text—fits the frozen cap.
    while len(_json_bytes(packet)) > limit and packet['excerpts']:
        packet['excerpts'].pop(); _coverage(packet['coverage'], 'late_basis', {'reason': 'input-limit'})
    while len(_json_bytes(packet)) > limit and packet['sources']:
        packet['sources'].pop(); _coverage(packet['coverage'], 'late_basis', {'reason': 'input-limit'})
    if len(_json_bytes(packet)) > limit: raise store.Refusal('max_input_bytes cannot encode required coverage')


def select_packet(home, *, cutoff, config_snapshot, retrospective_review_run_ids=()):
    config = validate_config(config_snapshot)
    if not isinstance(cutoff, (int, float)) or cutoff < 0 or not isinstance(retrospective_review_run_ids, tuple): raise store.Refusal('packet scope is invalid')
    root = store.home(home)
    scope = {'schema': 1, 'cutoff': cutoff, 'config_snapshot_sha256': _digest(config), 'retrospective_review_run_ids': list(retrospective_review_run_ids)}
    scope_id = _digest(scope)
    checkpoint = paths(root)['checkpoints'] / f'{scope_id}.json'
    if checkpoint.exists():
        saved = _read_json(checkpoint)
        if saved.get('scope') != scope: raise store.Refusal('reviewer checkpoint collision')
        return read_packet(root, saved['packet_id'])
    coverage = {'complete': True, 'withheld': [], 'unavailable': [], 'unknown': [], 'langfuse_index': 'unused', 'late_basis': [], 'central_feedback': 'unknown', 'retrospective_excluded': []}
    episodes, sources, excerpts, provenance, seen = [], [], [], [], set()
    for observed_at, task, event, task_dir in _observation_candidates(root, cutoff, coverage):
        key = (task.get('id'), event.get('incarnation'))
        if key in seen: continue
        try:
            binding = _read_json(root / 'data' / 'observability' / 'tasks' / key[0] / 'omp' / key[1] / 'binding.json'); session_dir = _fixed_session(root, key[0], key[1], binding); jsonls = sorted(session_dir.glob('*.jsonl'))
            if not jsonls: raise ValueError('no JSONL')
        except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'task_id': key[0], 'incarnation': key[1], 'reason': 'native-jsonl-unavailable'}); continue
        if _candidate_is_reviewed(root, key[0], key[1], session_dir) or len(episodes) >= config['limits']['max_candidates']: continue
        seen.add(key); episodes.append({'task_id': key[0], 'incarnation': key[1], 'closed_at': observed_at, 'exit_observation_id': event.get('id')}); provenance.append({'task_id': key[0], 'assigned': _manifest(root, task_dir, task, coverage), 'usage': 'unknown'})
        for jsonl in jsonls:
            try:
                sid, watermark, digest, rows, late, limited, withheld = _messages(jsonl, cutoff, config['limits']['max_input_bytes'])
                source = {'kind': 'native-jsonl', 'task_id': key[0], 'incarnation': key[1], 'session_id': sid, 'path': str(jsonl.relative_to(root)), 'byte_watermark': watermark, 'content_sha256': digest}; source['source_id'] = _digest(source); sources.append(source)
                for row in rows:
                    excerpt = {'source_id': source['source_id'], **row}; excerpt['excerpt_id'] = _digest(excerpt); excerpts.append(excerpt)
                for row in withheld: _coverage(coverage, 'withheld', row)
                if limited: _coverage(coverage, 'late_basis', {'task_id': key[0], 'session_id': sid, 'reason': 'input-limit'})
                if late: _coverage(coverage, 'late_basis', {'task_id': key[0], 'session_id': sid, 'reason': 'post-cutoff-native-row'})
            except (OSError, store.Refusal, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'task_id': key[0], 'incarnation': key[1], 'reason': 'native-jsonl-malformed'})
        extra_sources, extra_excerpts = _central(task_dir, task, cutoff, coverage)
        if extra_sources: sources.extend(extra_sources); excerpts.extend(extra_excerpts); coverage['central_feedback'] = 'active-partial'; coverage['complete'] = False
    if coverage['central_feedback'] == 'unknown': coverage['unknown'].append({'category': 'central-feedback', 'reason': 'no explicit retained links'})
    retrospective = []
    if retrospective_review_run_ids:
        requested = set(retrospective_review_run_ids)
        for receipt_path in sorted(paths(root)['receipts'].glob('*.json')):
            receipt = _read_json(receipt_path); rid = receipt.get('review_run_id'); closed = receipt.get('closed_at')
            if isinstance(rid, str) and isinstance(closed, (int, float)) and closed > cutoff: coverage['retrospective_excluded'].append(rid)
            if rid not in requested: continue
            if receipt.get('dispatch_state') != 'closed' or not isinstance(closed, (int, float)) or closed > cutoff: raise store.Refusal('retrospective run is not closed before cutoff')
            binding_path = root / 'data' / 'observability' / 'reviewer' / rid / 'binding.json'
            try:
                binding = _read_json(binding_path)
                session_dir = root / 'data' / 'observability' / 'reviewer' / rid / 'omp'
                if binding.get('review_run_id') != rid or binding.get('session_dir') != str(session_dir) or session_dir.is_symlink() or not session_dir.is_dir(): raise ValueError('binding')
                jsonls = sorted(session_dir.glob('*.jsonl'))
                if not jsonls: raise ValueError('session')
            except (OSError, ValueError, json.JSONDecodeError) as error: raise store.Refusal('retrospective native evidence is unavailable') from error
            retrospective.append({'review_run_id': rid, 'closed_at': closed, 'source_count': len(jsonls)})
            for jsonl in jsonls:
                try:
                    sid, watermark, digest, rows, late, limited, withheld = _messages(jsonl, cutoff, config['limits']['max_input_bytes'])
                    source = {'kind': 'reviewer-native-jsonl', 'review_run_id': rid, 'session_id': sid, 'path': str(jsonl.relative_to(root)), 'byte_watermark': watermark, 'content_sha256': digest}
                    source['source_id'] = _digest(source); sources.append(source)
                    for row in rows:
                        excerpt = {'source_id': source['source_id'], **row}; excerpt['excerpt_id'] = _digest(excerpt); excerpts.append(excerpt)
                    if limited: _coverage(coverage, 'late_basis', {'review_run_id': rid, 'reason': 'input-limit'})
                    if late: _coverage(coverage, 'late_basis', {'review_run_id': rid, 'reason': 'post-cutoff-native-row'})
                    for row in withheld: _coverage(coverage, 'withheld', row)
                except (OSError, store.Refusal, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'review_run_id': rid, 'reason': 'retrospective-native-jsonl-malformed'})
        if {item['review_run_id'] for item in retrospective} != requested: raise store.Refusal('unknown retrospective run')
    packet = {'schema': 1, 'purpose': 'background-skill-review', 'cutoff': cutoff, 'episodes': episodes, 'sources': sources, 'excerpts': excerpts, 'catalog': _catalog(root, coverage), 'skill_provenance': provenance, 'coverage': coverage, 'retrospective_review_run_ids': list(retrospective_review_run_ids), 'retrospective_episodes': retrospective, 'config_snapshot_sha256': _digest(config)}
    _cap(packet, config['limits']['max_input_bytes'])
    candidates = [{'candidate_id': _candidate_id(episode['task_id'], episode['incarnation']), 'task_id': episode['task_id'], 'incarnation': episode['incarnation'],
                   'watermarks': {Path(source['path']).name: source['byte_watermark'] for source in packet['sources']
                                  if source.get('kind') == 'native-jsonl' and source.get('task_id') == episode['task_id'] and source.get('incarnation') == episode['incarnation']}}
                  for episode in packet['episodes']]
    material_basis = _digest({'sources': [(source['source_id'], source.get('content_sha256'), source.get('answer_sha256')) for source in packet['sources']],
                              'excerpts': [excerpt['excerpt_id'] for excerpt in packet['excerpts']]})
    with store.lock(root):
        state_path = paths(root)['followups'] / f'{material_basis}.json'
        if not state_path.exists():
            store.atomic_json(state_path, {'schema': 1, 'material_basis': material_basis, 'state': 'pending', 'candidates': candidates})
    packet['coverage']['followup_id'] = material_basis
    _cap(packet, config['limits']['max_input_bytes'])
    packet_id = _digest(packet); publish_immutable(paths(root)['packets'] / f'{packet_id}.json', packet)
    store.atomic_json(checkpoint, {'scope': scope, 'packet_id': packet_id, 'material_basis': material_basis})
    packet['packet_id'] = packet_id; return packet


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
                if candidate.get('watermarks'):
                    store.atomic_json(paths(home)['candidates'] / f"{candidate['candidate_id']}.json",
                                      {'schema': 1, 'candidate_id': candidate['candidate_id'], 'state': 'reviewed',
                                       'watermarks': candidate['watermarks']})
        return state


def enqueue_intent(home, *, kind, retrospective_review_run_ids=()):
    if kind not in ('default', 'manual', 'timer', 'retrospective') or not isinstance(retrospective_review_run_ids, tuple): raise store.Refusal('invalid reviewer intent')
    if kind == 'retrospective' and not retrospective_review_run_ids: raise store.Refusal('retrospective intent needs explicit closed runs')
    root = store.home(home); scope = {'kind': kind, 'retrospective_review_run_ids': list(retrospective_review_run_ids)}
    with store.lock(root):
        if kind in ('default', 'timer'):
            for path in paths(root)['intents'].glob('*.json') if paths(root)['intents'].exists() else ():
                existing = _read_json(path)
                pending = paths(root)['pending'] / f'{existing["intent_id"]}.json'
                if {key: existing.get(key) for key in scope} == scope and pending.exists() and _read_json(pending).get('state') == 'pending':
                    return existing
        intent = {'schema': 1, 'intent_id': str(uuid.uuid4()), **scope, 'created_at': time.time()}
        publish_immutable(paths(root)['intents'] / f'{intent["intent_id"]}.json', intent)
        store.atomic_json(paths(root)['pending'] / f'{intent["intent_id"]}.json', {'intent_id': intent['intent_id'], 'state': 'pending'})
        return intent


@contextlib.contextmanager
def run_lease(home):
    lock = paths(home)['lock']; _private(lock.parent)
    with lock.open('a+') as stream:
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise store.Refusal('another reviewer run owns this home') from error
        yield


def reserve_run(home, intent, *, config_snapshot, instruction_snapshot, started_at, deadline_epoch_ms):
    config = validate_config(config_snapshot)
    if not isinstance(intent, dict) or not isinstance(intent.get('intent_id'), str) or not isinstance(instruction_snapshot, bytes) or not isinstance(started_at, (int, float)) or type(deadline_epoch_ms) is not int: raise store.Refusal('invalid reviewer reservation')
    run_id = str(uuid.uuid4()); config_ref, instruction_ref = _digest(config), hashlib.sha256(instruction_snapshot).hexdigest(); publish_immutable(paths(home)['manifests'] / f'{config_ref}.json', config); publish_immutable(paths(home)['manifests'] / f'{instruction_ref}.json', {'text': instruction_snapshot.decode('utf-8'), 'raw_utf8_sha256': instruction_ref})
    run = {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': run_id, 'intent': intent, 'config_snapshot_ref': config_ref, 'instruction_snapshot_ref': instruction_ref, 'started_at': started_at, 'deadline_epoch_ms': deadline_epoch_ms, 'counters': {'model_calls': 0}, 'dispatch_state': 'reserved'}
    with store.lock(store.home(home)):
        publish_immutable(paths(home)['receipts'] / f'{run_id}.json', run)
        pending = paths(home)['pending'] / f'{intent["intent_id"]}.json'
        if pending.exists(): store.atomic_json(pending, {'intent_id': intent['intent_id'], 'state': 'consumed', 'review_run_id': run_id})
    return run


def begin_run(home, run, packet, *, model):
    if not isinstance(run, dict) or not isinstance(run.get('review_run_id'), str) or not isinstance(model, str) or not model.strip(): raise store.Refusal('invalid reviewer dispatch')
    packet_id = packet.get('packet_id') if isinstance(packet, dict) else None; _hex_id(packet_id); receipt = paths(home)['receipts'] / f'{run["review_run_id"]}.json'
    with store.lock(store.home(home)):
        stored = _read_json(receipt)
        if stored != run or stored.get('dispatch_state') != 'reserved': raise store.Refusal('reviewer receipt is not reservable')
        stored.update(packet_id=packet_id, model=model, dispatch_state='begun'); store.atomic_json(receipt, stored)
    return stored
