import hashlib
import json
import os
import sys
import threading
import time
import tracemalloc
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer_episodes, store


def write_native(path, session_id, entries, *, title=None, cwd=None):
    header = {'type': 'session', 'version': 3, 'id': session_id, 'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': str(path.parent if cwd is None else cwd)}
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

@pytest.mark.parametrize('proved', [False, True])
def test_read_native_skips_consumed_material_before_retention_budget(tmp_path, proved):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    user['message']['content'] = 'x' * 3500
    final['message']['content'] = 'n' * 1000
    value = completion(path, 'session')
    value['final']['text_sha256'] = hashlib.sha256(final['message']['content'].encode()).hexdigest()
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding, final])
    read = reviewer_episodes.read_native(
        path, 300, 4096, completion=value if proved else None,
        skip_row=lambda row: row['message_id'] == user['id'])
    assert [row['message_id'] for row in read['rows']] == [final['id']]
    assert read['rows'][0]['text'] == final['message']['content']
    assert read['limited'] is False


def test_proved_native_chain_budget_excludes_other_branches(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, _ = episode_entries()
    value = completion(path, 'session')
    user['message']['content'] = 'u' * 1000
    final['message']['content'] = 'n' * 1000
    value['final']['text_sha256'] = hashlib.sha256(final['message']['content'].encode()).hexdigest()
    binding['details']['source_id'] = value['source_id']
    unrelated = message('other', None, 'x' * 3500, 99.0)
    write_native(path, 'session', [unrelated, user, binding, final])
    read = reviewer_episodes.read_native(path, 300, 4096, completion=value)
    assert [row['message_id'] for row in read['rows']] == [user['id'], final['id']]
    assert read['limited'] is False



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


def _json_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(',', ':')) + '\n')


def _central_case(tmp_path):
    home = tmp_path / 'home'
    data = home / 'data'
    observability_root = data / 'observability'
    central = observability_root / 'central' / 'omp'
    central.mkdir(parents=True)
    (home / 'state').mkdir(parents=True)
    for directory in (home, data, observability_root, central, home / 'state'):
        directory.chmod(0o700)
    _json_record(data / 'home.json', {'home_id': '11111111-1111-1111-1111-111111111111'})
    session = central / 'session.jsonl'
    write_native(session, 'central-session', [], cwd=home)
    owner = {'owner_pid': 1234, 'owner_start': '4567', 'token': 'secret-token',
             'watcher_pid': 1235, 'watcher_start': '4568', 'heartbeat': 4102444800.0}
    _json_record(home / 'state' / 'owner.json', owner)
    observability.record_process_generation(home, 'central', '0', pid=1234, start='4567')
    return home, central, session


def _worker_case(tmp_path):
    home = tmp_path / 'home'
    data = home / 'data'
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    task_id = 'task-one'
    incarnation = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    task_dir = data / 'tasks' / task_id
    binding_dir = data / 'observability' / 'tasks' / task_id / 'omp' / incarnation
    runner_dir = home / 'state' / 'tasks' / task_id
    binding_dir.mkdir(parents=True)
    runner_dir.mkdir(parents=True)
    (home / 'state').mkdir(exist_ok=True)
    for directory in (home, data, data / 'observability', home / 'state'):
        directory.chmod(0o700)
    _json_record(data / 'home.json', {'home_id': '11111111-1111-1111-1111-111111111111'})
    task = {'id': task_id, 'executor': 'worker', 'incarnation': incarnation,
            'worktree': str(worktree)}
    _json_record(task_dir / 'task.json', task)
    _json_record(binding_dir / 'binding.json',
                 {'task_id': task_id, 'incarnation': incarnation,
                  'session_dir': str(binding_dir), 'state': 'directory allocated'})
    _json_record(runner_dir / 'runner.json',
                 {'incarnation': incarnation, 'phase': 'spawning', 'child_pid': 2222,
                  'child_start': '3333', 'child': {'pid': 2222, 'start': '3333', 'uid': os.getuid()},
                  'target': {'canonical_home': str(home)}})
    observability.record_process_generation(
        home, 'worker', '0', task_id=task_id, incarnation=incarnation,
        pid=2222, start='3333')
    session = binding_dir / 'session.jsonl'
    write_native(session, 'worker-session', [], cwd=worktree)
    return home, worktree, task_id, incarnation, session


