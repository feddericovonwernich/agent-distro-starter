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
            'revisions': data / 'revisions', 'manifests': data / 'manifests', 'triage': data / 'triage', 'intents': state / 'intents',
            'receipts': state / 'receipts', 'pending': state / 'pending', 'checkpoints': state / 'checkpoints', 'followups': state / 'followups',
            'candidates': state / 'candidates', 'checkpoint': state / 'checkpoint.json', 'lock': state / 'run.lock'}


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
        result = []
        for item in worker_skills.catalog(home):
            text = (store.home(home) / 'config' / 'worker-skills' / item['id'] / 'SKILL.md').read_text()
            encoded = text.encode()
            excerpt = encoded[:512].decode('utf-8', 'ignore')
            result.append({'id': item['id'], 'description': item['description'],
                           'catalog_sha256': hashlib.sha256(encoded).hexdigest(), 'snapshot_excerpt': excerpt,
                           'snapshot_total_bytes': len(encoded), 'snapshot_truncated': len(encoded) > len(excerpt.encode())})
        return result
    except (OSError, store.Refusal):
        _coverage(coverage, 'unavailable', {'category': 'skill-catalog', 'reason': 'catalog-unavailable'})
        return []

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


def _central(task_dir, task_id, incarnation, cutoff, coverage):
    sources, excerpts = [], []; folder = task_dir / 'decisions'
    if not folder.exists(): return sources, excerpts
    for path in sorted(folder.glob('*.json')):
        if path.name.endswith('.answer.json'): continue
        try:
            decision = _read_json(path); at = decision.get('resolved_at')
            if decision.get('incarnation') != incarnation or decision.get('status') != 'resolved' or not isinstance(at, (int, float)) or at > cutoff or not isinstance(decision.get('answer'), str): continue
            answer = task_dir / decision['answer']
            answer_value = _read_json(answer); body = answer_value.get('body')
            if answer_value.get('task_id') != task_id or answer_value.get('incarnation') != incarnation or answer_value.get('resolved_at') != at or not isinstance(body, str): raise ValueError('answer')
            source = {'kind': 'central-decision', 'linked_task_id': task_id, 'incarnation': incarnation, 'decision_key': decision.get('key'), 'resolved_at': at, 'decision_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'answer_sha256': hashlib.sha256(answer.read_bytes()).hexdigest(), 'byte_watermark': answer.stat().st_size}
            source['source_id'] = _digest(source); sources.append(source)
            excerpt = {'source_id': source['source_id'], 'message_id': decision.get('message_id', decision.get('key')), 'text': body, 'line_start': 1, 'line_end': 1, 'byte_start': 0, 'byte_end': len(body.encode())}; excerpt['excerpt_id'] = _digest(excerpt); excerpts.append(excerpt)
        except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'task_id': task_id, 'reason': 'linked-central-decision-unavailable'})
    return sources, excerpts


def _candidate_id(task_id, incarnation): return _digest({'task_id': task_id, 'incarnation': incarnation})


