import hashlib
import os
import json
import sys
import time
import uuid
import resource
from pathlib import Path

import pytest

from support import command, lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, reviewer_episodes, store, supervision, tasks, worker_skills, workers


def ended_worker(home, task_id, incarnation, rows, status=1):
    brief = home / f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief))
    task.update(executor='worker', incarnation=incarnation, phase='running', worker_alive=False)
    tasks.save(home, task)
    binding = observability.worker_binding(home, task_id, incarnation)
    session_id = str(uuid.uuid4())
    header = {'type': 'session', 'version': 3, 'id': session_id, 'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': str(home)}
    Path(binding['session_dir'], f'{session_id}.jsonl').write_text('\n'.join(json.dumps(row) for row in [header, *rows]) + '\n')
    note = tasks.task_file(home, task_id).parent / 'runner-exit.txt'; note.write_text(f'worker process exited {status}')
    workers.event(home, task_id, incarnation, 'failed' if status else 'idle', 'runner-exit', note)
    return task, binding, session_id


def message(text, when=100.0, role='user', content=None):
    return {'type': 'message', 'id': str(uuid.uuid4()), 'parentId': None, 'timestamp': when,
            'message': {'role': role, 'content': text if content is None else content, 'timestamp': int(when * 1000)}}


def packet(home, cutoff=None, limit=262144, retrospective=()):
    cutoff = time.time() if cutoff is None else cutoff
    config = reviewer.read_config(home); config['limits']['max_input_bytes'] = limit
    return reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config,
                                  retrospective_review_run_ids=retrospective)