def _fake_identity(monkeypatch, pid, start):
    identity = {'pid': pid, 'start': start, 'uid': os.getuid(), 'state': 'S'}
    monkeypatch.setattr(reviewer_episodes, '_caller_identity', lambda: identity)
    monkeypatch.setattr(reviewer_episodes, '_check_caller', lambda value: value)
    return identity


def _central_env(monkeypatch):
    monkeypatch.setenv('AGD_MANAGED_CENTRAL', '1')
    monkeypatch.setenv('AGD_ENDPOINT_TOKEN', 'secret-token')


def _complete_request(source_id, *, text='answer', **message):
    payload = {'role': 'assistant', 'timestamp': 200000, 'stopReason': 'stop',
               'content': [{'type': 'thinking', 'thinking': 'hidden'},
                           {'type': 'text', 'text': text}]}
    payload.update(message)
    return {'source_id': source_id, 'interaction_id': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
            'will_continue': False, 'awaiting_async_work': False, 'message': payload}


@pytest.mark.parametrize('role', ['central', 'worker'])
def test_valid_other_role_process_history_does_not_block_registration(tmp_path, monkeypatch, role):
    if role == 'central':
        home, _, session = _central_case(tmp_path)
        _fake_identity(monkeypatch, 1234, '4567')
        _central_env(monkeypatch)
        observability.record_process_generation(
            home, 'worker', '0', task_id='other', incarnation=str(uuid.uuid4()), pid=4321, start='6543')
    else:
        home, _, task_id, incarnation, session = _worker_case(tmp_path)
        _fake_identity(monkeypatch, 2222, '3333')
        monkeypatch.setenv('AGD_TASK_ID', task_id)
        monkeypatch.setenv('AGD_INCARNATION', incarnation)
        observability.record_process_generation(home, 'central', '0', pid=4321, start='6543')
    source = reviewer_episodes.register_source(
        home, {'role': role, 'session_id': role + '-session', 'session_file': str(session)})
    assert source['role'] == role and source['path'] == str(session.relative_to(home))


def test_register_central_authority_and_immutable_idempotent_completion(tmp_path, monkeypatch):
    home, central, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    original = reviewer_episodes.register_source(
        home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    repeated = reviewer_episodes.register_source(
        home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    assert repeated == original
    request = _complete_request(original['source_id'], text='final café')
    saved = reviewer_episodes.record_completion(home, request)
    assert 'message' not in saved and 'content' not in saved and 'secret-token' not in json.dumps(saved)
    assert saved['final']['text_sha256'] == hashlib.sha256('final café'.encode()).hexdigest()
    assert reviewer_episodes.record_completion(home, request) == saved
    with pytest.raises(store.Refusal):
        reviewer_episodes.record_completion(home, {**request, 'message': {**request['message'], 'content': 'changed'}})


def test_worker_authority_requires_exact_binding_incarnation_and_child_receipt(tmp_path, monkeypatch):
    home, worktree, task_id, incarnation, session = _worker_case(tmp_path)
    _fake_identity(monkeypatch, 2222, '3333')
    monkeypatch.setenv('AGD_TASK_ID', task_id)
    monkeypatch.setenv('AGD_INCARNATION', incarnation)
    source = reviewer_episodes.register_source(
        home, {'role': 'worker', 'session_id': 'worker-session', 'session_file': str(session)})
    assert source['task_id'] == task_id and source['incarnation'] == incarnation
    monkeypatch.setenv('AGD_INCARNATION', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'worker', 'session_id': 'worker-session', 'session_file': str(session)})



def test_central_registration_requires_matching_environment_token(tmp_path, monkeypatch):
    home, _, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    monkeypatch.setenv('AGD_MANAGED_CENTRAL', '1')
    monkeypatch.delenv('AGD_ENDPOINT_TOKEN', raising=False)
    monkeypatch.delenv('AGD_OWNER', raising=False)
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})