def _candidate_is_reviewed(root, task_id, incarnation, material_basis):
    try:
        state = _read_json(paths(root)['candidates'] / f'{_candidate_id(task_id, incarnation)}.json')
        return state.get('state') == 'reviewed' and state.get('material_basis') == material_basis
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _candidate_material(root, task, event, task_dir, jsonls, cutoff, limit, coverage):
    key = task['id'], event['incarnation']; sources, excerpts = [], []
    for jsonl in jsonls:
        try:
            sid, watermark, digest, rows, late, limited, withheld = _messages(jsonl, cutoff, limit)
            source = {'kind': 'native-jsonl', 'task_id': key[0], 'incarnation': key[1], 'session_id': sid, 'path': str(jsonl.relative_to(root)), 'byte_watermark': watermark, 'content_sha256': digest}; source['source_id'] = _digest(source); sources.append(source)
            for row in rows:
                excerpt = {'source_id': source['source_id'], **row}; excerpt['excerpt_id'] = _digest(excerpt); excerpts.append(excerpt)
            for row in withheld: _coverage(coverage, 'withheld', row)
            if limited: _coverage(coverage, 'late_basis', {'task_id': key[0], 'session_id': sid, 'reason': 'input-limit'})
            if late: _coverage(coverage, 'late_basis', {'task_id': key[0], 'session_id': sid, 'reason': 'post-cutoff-native-row'})
        except (OSError, store.Refusal, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'task_id': key[0], 'incarnation': key[1], 'reason': 'native-jsonl-malformed'})
    extra_sources, extra_excerpts = _central(task_dir, key[0], key[1], cutoff, coverage)
    if extra_sources: sources.extend(extra_sources); excerpts.extend(extra_excerpts); coverage['central_feedback'] = 'active-partial'; coverage['complete'] = False
    admitted = {'sources': sources, 'excerpts': excerpts, 'coverage': {'late_basis': []}}
    _cap(admitted, limit)
    return sources, excerpts, _digest({'sources': [source['source_id'] for source in admitted['sources']],
                                        'excerpts': [excerpt['excerpt_id'] for excerpt in admitted['excerpts']],
                                        'max_input_bytes': limit})


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
    episodes, sources, excerpts, provenance, seen, candidate_versions = [], [], [], [], set(), {}
    for observed_at, task, event, task_dir in _observation_candidates(root, cutoff, coverage):
        key = (task.get('id'), event.get('incarnation'))
        if key in seen: continue
        try:
            binding = _read_json(root / 'data' / 'observability' / 'tasks' / key[0] / 'omp' / key[1] / 'binding.json'); session_dir = _fixed_session(root, key[0], key[1], binding); jsonls = sorted(session_dir.glob('*.jsonl'))
            if not jsonls: raise ValueError('no JSONL')
        except (OSError, ValueError, json.JSONDecodeError): _coverage(coverage, 'unavailable', {'task_id': key[0], 'incarnation': key[1], 'reason': 'native-jsonl-unavailable'}); continue
        candidate_sources, candidate_excerpts, candidate_basis = _candidate_material(
            root, task, event, task_dir, jsonls, cutoff, config['limits']['max_input_bytes'], coverage)
        try: reviewed = set(_read_json(paths(root)['candidates'] / f'{_candidate_id(*key)}.json').get('reviewed_excerpt_ids', []))
        except (OSError, ValueError, json.JSONDecodeError): reviewed = set()
        candidate_excerpts = [excerpt for excerpt in candidate_excerpts if f"{excerpt['message_id']}:{hashlib.sha256(excerpt['text'].encode()).hexdigest()}" not in reviewed]
        candidate_sources = [source for source in candidate_sources if any(excerpt['source_id'] == source['source_id'] for excerpt in candidate_excerpts)]
        if not candidate_excerpts or len(episodes) >= config['limits']['max_candidates']: continue
        seen.add(key); episodes.append({'task_id': key[0], 'incarnation': key[1], 'closed_at': observed_at, 'exit_observation_id': event.get('id')}); provenance.append({'task_id': key[0], 'assigned': _manifest(root, task_dir, task, coverage), 'usage': 'unknown'})
        sources.extend(candidate_sources); excerpts.extend(candidate_excerpts); candidate_versions[key] = candidate_basis
    if coverage['central_feedback'] == 'unknown': coverage['unknown'].append({'category': 'central-feedback', 'reason': 'no explicit retained links'})
    retrospective = []
    if retrospective_review_run_ids:
        requested = set(retrospective_review_run_ids)
        for receipt_path in sorted(paths(root)['receipts'].glob('*.json')):
            receipt = _read_json(receipt_path); rid = receipt.get('review_run_id'); closed = receipt.get('closed_at')
            if isinstance(rid, str) and isinstance(closed, (int, float)) and closed > cutoff: coverage['retrospective_excluded'].append(rid)
            if rid not in requested: continue
            if receipt.get('dispatch_state') != 'final' or not isinstance(closed, (int, float)) or closed > cutoff: raise store.Refusal('retrospective run is not finalized before cutoff')
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
            for report_id in receipt.get('report_ids', []):
                try:
                    report = _read_json(report_path(root, report_id))
                    source = {'kind': 'reviewer-report', 'review_run_id': rid, 'report_id': report_id,
                              'content_sha256': hashlib.sha256(_json_bytes(report)).hexdigest()}
                    source['source_id'] = _digest(source); sources.append(source)
                    excerpt = {'source_id': source['source_id'], 'message_id': report_id, 'text': json.dumps(report, sort_keys=True),
                               'line_start': 1, 'line_end': 1, 'byte_start': 0, 'byte_end': len(_json_bytes(report))}
                    excerpt['excerpt_id'] = _digest(excerpt); excerpts.append(excerpt)
                except (OSError, ValueError, json.JSONDecodeError):
                    _coverage(coverage, 'unavailable', {'review_run_id': rid, 'reason': 'committed-report-unavailable'})
        if {item['review_run_id'] for item in retrospective} != requested: raise store.Refusal('unknown retrospective run')
    coverage['followup_id'] = '0' * 64  # reserve final metadata before the sole shared cap
    packet = {'schema': 1, 'purpose': 'background-skill-review', 'cutoff': cutoff, 'episodes': episodes, 'sources': sources, 'excerpts': excerpts, 'catalog': _catalog(root, coverage), 'skill_provenance': provenance, 'coverage': coverage, 'retrospective_review_run_ids': list(retrospective_review_run_ids), 'retrospective_episodes': retrospective, 'config_snapshot_sha256': _digest(config)}
    _cap(packet, config['limits']['max_input_bytes'])
    candidates = []
    for episode in packet['episodes']:
        retained = [f"{excerpt['message_id']}:{hashlib.sha256(excerpt['text'].encode()).hexdigest()}" for excerpt in packet['excerpts'] if any(
            source['source_id'] == excerpt['source_id']
            and ((source.get('kind') == 'native-jsonl' and source.get('task_id') == episode['task_id'] and source.get('incarnation') == episode['incarnation'])
                 or (source.get('kind') == 'central-decision' and source.get('linked_task_id') == episode['task_id'] and source.get('incarnation') == episode['incarnation']))
            for source in packet['sources'])]
        if retained:
            candidates.append({'candidate_id': _candidate_id(episode['task_id'], episode['incarnation']),
                               'material_basis': _digest(retained), 'excerpt_ids': retained})
    packet['episodes'] = [episode for episode in packet['episodes'] if any(
        candidate['candidate_id'] == _candidate_id(episode['task_id'], episode['incarnation']) for candidate in candidates)]
    material_basis = _digest({'sources': [(source['source_id'], source.get('content_sha256'), source.get('answer_sha256')) for source in packet['sources']],
                              'excerpts': [excerpt['excerpt_id'] for excerpt in packet['excerpts']]})
    packet['coverage']['followup_id'] = material_basis
    with store.lock(root):
        state_path = paths(root)['followups'] / f'{material_basis}.json'
        if not state_path.exists():
            store.atomic_json(state_path, {'schema': 1, 'material_basis': material_basis, 'state': 'pending', 'candidates': candidates})
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
            if isinstance(candidate.get('material_basis'), str):
                path = paths(home)['candidates'] / f"{candidate['candidate_id']}.json"
                try: prior = _read_json(path)
                except (OSError, ValueError, json.JSONDecodeError): prior = {}
                excerpts = sorted(set(prior.get('reviewed_excerpt_ids', [])) | set(candidate.get('excerpt_ids', [])))
                store.atomic_json(path, {'schema': 1, 'candidate_id': candidate['candidate_id'], 'state': 'reviewed',
                                         'material_basis': candidate['material_basis'], 'reviewed_excerpt_ids': excerpts})
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
        stored.update(packet_id=packet_id, model=model, dispatch_state='begun')
        if model.endswith('-fixture'): stored['omp_version'] = 'fixture-only'
        store.atomic_json(receipt, stored)
    return stored


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


