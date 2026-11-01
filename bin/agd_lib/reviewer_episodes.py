"""Bounded native session evidence reader for reviewer episodes."""
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import store


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('naive timestamp')
        return parsed.astimezone(timezone.utc).timestamp()
    raise ValueError('timestamp')


def _render_content(content):
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return None, True
    texts, unsupported = [], False
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
            texts.append(block['text'])
        elif isinstance(block, dict) and block.get('type') in ('thinking', 'redacted_thinking'):
            unsupported = True
        else:
            unsupported = True
    return ''.join(texts), unsupported


def _check_deadline(deadline_epoch_ms):
    if deadline_epoch_ms is not None and (deadline_epoch_ms / 1000 - __import__('time').time()) <= 0:
        raise store.Refusal('reviewer deadline exceeded')


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def source_fingerprint(receipt):
    """Return the source identity hash, excluding its publication fields."""
    if not isinstance(receipt, dict):
        raise store.Refusal('source receipt is invalid')
    identity = {key: value for key, value in receipt.items() if key not in ('source_id', 'recorded_at')}
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _validate_source(receipt, session_path, session_id, native_cwd=None):
    if not isinstance(receipt, dict) or receipt.get('schema') != 1:
        raise store.Refusal('source receipt is invalid')
    expected = {'schema', 'source_id', 'home', 'role', 'session_id', 'path', 'cwd', 'pid', 'start', 'uid', 'recorded_at'}
    role = receipt.get('role')
    if role == 'worker':
        expected |= {'task_id', 'incarnation'}
    elif role != 'central':
        raise store.Refusal('source receipt role is invalid')
    if set(receipt) != expected:
        raise store.Refusal('source receipt fields are invalid')
    if not isinstance(receipt.get('source_id'), str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['source_id']):
        raise store.Refusal('source receipt id is invalid')
    if source_fingerprint(receipt) != receipt['source_id']:
        raise store.Refusal('source receipt fingerprint mismatch')
    if not isinstance(receipt.get('home'), str) or not os.path.isabs(receipt['home']):
        raise store.Refusal('source home is invalid')
    home = Path(receipt['home'])
    try:
        if home.resolve(strict=True) != home or not home.is_dir():
            raise ValueError
    except (OSError, ValueError):
        raise store.Refusal('source home is invalid')
    rel = receipt.get('path')
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
        raise store.Refusal('source path is invalid')
    expected_path = home / rel
    try:
        if expected_path.resolve(strict=True) != expected_path or expected_path != session_path.resolve(strict=True):
            raise ValueError
    except (OSError, ValueError):
        raise store.Refusal('source path mismatch')
    if receipt.get('session_id') != session_id:
        raise store.Refusal('source session mismatch')
    if not isinstance(receipt.get('cwd'), str) or not os.path.isabs(receipt['cwd']) or (native_cwd is not None and receipt['cwd'] != native_cwd):
        raise store.Refusal('source cwd is invalid')
    if type(receipt.get('pid')) is not int or receipt['pid'] <= 0 or type(receipt.get('uid')) is not int or receipt['uid'] < 0:
        raise store.Refusal('source process identity is invalid')
    if not isinstance(receipt.get('start'), str) or not re.fullmatch(r'\d+', receipt['start']):
        raise store.Refusal('source timestamp is invalid')
    if not isinstance(receipt.get('recorded_at'), (int, float)) or isinstance(receipt['recorded_at'], bool) or not math.isfinite(receipt['recorded_at']):
        raise store.Refusal('source timestamp is invalid')
    if role == 'worker' and (not isinstance(receipt.get('task_id'), str) or not isinstance(receipt.get('incarnation'), str)):
        raise store.Refusal('worker source identity is invalid')
    return receipt


def _title_line(value):
    return (isinstance(value, dict) and value.get('type') == 'title' and value.get('v') == 1
            and isinstance(value.get('title'), str) and isinstance(value.get('updatedAt'), str)
            and isinstance(value.get('pad'), str))