def test_completion_rejects_pid_start_uid_substitution(tmp_path, monkeypatch):
    home, _, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    source = reviewer_episodes.register_source(
        home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    _fake_identity(monkeypatch, 9876, '9999')
    with pytest.raises(store.Refusal):
        reviewer_episodes.record_completion(home, _complete_request(source['source_id']))

@pytest.mark.parametrize('field,value', [
    ('owner_pid', 9999), ('owner_start', 'different'),
])
def test_central_registration_rejects_owner_identity_substitution(tmp_path, monkeypatch, field, value):
    home, _, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    owner_path = home / 'state' / 'owner.json'
    owner = json.loads(owner_path.read_text())
    owner[field] = value
    owner_path.write_text(json.dumps(owner))
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})


def test_registration_rejects_reviewer_role_retirement_and_file_substitution(tmp_path, monkeypatch):
    home, _, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'reviewer', 'session_id': 'central-session', 'session_file': str(session)})
    session.unlink()
    session.symlink_to(home / 'state' / 'owner.json')
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    session.unlink()
    write_native(session, 'central-session', [])
    (home / 'state' / 'uninstall.json').write_text('{}')
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})


def test_registration_rejects_wrong_native_header_and_cwd(tmp_path, monkeypatch):
    home, central, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    write_native(session, 'different-session', [])
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    write_native(session, 'central-session', [])
    raw = json.loads(session.read_text().splitlines()[0])
    raw['cwd'] = str(central.parent)
    session.write_text(json.dumps(raw) + '\n')
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})


@pytest.mark.parametrize('request_mutation', [
    {'will_continue': True},
    {'awaiting_async_work': True},
    {'message': {'role': 'assistant', 'timestamp': 1, 'stopReason': 'error', 'content': 'bad'}},
    {'message': {'role': 'assistant', 'timestamp': 1, 'stopReason': 'stop',
                 'content': [{'type': 'toolCall', 'name': 'tool'}]}},
    {'message': {'role': 'assistant', 'timestamp': 1, 'stopReason': 'stop',
                 'content': [{'type': 'text', 'text': '   '}]}},
    {'message': {'role': 'assistant', 'timestamp': 1, 'stopReason': 'stop',
                 'content': [{'type': 'text', 'text': 'x' * 1048577}]}},
])
def test_completion_rejects_nonterminal_or_invalid_final_inputs(request_mutation):
    request = _complete_request('a' * 64)
    request.update(request_mutation)
    with pytest.raises(store.Refusal):
        reviewer_episodes._completion_request(request)


