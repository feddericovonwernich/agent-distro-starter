"""Bounded native session evidence reader for reviewer episodes."""
import hashlib
import json
import math
import os
import re
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






def _is_binding(entry, source_id, interaction_id):
    details = entry.get('details') if isinstance(entry, dict) else None
    return (isinstance(entry, dict) and entry.get('type') == 'custom_message'
            and entry.get('customType') == 'agd:episode-binding' and entry.get('display') is False
            and isinstance(details, dict) and details.get('schema') == 1
            and details.get('source_id') == source_id and details.get('interaction_id') == interaction_id)


def _final_valid(entry):
    return (isinstance(entry, dict) and entry.get('type') == 'message'
            and entry.get('_role') == 'assistant' and entry.get('_stop_reason') == 'stop'
            and not entry.get('_is_error') and not entry.get('_error_message')
            and (not entry.get('_unsupported') or entry.get('_thinking_only'))
            and entry.get('_text_size', 0) > 0 and entry.get('_message_epoch') is not None)


def _parent_chain(entries, leaf):
    by_id = {entry.get('id'): entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get('id'), str)}
    chain, seen = [], set()
    current = leaf
    while current is not None:
        ident = current.get('id')
        if not isinstance(ident, str) or ident in seen:
            raise store.Refusal('native parent chain is invalid')
        seen.add(ident); chain.append(current)
        parent = current.get('parentId')
        if parent is None:
            break
        current = by_id.get(parent)
        if current is None:
            return None
    chain.reverse()
    return chain


def read_native(session_path: Path, cutoff: float, budget: int, deadline_epoch_ms: int | None = None, *, completion: dict | None = None) -> dict:
    if not isinstance(cutoff, (int, float)) or isinstance(cutoff, bool) or not math.isfinite(cutoff):
        raise store.Refusal('native cutoff is invalid')
    state = {'header': None, 'header_record': None, 'header_index': None, 'entries': [],
             'rows': [], 'candidates': [], 'retained': 0, 'late': False, 'limited': False, 'withheld': []}

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
            state['entries'].append(entry)
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
    ids = [entry['id'] for entry in state['entries'] if isinstance(entry.get('id'), str)]
    if len(ids) != len(set(ids)):
        raise store.Refusal('ambiguous native entry identity')
    bindings = [entry for entry in state['entries']
                if _is_binding(entry, source['source_id'], interaction_id) and entry['_reader_epoch'] <= cutoff]
    if len(bindings) > 1:
        raise store.Refusal('ambiguous native binding')
    if not bindings:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    matching = []
    for entry in state['entries']:
        if entry['_reader_epoch'] > cutoff or not _final_valid(entry):
            continue
        if (entry.get('_text_sha256') != final['text_sha256']
                or abs(entry.get('_message_epoch') - final_at) > 1e-9):
            continue
        matching.append(entry)
    if not matching:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    if len(matching) != 1:
        raise store.Refusal('ambiguous native final')
    final_entry = matching[0]
    chain = _parent_chain(state['entries'], final_entry)
    if chain is None:
        return {**result, 'status': 'pending', 'rows': [], 'boundary': None}
    binding_pos = next((index for index, entry in enumerate(chain)
                        if entry.get('id') == bindings[0].get('id')), None)
    if binding_pos is None:
        raise store.Refusal('native final is on the wrong branch')
    boundary_record = final_entry['_reader_record']
    result['byte_watermark'] = boundary_record['byte_end']
    result['content_sha256'] = _prefix_digest(session_path, boundary_record['byte_end'], budget, deadline_epoch_ms)
    result['boundary'] = {'interaction_id': interaction_id, 'entry_id': final_entry['id'],
                          'parent_id': final_entry.get('parentId'),
                          'closed_at': max(published, final_entry['_reader_epoch'])}
    chain_ids = {entry.get('id') for entry in chain}
    result['rows'] = [
        {'message_id': entry['id'], 'text': text,
         'line_start': entry['_reader_record']['line_start'], 'line_end': entry['_reader_record']['line_start'],
         'byte_start': entry['_reader_record']['byte_start'], 'byte_end': entry['_reader_record']['byte_end']}
        for entry, text in state['candidates']
        if entry.get('id') in chain_ids and entry['_reader_record']['byte_end'] <= boundary_record['byte_end']
    ]
    result['late'] = False
    result['withheld'] = [item for item in result['withheld']
                          if item['line_start'] <= boundary_record['line_start']]
    return result