def _read_record(stream, number, budget, deadline_epoch_ms, hasher):
    _check_deadline(deadline_epoch_ms)
    start = stream.tell()
    chunks = bytearray()
    line_size = 0
    complete = False
    while True:
        _check_deadline(deadline_epoch_ms)
        chunk = stream.readline(budget + 1)
        _check_deadline(deadline_epoch_ms)
        if not chunk:
            break
        hasher.update(chunk)
        line_size += len(chunk)
        if line_size <= budget:
            chunks.extend(chunk)
        else:
            chunks = None
        if chunk.endswith(b'\n'):
            complete = True
            break
    end = stream.tell()
    value = None
    malformed = not complete or chunks is None
    if not malformed:
        try:
            value = json.loads(bytes(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed = True
    return {'line_start': number, 'byte_start': start, 'byte_end': end, 'complete': complete,
            'size': line_size, 'value': value, 'malformed': malformed}


def _scan(session_path, budget, deadline_epoch_ms, visit, *, stop_at=None):
    _check_deadline(deadline_epoch_ms)
    if type(budget) is not int or budget <= 0:
        raise store.Refusal('native read budget is invalid')
    path = Path(session_path)
    if path.is_symlink() or not path.is_file():
        raise store.Refusal('unsafe native JSONL')
    hasher = hashlib.sha256()
    watermark = 0
    with path.open('rb') as stream:
        number = 0
        while True:
            _check_deadline(deadline_epoch_ms)
            start = stream.tell()
            record = _read_record(stream, number + 1, budget, deadline_epoch_ms, hasher)
            if record['byte_end'] == start:
                break
            number += 1
            watermark = record['byte_end']
            visit(record)
            if not record['complete'] or stop_at is not None and watermark >= stop_at:
                break
    return hasher.hexdigest(), watermark


def _material_digest(session_id, rows):
    hasher = hashlib.sha256()
    hasher.update(_json_bytes({'session_id': session_id}) + b'\n')
    for row in rows:
        hasher.update(_json_bytes({'message_id': row['message_id'], 'text': row['text']}) + b'\n')
    return hasher.hexdigest()


def _base_result(session_id, watermark, digest):
    return {'session_id': session_id, 'byte_watermark': watermark,
            'content_sha256': digest, 'rows': [], 'late': False,
            'limited': False, 'withheld': [], 'status': 'ready', 'boundary': None}



class _MetadataIndex:
    def __init__(self):
        self._directory = tempfile.TemporaryDirectory(prefix='agd-reviewer-')
        self._connection = sqlite3.connect(Path(self._directory.name) / 'metadata.sqlite')
        self._connection.execute('PRAGMA journal_mode=OFF')
        self._connection.execute('PRAGMA synchronous=OFF')
        self._connection.execute('PRAGMA temp_store=FILE')
        self._connection.execute('PRAGMA cache_size=-1024')
        self._connection.execute('''CREATE TABLE entries (
            seq INTEGER PRIMARY KEY, id TEXT, parent_id TEXT, type TEXT, at REAL,
            message_at REAL, role TEXT, stop_reason TEXT, is_error INTEGER,
            error_message TEXT, unsupported INTEGER, thinking_only INTEGER,
            text_sha256 TEXT, text_size INTEGER, line_start INTEGER,
            byte_start INTEGER, byte_end INTEGER, custom_type TEXT, display INTEGER,
            details_schema INTEGER, details_source_id TEXT, details_interaction_id TEXT)''')
        self._connection.execute('CREATE UNIQUE INDEX entry_ids ON entries(id)')
        self._connection.execute('CREATE TEMP TABLE selected_chain (id TEXT PRIMARY KEY)')
        self.ambiguous = False

    def close(self):
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        finally:
            self._directory.cleanup()

    def add(self, entry):
        if not isinstance(entry.get('id'), str):
            return
        record = entry['_reader_record']
        details = entry.get('details') if isinstance(entry.get('details'), dict) else {}
        try:
            self._connection.execute(
                'INSERT INTO entries VALUES (NULL,' + ','.join('?' for _ in range(21)) + ')',
                (entry['id'], entry.get('parentId'), entry.get('type'), entry.get('_reader_epoch'),
                 entry.get('_message_epoch'), entry.get('_role'), entry.get('_stop_reason'),
                 int(bool(entry.get('_is_error'))), entry.get('_error_message'),
                 int(bool(entry.get('_unsupported'))), int(bool(entry.get('_thinking_only'))),
                 entry.get('_text_sha256'), entry.get('_text_size'), record['line_start'],
                 record['byte_start'], record['byte_end'], entry.get('customType'),
                 int(bool(entry.get('display'))) if entry.get('display') is not None else None,
                 details.get('schema'), details.get('source_id'), details.get('interaction_id')))
        except sqlite3.IntegrityError:
            self.ambiguous = True

    def _row(self, ident):
        row = self._connection.execute(
            'SELECT id,parent_id,type,at,message_at,role,stop_reason,is_error,error_message,unsupported,thinking_only,text_sha256,text_size,line_start,byte_start,byte_end,custom_type,display,details_schema,details_source_id,details_interaction_id FROM entries WHERE id=?',
            (ident,)).fetchone()
        if row is None:
            return None
        keys = ('id', 'parentId', 'type', '_reader_epoch', '_message_epoch', '_role', '_stop_reason',
                '_is_error', '_error_message', '_unsupported', '_thinking_only', '_text_sha256',
                '_text_size', '_line_start', '_byte_start', '_byte_end', 'customType', 'display',
                '_details_schema', '_details_source_id', '_details_interaction_id')
        value = dict(zip(keys, row))
        value['_reader_record'] = {'line_start': value.pop('_line_start'), 'byte_start': value.pop('_byte_start'),
                                   'byte_end': value.pop('_byte_end')}
        value['details'] = {'schema': value.pop('_details_schema'), 'source_id': value.pop('_details_source_id'),
                            'interaction_id': value.pop('_details_interaction_id')}
        return value

    def bindings(self, source_id, interaction_id, cutoff):
        return [self._row(row[0]) for row in self._connection.execute(
            '''SELECT id FROM entries WHERE type='custom_message' AND custom_type='agd:episode-binding'
               AND display=0 AND details_schema=1 AND details_source_id=? AND details_interaction_id=? AND at<=?
               LIMIT 2''', (source_id, interaction_id, cutoff))]

    def finals(self, text_sha256, final_at, cutoff):
        return [self._row(row[0]) for row in self._connection.execute(
            '''SELECT id FROM entries WHERE type='message' AND at<=? AND role='assistant'
               AND stop_reason='stop' AND NOT is_error AND (NOT unsupported OR thinking_only)
               AND text_size>0 AND text_sha256=? AND message_at=? LIMIT 2''',
            (cutoff, text_sha256, final_at))]

    def prove_chain(self, final_id, binding_id):
        current, found = final_id, False
        while current is not None:
            row = self._row(current)
            if row is None:
                return None
            try:
                self._connection.execute('INSERT INTO selected_chain VALUES (?)', (current,))
            except sqlite3.IntegrityError:
                raise store.Refusal('native parent chain is invalid')
            found = found or current == binding_id
            current = row.get('parentId')
        return found

    def window_marker(self, binding_id, final_id):
        row = self._connection.execute(
            '''SELECT marker.id FROM entries AS marker
               WHERE marker.type='custom_message'
                 AND marker.custom_type IN ('agd:episode-binding', 'agd:episode-gap')
                 AND marker.seq > (SELECT seq FROM entries WHERE id=?)
                 AND marker.seq <= (SELECT seq FROM entries WHERE id=?)
               ORDER BY marker.seq LIMIT 1''', (binding_id, final_id)).fetchone()
        return row[0] if row else None

    def in_chain(self, ident):
        return self._connection.execute('SELECT 1 FROM selected_chain WHERE id=?', (ident,)).fetchone() is not None


def read_native(session_path: Path, cutoff: float, budget: int, deadline_epoch_ms: int | None = None, *, completion: dict | None = None, skip_row=None) -> dict:
    index = None
    try:
        index = _MetadataIndex() if completion is not None else None
        return _read_native(session_path, cutoff, budget, deadline_epoch_ms, completion=completion, skip_row=skip_row, _index=index)
    finally:
        if index is not None:
            index.close()


def _read_native(session_path: Path, cutoff: float, budget: int, deadline_epoch_ms: int | None = None, *, completion: dict | None = None, skip_row=None, _index=None) -> dict:
    if not isinstance(cutoff, (int, float)) or isinstance(cutoff, bool) or not math.isfinite(cutoff):
        raise store.Refusal('native cutoff is invalid')
    state = {'header': None, 'header_record': None, 'header_index': None,
             'index': _index if completion is not None else None,
             'rows': [], 'retained': 0, 'late': False,
             'limited': False, 'withheld': []}

    def withhold(reason, record, message_id=None):
        state['withheld'].append({'reason': reason, 'line_start': record['line_start'],
                                  **({'message_id': message_id} if message_id is not None else {})})

    def retain(value, record, text):
        row = {'message_id': value['id'], 'text': text,
               'line_start': record['line_start'], 'line_end': record['line_start'],
               'byte_start': record['byte_start'], 'byte_end': record['byte_end']}
        if skip_row is not None and skip_row(row):
            return
        size = len(text.encode())
        if state['retained'] + size <= budget:
            state['retained'] += size
            state['rows'].append(row)
        else:
            state['limited'] = True

    def visit(record):
        value = record.pop('value')
        index = record['line_start'] - 1
        if index == 0 and _title_line(value):
            state['header_index'] = 1
            return
        if state['header_index'] is None:
            state['header_index'] = 0
        if index == state['header_index']:
            if record['malformed'] or record['size'] > budget or not isinstance(value, dict):
                return
            state['header'] = value
            state['header_record'] = dict(record)
            return
        if record['malformed']:
            state['limited'] = True
            withhold('native-row-too-large' if record['size'] > budget else 'native-row-malformed', record)
            return
        if not isinstance(value, dict):
            withhold('native-row-malformed', record)
            return
        try:
            at = _epoch(value.get('timestamp'))
        except (TypeError, ValueError, OverflowError):
            withhold('native-row-missing-timestamp', record)
            return
        entry = {'type': value.get('type'), 'id': value.get('id'), 'parentId': value.get('parentId'),
                 '_reader_epoch': at, '_reader_record': dict(record)}
        if value.get('type') == 'custom_message':
            details = value.get('details')
            entry.update({'customType': value.get('customType'), 'display': value.get('display'),
                          'details': ({key: details.get(key) for key in ('schema', 'source_id', 'interaction_id')}
                                      if isinstance(details, dict) else None)})
        message = value.get('message')
        if isinstance(message, dict):
            entry['_role'] = message.get('role')
            entry['_stop_reason'] = message.get('stopReason')
            entry['_is_error'] = message.get('isError')
            entry['_error_message'] = message.get('errorMessage')
            message_timestamp = message.get('timestamp')
            if (isinstance(message_timestamp, (int, float)) and not isinstance(message_timestamp, bool)
                    and math.isfinite(message_timestamp)):
                entry['_message_epoch'] = float(message_timestamp) / 1000
            content = message.get('content')
            text, unsupported = _render_content(content)
            if isinstance(content, list):
                non_text = [block for block in content if not (isinstance(block, dict) and block.get('type') == 'text')]
                entry['_thinking_only'] = bool(non_text) and all(
                    isinstance(block, dict) and block.get('type') in ('thinking', 'redacted_thinking')
                    for block in non_text)
            if text is not None:
                encoded = text.encode()
                entry['_text_sha256'] = hashlib.sha256(encoded).hexdigest()
                entry['_text_size'] = len(encoded)
                entry['_unsupported'] = unsupported
                if unsupported:
                    withhold('unsupported-content-block', record, value.get('id'))
                if completion is None and text and at <= cutoff and isinstance(value.get('id'), str):
                    retain(value, record, text)
        if completion is not None:
            state['index'].add(entry)
        if at > cutoff:
            state['late'] = True

    digest, watermark = _scan(session_path, budget, deadline_epoch_ms, visit)
    header = state['header']
    header_record = state['header_record']
    if header is None or header_record is None or not isinstance(header, dict):
        raise store.Refusal('invalid native JSONL header')
    if header.get('type') != 'session' or header.get('version') != 3 or not isinstance(header.get('id'), str):
        raise store.Refusal('invalid native JSONL header')
    result = _base_result(header['id'], watermark, digest)
    result['late'] = state['late']
    result['limited'] = state['limited']
    result['withheld'] = state['withheld']
    if completion is None:
        result['rows'] = state['rows']
        result['material_sha256'] = _material_digest(header['id'], result['rows'])
        return result
    if not isinstance(completion, dict):
        raise store.Refusal('completion provenance is invalid')
    source = completion.get('source')
    if not isinstance(header.get('cwd'), str) or not os.path.isabs(header['cwd']):
        raise store.Refusal('native header cwd is invalid')
    _validate_source(source, Path(session_path), header['id'], header['cwd'])
    if (completion.get('schema') != 1 or completion.get('source_id') != source.get('source_id')
            or completion.get('will_continue') is not False or completion.get('awaiting_async_work') is not False):
        raise store.Refusal('completion provenance is invalid')
    interaction_id = completion.get('interaction_id')
    try:
        if str(uuid.UUID(interaction_id)) != interaction_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise store.Refusal('completion interaction is invalid')
    final = completion.get('final')
    if (not isinstance(final, dict) or final.get('stop_reason') != 'stop'
            or not isinstance(final.get('text_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', final['text_sha256'])):
        raise store.Refusal('completion final is invalid')
    try:
        published = _epoch(completion.get('recorded_at'))
        final_ms = final.get('timestamp')
        if not isinstance(final_ms, (int, float)) or isinstance(final_ms, bool) or not math.isfinite(final_ms):
            raise ValueError
        final_at = final_ms / 1000
    except (TypeError, ValueError, OverflowError):
        raise store.Refusal('completion timestamp is invalid')
    if published > cutoff or final_at > cutoff:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    index = state['index']
    if index.ambiguous:
        raise store.Refusal('ambiguous native entry identity')
    bindings = index.bindings(source['source_id'], interaction_id, cutoff)
    if len(bindings) > 1:
        raise store.Refusal('ambiguous native binding')
    if not bindings:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    matching = index.finals(final['text_sha256'], final_at, cutoff)
    if not matching:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    if len(matching) != 1:
        raise store.Refusal('ambiguous native final')
    final_entry = matching[0]
    chain_result = index.prove_chain(final_entry['id'], bindings[0]['id'])
    if chain_result is None:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    if not chain_result:
        raise store.Refusal('native final is on the wrong branch')
    if index.window_marker(bindings[0]['id'], final_entry['id']) is not None:
        raise store.Refusal('native interaction window crossed')
    boundary_record = final_entry['_reader_record']
    result['byte_watermark'] = boundary_record['byte_end']
    result['boundary'] = {'interaction_id': interaction_id, 'entry_id': final_entry['id'],
                          'parent_id': final_entry.get('parentId'),
                          'closed_at': max(published, final_entry['_reader_epoch'])}
    def retain_proved(record):
        value = record.get('value')
        if (not isinstance(value, dict) or value.get('type') != 'message'
                or not isinstance(value.get('id'), str) or not index.in_chain(value['id'])):
            return
        entry = index._row(value['id'])
        message = value.get('message')
        text, _ = _render_content(message.get('content')) if isinstance(message, dict) else (None, False)
        if (record['byte_start'] != entry['_reader_record']['byte_start']
                or record['byte_end'] != entry['_reader_record']['byte_end']
                or value.get('parentId') != entry.get('parentId')
                or text is None or hashlib.sha256(text.encode()).hexdigest() != entry['_text_sha256']):
            raise store.Refusal('native source changed while selecting material')
        if text and entry['_reader_epoch'] <= cutoff:
            retain(value, record, text)

    # Proof selects the branch first; unrelated or consumed rows cannot spend its text budget.
    digest, watermark = _scan(session_path, budget, deadline_epoch_ms, retain_proved,
                              stop_at=boundary_record['byte_end'])
    if watermark != boundary_record['byte_end']:
        raise store.Refusal('native source changed while selecting material')
    result['content_sha256'] = digest
    result['rows'] = state['rows']
    result['limited'] = state['limited']
    result['material_sha256'] = _material_digest(header['id'], result['rows'])
    result['late'] = False
    result['withheld'] = [item for item in result['withheld']
                          if item['line_start'] <= boundary_record['line_start']]
    return result


def _episode_paths(home):
    root = Path(home) / 'state' / 'reviewer' / 'episodes'
    return {'root': root, 'sources': root / 'sources', 'completions': root / 'completions'}


def _private_dir(path):
    path = Path(path)
    current = path
    while True:
        if current.exists() or current.is_symlink():
            identity = os.lstat(current)
            if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
                raise store.Refusal('episode storage path is unsafe')
        if current.name == 'state':
            break
        current = current.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while True:
        identity = os.lstat(current)
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
            raise store.Refusal('episode storage path is unsafe')
        if stat.S_IMODE(identity.st_mode) & 0o077:
            current.chmod(0o700)
        if current.name == 'state':
            break
        current = current.parent
    return path


def _read_private_json(path):
    path = Path(path)
    identity = os.lstat(path)
    if (stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode)
            or identity.st_nlink != 1 or identity.st_uid != os.getuid()):
        raise store.Refusal('episode record is unsafe')
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise store.Refusal('episode record is malformed') from error


def _publish_immutable(path, value):
    path = Path(path)
    body = _json_bytes(value)
    _private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.episode-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _read_private_json(path)
            if existing != value:
                raise store.Refusal('episode publication conflicts with existing record')
            return existing
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return value
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _uuid(value, label):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal(f'{label} is invalid') from error
    if value != parsed:
        raise store.Refusal(f'{label} is invalid')
    return parsed


def _regular_session(path):
    path = Path(path)
    try:
        identity = os.lstat(path)
    except OSError as error:
        raise store.Refusal('native session file is unavailable') from error
    if (stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode)
            or identity.st_nlink != 1 or identity.st_uid != os.getuid()):
        raise store.Refusal('native session file is unsafe')
    return path


def _native_header(path):
    path = _regular_session(path)
    try:
        with path.open('rb') as stream:
            first = stream.readline()
            if not first:
                raise ValueError
            value = json.loads(first)
            if _title_line(value):
                value = json.loads(stream.readline())
            if (not isinstance(value, dict) or value.get('type') != 'session'
                    or value.get('version') != 3 or not isinstance(value.get('id'), str)
                    or not value['id'] or '/' in value['id']):
                raise ValueError
            cwd = value.get('cwd')
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                raise ValueError
            return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        if isinstance(error, store.Refusal):
            raise
        raise store.Refusal('invalid native JSONL header') from error


def _caller_identity():
    from . import supervision
    pid = os.getppid()
    try:
        identity = supervision._process_stat(pid)
    except (FileNotFoundError, ProcessLookupError, OSError, ValueError) as error:
        raise store.Refusal('captured native caller is unavailable') from error
    if identity.get('uid') != os.getuid() or identity.get('state') == 'Z':
        raise store.Refusal('captured native caller identity is invalid')
    return identity


def _check_caller(identity):
    from . import supervision
    try:
        current = supervision._process_stat(identity['pid'])
    except (FileNotFoundError, ProcessLookupError, OSError, ValueError) as error:
        raise store.Refusal('captured native caller exited or changed') from error
    if any(current.get(key) != identity.get(key) for key in ('pid', 'start', 'uid')):
        raise store.Refusal('captured native caller identity changed')
    return current


def _managed_process_receipt(home, role, caller, task_id=None, incarnation=None):
    directory = home / 'state' / 'observability' / 'processes'
    if not directory.is_dir() or directory.is_symlink():
        raise store.Refusal('managed native process receipt is missing')
    matches = []
    for path in directory.glob('*.json'):
        try:
            value = _read_private_json(path)
        except store.Refusal:
            continue
        expected = {'schema', 'role', 'generation', 'pid', 'start', 'uid', 'recorded_at'}
        if role == 'worker':
            expected |= {'task_id', 'incarnation'}
        if set(value) != expected or value.get('schema') != 1 or value.get('role') != role:
            continue
        if role == 'worker' and (value.get('task_id') != task_id or value.get('incarnation') != incarnation):
            continue
        if (value.get('pid') == caller.get('pid') and value.get('start') == caller.get('start')
                and value.get('uid') == caller.get('uid')):
            matches.append(value)
    if len(matches) != 1:
        raise store.Refusal('managed native process receipt is missing or ambiguous')
    return matches[0]


def _expected_cwd(home, role, task_id=None):
    if role == 'central':
        return str(home)
    from . import tasks
    try:
        task = tasks.read(home, task_id)
        raw = Path(task.get('worktree', ''))
        if not raw.is_absolute() or raw.is_symlink():
            raise ValueError
        cwd = raw.resolve(strict=True)
    except (OSError, ValueError, TypeError, store.Refusal) as error:
        raise store.Refusal('worker initial CWD is unavailable') from error
    if not cwd.is_dir():
        raise store.Refusal('worker initial CWD is unsafe')
    return str(cwd)


def _authorize_worker(home, caller):
    from . import observability, tasks
    task_id = os.environ.get('AGD_TASK_ID')
    incarnation = os.environ.get('AGD_INCARNATION')
    try:
        task_id = store.validate_id(task_id)
        incarnation = observability._incarnation(incarnation)
    except (TypeError, ValueError) as error:
        raise store.Refusal('worker capture environment is invalid') from error
    task_path = home / 'data' / 'tasks' / task_id / 'task.json'
    task = _read_private_json(task_path)
    if (task.get('id') != task_id or task.get('executor') != 'worker'
            or task.get('incarnation') != incarnation):
        raise store.Refusal('worker task incarnation is not current')
    binding_path = home / 'data' / 'observability' / 'tasks' / task_id / 'omp' / incarnation / 'binding.json'
    binding = _read_private_json(binding_path)
    expected_session_dir = binding_path.parent
    if (set(binding) != {'task_id', 'incarnation', 'session_dir', 'state'}
            or binding.get('task_id') != task_id or binding.get('incarnation') != incarnation
            or binding.get('session_dir') != str(expected_session_dir)
            or binding.get('state') != 'directory allocated'):
        raise store.Refusal('worker observability binding is invalid')
    _managed_process_receipt(home, 'worker', caller, task_id, incarnation)
    return task_id, incarnation, expected_session_dir, task


def _authorize_central(home, caller):
    if os.environ.get('AGD_MANAGED_CENTRAL') != '1':
        raise store.Refusal('central capture requires managed native environment')
    owner = _read_private_json(home / 'state' / 'owner.json')
    required = {'owner_pid', 'owner_start', 'token', 'watcher_pid', 'watcher_start', 'heartbeat'}
    if (not required <= set(owner) or type(owner.get('owner_pid')) is not int
            or not isinstance(owner.get('owner_start'), str) or not isinstance(owner.get('token'), str)
            or not owner['token']):
        raise store.Refusal('central owner receipt is invalid')
    if owner['owner_pid'] != caller['pid'] or owner['owner_start'] != caller['start']:
        raise store.Refusal('central owner identity differs')
    supplied = [os.environ.get(key) for key in ('AGD_OWNER', 'AGD_ENDPOINT_TOKEN')
                if os.environ.get(key)]
    if not supplied or any(token != owner['token'] for token in supplied):
        raise store.Refusal('central owner token differs')
    _managed_process_receipt(home, 'central', caller)
    return None, None, home / 'data' / 'observability' / 'central' / 'omp', None


def _source_request(request):
    if not isinstance(request, dict) or set(request) != {'role', 'session_id', 'session_file'}:
        raise store.Refusal('source binding request is invalid')
    role = request.get('role')
    if role not in ('worker', 'central'):
        raise store.Refusal('source role is invalid')
    session_id = request.get('session_id')
    if not isinstance(session_id, str) or not session_id or '/' in session_id:
        raise store.Refusal('source session ID is invalid')
    session_file = request.get('session_file')
    if not isinstance(session_file, str) or not os.path.isabs(session_file):
        raise store.Refusal('source session file is invalid')
    return role, session_id, Path(session_file)


def _source_authorize(home, request, *, require_live=True):
    role, session_id, session_file = _source_request(request)
    caller = _caller_identity()
    if role == 'worker':
        task_id, incarnation, session_dir, task = _authorize_worker(home, caller)
    else:
        task_id = incarnation = task = None
        _, _, session_dir, _ = _authorize_central(home, caller)
    session_file = _regular_session(session_file)
    try:
        canonical_home = home.resolve(strict=True)
        canonical_dir = session_dir.resolve(strict=True)
        canonical_file = session_file.resolve(strict=True)
        if home != canonical_home or session_dir != canonical_dir:
            raise ValueError
        if canonical_file.parent != canonical_dir:
            raise ValueError
        rel = canonical_file.relative_to(canonical_home)
    except (OSError, ValueError) as error:
        raise store.Refusal('source session path is not dedicated') from error
    header = _native_header(canonical_file)
    if header.get('id') != session_id:
        raise store.Refusal('native session ID differs')
    expected_cwd = _expected_cwd(home, role, task_id)
    if header.get('cwd') != expected_cwd:
        raise store.Refusal('native initial CWD differs')
    if require_live:
        _check_caller(caller)
    return caller, role, session_id, canonical_file, rel, header, task_id, incarnation


def register_source(home: Path, request: dict) -> dict:
    from .uninstall_state import admission
    home = store.home(home)
    with admission(home):
        with store.lock(home):
            caller, role, session_id, session_file, rel, header, task_id, incarnation = _source_authorize(home, request)
            value = {
                'schema': 1, 'source_id': '', 'home': str(home), 'role': role,
                'session_id': session_id, 'path': str(rel), 'cwd': header['cwd'],
                'pid': caller['pid'], 'start': caller['start'], 'uid': caller['uid'],
                'recorded_at': time.time(),
            }
            if role == 'worker':
                value.update(task_id=task_id, incarnation=incarnation)
            value['source_id'] = source_fingerprint(value)
            path = _episode_paths(home)['sources'] / f"{value['source_id']}.json"
            existing = _read_private_json(path) if path.exists() or path.is_symlink() else None
            if existing is not None:
                if existing != value and {key: existing.get(key) for key in value if key != 'recorded_at'} != {key: value.get(key) for key in value if key != 'recorded_at'}:
                    raise store.Refusal('source publication conflicts with existing record')
                return existing
            return _publish_immutable(path, value)


def _completion_request(request):
    if not isinstance(request, dict):
        raise store.Refusal('completion request is invalid')
    required = {'source_id', 'interaction_id', 'will_continue', 'awaiting_async_work', 'message'}
    if set(request) != required or request.get('will_continue') is not False or request.get('awaiting_async_work') is not False:
        raise store.Refusal('completion request is invalid')
    source_id = request['source_id']
    if not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-f]{64}', source_id):
        raise store.Refusal('source ID is invalid')
    interaction_id = _uuid(request['interaction_id'], 'interaction ID')
    message = request['message']
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise store.Refusal('final native message is invalid')
    if (message.get('stopReason') != 'stop' or message.get('isError') is True
            or any(message.get(key) for key in ('error', 'errorMessage', 'errorDetails', 'truncated'))):
        raise store.Refusal('final native message is not a successful stop')
    timestamp = message.get('timestamp')
    if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp) or timestamp < 0):
        raise store.Refusal('final native timestamp is invalid')
    content = message.get('content')
    blocks = [content] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        raise store.Refusal('final native content is invalid')
    visible = []
    for block in blocks:
        if isinstance(block, str):
            visible.append(block)
        elif isinstance(block, dict) and block.get('type') in ('text', 'thinking', 'redacted_thinking'):
            if block.get('type') == 'text':
                text = block.get('text')
                if not isinstance(text, str):
                    raise store.Refusal('final text block is invalid')
                visible.append(text)
        else:
            raise store.Refusal('tool-only or malformed final content')
    text = ''.join(visible)
    if not text.strip():
        raise store.Refusal('final native message is empty')
    if len(text.encode('utf-8')) > 1048576:
        raise store.Refusal('final native message is too long')
    return source_id, interaction_id, timestamp, text