def completed_session(home, *, role='central', task_id=None, texts=('answer',), cutoff=200.0,
                      active='unfinished', title=None, start_at=10.0, stable_ids=False, alive=False):
    incarnation = str(uuid.uuid4()) if role == 'worker' else None
    if role == 'worker':
        task, binding, session_id = ended_worker(home, task_id, incarnation, [], 0)
        if alive:
            task.update(worker_alive=True)
            tasks.save(home, task)
        native = Path(binding['session_dir']) / f'{session_id}.jsonl'
    else:
        session_id = str(uuid.uuid4())
        native = home / 'data' / 'observability' / 'central' / 'omp' / f'{session_id}.jsonl'
        native.parent.mkdir(parents=True, exist_ok=True)
    source = {'schema': 1, 'source_id': '', 'home': str(home.resolve()), 'role': role,
              'session_id': session_id, 'path': str(native.relative_to(home.resolve())),
              'cwd': str(home.resolve()), 'pid': 1, 'start': '1', 'uid': os.getuid(),
              'recorded_at': 1.0}
    if role == 'worker':
        source.update(task_id=task_id, incarnation=incarnation)
    source['source_id'] = reviewer_episodes.source_fingerprint(source)
    entries = []
    parent = None
    completions = []
    for number, text in enumerate(texts):
        prompt_id = 'prompt' if stable_ids else f'{session_id}-prompt-{number}'
        binding_id = 'binding' if stable_ids else f'{session_id}-binding-{number}'
        final_id = 'final' if stable_ids else f'{session_id}-final-{number}'
        prompt_at = start_at + number * 20
        binding_at = prompt_at + 1
        final_at = prompt_at + 2
        prompt = message('prompt', prompt_at)
        prompt.update(id=prompt_id, parentId=parent)
        entries.append(prompt)
        entries.append({'type': 'custom_message', 'id': binding_id, 'parentId': prompt_id,
                        'timestamp': binding_at, 'customType': 'agd:episode-binding', 'display': False,
                        'details': {'schema': 1, 'source_id': source['source_id'],
                                    'interaction_id': str(uuid.uuid5(uuid.NAMESPACE_URL, binding_id))}})
        final = message(text, final_at, role='assistant', content=text)
        final.update(id=final_id, parentId=binding_id)
        final['message'].update(stopReason='stop', isError=False)
        entries.append(final)
        interaction_id = str(uuid.uuid5(uuid.NAMESPACE_URL, binding_id))
        completions.append((interaction_id, final_at, text))
        parent = final_id
    active_row = message(active, cutoff + 1)
    active_row.update(id='active', parentId=parent)
    entries.append(active_row)
    header = {'type': 'session', 'version': 3, 'id': session_id,
              'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': str(home.resolve())}
    lines = []
    if title is not None:
        lines.append({'type': 'title', 'v': 1, 'title': title,
                      'updatedAt': '2026-01-01T00:00:00.000Z', 'pad': ''})
    lines.extend([header, *entries])
    native.write_text('\n'.join(json.dumps(row) for row in lines) + '\n')
    episode_paths = reviewer_episodes._episode_paths(home)
    reviewer_episodes._publish_immutable(episode_paths['sources'] / f"{source['source_id']}.json", source)
    for interaction_id, final_at, text in completions:
        record = {'schema': 1, 'source_id': source['source_id'], 'interaction_id': interaction_id,
                  'recorded_at': final_at + 0.1, 'will_continue': False,
                  'awaiting_async_work': False,
                  'final': {'timestamp': final_at * 1000,
                            'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                            'stop_reason': 'stop'}}
        reviewer_episodes._publish_immutable(
            episode_paths['completions'] / source['source_id'] / f'{interaction_id}.json', record)
    return source, native, task if role == 'worker' else None


def complete_review(home, selected):
    started = time.time()
    intent = reviewer.enqueue_intent(home, kind='manual')
    config = reviewer.read_config(home)
    run = reviewer.reserve_run(
        home, intent, config_snapshot=config, instruction_snapshot=b'fixture',
        started_at=started, deadline_epoch_ms=int((started + 1800) * 1000))
    reviewer.begin_run(home, run, selected, model='fixture/model', observed_omp_version='fixture')
    reviewer.submit_report(
        home, run['review_run_id'], str(uuid.uuid4()),
        {'schema': 1, 'summary': 'fixture', 'findings': []})
    return reviewer.finalize_run(home, run['review_run_id'], 'exited')

def append_episode(source, native, text, number, *, cutoff=200.0):
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    active = rows.pop()
    parent = next(row['id'] for row in reversed(rows) if row.get('type') == 'message')
    binding_id = f'{source["session_id"]}-binding-{number}'
    final_id = f'{source["session_id"]}-final-{number}'
    prompt_id = f'{source["session_id"]}-prompt-{number}'
    prompt_at = 10 + number * 20
    prompt = message('prompt', prompt_at)
    prompt.update(id=prompt_id, parentId=parent)
    rows.append(prompt)
    rows.append({'type': 'custom_message', 'id': binding_id, 'parentId': prompt_id,
                 'timestamp': prompt_at + 1, 'customType': 'agd:episode-binding', 'display': False,
                 'details': {'schema': 1, 'source_id': source['source_id'],
                             'interaction_id': str(uuid.uuid5(uuid.NAMESPACE_URL, binding_id))}})
    final = message(text, prompt_at + 2, role='assistant', content=text)
    final.update(id=final_id, parentId=binding_id)
    final['message'].update(stopReason='stop', isError=False)
    rows.append(final)
    rows.append({**active, 'parentId': final_id})
    native.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    interaction_id = str(uuid.uuid5(uuid.NAMESPACE_URL, binding_id))
    record = {'schema': 1, 'source_id': source['source_id'], 'interaction_id': interaction_id,
              'recorded_at': prompt_at + 2.1, 'will_continue': False, 'awaiting_async_work': False,
              'final': {'timestamp': (prompt_at + 2) * 1000,
                        'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'stop_reason': 'stop'}}
    paths = reviewer_episodes._episode_paths(Path(source['home']))
    reviewer_episodes._publish_immutable(
        paths['completions'] / source['source_id'] / f'{interaction_id}.json', record)


def rewrite_title(native, title):
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    title_row = {'type': 'title', 'v': 1, 'title': title,
                 'updatedAt': '2026-01-01T00:00:00.000Z', 'pad': ''}
    native.write_text(json.dumps(title_row) + '\n' + '\n'.join(json.dumps(row) for row in rows) + '\n')

def append_unfinished(native, text, when=300.0):
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    active = rows.pop()
    parent = next(row['id'] for row in reversed(rows) if row.get('type') == 'message')
    tail = message(text, when)
    tail.update(id='unfinished-tail', parentId=parent)
    rows.append(tail)
    rows.append({**active, 'parentId': 'unfinished-tail'})
    native.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')


def test_messages_tracks_physical_prefix_across_malformed_rows(tmp_path):
    header = b'{\"type\":\"session\",\"version\":3,\"id\":\"native\"}\n'
    bad = b'{bad}\n'; array = b'[]\n'
    late = json.dumps(message('late', 300)).encode() + b'\n'
    valid = json.dumps(message('kept', 100)).encode() + b'\n'
    path = tmp_path / 'native.jsonl'; path.write_bytes(header + bad + array + late + valid)
    result = reviewer_episodes.read_native(path, 200, 4096)
    body = path.read_bytes()
    assert result['byte_watermark'] == len(body) and result['content_sha256'] == __import__('hashlib').sha256(body).hexdigest()
    assert result['rows'][0]['text'] == 'kept' and body[result['rows'][0]['byte_start']:result['rows'][0]['byte_end']] == valid
    assert result['late'] and [item['reason'] for item in result['withheld']].count('native-row-malformed') == 2


def test_native_followup_identity_excludes_physical_proof():
    first = {'kind': 'native-jsonl', 'task_id': 'task', 'incarnation': 'inc', 'session_id': 'session',
             'path': 'session.jsonl', 'byte_watermark': 100, 'content_sha256': 'a' * 64,
             'material_sha256': 'b' * 64}
    second = {**first, 'byte_watermark': 356, 'content_sha256': 'c' * 64}
    row1 = {'message_id': 'message', 'text': 'evidence', 'line_start': 2, 'line_end': 2,
            'byte_start': 20, 'byte_end': 100}
    row2 = {**row1, 'line_start': 3, 'line_end': 3, 'byte_start': 276, 'byte_end': 356}
    assert reviewer._digest(reviewer._native_material_identity(first)) == reviewer._digest(reviewer._native_material_identity(second))
    assert reviewer._native_excerpt_id('source', row1) == reviewer._native_excerpt_id('source', row2)


def test_material_identity_is_scoped_to_home_role_session_and_native_message():
    scope = {'home': '/tmp/home', 'role': 'worker', 'session_id': 'session',
             'task_id': 'task', 'incarnation': 'incarnation'}
    row = {'message_id': 'same', 'text': 'same'}
    assert reviewer._source_candidate_id(scope) != reviewer._source_candidate_id(
        {**scope, 'session_id': 'other'})
    assert reviewer._source_candidate_id(scope) != reviewer._source_candidate_id(
        {'home': '/tmp/home', 'role': 'central', 'session_id': 'session'})
    assert reviewer._material_id(scope, row) != reviewer._material_id(
        scope, {'message_id': 'other', 'text': 'same'})
    assert reviewer._material_id(scope, row) != reviewer._material_id(
        scope, {'message_id': 'same', 'text': 'changed'})

def test_completed_worker_and_taskless_central_admit_only_closed_material():
    with lab() as (home, _):
        worker_source, _, _ = completed_session(home, role='worker', task_id='alive',
                                                texts=('worker answer',), alive=True)
        central_source, _, _ = completed_session(home, texts=('central answer',))
        selected = packet(home, cutoff=200)
        assert {episode['role'] for episode in selected['episodes']} == {'worker', 'central'}
        assert all('task_id' not in episode for episode in selected['episodes']
                   if episode['role'] == 'central')
        assert {row['text'] for row in selected['excerpts']} >= {'worker answer', 'central answer'}
        assert 'unfinished' not in '\n'.join(row['text'] for row in selected['excerpts'])
        assert worker_source['source_id'] != central_source['source_id']

def test_confirmed_exit_preserves_unreviewed_tail_after_proved_completion():
    with lab() as (home, _):
        _, native, _ = completed_session(home, role='worker', task_id='exit-tail',
                                         texts=('proved answer',))
        append_unfinished(native, 'post-exit evidence')
        selected = packet(home, cutoff=time.time() + 10)
        assert {row['text'] for row in selected['excerpts']} >= {'proved answer', 'post-exit evidence'}

def test_finalization_consumes_first_response_and_admits_next_response():
    with lab() as (home, _):
        source, native, _ = completed_session(home, texts=('first answer',))
        first = packet(home, cutoff=200)
        complete_review(home, first)
        append_episode(source, native, 'second answer', 1)
        second = packet(home, cutoff=250)
        assert 'first answer' not in {row['text'] for row in second['excerpts']}
        assert 'second answer' in {row['text'] for row in second['excerpts']}
        complete_review(home, second)
        assert packet(home, cutoff=300)['excerpts'] == []

def test_multiple_episodes_and_repeated_native_ids_are_source_isolated():
    with lab() as (home, _):
        first_source, _, _ = completed_session(home, texts=('same answer',), stable_ids=True)
        second_source, _, _ = completed_session(home, texts=('same answer',), stable_ids=True)
        third_source, _, _ = completed_session(home, texts=('next answer', 'later answer'))
        selected = packet(home, cutoff=200)
        material_ids = [row['material_id'] for row in selected['excerpts']]
        assert len(material_ids) == len(set(material_ids))
        assert {row['text'] for row in selected['excerpts']} >= {'same answer', 'next answer'}
        native = [source for source in selected['sources'] if source['kind'] == 'native-jsonl']
        assert {source['candidate_id'] for source in native} >= {
            reviewer._source_candidate_id({'home': str(home.resolve()), 'role': 'central',
                                           'session_id': first_source['session_id']}),
            reviewer._source_candidate_id({'home': str(home.resolve()), 'role': 'central',
                                           'session_id': second_source['session_id']})}

def test_title_only_native_rewrite_does_not_create_new_material():
    with lab() as (home, _):
        _, native, _ = completed_session(home, texts=('stable answer',))
        first = packet(home, cutoff=200)
        complete_review(home, first)
        rewrite_title(native, 'renamed session')
        unchanged = packet(home, cutoff=250)
        assert unchanged['excerpts'] == []

@pytest.mark.parametrize('outcome', ['failed', 'no-work'])
def test_failed_or_no_work_review_does_not_consume_retained_material(outcome):
    with lab() as (home, _):
        completed_session(home, texts=('retryable answer',))
        first = packet(home, cutoff=200)
        started = time.time()
        intent = reviewer.enqueue_intent(home, kind='manual')
        config = reviewer.read_config(home)
        run = reviewer.reserve_run(
            home, intent, config_snapshot=config, instruction_snapshot=b'fixture',
            started_at=started, deadline_epoch_ms=int((started + 1800) * 1000))
        reviewer.begin_run(home, run, packet=first, model='fixture/model', observed_omp_version='fixture')
        reviewer.close_run(home, run['review_run_id'], outcome, packet=first)
        retry = packet(home, cutoff=250)
        assert {row['text'] for row in retry['excerpts']} >= {'retryable answer'}

def test_single_session_cap_skips_consumed_prefix_and_eventually_retains_tail():
    with lab() as (home, _):
        texts = tuple(f'evidence-{number}-' + ('x' * 2200) for number in range(8))
        completed_session(home, texts=texts)
        config = reviewer.read_config(home)
        config['limits'].update(max_input_bytes=15000, max_candidates=20)
        consumed, packet_count = set(), 0
        while packet_count < len(texts) + 2:
            selected = reviewer.select_packet(home, cutoff=250 + packet_count,
                                              config_snapshot=config)
            retained = {(row['message_id'], row['text']) for row in selected['excerpts']}
            if not retained:
                break
            assert not consumed.intersection(retained)
            consumed.update(retained)
            complete_review(home, selected)
            packet_count += 1
        assert set(texts) <= {text for _, text in consumed}
        assert packet_count > 1

def test_candidate_cap_leaves_omitted_episodes_eligible():
    with lab() as (home, _):
        texts = ('candidate-a', 'candidate-b', 'candidate-c')
        for index, text in enumerate(texts):
            completed_session(home, texts=(text,), stable_ids=True)
        config = reviewer.read_config(home)
        config['limits']['max_candidates'] = 1
        seen = set()
        for offset in range(5):
            selected = reviewer.select_packet(home, cutoff=250 + offset, config_snapshot=config)
            retained = {row['text'] for row in selected['excerpts']}
            if not retained:
                break
            seen.update(retained)
            complete_review(home, selected)
        assert set(texts) <= seen

def test_pending_completion_waits_for_fresh_cutoff_without_mutating_old_packet():
    with lab() as (home, _):
        completed_session(home, texts=('future answer',), cutoff=200, start_at=1000)
        frozen = packet(home, cutoff=200)
        assert frozen['episodes'] == [] and frozen['excerpts'] == []
        later = packet(home, cutoff=1100)
        assert {row['text'] for row in later['excerpts']} >= {'future answer'}
        assert packet(home, cutoff=200)['packet_id'] == frozen['packet_id']

def test_pending_native_proof_marks_incomplete_and_later_finalizes_without_mutating_packet():
    with lab() as (home, _):
        source, native, _ = completed_session(home, texts=('pending answer',))
        lines = native.read_text().splitlines()
        native.write_text('\n'.join(lines[:2]) + '\n')
        pending = packet(home, cutoff=200)
        assert pending['episodes'] == [] and pending['excerpts'] == []
        assert pending['coverage']['complete'] is False
        assert {'source_id': source['source_id'],
                'interaction_id': str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                 f'{source["session_id"]}-binding-0')),
                'reason': 'native-proof-pending'} in pending['coverage']['unavailable']
        native_packet_id = pending['packet_id']
        with native.open('a') as stream:
            stream.write('\n'.join(lines[2:]) + '\n')
        later = packet(home, cutoff=250)
        assert {row['text'] for row in later['excerpts']} >= {'pending answer'}
        complete_review(home, later)
        assert packet(home, cutoff=300)['excerpts'] == []
        assert packet(home, cutoff=200) == pending

def test_legacy_reviewed_packet_migrates_only_matching_source_scope():
    with lab() as (home, _):
        source, native, task = completed_session(home, role='worker', task_id='legacy',
                                                texts=('legacy answer',))
        row = {'message_id': 'legacy-message', 'text': 'legacy answer',
               'line_start': 2, 'line_end': 2, 'byte_start': 0, 'byte_end': 1}
        old_source = {'kind': 'native-jsonl', 'task_id': task['id'], 'incarnation': task['incarnation'],
                      'session_id': source['session_id'], 'path': str(native.relative_to(home)),
                      'byte_watermark': 1, 'content_sha256': 'a' * 64, 'material_sha256': 'b' * 64}
        packet_value = {'schema': 1, 'purpose': 'background-skill-review', 'cutoff': 200,
                        'episodes': [{'task_id': task['id'], 'incarnation': task['incarnation']}],
                        'sources': [{**old_source, 'source_id': 'c' * 64}],
                        'excerpts': [{**row, 'source_id': 'c' * 64, 'excerpt_id': 'd' * 64}],
                        'catalog': [], 'skill_provenance': [], 'coverage': {'followup_id': 'e' * 64},
                        'retrospective_review_run_ids': [], 'retrospective_episodes': [],
                        'config_snapshot_sha256': 'f' * 64}
        packet_id = reviewer._digest(packet_value)
        reviewer.publish_immutable(reviewer.paths(home)['packets'] / f'{packet_id}.json', packet_value)
        reviewer.store.atomic_json(
            reviewer.paths(home)['followups'] / f"{'e' * 64}.json",
            {'schema': 1, 'material_basis': 'e' * 64, 'state': 'reviewed',
             'candidates': [{'candidate_id': reviewer._candidate_id(task['id'], task['incarnation']),
                             'excerpt_ids': [f"legacy-message:{hashlib.sha256(b'legacy answer').hexdigest()}"]}]})
        reviewer.store.atomic_json(
            reviewer.paths(home)['checkpoints'] / 'legacy.json',
            {'scope': {}, 'packet_id': packet_id, 'material_basis': 'e' * 64})
        scope = {'home': str(home.resolve()), 'role': 'worker', 'session_id': source['session_id'],
                 'task_id': task['id'], 'incarnation': task['incarnation']}
        assert reviewer._material_id(scope, row) in reviewer._reviewed_material(home, scope)
        assert reviewer._legacy_material_id(row) not in reviewer._reviewed_material(home, scope)

def test_legacy_reviewed_prefix_skips_before_budget_for_new_same_session_completion():
    with lab() as (home, _):
        old_texts = tuple(f'old answer {index} ' + ('o' * 5000) for index in range(4))
        source, native, task = completed_session(
            home, role='worker', task_id='legacy-prefix', texts=old_texts)
        original = packet(home, cutoff=200, limit=100000)
        old_ids = {row['message_id'] for row in original['excerpts']}
        followup_id = 'a' * 64
        legacy = json.loads(json.dumps(original))
        legacy.pop('packet_id', None)
        legacy['coverage']['followup_id'] = followup_id
        legacy_ids = []
        for row in legacy['excerpts']:
            row.pop('material_id', None)
            legacy_ids.append(reviewer._legacy_material_id(row))
        packet_id = reviewer._digest(legacy)
        reviewer.publish_immutable(reviewer.paths(home)['packets'] / f'{packet_id}.json', legacy)
        scope = reviewer._source_scope(home, source)
        reviewer.store.atomic_json(
            reviewer.paths(home)['followups'] / f'{followup_id}.json',
            {'schema': 1, 'material_basis': followup_id, 'state': 'reviewed',
            'candidates': [{'candidate_id': reviewer._candidate_id(task['id'], task['incarnation']),
                             'excerpt_ids': legacy_ids}]})
        reviewer.store.atomic_json(
            reviewer.paths(home)['checkpoints'] / 'legacy-prefix.json',
            {'scope': {}, 'packet_id': packet_id, 'material_basis': followup_id})
        append_episode(source, native, 'new answer', len(old_texts))
        selected = packet(home, cutoff=250, limit=16000)
        selected_ids = {row['message_id'] for row in selected['excerpts']}
        assert not old_ids.intersection(selected_ids)
        assert 'new answer' in {row['text'] for row in selected['excerpts']}
        complete_review(home, selected)
        assert packet(home, cutoff=300, limit=16000)['excerpts'] == []


def test_exited_worker_multiple_native_sessions_each_stay_reviewed():
    with lab() as (home, _):
        task, binding, first_session = ended_worker(
            home, 'multi-native', str(uuid.uuid4()), [message('first source', 100)], 0)
        second_session = str(uuid.uuid4())
        second = Path(binding['session_dir']) / f'{second_session}.jsonl'
        header = {'type': 'session', 'version': 3, 'id': second_session,
                  'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': str(home)}
        second.write_text('\n'.join(json.dumps(row) for row in [
            header, message('second source', 100)]) + '\n')
        first = packet(home, cutoff=time.time() + 10)
        assert {source['session_id'] for source in first['sources']
                if source['kind'] == 'native-jsonl'} >= {first_session, second_session}
        complete_review(home, first)
        retry = packet(home, cutoff=time.time() + 20)
        assert retry['excerpts'] == []


def test_capped_completion_episodes_keep_exact_source_boundaries():
    with lab() as (home, _):
        completed_session(home, texts=('a' * 3000, 'b' * 3000))
        full_config = reviewer.read_config(home)
        full_config['limits']['max_input_bytes'] = 1000000
        full = reviewer.select_packet(home, cutoff=250, config_snapshot=full_config)
        full_size = len(reviewer._json_bytes({key: value for key, value in full.items()
                                              if key != 'packet_id'}))
        config = reviewer.read_config(home)
        config['limits']['max_input_bytes'] = full_size - 1
        selected = reviewer.select_packet(home, cutoff=251, config_snapshot=config)
        assert selected['coverage']['late_basis']
        source_ids = {source['source_id'] for source in selected['sources']}
        excerpt_sources = {excerpt['source_id'] for excerpt in selected['excerpts']}
        for episode in selected['episodes']:
            if episode.get('source_id'):
                assert episode['source_id'] in source_ids & excerpt_sources

def test_packet_collects_closed_failure_and_success_with_stable_cited_spans():
    with lab() as (home, _):
        failed, _, _ = ended_worker(home, 'failed', str(uuid.uuid4()), [message('failure')])
        succeeded, _, _ = ended_worker(home, 'success', str(uuid.uuid4()), [message('counterexample')], 0)
        first = packet(home); second = packet(home)
        native = [source for source in first['sources'] if source['kind'] == 'native-jsonl']
        assert {episode['task_id'] for episode in first['episodes']} == {failed['id'], succeeded['id']}
        assert {excerpt['text'] for excerpt in first['excerpts']} == {'failure', 'counterexample'}
        assert all(excerpt['message_id'] and excerpt['line_start'] <= excerpt['line_end'] for excerpt in first['excerpts'])
        assert [source['source_id'] for source in first['sources']] == [source['source_id'] for source in second['sources']]
        assert len({source['source_id'] for source in native}) == 2


def test_packet_excludes_late_rows_and_records_followup_basis():
    with lab() as (home, _):
        ended_worker(home, 'late', str(uuid.uuid4()), [message('before', 100), message('after', 300)])
        event = next((tasks.task_file(home, 'late').parent / 'observations').glob('*.json'))
        observed = json.loads(event.read_text()); observed['observed_at'] = 150.0; store.atomic_json(event, observed)
        selected = packet(home, cutoff=200)
        assert [row['text'] for row in selected['excerpts']] == ['before']
        assert selected['coverage']['late_basis'][0]['reason'] == 'post-cutoff-native-row'


def test_late_append_does_not_change_frozen_source_identity():
    with lab() as (home, _):
        task, binding, session = ended_worker(home, 'identity', str(uuid.uuid4()), [message('kept', 100)])
        event = next((tasks.task_file(home, task['id']).parent / 'observations').glob('*.json'))
        value = json.loads(event.read_text()); value['observed_at'] = 150.0; store.atomic_json(event, value)
        first = packet(home, cutoff=200)
        jsonl = Path(binding['session_dir']) / f'{session}.jsonl'
        with jsonl.open('a') as stream:
            stream.write(json.dumps(message('future', 300)) + '\n')
        second = packet(home, cutoff=200)
        assert first['sources'][0]['source_id'] == second['sources'][0]['source_id']


def test_packet_reports_unsupported_blocks_and_whole_packet_limit():
    with lab() as (home, _):
        ended_worker(home, 'bounded', str(uuid.uuid4()), [message('a' * 200, content=[{'type': 'text', 'text': 'ok'}, {'type': 'image'}])])
        selected = packet(home, limit=10000)
        assert len(reviewer._json_bytes({key: value for key, value in selected.items() if key != 'packet_id'})) <= 10000
        assert any(row['reason'] == 'unsupported-content-block' for row in selected['coverage']['withheld'])
        assert selected['coverage']['complete'] is False


def test_packet_rejects_redirected_binding_and_reports_malformed_inputs():
    with lab() as (home, _):
        task, binding, _ = ended_worker(home, 'unsafe', str(uuid.uuid4()), [message('secret')])
        outside = home / 'outside'; outside.mkdir()
        binding_path = Path(binding['session_dir']) / 'binding.json'
        data = json.loads(binding_path.read_text()); data['session_dir'] = str(outside); store.atomic_json(binding_path, data)
        malformed = home / 'data' / 'tasks' / 'broken'; malformed.mkdir(); (malformed / 'task.json').write_text('{bad')
        selected = packet(home)
        assert selected['episodes'] == []
        assert any(row['reason'] == 'native-jsonl-unavailable' and row['task_id'] == task['id'] for row in selected['coverage']['unavailable'])
        assert any(row['reason'] == 'task-record-malformed' for row in selected['coverage']['unavailable'])


def test_packet_freezes_native_catalog_and_assigned_manifest_without_mutation():
    with lab() as (home, _):
        source = Path(next(
            row['source'] for row in worker_skills.catalog(home)
            if row['id'] == 'systematic-debugging'))
        raw = (b'---\r\nname: systematic-debugging\r\n'
               b'description: Caf\xc3\xa9 debugging\r\n---\r\n'
               + '\xc3\xa9'.encode() * 400)
        (source/'SKILL.md').write_bytes(raw)
        task, _, _ = ended_worker(
            home, 'skills', str(uuid.uuid4()), [message('evidence')])
        assigned = next(
            row for row in task['worker_skills']['skills']
            if row['id'] == 'systematic-debugging')
        watched = [
            home/'.omp/plugins/installed_plugins.json',
            home/'.omp/plugins/omp-plugins.lock.json',
            home/'config/workers.json',
            source/'SKILL.md',
            Path(assigned['snapshot'])/'SKILL.md',
        ]
        before = {str(path): path.read_bytes() for path in watched}
        selected = packet(home)
        catalog_entry = next(
            row for row in selected['catalog']
            if row['id'] == 'systematic-debugging')
        assert catalog_entry['catalog_sha256'] == __import__('hashlib').sha256(raw).hexdigest()
        assert catalog_entry['snapshot_truncated'] is True
        assert catalog_entry['snapshot_total_bytes'] == len(raw)
        assert catalog_entry['snapshot_excerpt'] == raw[:256].decode('utf-8', 'replace')
        assert 'systematic-debugging' in {
            row['id'] for row in selected['skill_provenance'][0]['assigned']}
        assert selected['skill_provenance'][0]['usage'] == 'unknown'
        assert before == {str(path): path.read_bytes() for path in watched}


def test_packet_collects_explicit_linked_decision_answer_as_partial_central_evidence():
    with lab() as (home, _):
        task, _, _ = ended_worker(home, 'linked', str(uuid.uuid4()), [message('work')])
        question = tasks.task_file(home, task['id']).parent / 'question.txt'; question.write_text('Which?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
        supervision.reconcile(home)
        answer = tasks.task_file(home, task['id']).parent / 'answer.txt'; answer.write_text('Use safe path.\n')
        supervision.resolve(home, task['id'], 'choice', answer)
        selected = packet(home)
        central = [source for source in selected['sources'] if source['kind'] == 'central-decision']
        assert central and central[0]['linked_task_id'] == task['id'] and central[0]['resolved_at'] > 0
        assert selected['coverage']['central_feedback'] == 'active-partial'

def test_old_incarnation_collects_only_its_resolved_central_feedback():
    with lab() as (home, _):
        task, _, _ = ended_worker(home, 'incarnations', str(uuid.uuid4()), [message('old work')])
        old = task['incarnation']
        old_question = tasks.task_file(home, task['id']).parent / 'old.txt'; old_question.write_text('old?\n')
        workers.event(home, task['id'], old, 'decision', 'old-choice', old_question); supervision.reconcile(home)
        old_answer = tasks.task_file(home, task['id']).parent / 'old-answer.txt'; old_answer.write_text('old answer\n')
        supervision.resolve(home, task['id'], 'old-choice', old_answer)
        task.update(incarnation=str(uuid.uuid4()), worker_alive=False); tasks.save(home, task)
        new_question = tasks.task_file(home, task['id']).parent / 'new.txt'; new_question.write_text('new?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'new-choice', new_question); supervision.reconcile(home)
        new_answer = tasks.task_file(home, task['id']).parent / 'new-answer.txt'; new_answer.write_text('new answer\n')
        supervision.resolve(home, task['id'], 'new-choice', new_answer)
        selected = packet(home)
        old_feedback = [row['text'] for row in selected['excerpts'] if row['text'] in ('old answer\n', 'new answer\n')]
        assert old_feedback == ['old answer\n']

def test_retrospective_includes_committed_finalized_report():
    with lab() as (home, _):
        run, selected = produced_run(home)
        submitted = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), build_report(selected))
        binding = observability.reviewer_binding(home, run['review_run_id'], 'native-review')
        Path(binding['session_dir'], 'native-review.jsonl').write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session', 'version': 3, 'id': 'native-review', 'timestamp': '1970-01-01T00:00:00Z'}, message('review native', time.time())]) + '\n')
        reviewer.finalize_run(home, run['review_run_id'], 'exited')
        retrospective = packet(home, cutoff=time.time() + 1, retrospective=(run['review_run_id'],))
        assert any(source['kind'] == 'reviewer-report' and source['report_id'] == submitted['report_id'] for source in retrospective['sources'])


def test_retrospective_requires_closed_prior_receipt_and_excludes_later_runs():
    with lab() as (home, _):
        prior, later = str(uuid.uuid4()), str(uuid.uuid4())
        receipts = reviewer.paths(home)['receipts']; receipts.mkdir(parents=True)
        store.atomic_json(receipts / f'{prior}.json', {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': prior, 'dispatch_state': 'final', 'closed_at': 100.0})
        store.atomic_json(receipts / f'{later}.json', {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': later, 'dispatch_state': 'final', 'closed_at': 300.0})
        binding = observability.reviewer_binding(home, prior, 'native-review')
        native = Path(binding['session_dir']) / 'native-review.jsonl'
        native.write_text('\n'.join(json.dumps(row) for row in [{'type': 'session', 'version': 3, 'id': 'native-review', 'timestamp': '1970-01-01T00:00:00Z'}, message('retrospective evidence', 100)]) + '\n')
        selected = packet(home, cutoff=200, retrospective=(prior,))
        assert selected['retrospective_episodes'][0]['review_run_id'] == prior
        assert any(source['kind'] == 'reviewer-native-jsonl' for source in selected['sources'])
        assert selected['coverage']['retrospective_excluded'] == [later]
        with pytest.raises(store.Refusal):
            packet(home, retrospective=(later,))


def test_default_intents_coalesce_identical_scope_but_retrospectives_do_not():
    with lab() as (home, _):
        first = reviewer.enqueue_intent(home, kind='default'); second = reviewer.enqueue_intent(home, kind='default')
        one = reviewer.enqueue_intent(home, kind='retrospective', retrospective_review_run_ids=('a',)); two = reviewer.enqueue_intent(home, kind='retrospective', retrospective_review_run_ids=('a',))
        assert first['intent_id'] == second['intent_id']
        assert one['intent_id'] != two['intent_id']



def test_reserved_default_intent_is_consumed_before_next_identical_trigger():
    with lab() as (home, _):
        intent = reviewer.enqueue_intent(home, kind='default')
        reviewer.reserve_run(home, intent, config_snapshot=reviewer.read_config(home), instruction_snapshot=b'frozen',
                             started_at=1.0, deadline_epoch_ms=2)
        assert reviewer.enqueue_intent(home, kind='default')['intent_id'] != intent['intent_id']


def test_frozen_scope_reuses_packet_and_new_scope_creates_one_material_followup():
    with lab() as (home, _):
        task, binding, session = ended_worker(home, 'frozen', str(uuid.uuid4()), [message('first', 100)])
        event = next((tasks.task_file(home, task['id']).parent / 'observations').glob('*.json'))
        value = json.loads(event.read_text()); value['observed_at'] = 150.0; store.atomic_json(event, value)
        frozen = packet(home, cutoff=200)
        jsonl = Path(binding['session_dir']) / f'{session}.jsonl'
        with jsonl.open('a') as stream:
            stream.write(json.dumps(message('backdated arrival', 150)) + '\n')
        assert packet(home, cutoff=200)['packet_id'] == frozen['packet_id']
        followup = packet(home, cutoff=400)
        repeated = packet(home, cutoff=400)
        assert {row['text'] for row in followup['excerpts']} == {'first', 'backdated arrival'}
        assert followup['coverage']['followup_id'] == repeated['coverage']['followup_id']
        with jsonl.open('a') as stream:
            stream.write(json.dumps(message('further material', 350)) + '\n')
        newer = packet(home, cutoff=500)
        assert newer['coverage']['followup_id'] != followup['coverage']['followup_id']


def test_large_native_history_keeps_only_bounded_selected_evidence():
    with lab() as (home, _):
        rows = [message(f'{number}-' + 'x' * 400, 100) for number in range(100)]
        task, _, _ = ended_worker(home, 'large', str(uuid.uuid4()), rows)
        selected = packet(home, limit=12000)
        assert len(selected['excerpts']) < len(rows)
        assert sum(len(row['text'].encode()) for row in selected['excerpts']) < 12000
        session = home / 'data' / 'observability' / 'tasks' / task['id'] / 'omp' / task['incarnation']
        assert selected['sources'][0]['byte_watermark'] == next(session.glob('*.jsonl')).stat().st_size
        assert any(row['reason'] == 'input-limit' for row in selected['coverage']['late_basis'])


def test_material_state_consumes_once_without_recreating_same_basis():
    with lab() as (home, _):
        task, _, _ = ended_worker(home, 'material', str(uuid.uuid4()), [message('evidence', 100)])
        first = packet(home, cutoff=200)
        basis = first['coverage']['followup_id']
        reviewer.mark_material_reviewed(home, basis)
        assert reviewer.material_state(home, basis)['state'] == 'reviewed'
        assert packet(home, cutoff=300)['coverage']['followup_id'] == basis
        assert reviewer.material_state(home, basis)['state'] == 'reviewed'


def test_reviewed_candidates_advance_and_new_material_resurfaces_once():
    with lab() as (home, _):
        workers_data = [ended_worker(home, name, str(uuid.uuid4()), [message(name, 100)])
                        for name in ('first', 'second', 'third')]
        config = reviewer.read_config(home); config['limits']['max_candidates'] = 2
        cutoff = time.time() + 100
        first = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
        assert [item['task_id'] for item in first['episodes']] == ['first', 'second']
        reviewer.mark_material_reviewed(home, first['coverage']['followup_id'])
        second = reviewer.select_packet(home, cutoff=cutoff + 1, config_snapshot=config)
        assert [item['task_id'] for item in second['episodes']] == ['third']
        reviewer.mark_material_reviewed(home, second['coverage']['followup_id'])
        binding, session = workers_data[0][1:]
        with (Path(binding['session_dir']) / f'{session}.jsonl').open('a') as stream:
            stream.write(json.dumps(message('new first material', 150)) + '\n')
        resurfaced = reviewer.select_packet(home, cutoff=cutoff + 2, config_snapshot=config)
        assert [item['task_id'] for item in resurfaced['episodes']] == ['first']
        reviewer.mark_material_reviewed(home, resurfaced['coverage']['followup_id'])
        assert reviewer.select_packet(home, cutoff=cutoff + 3, config_snapshot=config)['episodes'] == []


def test_shared_cap_revisits_clipped_material_without_starving_next():
    with lab() as (home, _):
        fixtures = {
            'a': [message('a' * 2500, 100)],
            'b': [message('b1' * 1200, 100), message('b2' * 1200, 101)],
            'c': [message('c' * 2500, 100)],
        }
        expected = set()
        for task_id, rows in fixtures.items():
            task, _, _ = ended_worker(home, task_id, str(uuid.uuid4()), rows)
            expected.update((task['id'], row['id']) for row in rows)
        config = reviewer.read_config(home)
        cap = 17000
        config['limits'].update(max_candidates=3, max_input_bytes=cap)
        consumed = set()
        packets = 0
        while True:
            selected = reviewer.select_packet(home, cutoff=time.time() + packets + 100, config_snapshot=config)
            assert len(reviewer._json_bytes({key: value for key, value in selected.items() if key != 'packet_id'})) <= cap
            sources = {source['source_id']: source for source in selected['sources']}
            retained = {(sources[excerpt['source_id']]['task_id'], excerpt['message_id'])
                        for excerpt in selected['excerpts']}
            if not retained:
                assert selected['episodes'] == []
                break
            assert not consumed.intersection(retained)
            consumed.update(retained)
            reviewer.mark_material_reviewed(home, selected['coverage']['followup_id'])
            packets += 1
            assert packets <= len(expected)
        assert consumed == expected


def test_reviewed_oversized_candidate_does_not_repeat_its_admitted_prefix():
    with lab() as (home, _):
        ended_worker(home, 'oversized', str(uuid.uuid4()), [message('x' * 1000) for _ in range(40)])
        config = reviewer.read_config(home); config['limits']['max_input_bytes'] = 12000; cutoff = time.time() + 100
        first = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
        reviewer.mark_material_reviewed(home, first['coverage']['followup_id'])
        next_packet = reviewer.select_packet(home, cutoff=cutoff + 1, config_snapshot=config)
        assert [row['message_id'] for row in next_packet['excerpts']] != [row['message_id'] for row in first['excerpts']]


def test_same_size_native_rewrite_resurfaces_reviewed_candidate_once():
    with lab() as (home, _):
        task, binding, session = ended_worker(home, 'rewrite', str(uuid.uuid4()), [message('alpha', 100)])
        config = reviewer.read_config(home); cutoff = time.time() + 100
        first = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
        reviewer.mark_material_reviewed(home, first['coverage']['followup_id'])
        native = Path(binding['session_dir']) / f'{session}.jsonl'
        native.write_text(native.read_text().replace('alpha', 'bravo'))
        changed = reviewer.select_packet(home, cutoff=cutoff + 1, config_snapshot=config)
        assert [item['task_id'] for item in changed['episodes']] == [task['id']]
        reviewer.mark_material_reviewed(home, changed['coverage']['followup_id'])
        assert reviewer.select_packet(home, cutoff=cutoff + 2, config_snapshot=config)['episodes'] == []


def test_new_linked_decision_resurfaces_reviewed_candidate_once():
    with lab() as (home, _):
        task, _, _ = ended_worker(home, 'central-later', str(uuid.uuid4()), [message('native', 100)])
        config = reviewer.read_config(home); cutoff = time.time() + 100
        first = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
        reviewer.mark_material_reviewed(home, first['coverage']['followup_id'])
        question = tasks.task_file(home, task['id']).parent / 'question.txt'; question.write_text('Which?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
        supervision.reconcile(home)
        answer = tasks.task_file(home, task['id']).parent / 'answer.txt'; answer.write_text('Later feedback.\n')
        supervision.resolve(home, task['id'], 'choice', answer)
        changed = reviewer.select_packet(home, cutoff=cutoff + 1, config_snapshot=config)
        assert [item['task_id'] for item in changed['episodes']] == [task['id']]
        assert any(source['kind'] == 'central-decision' for source in changed['sources'])
        reviewer.mark_material_reviewed(home, changed['coverage']['followup_id'])
        assert reviewer.select_packet(home, cutoff=cutoff + 2, config_snapshot=config)['episodes'] == []

def tree_bytes(root):
    return [(str(path.relative_to(root)), path.read_bytes()) for path in sorted(root.rglob('*')) if path.is_file()]


def produced_run(home):
    started = time.time()
    ended_worker(home, 'failed', str(uuid.uuid4()), [message('failure')])
    ended_worker(home, 'success', str(uuid.uuid4()), [message('counterexample')], 0)
    ended_worker(home, 'additional', str(uuid.uuid4()), [message('additional failure')])
    intent = reviewer.enqueue_intent(home, kind='default')
    config = reviewer.read_config(home)
    run = reviewer.reserve_run(home, intent, config_snapshot=config,
                               instruction_snapshot=reviewer.INSTRUCTION_FILE.read_bytes(), started_at=started,
                               deadline_epoch_ms=int((started + config['limits']['max_runtime_seconds']) * 1000))
    selected = reviewer.select_packet(home, cutoff=time.time(), config_snapshot=config)
    return reviewer.begin_run(home, run, selected, model='openai/gpt-5.2-codex', observed_omp_version='fixture-test'), selected


def build_report(selected):
    failed = next(source['source_id'] for source in selected['sources'] if source['kind'] == 'native-jsonl' and source['task_id'] == 'failed')
    success = next(source['source_id'] for source in selected['sources'] if source['kind'] == 'native-jsonl' and source['task_id'] == 'success')
    return {'schema': 1, 'summary': 'native fixture', 'findings': [{'kind': 'new-skill', 'target': 'bounded-review',
            'finding': 'failure', 'applicability': 'all', 'citations': [failed], 'counterexamples': [success],
            'uncertainty': 'low', 'resolution': 'draft', 'validation': 'curated resolve',
            'validation_limits': ['Requires human verification; not proof of causality'],
            'new_skill': {'id': 'agd-review-draft', 'draft': '---\nname: agd-review-draft\ndescription: Review bounded evidence\n---\n# Draft\n',
                          'triggers': ['review'], 'scope': 'review', 'prerequisites': []}}]}


def test_report_revisions_triage_and_finalization_preserve_inert_state():
    with lab() as (home, _):
        before_catalog, before_profiles = worker_skills.catalog(home), worker_skills.resolve(home)
        run, selected = produced_run(home)
        before_tasks = tree_bytes(home / 'data' / 'tasks')
        before_sources = tree_bytes(home / 'data' / 'observability')
        report = build_report(selected)
        first = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        proposal = first['proposal_ids'][0]
        reviewer.dispose(home, proposal, 'rejected', 'insufficient', None, {'uid': os.getuid(), 'identity': str(os.getuid())})
        rejected = reviewer.read_triage(home, proposal)
        report['findings'][0]['citations'].append(next(source['source_id'] for source in selected['sources'] if source['kind'] == 'native-jsonl' and source['task_id'] == 'additional'))
        second = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        row = reviewer.read_proposal(home, proposal)
        assert second['proposal_ids'] == [proposal]
        assert reviewer.read_triage(home, proposal) == rejected
        assert row['head_revision'] != row['revisions'][0]['revision_hash']
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), {**build_report(selected), 'findings': [{**build_report(selected)['findings'][0], 'citations': ['unknown-source']}]})
        finalized = reviewer.finalize_run(home, run['review_run_id'], 'exited')
        event = supervision.pending(home)[0]
        report_bytes = reviewer.report_path(home, first['report_id']).read_bytes()
        triage_bytes = reviewer.triage_path(home, proposal).read_bytes()
        supervision.acknowledge(home, [event['id']])
        assert reviewer.report_path(home, first['report_id']).read_bytes() == report_bytes
        assert reviewer.triage_path(home, proposal).read_bytes() == triage_bytes
        assert reviewer.finalize_run(home, run['review_run_id'], 'exited') == finalized
        reviewer.dispose(home, proposal, 'accepted', None, None, {'uid': os.getuid(), 'identity': str(os.getuid())})
        assert (worker_skills.catalog(home), worker_skills.resolve(home)) == (before_catalog, before_profiles)
        assert tree_bytes(home / 'data' / 'tasks') == before_tasks
        assert tree_bytes(home / 'data' / 'observability') == before_sources

def test_same_report_duplicate_finding_reuses_proposal_identity():
    with lab() as (home, _):
        run, selected = produced_run(home)
        report = build_report(selected)
        report['findings'].append(dict(report['findings'][0]))
        result = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        assert result['proposal_ids'][0] == result['proposal_ids'][1]

def test_finalization_recovers_missing_event_for_final_receipt():
    with lab() as (home, _):
        run, selected = produced_run(home)
        submitted = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), build_report(selected))
        final = reviewer.finalize_run(home, run['review_run_id'], 'exited')
        (home / 'state' / 'events' / f"{final['event_id']}.json").unlink()
        reviewer.finalize_run(home, run['review_run_id'], 'exited')
        assert supervision.pending(home)[0]['id'] == final['event_id']

def test_persisted_report_over_budget_refuses_before_revisions():
    with lab() as (home, _):
        run, selected = produced_run(home)
        report = build_report(selected)
        config = reviewer.read_config(home); config['limits']['max_output_bytes'] = len(reviewer._json_bytes(report)) + 1
        store.atomic_json(reviewer.paths(home)['manifests'] / f"{run['config_snapshot_ref']}.json", config)
        with pytest.raises(store.Refusal, match='cumulative'):
            reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        assert not reviewer.paths(home)['reports'].exists() and not reviewer.paths(home)['revisions'].exists()

@pytest.mark.parametrize('boundary', ['prepared', 'revision', 'header', 'report', 'receipt'])
def test_prepared_submission_recovers_real_write_boundaries(monkeypatch, boundary):
    with lab() as (home, _):
        run, selected = produced_run(home); report = build_report(selected); request = str(uuid.uuid4())
        original_publish, original_atomic, fired = reviewer.publish_immutable, store.atomic_json, False
        def publish(path, value):
            nonlocal fired
            original_publish(path, value)
            if not fired and ((boundary == 'prepared' and Path(path).parent == reviewer.paths(home)['prepared']) or (boundary == 'revision' and Path(path).parent == reviewer.paths(home)['revisions']) or (boundary == 'report' and Path(path).parent == reviewer.paths(home)['reports'])):
                fired = True; raise RuntimeError(boundary)
        def atomic(path, value):
            nonlocal fired
            if boundary == 'receipt' and not fired and Path(path) == reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json":
                fired = True; raise RuntimeError(boundary)
            original_atomic(path, value)
            if boundary == 'header' and not fired and Path(path).parent == reviewer.paths(home)['proposals']:
                fired = True; raise RuntimeError(boundary)
        monkeypatch.setattr(reviewer, 'publish_immutable', publish); monkeypatch.setattr(store, 'atomic_json', atomic)
        with pytest.raises(RuntimeError): reviewer.submit_report(home, run['review_run_id'], request, report)
        monkeypatch.setattr(reviewer, 'publish_immutable', original_publish); monkeypatch.setattr(store, 'atomic_json', original_atomic)
        reviewer.recover_submissions(home, run['review_run_id'])
        result = reviewer.submit_report(home, run['review_run_id'], request, report)
        prepared = json.loads(next(reviewer.paths(home)['prepared'].glob('*')).read_text())
        assert result == prepared['result'] and reviewer._receipt(home, run['review_run_id'])['output_bytes'] == prepared['output_bytes']
        assert len(list(reviewer.paths(home)['reports'].glob('*.json'))) == 1
        reviewer.finalize_run(home, run['review_run_id'], 'exited')
        assert len(supervision.pending(home)) == 1


def test_begin_run_requires_and_persists_observed_runtime_version():
    with lab() as (home, _):
        intent = reviewer.enqueue_intent(home, kind='manual'); config = reviewer.read_config(home)
        run = reviewer.reserve_run(home, intent, config_snapshot=config, instruction_snapshot=b'fixture', started_at=time.time(), deadline_epoch_ms=1)
        selected = reviewer.select_packet(home, cutoff=time.time(), config_snapshot=config)
        with pytest.raises(store.Refusal):
            reviewer.begin_run(home, run, selected, model='real/model', observed_omp_version='')
        assert not reviewer.paths(home)['prepared'].exists() and not reviewer.paths(home)['reports'].exists()
        begun = reviewer.begin_run(home, run, selected, model='real/model', observed_omp_version='omp 1.2.3')
        assert begun['omp_version'] == 'omp 1.2.3'


def test_packet_rejects_symlinked_task_directory_before_native_read(tmp_path):
    with lab() as (home, _):
        cutoff = time.time()
        ended_worker(home, 'unsafe', str(uuid.uuid4()), [message('outside', cutoff - 1)], status=1)
        task_dir = home / 'data' / 'tasks' / 'unsafe'
        outside = tmp_path / 'outside'; task_dir.rename(outside); task_dir.symlink_to(outside, target_is_directory=True)
        selected = packet(home, cutoff)
        assert selected['episodes'] == []
        assert any(row.get('reason') == 'task-record-malformed' for row in selected['coverage']['unavailable'])


@pytest.mark.parametrize('task_id,incarnation', [
    ('/absolute', str(uuid.uuid4())), ('../traversal', str(uuid.uuid4())),
    ('valid', '../traversal'), ('valid', str(uuid.uuid4()).upper()),
])
def test_packet_refuses_noncanonical_stored_task_or_incarnation_ids(task_id, incarnation):
    with lab() as (home, _):
        directory = home / 'data' / 'tasks' / 'record'; directory.mkdir(parents=True)
        (directory / 'task.json').write_text(json.dumps({'id': task_id, 'incarnation': incarnation, 'worker_alive': False}))
        observations = directory / 'observations'; observations.mkdir()
        (observations / 'exit.json').write_text(json.dumps({'key': 'runner-exit', 'id': 'exit', 'observed_at': time.time() - 1, 'incarnation': incarnation}))
        selected = packet(home)
        assert selected['episodes'] == []
        assert any(row.get('reason') == 'task-record-malformed' for row in selected['coverage']['unavailable'])


def test_reviewer_environment_sanitizes_ambient_identity_and_otel(monkeypatch):
    with lab() as (home, _):
        monkeypatch.setenv('AGD_TASK_ID', 'foreign-task')
        monkeypatch.setenv('AGD_INCARNATION', str(uuid.uuid4()))
        monkeypatch.setenv('AGD_MANAGED_CENTRAL', '1')
        monkeypatch.setenv('AGD_OWNER', 'owner-token')
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://outside.invalid')
        env, _, _ = observability.reviewer_environment(home, str(uuid.uuid4()))
        assert env['AGD_HOME'] == str(home)
        assert not {'AGD_TASK_ID', 'AGD_INCARNATION', 'AGD_MANAGED_CENTRAL', 'AGD_OWNER'} & set(env)
        assert not any(key.startswith('OTEL_') for key in env)



def test_retrospective_rejects_mismatched_receipt_and_session_symlink(tmp_path):
    with lab() as (home, _):
        run, selected = produced_run(home)
        reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), build_report(selected))
        final = reviewer.finalize_run(home, run['review_run_id'], 'done')
        observability.reviewer_binding(home, run['review_run_id'], 'native-retrospective')
        receipt = reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json"
        broken = json.loads(receipt.read_text()); broken['review_run_id'] = '../traversal'; store.atomic_json(receipt, broken)
        with pytest.raises(store.Refusal):
            packet(home, retrospective=(run['review_run_id'],))
        store.atomic_json(receipt, final)
        session = home / 'data' / 'observability' / 'reviewer' / run['review_run_id'] / 'omp'
        outside = tmp_path / 'outside'; session.rename(outside); session.symlink_to(outside, target_is_directory=True)
        with pytest.raises(store.Refusal):
            packet(home, retrospective=(run['review_run_id'],))




def test_past_deadline_refuses_packet_before_publish():
    with lab() as (home, _):
        with pytest.raises(store.Refusal, match='deadline'):
            reviewer.select_packet(home, cutoff=time.time(), config_snapshot=reviewer.read_config(home),
                                   deadline_epoch_ms=0)
        assert not reviewer.paths(home)['packets'].exists()


def test_reviewer_records_refusal_for_config_packet_and_immutable_symlinks():
    with lab() as (home, _):
        outside = home / 'outside.json'; outside.write_text('{"outside":true}')
        config = reviewer.paths(home)['config']; config.parent.mkdir(parents=True, exist_ok=True)
        config.symlink_to(outside)
        with pytest.raises(store.Refusal):
            reviewer.read_config(home)
        assert outside.read_text() == '{"outside":true}'
        config.unlink()
        packet_id = 'a' * 64; packet_path = reviewer.paths(home)['packets'] / f'{packet_id}.json'
        packet_path.parent.mkdir(parents=True); packet_path.symlink_to(outside)
        with pytest.raises(store.Refusal):
            reviewer.read_packet(home, packet_id)
        with pytest.raises(store.Refusal):
            reviewer.publish_immutable(packet_path, {'packet': 'new'})
        assert outside.read_text() == '{"outside":true}'


def test_observation_discovery_stops_at_deadline_between_task_and_observation_units(monkeypatch):
    with lab() as (home, _):
        ended_worker(home, 'deadline-discovery', str(uuid.uuid4()), [message('evidence')])
        now, read = [0], reviewer._read_json
        def advancing_read(path):
            value = read(path)
            if Path(path).name == 'task.json': now[0] = 2
            return value
        monkeypatch.setattr(reviewer.time, 'time', lambda: now[0])
        monkeypatch.setattr(reviewer, '_read_json', advancing_read)
        coverage = {'complete': True, 'unavailable': []}
        with pytest.raises(store.Refusal, match='deadline'):
            reviewer._observation_candidates(home, 10, coverage, deadline_epoch_ms=1000)
        assert not reviewer.paths(home)['packets'].exists()


def test_publish_immutable_refuses_symlink_swapped_before_no_follow_open(monkeypatch):
    with lab() as (home, _):
        path = reviewer.paths(home)['packets'] / ('b' * 64 + '.json')
        path.parent.mkdir(parents=True); path.write_bytes(b'{"packet":"new"}')
        outside = home / 'outside.json'; outside.write_bytes(b'{"packet":"new"}')
        open_file = reviewer.os.open
        def swap(pathname, *args, **kwargs):
            if Path(pathname) == path:
                path.unlink(); path.symlink_to(outside)
            return open_file(pathname, *args, **kwargs)
        monkeypatch.setattr(reviewer.os, 'open', swap)
        with pytest.raises(store.Refusal):
            reviewer.publish_immutable(path, {'packet': 'new'})
        assert outside.read_bytes() == b'{"packet":"new"}'
def test_schema1_config_preserves_values_and_drops_only_old_model_limit():
    with lab() as (home, _):
        path = reviewer.paths(home)['config']
        current = reviewer.read_config(home)
        legacy = {
            'schema': 1, 'schedule': {
                'mode': 'interval', 'timezone': None, 'calendar': None,
                'interval_hours': 17,
            }, 'review_model': 'private/reviewer',
            'limits': {**current['limits'], 'max_model_calls': 7},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        before = json.dumps(legacy, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        path.write_bytes(before)
        normalized = reviewer.read_config(home)
        assert normalized['schema'] == 2
        assert normalized['schedule'] == legacy['schedule']
        assert normalized['review_model'] == legacy['review_model']
        assert normalized['limits'] == current['limits']
        assert path.read_bytes() == before


def test_native_receipt_does_not_fabricate_provider_counters():
    with lab() as (home, _):
        intent = reviewer.enqueue_intent(home, kind='manual')
        started = time.time()
        run = reviewer.reserve_run(
            home, intent, config_snapshot=reviewer.read_config(home),
            instruction_snapshot=b'fixture', started_at=started,
            deadline_epoch_ms=int((started + 60) * 1000))
        receipt = reviewer._receipt(home, run['review_run_id'])
        assert receipt.get('execution') == 'native-cli'
        assert receipt.get('backend') is None
        assert 'counters' not in receipt


def test_historical_receipt_and_report_bytes_remain_unchanged_after_config_migration():
    with lab() as (home, _):
        config_path = reviewer.paths(home)['config']
        config_path.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            **reviewer.DEFAULT_CONFIG, 'schema': 1,
            'limits': {**reviewer.DEFAULT_LIMITS, 'max_model_calls': 20},
        }
        config_path.write_text(json.dumps(legacy, sort_keys=True) + '\n')
        run_id = '11111111-1111-4111-8111-111111111111'
        receipt_path = reviewer.paths(home)['receipts'] / f'{run_id}.json'
        report_path = reviewer.paths(home)['reports'] / ('a' * 64 + '.json')
        historical_receipt = {
            'schema': 1, 'review_run_id': run_id, 'dispatch_state': 'final',
            'outcome': 'exited', 'counters': {'model_calls': 1, 'provider_inflight': 0},
        }
        historical_report = {'schema': 1, 'summary': 'old', 'findings': []}
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(historical_receipt, sort_keys=True) + '\n')
        report_path.write_text(json.dumps(historical_report, sort_keys=True) + '\n')
        receipt_before, report_before = receipt_path.read_bytes(), report_path.read_bytes()
        assert reviewer.read_config(home)['schema'] == 2
        assert reviewer._receipt(home, run_id) == historical_receipt
        assert reviewer._read_json(report_path) == historical_report
        assert receipt_path.read_bytes() == receipt_before
        assert report_path.read_bytes() == report_before


def _evidence_source(evidence, source_id):
    sources = evidence.get('sources', evidence)
    if isinstance(sources, dict):
        return sources[source_id]
    return next(row for row in sources if row['source_id'] == source_id)


def _run_with_artifact(home, text='printf investigation\n'):
    run, selected = produced_run(home)
    runtime = reviewer.paths(home)['runtime'] / run['review_run_id']
    runtime.mkdir(parents=True, exist_ok=True)
    artifact = runtime / 'investigation.txt'
    artifact.write_text(text)
    return run, selected, runtime, artifact


def _retry_run_with_packet(home, selected):
    intent = reviewer.enqueue_intent(home, kind='manual')
    config = reviewer.read_config(home)
    started = time.time()
    run = reviewer.reserve_run(
        home, intent, config_snapshot=config,
        instruction_snapshot=reviewer.INSTRUCTION_FILE.read_bytes(),
        started_at=started,
        deadline_epoch_ms=int((started + config['limits']['max_runtime_seconds']) * 1000))
    reviewer.begin_run(home, run, selected, model='fixture/model', observed_omp_version='fixture')
    return run


def test_report_cites_captured_investigation_source():
    with lab() as (home, _):
        run, selected, _, artifact = _run_with_artifact(home)
        text = artifact.read_text()
        captured = reviewer.capture_evidence(home, run['review_run_id'], artifact, 'shell investigation')
        source_id = captured['source_id']
        source = _evidence_source(reviewer.read_evidence(home, run['review_run_id']), source_id)
        assert source['text'] == text
        assert source['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()

        report = build_report(selected)
        report['findings'][0]['citations'] = [source_id]
        submitted = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        stored = reviewer._read_json(reviewer.report_path(home, submitted['report_id']))
        assert stored['report']['findings'][0]['citations'] == [source_id]


def test_cross_run_or_modified_evidence_is_refused():
    with lab() as (home, _):
        run, selected, runtime, artifact = _run_with_artifact(home)
        reviewer.capture_evidence(home, run['review_run_id'], artifact, 'investigation')
        reviewer.close_run(home, run['review_run_id'], 'failed', error_code='fixture')
        other_run = _retry_run_with_packet(home, selected)
        with pytest.raises(store.Refusal):
            reviewer.capture_evidence(home, other_run['review_run_id'], artifact, 'foreign artifact')

        stored_path = next((home / 'data' / 'reviewer' / 'evidence' / run['review_run_id']).glob('*.json'))
        stored = json.loads(stored_path.read_text())
        stored['text'] = 'tampered'
        stored_path.write_text(json.dumps(stored))
        with pytest.raises(store.Refusal):
            reviewer.read_evidence(home, run['review_run_id'])


def test_symlink_special_file_and_known_secret_paths_refused():
    with lab() as (home, _):
        run, _, runtime, _ = _run_with_artifact(home)
        outside = home / 'outside.txt'
        outside.write_text('not a credential')
        symlink = runtime / 'linked.txt'
        symlink.symlink_to(outside)
        with pytest.raises(store.Refusal):
            reviewer.capture_evidence(home, run['review_run_id'], symlink, 'linked')

        special = runtime / 'pipe'
        os.mkfifo(special)
        pid = os.fork()
        if pid == 0:
            try:
                reviewer.capture_evidence(home, run['review_run_id'], special, 'special')
            except store.Refusal:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(1)
        status = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate
                break
            time.sleep(0.01)
        if status is None:
            os.kill(pid, 15)
            _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

        credentials = runtime / 'credentials.json'
        credentials.write_text('{"api_key":"do-not-capture"}')
        with pytest.raises(store.Refusal):
            reviewer.capture_evidence(home, run['review_run_id'], credentials, 'credentials')


def test_supplementary_capture_budget_is_cumulative():
    with lab() as (home, _):
        run, _, runtime, _ = _run_with_artifact(home)
        seed_path = reviewer.paths(home)['packets'] / f"{run['packet_id']}.json"
        seed_bytes = seed_path.stat().st_size
        remaining = reviewer.DEFAULT_LIMITS['max_input_bytes'] - seed_bytes
        assert remaining > 1

        first = runtime / 'first.txt'
        second = runtime / 'second.txt'
        first.write_text('a' * remaining)
        second.write_text('b')
        captured = reviewer.capture_evidence(home, run['review_run_id'], first, 'first')
        duplicate = reviewer.capture_evidence(home, run['review_run_id'], first, 'first')
        assert duplicate['source_id'] == captured['source_id']
        with pytest.raises(store.Refusal):
            reviewer.capture_evidence(home, run['review_run_id'], second, 'second')
        evidence = reviewer.read_evidence(home, run['review_run_id'])
        assert set(evidence['sources']) == {captured['source_id']}


def test_late_investigation_does_not_advance_unselected_episode_coverage():
    with lab() as (home, _):
        ended_worker(home, 'first', str(uuid.uuid4()), [message('first')])
        ended_worker(home, 'second', str(uuid.uuid4()), [message('second')])
        config = reviewer.read_config(home)
        config['limits']['max_candidates'] = 1
        cutoff = time.time() + 10
        selected = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
        assert len(selected['episodes']) == 1
        run = _retry_run_with_packet(home, selected)
        reviewer.mark_material_reviewed(home, selected['coverage']['followup_id'])
        baseline = reviewer.select_packet(home, cutoff=time.time() + 20, config_snapshot=config)

        runtime = reviewer.paths(home)['runtime'] / run['review_run_id']
        runtime.mkdir(parents=True, exist_ok=True)
        artifact = runtime / 'late.txt'
        artifact.write_text('late investigation')
        reviewer.capture_evidence(home, run['review_run_id'], artifact, 'late')
        after = reviewer.select_packet(home, cutoff=time.time() + 30, config_snapshot=config)
        assert [row['task_id'] for row in after['episodes']] == [row['task_id'] for row in baseline['episodes']]
        assert after['coverage']['followup_id'] == baseline['coverage']['followup_id']


def test_failed_review_leaves_seed_retryable():
    with lab() as (home, _):
        run, selected, _, artifact = _run_with_artifact(home)
        reviewer.capture_evidence(home, run['review_run_id'], artifact, 'failed-run supplement')
        before = {(row['source_id'], row['excerpt_id']) for row in selected['excerpts']}
        reviewer.close_run(home, run['review_run_id'], 'failed', error_code='fixture')
        retry = reviewer.select_packet(home, cutoff=time.time() + 1, config_snapshot=reviewer.read_config(home))
        assert {(row['source_id'], row['excerpt_id']) for row in retry['excerpts']} == before


def test_failed_run_supplement_is_not_automatically_reused():
    with lab() as (home, _):
        run, selected, _, artifact = _run_with_artifact(home)
        captured = reviewer.capture_evidence(home, run['review_run_id'], artifact, 'failed supplement')
        reviewer.close_run(home, run['review_run_id'], 'failed', error_code='fixture')
        retry = _retry_run_with_packet(home, selected)
        report = build_report(selected)
        report['findings'][0]['citations'].append(captured['source_id'])
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, retry['review_run_id'], str(uuid.uuid4()), report)


def test_deliberate_recapture_has_new_run_scope_without_seed_consumption():
    with lab() as (home, _):
        first, selected, _, artifact = _run_with_artifact(home, 'retained failed output')
        old = reviewer.capture_evidence(home, first['review_run_id'], artifact, 'failed output')
        reviewer.close_run(home, first['review_run_id'], 'failed', error_code='fixture')
        second = _retry_run_with_packet(home, selected)
        second_runtime = reviewer.paths(home)['runtime'] / second['review_run_id']
        second_runtime.mkdir(parents=True, exist_ok=True)
        recaptured_path = second_runtime / 'recaptured.txt'
        recaptured_path.write_bytes(artifact.read_bytes())
        new = reviewer.capture_evidence(home, second['review_run_id'], recaptured_path, 'recaptured failed output')
        assert new['source_id'] != old['source_id']
        assert new['text_sha256'] == old['text_sha256']
        old_source = _evidence_source(reviewer.read_evidence(home, first['review_run_id']), old['source_id'])
        new_source = _evidence_source(reviewer.read_evidence(home, second['review_run_id']), new['source_id'])
        assert old_source['origin']['run_id'] == first['review_run_id']
        assert new_source['origin']['run_id'] == second['review_run_id']
        assert old_source['text_sha256'] == new_source['text_sha256'] == old['text_sha256']
        assert reviewer.material_state(home, selected['coverage']['followup_id'])['state'] == 'pending'


def test_duplicate_capture_same_origin_bytes_keeps_first_label():
    with lab() as (home, _):
        run, _, _, artifact = _run_with_artifact(home, 'same-origin bytes')
        first = reviewer.capture_evidence(home, run['review_run_id'], artifact, 'first label')
        second = reviewer.capture_evidence(home, run['review_run_id'], artifact, 'changed label')
        assert second['source_id'] == first['source_id']
        assert second['label'] == 'first label'
        evidence = reviewer.read_evidence(home, run['review_run_id'])
        assert list(evidence['sources']) == [first['source_id']]


def test_dangling_evidence_root_refuses_read_submit_and_recovery(monkeypatch):
    with lab() as (home, _):
        run, selected, _, artifact = _run_with_artifact(home)
        reviewer.capture_evidence(home, run['review_run_id'], artifact, 'dangling-root fixture')
        report = build_report(selected)
        original_apply = reviewer._apply_prepared_locked
        def crash_apply(*args, **kwargs):
            raise RuntimeError('fixture crash')
        monkeypatch.setattr(reviewer, '_apply_prepared_locked', crash_apply)
        with pytest.raises(RuntimeError):
            reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        monkeypatch.setattr(reviewer, '_apply_prepared_locked', original_apply)

        evidence_root = reviewer.paths(home)['evidence'] / run['review_run_id']
        next(evidence_root.glob('*.json')).unlink()
        evidence_root.rmdir()
        evidence_root.symlink_to(home / 'missing-evidence-root', target_is_directory=True)
        with pytest.raises(store.Refusal):
            reviewer.read_evidence(home, run['review_run_id'])
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        with pytest.raises(store.Refusal):
            reviewer.recover_submissions(home, run['review_run_id'])


def test_oversized_sparse_artifact_is_refused_under_bounded_memory():
    with lab() as (home, _):
        run, _, runtime, _ = _run_with_artifact(home)
        artifact = runtime / 'oversized-sparse.bin'
        with artifact.open('wb') as stream:
            stream.truncate(reviewer.DEFAULT_LIMITS['max_input_bytes'] * 8192)
        pid = os.fork()
        if pid == 0:
            try:
                _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                limit = 256 * 1024 * 1024
                if hard != resource.RLIM_INFINITY:
                    limit = min(limit, hard)
                resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
                reviewer.capture_evidence(home, run['review_run_id'], artifact, 'oversized sparse')
            except store.Refusal:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_parent_symlink_swap_never_captures_external_artifact(monkeypatch):
    with lab() as (home, _):
        run, _, runtime, _ = _run_with_artifact(home)
        parent = runtime / 'nested'
        parent.mkdir()
        artifact = parent / 'output.txt'
        artifact.write_text('owned artifact')
        external = home / 'external-artifacts'
        external.mkdir()
        (external / 'output.txt').write_text('external artifact')
        moved = runtime / 'nested-owned'
        original_open = reviewer.os.open
        swapped = [False]

        def swap_parent(pathname, flags, mode=0o777, **kwargs):
            candidate = Path(pathname)
            final_open = candidate == artifact or (
                candidate.name == artifact.name and kwargs.get('dir_fd') is not None)
            if final_open and not swapped[0]:
                parent.rename(moved)
                parent.symlink_to(external, target_is_directory=True)
                swapped[0] = True
            return original_open(pathname, flags, mode, **kwargs)

        monkeypatch.setattr(reviewer.os, 'open', swap_parent)
        try:
            captured = reviewer.capture_evidence(home, run['review_run_id'], artifact, 'parent swap')
        except store.Refusal:
            captured = None
        assert swapped[0]
        if captured is not None:
            source = _evidence_source(reviewer.read_evidence(home, run['review_run_id']),
                                      captured['source_id'])
            assert source['text'] == 'owned artifact'


def test_oversized_sparse_artifact_refuses_before_reading_full_file():
    with lab() as (home, _):
        run, _, runtime, _ = _run_with_artifact(home)
        artifact = runtime / 'oversized-sparse.txt'
        with artifact.open('wb') as stream:
            stream.truncate(8 * 1024 * 1024)

        def rchar():
            values = Path('/proc/self/io').read_text().splitlines()
            return int(next(line.split(':', 1)[1] for line in values if line.startswith('rchar:')))

        before = rchar()
        with pytest.raises(store.Refusal):
            reviewer.capture_evidence(home, run['review_run_id'], artifact, 'oversized sparse')
        assert rchar() - before < reviewer.DEFAULT_LIMITS['max_input_bytes'] * 4


def test_runtime_ancestor_swap_never_captures_external_artifact(monkeypatch):
    with lab() as (home, _):
        run, _, runtime, _ = _run_with_artifact(home, 'owned artifact')
        run_root = runtime
        ancestor = runtime.parent
        external_runtime = home / 'external-runtime'
        external_run = external_runtime / run['review_run_id']
        external_run.mkdir(parents=True)
        (external_run / 'output.txt').write_text('external artifact')
        artifact = run_root / 'output.txt'
        artifact.write_text('owned artifact')
        moved_ancestor = ancestor.parent / 'runtime-owned'
        original_open = reviewer.os.open
        swapped = [False]

        def swap_runtime(pathname, flags, mode=0o777, **kwargs):
            candidate = Path(pathname)
            opens_run_root = candidate == run_root or (
                candidate.name == run['review_run_id'] and kwargs.get('dir_fd') is not None)
            if opens_run_root and not swapped[0]:
                ancestor.rename(moved_ancestor)
                ancestor.symlink_to(external_runtime, target_is_directory=True)
                swapped[0] = True
            return original_open(pathname, flags, mode, **kwargs)

        monkeypatch.setattr(reviewer.os, 'open', swap_runtime)
        try:
            captured = reviewer.capture_evidence(
                home, run['review_run_id'], artifact, 'runtime ancestor swap')
        except store.Refusal:
            captured = None
        assert swapped[0]
        if captured is not None:
            source = _evidence_source(
                reviewer.read_evidence(home, run['review_run_id']), captured['source_id'])
            assert source['text'] == 'owned artifact'
