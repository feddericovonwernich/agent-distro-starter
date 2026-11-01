"""Bounded native session evidence reader for reviewer episodes."""
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
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


def _scan(session_path, budget, deadline_epoch_ms, visit):
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
            if not record['complete']:
                break
    return hasher.hexdigest(), watermark


def _material_digest(session_id, rows):
    hasher = hashlib.sha256()
    hasher.update(_json_bytes({'session_id': session_id}) + b'\n')
    for row in rows:
        hasher.update(_json_bytes({'message_id': row['message_id'], 'text': row['text']}) + b'\n')
    return hasher.hexdigest()


def _prefix_digest(session_path, end, budget, deadline_epoch_ms):
    hasher = hashlib.sha256()
    remaining = end
    path = Path(session_path)
    if path.is_symlink() or not path.is_file():
        raise store.Refusal('unsafe native JSONL')
    with path.open('rb') as stream:
        while remaining:
            _check_deadline(deadline_epoch_ms)
            chunk = stream.read(min(budget, remaining))
            _check_deadline(deadline_epoch_ms)
            if not chunk:
                raise store.Refusal('native evidence changed during read')
            hasher.update(chunk)
            remaining -= len(chunk)
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


def read_native(session_path: Path, cutoff: float, budget: int, deadline_epoch_ms: int | None = None, *, completion: dict | None = None) -> dict:
    index = None
    try:
        index = _MetadataIndex() if completion is not None else None
        return _read_native(session_path, cutoff, budget, deadline_epoch_ms, completion=completion, _index=index)
    finally:
        if index is not None:
            index.close()


def _read_native(session_path: Path, cutoff: float, budget: int, deadline_epoch_ms: int | None = None, *, completion: dict | None = None, _index=None) -> dict:
    if not isinstance(cutoff, (int, float)) or isinstance(cutoff, bool) or not math.isfinite(cutoff):
        raise store.Refusal('native cutoff is invalid')
    state = {'header': None, 'header_record': None, 'header_index': None,
             'index': _index if completion is not None else None,
             'rows': [], 'candidates': [], 'retained': 0, 'late': False,
             'limited': False, 'withheld': []}

    def withhold(reason, record, message_id=None):
        state['withheld'].append({'reason': reason, 'line_start': record['line_start'],
                                  **({'message_id': message_id} if message_id is not None else {})})

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
                if text and at <= cutoff and isinstance(value.get('id'), str):
                    if state['retained'] + len(encoded) <= budget:
                        state['retained'] += len(encoded)
                        if completion is None:
                            state['rows'].append(
                                {'message_id': value['id'], 'text': text,
                                 'line_start': record['line_start'], 'line_end': record['line_start'],
                                 'byte_start': record['byte_start'], 'byte_end': record['byte_end']})
                        else:
                            state['candidates'].append((entry, text))
                    else:
                        state['limited'] = True
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
    result['content_sha256'] = _prefix_digest(session_path, boundary_record['byte_end'], budget, deadline_epoch_ms)
    result['boundary'] = {'interaction_id': interaction_id, 'entry_id': final_entry['id'],
                          'parent_id': final_entry.get('parentId'),
                          'closed_at': max(published, final_entry['_reader_epoch'])}
    result['rows'] = [
        {'message_id': entry['id'], 'text': text,
         'line_start': entry['_reader_record']['line_start'], 'line_end': entry['_reader_record']['line_start'],
         'byte_start': entry['_reader_record']['byte_start'], 'byte_end': entry['_reader_record']['byte_end']}
        for entry, text in sorted(
            ((entry, text) for entry, text in state['candidates']
             if index.in_chain(entry.get('id')) and entry['_reader_record']['byte_end'] <= boundary_record['byte_end']),
            key=lambda item: item[0]['_reader_record']['byte_start'])
    ]
    result['material_sha256'] = _material_digest(header['id'], result['rows'])
    result['late'] = False
    result['withheld'] = [item for item in result['withheld']
                          if item['line_start'] <= boundary_record['line_start']]
    return result