def record_completion(home: Path, request: dict) -> dict:
    from .uninstall_state import admission
    home = store.home(home)
    source_id, interaction_id, timestamp, text = _completion_request(request)
    paths = _episode_paths(home)
    with admission(home):
        with store.lock(home):
            source_path = paths['sources'] / f'{source_id}.json'
            source = _read_private_json(source_path)
            if source.get('source_id') != source_id:
                raise store.Refusal('source receipt ID differs')
            if not isinstance(source.get('path'), str):
                raise store.Refusal('source receipt path is invalid')
            native = home / source['path']
            _validate_source(source, native, source.get('session_id'), source.get('cwd'))
            source_request = {
                'role': source.get('role'), 'session_id': source.get('session_id'),
                'session_file': str(native),
            }
            caller, *_ = _source_authorize(home, source_request)
            if any(caller.get(key) != source.get(key) for key in ('pid', 'start', 'uid')):
                raise store.Refusal('completion caller differs from source')
            value = {
                'schema': 1, 'source_id': source_id, 'interaction_id': interaction_id,
                'recorded_at': time.time(), 'will_continue': False,
                'awaiting_async_work': False,
                'final': {
                    'timestamp': timestamp,
                    'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'stop_reason': 'stop',
                },
            }
            path = paths['completions'] / source_id / f'{interaction_id}.json'
            if path.exists() or path.is_symlink():
                existing = _read_private_json(path)
                if {key: existing.get(key) for key in value if key != 'recorded_at'} != {key: value.get(key) for key in value if key != 'recorded_at'}:
                    raise store.Refusal('completion publication conflicts with existing record')
                return existing
            return _publish_immutable(path, value)