def test_completion_records_apply_cutoff_and_pending_native_read(tmp_path, monkeypatch):
    home, central, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    source = reviewer_episodes.register_source(
        home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    binding = {'type': 'custom_message', 'id': 'binding-id', 'parentId': None, 'timestamp': 100.0,
               'customType': 'agd:episode-binding', 'display': False, 'content': '',
               'details': {'schema': 1, 'source_id': source['source_id'],
                           'interaction_id': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'}}
    final = message('final-id', 'binding-id', 'answer', 200.0, role='assistant', stop_reason='stop')
    write_native(session, 'central-session', [binding])
    header = json.loads(session.read_text().splitlines()[0])
    header['cwd'] = str(home)
    lines = session.read_text().splitlines()
    session.write_text(json.dumps(header) + '\n' + '\n'.join(lines[1:]) + '\n')
    completion_value = reviewer_episodes.record_completion(
        home, _complete_request(source['source_id'], text='answer'))
    coverage = {}
    now = time.time()
    records = reviewer_episodes.completion_records(home, now - 1, coverage)
    assert records == [] and coverage['complete']
    pending = reviewer_episodes.read_native(session, now + 1, 4096, completion={**completion_value, 'source': source})
    assert pending['status'] == 'pending'
    with session.open('ab') as stream:
        stream.write(json.dumps(final, separators=(',', ':')).encode() + b'\n')
    records = reviewer_episodes.completion_records(home, now + 1, {})
    assert len(records) == 1 and records[0]['source'] == source
    ready = reviewer_episodes.read_native(session, now + 1, 4096, completion=records[0])
    assert ready['status'] == 'ready' and ready['boundary']['entry_id'] == 'final-id'


def test_worker_registration_waits_for_receipt_publication_barrier(tmp_path, monkeypatch):
    home, worktree, task_id, incarnation, session = _worker_case(tmp_path)
    process_receipts = home / 'state' / 'observability' / 'processes'
    for path in process_receipts.glob('*.json'):
        path.unlink()
    _fake_identity(monkeypatch, 2222, '3333')
    monkeypatch.setenv('AGD_TASK_ID', task_id)
    monkeypatch.setenv('AGD_INCARNATION', incarnation)
    real_once = reviewer_episodes._managed_process_receipt_once
    published = False
    writer = None
    writer_errors = []

    def write_receipt():
        try:
            observability.record_process_generation(
                home, 'worker', '0', task_id=task_id, incarnation=incarnation,
                pid=2222, start='3333')
        except Exception as error:
            writer_errors.append(error)

    def publish_after_first_missing(*args, **kwargs):
        nonlocal published, writer
        try:
            return real_once(*args, **kwargs)
        except reviewer_episodes._ManagedReceiptMissing:
            if not published:
                published = True
                writer = threading.Thread(target=write_receipt)
                writer.start()
            raise

    monkeypatch.setattr(reviewer_episodes, '_managed_process_receipt_once', publish_after_first_missing)
    source = reviewer_episodes.register_source(
        home, {'role': 'worker', 'session_id': 'worker-session', 'session_file': str(session)})
    writer.join(timeout=1)
    assert published and not writer.is_alive() and not writer_errors and source['task_id'] == task_id


def test_idempotent_reuse_refuses_tampered_source_and_completion(tmp_path, monkeypatch):
    home, _, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    source = reviewer_episodes.register_source(
        home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})
    source_path = home / 'state' / 'reviewer' / 'episodes' / 'sources' / f"{source['source_id']}.json"
    tampered_source = json.loads(source_path.read_text())
    tampered_source['message'] = 'raw body'
    source_path.write_text(json.dumps(tampered_source))
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})

    source_path.write_text(json.dumps(source))
    completion = reviewer_episodes.record_completion(home, _complete_request(source['source_id']))
    completion_path = (home / 'state' / 'reviewer' / 'episodes' / 'completions'
                       / source['source_id'] / f"{completion['interaction_id']}.json")
    tampered_completion = json.loads(completion_path.read_text())
    tampered_completion['content'] = 'raw body'
    completion_path.write_text(json.dumps(tampered_completion))
    with pytest.raises(store.Refusal):
        reviewer_episodes.record_completion(home, _complete_request(source['source_id']))


@pytest.mark.parametrize('value', [None, [], 'invalid', 4])
def test_non_object_managed_process_record_refuses_cleanly(tmp_path, monkeypatch, value):
    home, _, session = _central_case(tmp_path)
    _fake_identity(monkeypatch, 1234, '4567')
    _central_env(monkeypatch)
    receipt = next((home / 'state' / 'observability' / 'processes').glob('*.json'))
    receipt.write_text(json.dumps(value))
    with pytest.raises(store.Refusal):
        reviewer_episodes.register_source(
            home, {'role': 'central', 'session_id': 'central-session', 'session_file': str(session)})


def test_metadata_only_semantic_projection_proves_branch_without_bodies(tmp_path):
    path = tmp_path / 'native.jsonl'
    user, binding, final, later = episode_entries()
    value = completion(path, 'session')
    binding['details']['source_id'] = value['source_id']
    write_native(path, 'session', [user, binding, final, later])
    first = reviewer_episodes.read_native(path, 300, 4096, completion=value, metadata_only=True)
    assert first['status'] == 'ready'
    assert first['boundary']['entry_id'] == final['id']
    assert [row['message_id'] for row in first['projection']] == [user['id'], final['id']]
    assert [row['text_sha256'] for row in first['projection']] == [
        hashlib.sha256('héllo'.encode()).hexdigest(),
        hashlib.sha256('final café'.encode()).hexdigest()]
    assert all('text' not in row for row in first['projection'])
    assert 'héllo' not in json.dumps(first, ensure_ascii=False)
    assert 'final café' not in json.dumps(first, ensure_ascii=False)
    assert 'unfinished' not in json.dumps(first, ensure_ascii=False)
    write_native(path, 'session', [user, binding, final, later], title='different title')
    titled = reviewer_episodes.read_native(path, 300, 4096, completion=value, metadata_only=True)
    assert titled['projection'] == first['projection']
    user['message']['content'] = 'changed'
    write_native(path, 'session', [user, binding, final, later], title='different title')
    changed = reviewer_episodes.read_native(path, 300, 4096, completion=value, metadata_only=True)
    assert changed['projection'] != first['projection']