def _finding_fingerprint(finding):
    target = normalize(finding['target'])
    if not target: raise store.Refusal('target')
    return _digest([1, finding['kind'], target, normalize(finding['finding']), normalize(finding['applicability'])])


def _revision_hash(proposal_id, finding):
    return _digest([proposal_id, sorted(finding['citations']), sorted(finding['counterexamples']), finding['resolution'],
                    finding['validation'], finding['validation_limits'], finding.get('new_skill'), finding['uncertainty'],
                    finding.get('target_source_version')])


def _receipt(home, review_run_id):
    try: review_run_id = str(uuid.UUID(str(review_run_id)))
    except (ValueError, TypeError, AttributeError) as error: raise store.Refusal('review run ID') from error
    try: return _read_json(paths(home)['receipts'] / f'{review_run_id}.json')
    except (OSError, ValueError, json.JSONDecodeError) as error: raise store.Refusal('unknown reviewer run') from error


def _validate_report(home, receipt, packet, report):
    if not isinstance(report, dict) or set(report) != {'schema', 'summary', 'findings'} or report['schema'] != 1 or not isinstance(report['summary'], str) or not isinstance(report['findings'], list):
        raise store.Refusal('report schema')
    config = _read_json(paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")
    if len(_json_bytes(report)) > config['limits']['max_output_bytes']: raise store.Refusal('report exceeds output limit')
    admitted = {source['source_id'] for source in packet['sources'] if any(excerpt['source_id'] == source['source_id'] for excerpt in packet['excerpts'])}
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


def submit_report(home, review_run_id, request_id, report):
    try: request_id = str(uuid.UUID(str(request_id)))
    except (ValueError, TypeError, AttributeError) as error: raise store.Refusal('request ID') from error
    receipt = _receipt(home, review_run_id)
    if receipt.get('dispatch_state') not in ('begun', 'report-committed'): raise store.Refusal('reviewer receipt cannot accept a report')
    packet = read_packet(home, receipt.get('packet_id'))
    report = _validate_report(home, receipt, packet, report)
    report_id = _digest({'review_run_id': receipt['review_run_id'], 'request_id': request_id, 'report': report})
    result = {'report_id': report_id, 'proposal_ids': []}
    with store.lock(store.home(home)):
        receipt = _receipt(home, review_run_id)
        requests = receipt.setdefault('report_requests', {})
        if request_id in requests:
            if requests[request_id] != _digest(report): raise store.Refusal('conflicting request reuse')
            return receipt['report_results'][request_id]
        proposals = list_proposals(home)
        for finding in report['findings']:
            fingerprint = _finding_fingerprint(finding)
            matches = [row for row in proposals if row['fingerprint'] == fingerprint]
            proposal = matches[0] if len(matches) == 1 else None
            if proposal is None:
                proposal_id = str(uuid.uuid4())
                proposal = {'schema': 1, 'proposal_id': proposal_id, 'fingerprint': fingerprint, 'head_revision': None,
                            'revisions': [], 'uncertain_distinct': bool(matches)}
                proposals.append(proposal)
            else: proposal_id = proposal['proposal_id']
            revision = _revision_hash(proposal_id, finding)
            if revision not in {row['revision_hash'] for row in proposal['revisions']}:
                revision_row = {'schema': 1, 'proposal_id': proposal_id, 'revision_hash': revision, 'report_id': report_id,
                                'packet_id': receipt['packet_id'], 'basis_source_ids': sorted(finding['citations']),
                                'proposed_change': finding, 'observer': {'model': receipt['model'],
                                'omp_version': receipt.get('omp_version'), 'instruction_snapshot_ref': receipt['instruction_snapshot_ref']},
                                'resurfaced': bool(proposal['revisions'])}
                publish_immutable(paths(home)['revisions'] / f'{revision}.json', revision_row)
                proposal['revisions'].append(revision_row)
                proposal['head_revision'] = revision
                store.atomic_json(proposal_path(home, proposal_id), proposal)
            result['proposal_ids'].append(proposal_id)
        record = {'schema': 1, 'review_run_id': receipt['review_run_id'], 'request_id': request_id, 'report': report,
                  'packet_id': receipt['packet_id'], 'proposal_ids': result['proposal_ids'],
                  'observer': {'model': receipt['model'], 'omp_version': receipt.get('omp_version'),
                               'instruction_snapshot_ref': receipt['instruction_snapshot_ref'],
                               'catalog_versions': [{'id': row['id'], 'catalog_sha256': row['catalog_sha256']} for row in packet['catalog']]}}
        config = _read_json(paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json")
        used = sum(report_path(home, old).stat().st_size for old in receipt.get('report_ids', []) if report_path(home, old).exists())
        if used + len(_json_bytes(record)) > config['limits']['max_output_bytes']: raise store.Refusal('cumulative report output exceeds limit')
        publish_immutable(report_path(home, report_id), record)
        receipt['report_requests'][request_id] = _digest(report)
        receipt.setdefault('report_results', {})[request_id] = result
        receipt.setdefault('report_ids', []).append(report_id)
        receipt.setdefault('proposal_ids', []).extend(result['proposal_ids'])
        receipt['proposal_ids'] = sorted(set(receipt['proposal_ids']))
        receipt['dispatch_state'] = 'report-committed'
        store.atomic_json(paths(home)['receipts'] / f"{receipt['review_run_id']}.json", receipt)
    return result


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
        store.atomic_json(pending, {'schema': 1, 'id': event_id, 'kind': 'background-skill-review',
                                    'key': receipt['review_run_id'], 'observed_at': time.time(),
                                    'details': {'purpose': 'background-skill-review', 'review_run_id': receipt['review_run_id'],
                                                'report_ids': receipt.get('report_ids', []), 'proposal_ids': proposal_ids}})
    return event_id


def finalize_run(home, review_run_id, outcome):
    if not isinstance(outcome, str) or not outcome: raise store.Refusal('outcome')
    receipt = _receipt(home, review_run_id)
    if receipt.get('dispatch_state') not in ('report-committed', 'final'): raise store.Refusal('report is not committed')
    event_id = _review_event(home, receipt)
    with store.lock(store.home(home)):
        receipt = _receipt(home, review_run_id)
        if receipt.get('dispatch_state') != 'final':
            receipt.update(dispatch_state='final', outcome=outcome, closed_at=time.time(), event_id=event_id)
            store.atomic_json(paths(home)['receipts'] / f"{receipt['review_run_id']}.json", receipt)
    mark_material_reviewed(home, read_packet(home, receipt['packet_id'])['coverage']['followup_id'])
    return _receipt(home, review_run_id)