def _valid_record(source, value, source_id, interaction_id):
    if (not isinstance(value, dict) or set(value) != {
            'schema', 'source_id', 'interaction_id', 'recorded_at',
            'will_continue', 'awaiting_async_work', 'final'}
            or value.get('schema') != 1
            or value.get('source_id') != source_id or value.get('interaction_id') != interaction_id
            or value.get('will_continue') is not False or value.get('awaiting_async_work') is not False):
        raise store.Refusal('completion record is invalid')
    recorded = value.get('recorded_at')
    final = value.get('final')
    if not isinstance(final, dict) or set(final) != {'timestamp', 'text_sha256', 'stop_reason'}:
        raise store.Refusal('completion final is invalid')
    timestamp = final.get('timestamp')
    digest = final.get('text_sha256')
    if (final.get('stop_reason') != 'stop'
            or isinstance(recorded, bool) or not isinstance(recorded, (int, float)) or not math.isfinite(recorded)
            or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(timestamp) or timestamp < 0
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise store.Refusal('completion final metadata is invalid')
    return value


def completion_records(home: Path, cutoff: float, coverage: dict, deadline_epoch_ms=None) -> list:
    home = store.home(home)
    if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff):
        raise store.Refusal('completion cutoff is invalid')
    if deadline_epoch_ms is not None:
        _check_deadline(deadline_epoch_ms)
    coverage.setdefault('complete', True)
    coverage.setdefault('unavailable', [])
    paths = _episode_paths(home)
    result = []
    if not paths['sources'].exists():
        return result
    for source_path in sorted(paths['sources'].glob('*.json')):
        if deadline_epoch_ms is not None:
            _check_deadline(deadline_epoch_ms)
        try:
            source = _read_private_json(source_path)
            if source.get('source_id') != source_path.stem or source_fingerprint(source) != source_path.stem:
                raise store.Refusal('source fingerprint mismatch')
            native = home / source['path']
            _validate_source(source, native, source['session_id'], source.get('cwd'))
            header = _native_header(native)
            if header.get('id') != source.get('session_id') or header.get('cwd') != source.get('cwd'):
                raise store.Refusal('source native header changed')
            completion_dir = paths['completions'] / source_path.stem
            if not completion_dir.exists():
                continue
            for path in sorted(completion_dir.glob('*.json')):
                if deadline_epoch_ms is not None:
                    _check_deadline(deadline_epoch_ms)
                try:
                    value = _valid_record(source, _read_private_json(path), source_path.stem, path.stem)
                    if value['recorded_at'] <= cutoff and value['final']['timestamp'] / 1000 <= cutoff:
                        attached = dict(value)
                        attached['source'] = source
                        result.append(attached)
                except (store.Refusal, ValueError, OSError, TypeError) as error:
                    coverage['complete'] = False
                    coverage['unavailable'].append({'path': str(path), 'reason': str(error)})
        except (store.Refusal, ValueError, OSError, KeyError, TypeError) as error:
            coverage['complete'] = False
            coverage['unavailable'].append({'path': str(source_path), 'reason': str(error)})
    result.sort(key=lambda item: (item['recorded_at'], item['source_id'], item['interaction_id']))
    return result
