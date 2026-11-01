import hashlib
import json
import os
import sys
import tracemalloc
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer_episodes, store


def write_native(path, session_id, entries, *, title=None):
    header = {'type': 'session', 'version': 3, 'id': session_id, 'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': str(path.parent)}
    lines = []
    if title is not None:
        slot = {'type': 'title', 'v': 1, 'title': title, 'updatedAt': '2026-01-01T00:00:00.000Z', 'pad': ''}
        line = json.dumps(slot, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
        slot['pad'] = ' ' * (256 - len(line))
        line = json.dumps(slot, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
        assert len(line) == 256
        lines.append(line)
    lines.append(json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
    lines.extend(json.dumps(entry, ensure_ascii=False, separators=(',', ':')).encode() + b'\n' for entry in entries)
    path.write_bytes(b''.join(lines))


def message(message_id, parent_id, text, timestamp, *, role='user', stop_reason=None, content=None):
    message = {'role': role, 'content': text if content is None else content, 'timestamp': int(timestamp * 1000)}
    if stop_reason is not None:
        message['stopReason'] = stop_reason
    return {'type': 'message', 'id': message_id, 'parentId': parent_id, 'timestamp': timestamp, 'message': message}


def completion(path, session_id, interaction_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', *, recorded_at=200.5):
    source = {'schema': 1, 'source_id': '', 'home': str(path.parent), 'role': 'central', 'session_id': session_id,
              'path': path.name, 'cwd': str(path.parent), 'pid': 1, 'start': '1', 'uid': os.getuid(), 'recorded_at': 100.0}
    source['source_id'] = reviewer_episodes.source_fingerprint(source)
    return {'schema': 1, 'source_id': source['source_id'], 'interaction_id': interaction_id, 'recorded_at': recorded_at,
            'will_continue': False, 'awaiting_async_work': False,
            'final': {'timestamp': 200000, 'text_sha256': hashlib.sha256('final café'.encode()).hexdigest(), 'stop_reason': 'stop'},
            'source': source}


def episode_entries():
    user = message('11111111', None, 'héllo', 100.0)
    binding = {'type': 'custom_message', 'id': 'bbbbbbbb', 'parentId': '11111111', 'timestamp': 101.0,
               'customType': 'agd:episode-binding', 'display': False, 'content': 'marker never excerpt',
               'details': {'schema': 1, 'source_id': 'SOURCE', 'interaction_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}}
    final = message('22222222', 'bbbbbbbb', 'final café', 200.0, role='assistant', stop_reason='stop')
    later = message('33333333', '22222222', 'unfinished', 201.0, role='user')
    return user, binding, final, later


def test_read_native_title_and_proved_boundary_preserve_utf8_coordinates(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, later = episode_entries()
    write_native(path, 'session', [user, binding, final, later], title='marker title')
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding, final, later], title='marker title')
    result = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    raw = path.read_bytes()
    admitted_native_line = json.dumps(user, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    assert raw[result['rows'][0]['byte_start']:result['rows'][0]['byte_end']] == admitted_native_line
    assert result['boundary']['entry_id'] == '22222222'
    assert [row['message_id'] for row in result['rows']] == ['11111111', '22222222']
    assert b'marker title' not in admitted_native_line and b'marker never excerpt' not in admitted_native_line


def test_read_native_material_digest_ignores_title_and_coordinate_shift(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, later = episode_entries()
    write_native(path, 'session', [user, binding, final, later], title=None)
    plain = reviewer_episodes.read_native(path, 300, 4096)
    write_native(path, 'session', [user, binding, final, later], title='renamed title')
    titled = reviewer_episodes.read_native(path, 300, 4096)
    assert plain['material_sha256'] == titled['material_sha256']
    assert plain['content_sha256'] != titled['content_sha256']
    assert [row['text'] for row in plain['rows']] == [row['text'] for row in titled['rows']]

def test_read_native_material_digest_excludes_late_and_clipped_rows(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, _, _, _ = episode_entries()
    write_native(path, 'session', [user], title=None)
    first = reviewer_episodes.read_native(path, 200, 4096)
    late = message('late', None, 'late', 300.0)
    clipped = message('clipped', None, 'z' * 10000, 150.0)
    with path.open('ab') as stream:
        stream.write(json.dumps(late, separators=(',', ':')).encode() + b'\n')
        stream.write(json.dumps(clipped, separators=(',', ':')).encode() + b'\n')
    second = reviewer_episodes.read_native(path, 200, 4096)
    assert first['material_sha256'] == second['material_sha256']


def test_read_native_proved_material_digest_excludes_active_tail(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding, final], title=None)
    first = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    with path.open('ab') as stream:
        stream.write(json.dumps(message('active', 'bbbbbbbb', 'active', 201.0), separators=(',', ':')).encode() + b'\n')
    second = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert first['status'] == second['status'] == 'ready'
    assert first['material_sha256'] == second['material_sha256']


def test_read_native_pending_then_arriving_final(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding], title=None)
    pending = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert pending['status'] == 'pending' and pending['rows'] == []
    with path.open('ab') as stream:
        stream.write(json.dumps(final, separators=(',', ':')).encode() + b'\n')
    ready = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert ready['status'] == 'ready' and ready['boundary']['entry_id'] == '22222222'


def test_read_native_rejects_wrong_branch_and_ambiguous_final(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    wrong = dict(final, parentId='other')
    other = message('other', None, 'other', 99.0)
    write_native(path, 'session', [user, binding, other, wrong], title=None)
    with pytest.raises(store.Refusal):
        reviewer_episodes.read_native(path, 300, 4096, completion=value)
    final2 = dict(final, id='44444444', parentId='bbbbbbbb')
    write_native(path, 'session', [user, binding, final, final2], title=None)
    with pytest.raises(store.Refusal):
        reviewer_episodes.read_native(path, 300, 4096, completion=value)

def test_read_native_gap_carrier_closes_prior_binding_window(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    gap = {'type': 'custom_message', 'id': 'gapgapgg', 'parentId': 'bbbbbbbb', 'timestamp': 102.0,
           'customType': 'agd:episode-gap', 'display': False,
           'details': {'schema': 1, 'interaction_id': 'cccccccc-cccc-cccc-cccc-cccccccccccc'}}
    final['parentId'] = gap['id']
    write_native(path, 'session', [user, binding, gap, final])
    legacy = reviewer_episodes.read_native(path, 300, 4096)
    with pytest.raises(store.Refusal, match='interaction window'):
        reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert all(row['message_id'] != gap['id'] for row in legacy['rows'])

def test_read_native_off_branch_window_markers_use_physical_interval(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    gap = {'type': 'custom_message', 'id': 'offbranch', 'parentId': 'other', 'timestamp': 150.0,
           'customType': 'agd:episode-gap', 'display': False,
           'details': {'schema': 1, 'interaction_id': 'cccccccc-cccc-cccc-cccc-cccccccccccc'}}
    write_native(path, 'session', [user, binding, gap, final])
    with pytest.raises(store.Refusal, match='interaction window'):
        reviewer_episodes.read_native(path, 300, 4096, completion=value)
    write_native(path, 'session', [user, binding, final, gap])
    result = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert result['status'] == 'ready' and result['boundary']['entry_id'] == final['id']

def test_read_native_completion_index_cleans_up_all_paths(tmp_path, monkeypatch):
    created = []
    real_temporary_directory = reviewer_episodes.tempfile.TemporaryDirectory

    class TrackingTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self._inner = real_temporary_directory(*args, **kwargs)
            self.name = self._inner.name
            created.append(self.name)

        def cleanup(self):
            self._inner.cleanup()

    monkeypatch.setattr(reviewer_episodes.tempfile, 'TemporaryDirectory', TrackingTemporaryDirectory)
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding, final])
    reviewer_episodes.read_native(path, 300, 4096, completion=value)
    write_native(path, 'session', [user])
    reviewer_episodes.read_native(path, 300, 4096, completion=value)
    wrong = dict(final, parentId='other')
    other = message('other', None, 'other', 99.0)
    write_native(path, 'session', [user, binding, other, wrong])
    with pytest.raises(store.Refusal):
        reviewer_episodes.read_native(path, 300, 4096, completion=value)
    with pytest.raises(store.Refusal):
        reviewer_episodes.read_native(path, 300, 4096, 1, completion=value)
    assert created and all(not Path(directory).exists() for directory in created)


def test_read_native_thinking_is_withheld_but_final_is_proved(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, later = episode_entries()
    final = message('22222222', 'bbbbbbbb', 'final café', 200.0, role='assistant', stop_reason='stop',
                    content=[{'type': 'text', 'text': 'final café'}, {'type': 'thinking', 'thinking': 'secret'}])
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding, final, later])
    result = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert result['boundary']['entry_id'] == '22222222'
    assert any(item['reason'] == 'unsupported-content-block' and item['message_id'] == '22222222'
               for item in result['withheld'])


def test_read_native_streams_large_history_and_drains_oversized_row(tmp_path):
    path = tmp_path / 'native.jsonl'
    entries = [message(f'{index:08d}', None, 'x' * 180, float(index + 1)) for index in range(12000)]
    entries.append({'type': 'message', 'id': 'oversize', 'parentId': None, 'timestamp': 13000.0,
                    'message': {'role': 'user', 'content': 'y' * 10000, 'timestamp': 13000000}})
    write_native(path, 'session', entries)
    tracemalloc.start()
    result = reviewer_episodes.read_native(path, 20000, 4096)
    _, legacy_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    value = completion(path, 'session')
    tracemalloc.start()
    pending = reviewer_episodes.read_native(path, 20000, 4096, completion=value)
    _, completion_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert max(legacy_peak, completion_peak) < 40 * 1024 * 1024
    assert pending['status'] == 'pending' and pending['rows'] == []
    assert result['limited'] is True
    assert any(item['reason'] == 'native-row-too-large' for item in result['withheld'])
