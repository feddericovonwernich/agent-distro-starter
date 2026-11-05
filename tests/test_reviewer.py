import hashlib
import builtins
import io
import os
import json
import sys
import subprocess
import threading
import time
import uuid
import resource
from pathlib import Path

import pytest

from support import command, lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, reviewer_episodes, store, supervision, tasks, worker_skills, workers


def ended_worker(home, task_id, incarnation, rows, status=1, *, project='sample'):
    brief = home / f'{task_id}.md'; brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', project, '--brief', str(brief))
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



def completed_session(home, *, role='central', task_id=None, texts=('answer',), cutoff=200.0,
                      active='unfinished', title=None, start_at=10.0, stable_ids=False, alive=False, project='sample'):
    incarnation = str(uuid.uuid4()) if role == 'worker' else None
    if role == 'worker':
        task, binding, session_id = ended_worker(home, task_id, incarnation, [], 0, project=project)
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
             'candidates': [{'candidate_id': reviewer._digest({'task_id': task['id'], 'incarnation': task['incarnation']}),
                             'excerpt_ids': [f"legacy-message:{hashlib.sha256(b'legacy answer').hexdigest()}"]}]})
        reviewer.store.atomic_json(
            reviewer.paths(home)['checkpoints'] / 'legacy.json',
            {'scope': {}, 'packet_id': packet_id, 'material_basis': 'e' * 64})
        scope = {'home': str(home.resolve()), 'role': 'worker', 'session_id': source['session_id'],
                 'task_id': task['id'], 'incarnation': task['incarnation']}
        assert reviewer._material_id(scope, row) in reviewer._reviewed_material(home, scope)
        assert reviewer._legacy_material_id(row) not in reviewer._reviewed_material(home, scope)



def test_whole_fix_unchanged_legacy_packet_is_no_work_but_explicit_detail_reopens():
    with lab() as (home, _):
        source, native, _ = completed_session(home, texts=('legacy answer',))
        initial = selection(home)['episodes'][0]
        source_id, followup = 'c' * 64, 'e' * 64
        packet = {'schema': 1, 'purpose': 'background-skill-review', 'cutoff': 200,
                  'episodes': [], 'sources': [{'kind': 'native-jsonl', 'role': 'central',
                  'session_id': source['session_id'], 'path': str(native.relative_to(home)),
                  'byte_watermark': 1, 'content_sha256': 'a' * 64, 'material_sha256': 'b' * 64,
                  'source_id': source_id}],
                  'excerpts': [{'source_id': source_id, 'message_id': source['session_id'] + '-prompt-0',
                                'text': 'prompt', 'line_start': 2, 'line_end': 2,
                                'byte_start': 0, 'byte_end': 1, 'excerpt_id': 'd' * 64},
                               {'source_id': source_id, 'message_id': source['session_id'] + '-final-0',
                                'text': 'legacy answer', 'line_start': 3, 'line_end': 3,
                                'byte_start': 1, 'byte_end': 2, 'excerpt_id': '1' * 64}],
                  'catalog': [], 'skill_provenance': [], 'coverage': {'followup_id': followup},
                  'retrospective_review_run_ids': [], 'retrospective_episodes': [],
                  'config_snapshot_sha256': 'f' * 64}
        packet_id = reviewer._digest(packet)
        packet_path = reviewer.paths(home)['packets'] / f'{packet_id}.json'
        reviewer.publish_immutable(packet_path, packet)
        store.atomic_json(reviewer.paths(home)['followups'] / f'{followup}.json',
                          {'schema': 1, 'material_basis': followup, 'state': 'reviewed', 'candidates': []})
        store.atomic_json(reviewer.paths(home)['checkpoints'] / 'legacy.json',
                          {'scope': {}, 'packet_id': packet_id, 'material_basis': followup})
        before = packet_path.read_bytes()
        assert selection(home, cutoff=201)['episodes'] == []
        assert packet_path.read_bytes() == before
        detail = selection(home, cutoff=202, detail=(initial['episode_id'],))
        reopened = detail['episodes'][0]
        assert reopened['episode_id'] == initial['episode_id']
        assert not reopened['progress']['reflected']
        rows = [json.loads(line) for line in native.read_text().splitlines()]
        next(row for row in rows if row.get('id') == source['session_id'] + '-prompt-0')['message']['content'] = 'new prompt'
        native.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        changed = selection(home, cutoff=203)['episodes'][0]
        assert changed['episode_id'] == initial['episode_id']
        assert changed['basis'] != initial['basis']
        assert not changed['progress']['reflected']
        assert packet_path.read_bytes() == before


def test_whole_fix_legacy_worker_unproved_artifacts_and_new_feedback_remain_actionable():
    with lab() as (home, _):
        source, native, task = completed_session(home, role='worker', task_id='legacy-context',
                                                 texts=('legacy outcome',), alive=True)
        cutoff = time.time() + 100
        old = next(item for item in selection(home, cutoff=cutoff)['episodes']
                   if item.get('task_id') == task['id'])
        source_id, followup = 'c' * 64, 'e' * 64
        packet = {'schema': 1, 'purpose': 'background-skill-review', 'cutoff': cutoff,
                  'episodes': [{'task_id': task['id'], 'incarnation': task['incarnation']}],
                  'sources': [{'kind': 'native-jsonl', 'task_id': task['id'],
                               'incarnation': task['incarnation'], 'session_id': source['session_id'],
                               'path': str(native.relative_to(home)), 'byte_watermark': 1,
                               'content_sha256': 'a' * 64, 'material_sha256': 'b' * 64,
                               'source_id': source_id}],
                  'excerpts': [{'source_id': source_id, 'message_id': source['session_id'] + '-prompt-0',
                                'text': 'prompt', 'line_start': 2, 'line_end': 2,
                                'byte_start': 0, 'byte_end': 1, 'excerpt_id': 'd' * 64},
                               {'source_id': source_id, 'message_id': source['session_id'] + '-final-0',
                                'text': 'legacy outcome', 'line_start': 3, 'line_end': 3,
                                'byte_start': 1, 'byte_end': 2, 'excerpt_id': 'f' * 64}],
                  'catalog': [], 'skill_provenance': [], 'coverage': {'followup_id': followup},
                  'retrospective_review_run_ids': [], 'retrospective_episodes': [],
                  'config_snapshot_sha256': '0' * 64}
        packet_id = reviewer._digest(packet)
        packet_path = reviewer.paths(home)['packets'] / f'{packet_id}.json'
        reviewer.publish_immutable(packet_path, packet)
        store.atomic_json(reviewer.paths(home)['followups'] / f'{followup}.json',
                          {'schema': 1, 'material_basis': followup, 'state': 'reviewed', 'candidates': []})
        store.atomic_json(reviewer.paths(home)['checkpoints'] / 'legacy.json',
                          {'scope': {}, 'packet_id': packet_id, 'material_basis': followup})
        history = packet_path.read_bytes()
        scope = reviewer._source_scope(home, {'role': 'worker', 'session_id': source['session_id'],
                                              'task_id': task['id'], 'incarnation': task['incarnation']})
        for record_id, text in ((source['session_id'] + '-prompt-0', 'prompt'),
                                (source['session_id'] + '-final-0', 'legacy outcome')):
            assert reviewer._material_id(scope, {'message_id': record_id, 'text': text}) in reviewer._reviewed_material(home, scope)
        unproved = selection(home, cutoff=cutoff + 1)
        selected = next(item for item in unproved['episodes'] if item['episode_id'] == old['episode_id'])
        assert not selected['progress']['reflected']
        assert selected['limitations'] or not unproved['coverage']['complete']
        brief = tasks.task_file(home, task['id']).parent / 'brief.md'
        brief.write_text('New intent not proved by the legacy packet.\n')
        changed = selection(home, cutoff=cutoff + 2)
        changed_episode = next(item for item in changed['episodes'] if item['episode_id'] == old['episode_id'])
        assert changed_episode['basis'] != selected['basis']
        question = brief.parent / 'question.txt'
        question.write_text('Which?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
        supervision.reconcile(home)
        answer = brief.parent / 'answer.txt'
        answer.write_text('New linked feedback.\n')
        supervision.resolve(home, task['id'], 'choice', answer)
        feedback = selection(home, cutoff=cutoff + 3)
        feedback_episode = next(item for item in feedback['episodes'] if item['episode_id'] == old['episode_id'])
        assert feedback_episode['basis'] != changed_episode['basis']
        assert not feedback_episode['progress']['reflected']
        assert packet_path.read_bytes() == history


def test_retrospective_includes_committed_finalized_report():
    with lab() as (home, _):
        from reviewer_history_fixture import historical_final
        run, submitted = historical_final(home)
        binding = observability.reviewer_binding(home, run['review_run_id'], 'native-review')
        Path(binding['session_dir'], 'native-review.jsonl').write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session', 'version': 3, 'id': 'native-review', 'timestamp': '1970-01-01T00:00:00Z'}, message('review native', time.time())]) + '\n')
        retrospective = reviewer.select_manifest(home, cutoff=time.time() + 1,
            config_snapshot=reviewer.read_config(home), retrospective_review_run_ids=(run['review_run_id'],))
        episode = next(row for row in retrospective['episodes'] if row.get('review_run_id') == run['review_run_id'])
        locators = reviewer.read_selection_locators(home, retrospective['manifest_id'], episode['episode_id'])
        assert any(row.get('kind') == 'reviewer-report' for row in locators['rows'])


def test_retrospective_requires_closed_prior_receipt_and_excludes_later_runs():
    with lab() as (home, _):
        prior, later = str(uuid.uuid4()), str(uuid.uuid4())
        receipts = reviewer.paths(home)['receipts']; receipts.mkdir(parents=True)
        store.atomic_json(receipts / f'{prior}.json', {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': prior, 'dispatch_state': 'final', 'closed_at': 100.0})
        store.atomic_json(receipts / f'{later}.json', {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': later, 'dispatch_state': 'final', 'closed_at': 300.0})
        binding = observability.reviewer_binding(home, prior, 'native-review')
        native = Path(binding['session_dir']) / 'native-review.jsonl'
        native.write_text('\n'.join(json.dumps(row) for row in [{'type': 'session', 'version': 3, 'id': 'native-review', 'timestamp': '1970-01-01T00:00:00Z'}, message('retrospective evidence', 100)]) + '\n')
        selected = reviewer.select_manifest(home, cutoff=200, config_snapshot=reviewer.read_config(home),
                                            retrospective_review_run_ids=(prior,))
        assert any(row.get('review_run_id') == prior for row in selected['episodes'])
        with pytest.raises(store.Refusal):
            reviewer.select_manifest(home, cutoff=200, config_snapshot=reviewer.read_config(home),
                                     retrospective_review_run_ids=(later,))


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
        assert not reviewer.paths(home)['selections'].exists()


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
    from test_reviewer_inspection import stage
    completed_session(home)
    run_id, selected = stage(home)
    run = reviewer._receipt(home, run_id)
    runtime = reviewer.paths(home)['runtime'] / run['review_run_id']
    runtime.mkdir(parents=True, exist_ok=True)
    artifact = runtime / 'investigation.txt'
    artifact.write_text(text)
    return run, selected, runtime, artifact


def _retry_run_with_manifest(home, selected):
    from test_reviewer_workspaces import _workspace_run
    return _workspace_run(home, selected)[0]


def test_report_cites_captured_investigation_source():
    with lab() as (home, _):
        run, selected, _, artifact = _run_with_artifact(home)
        text = artifact.read_text()
        captured = reviewer.capture_evidence(home, run['review_run_id'], artifact, 'shell investigation')
        source_id = captured['source_id']
        source = _evidence_source(reviewer.read_evidence(home, run['review_run_id']), source_id)
        assert source['text'] == text
        assert source['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()

        from test_reviewer_inspection import report_v2_inputs, report_v2_finding, report_v2, target
        native = {'session_id': selected['episodes'][0]['session_id']}
        row, _, citation = report_v2_inputs(home, run['review_run_id'], selected, native)
        dest = target(home, run['review_run_id'], scope='distro', component='fixture',
                      basis_source_ids=(citation,), authority_hint='unknown')
        report = report_v2(row, [report_v2_finding(row, dest['target_ref'], source_id, 'distro')])
        submitted = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), report)
        stored = reviewer._read_json(reviewer.report_path(home, submitted['report_id']))
        assert stored['report']['findings'][0]['citations'] == [source_id]


def test_cross_run_or_modified_evidence_is_refused():
    with lab() as (home, _):
        run, selected, runtime, artifact = _run_with_artifact(home)
        reviewer.capture_evidence(home, run['review_run_id'], artifact, 'investigation')
        reviewer.close_run(home, run['review_run_id'], 'failed', error_code='fixture')
        other_run = _retry_run_with_manifest(home, selected)
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
        from agd_lib import reviewer_inspection
        seed_bytes = reviewer_inspection._input_used_locked(home, reviewer._receipt(home, run['review_run_id']))
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




def test_deliberate_recapture_has_new_run_scope_without_seed_consumption():
    with lab() as (home, _):
        first, selected, _, artifact = _run_with_artifact(home, 'retained failed output')
        old = reviewer.capture_evidence(home, first['review_run_id'], artifact, 'failed output')
        reviewer.close_run(home, first['review_run_id'], 'failed', error_code='fixture')
        second = _retry_run_with_manifest(home, selected)
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
        assert not reviewer._episode_progress(home, selected['episodes'][0]['episode_id'],
                                              selected['episodes'][0]['basis'])['reflected']


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
        report = {'schema': 2, 'summary': 'fixture', 'episode_reviews': [], 'findings': []}
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


def selection(home, *, cutoff=200.0, max_candidates=20, detail=()):
    config = reviewer.read_config(home)
    config['limits']['max_candidates'] = max_candidates
    return reviewer.select_manifest(home, cutoff=cutoff, config_snapshot=config,
                                    detail_episode_ids=detail)


def record_assessment(home, episode, assessment, *, limitations=(), ranges=()):
    # Simulates the successful-finalization consumer; failed runs must not call it.
    with store.lock(home):
        reviewer._record_episode_progress(
            home, episode=episode, assessment=assessment, limitations=list(limitations),
            ranges=list(ranges), submission_id=str(uuid.uuid4()))


def test_manifest_keeps_live_final_episode_and_excludes_live_tail():
    with lab() as (home, _):
        source, _, _ = completed_session(home, role='worker', task_id='living',
                                         texts=('FINAL_SENTINEL',), active='TAIL_SENTINEL',
                                         alive=True)
        selected = selection(home)
        matching = [e for e in selected['episodes'] if e.get('interaction_id') ==
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source['session_id']}-binding-0"))]
        assert len(matching) == 1
        assert matching[0]['boundary']['entry_id'] == f"{source['session_id']}-final-0"
        assert 'TAIL_SENTINEL' not in json.dumps(selected)
        assert reviewer.read_manifest(home, selected['manifest_id']) == selected


def test_manifest_freezes_project_identity_not_langfuse_project():
    with lab() as (home, project):
        _, _, task = completed_session(home, role='worker', task_id='identity')
        selected = selection(home)
        episode = next(e for e in selected['episodes'] if e.get('task_id') == task['id'])
        binding = episode['project_binding']
        assert binding['project_id'] == 'sample'
        assert binding['home_id']
        assert binding['task_revision'] == task['start']
        assert len(binding['identity_sha256']) == 64
        registry = home / 'data' / 'projects' / 'sample.json'
        record = json.loads(registry.read_text())
        record['base'] = 'another-base'
        record['trusted'] = not record.get('trusted', False)
        registry.write_text(json.dumps(record))
        again = selection(home, cutoff=201)
        other = next(e for e in again['episodes'] if e.get('task_id') == task['id'])
        assert other['project_binding']['identity_sha256'] == binding['identity_sha256']
        assert str(project) not in binding['project_id']
        record['source'] = 'changed-origin'
        registry.write_text(json.dumps(record))
        changed = selection(home, cutoff=202)
        changed_episode = next(e for e in changed['episodes'] if e.get('task_id') == task['id'])
        assert changed_episode['project_binding']['identity_sha256'] != binding['identity_sha256']


def test_central_unknown_project_does_not_gain_target_scope():
    with lab() as (home, _):
        completed_session(home, texts=('Please modify project sample and its AGENTS.md',))
        episode = next(e for e in selection(home)['episodes'] if e['role'] == 'central')
        assert episode.get('task_id') is None
        assert episode['project_bindings'] == []


def test_selection_has_locators_not_body_sentinels():
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='locators',
                                       texts=('TERMINAL_PRIVATE_SENTINEL',))
        brief = tasks.task_file(home, task['id']).parent / 'brief.md'
        brief.write_text('INTENT_PRIVATE_SENTINEL')
        selected = selection(home)
        episode = next(e for e in selected['episodes'] if e.get('task_id') == task['id'])
        serialized = json.dumps(selected)
        assert 'TERMINAL_PRIVATE_SENTINEL' not in serialized
        assert 'INTENT_PRIVATE_SENTINEL' not in serialized
        assert episode['locators']
        assert len(json.dumps(selected, indent=2, ensure_ascii=False).encode()) <= 16384


def test_semantic_rewrite_changes_basis_but_title_append_does_not():
    with lab() as (home, _):
        _, native, _ = completed_session(home)
        first = selection(home)['episodes'][0]
        rewrite_title(native, 'new title')
        append_unfinished(native, 'later unfinished')
        second = selection(home, cutoff=201)['episodes'][0]
        assert second['basis'] == first['basis']
        rows = [json.loads(line) for line in native.read_text().splitlines()]
        prompt = next(row for row in rows if row.get('type') == 'message' and
                      row['message']['role'] == 'user' and row['message']['content'] == 'prompt')
        prompt['message']['content'] = 'rewritten prompt'
        native.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        changed = selection(home, cutoff=202)['episodes'][0]
        assert changed['basis'] != first['basis']


def test_screened_episode_remains_pending_reflection():
    with lab() as (home, _):
        completed_session(home)
        episode = selection(home)['episodes'][0]
        record_assessment(home, episode, 'screened')
        next_episode = selection(home, cutoff=201)['episodes'][0]
        assert next_episode['episode_id'] == episode['episode_id']
        assert next_episode['progress']['screened'] is True
        assert next_episode['progress']['reflected'] is False


def test_unfinished_old_episode_does_not_starve_new_episode():
    with lab() as (home, _):
        completed_session(home, start_at=10)
        old = selection(home, max_candidates=1)['episodes'][0]
        record_assessment(home, old, 'screened')
        completed_session(home, start_at=40)
        next_episode = selection(home, cutoff=201, max_candidates=1)['episodes'][0]
        assert next_episode['episode_id'] != old['episode_id']
        later = selection(home, cutoff=202, max_candidates=2)
        assert old['episode_id'] in {e['episode_id'] for e in later['episodes']}


def test_unchanged_limited_basis_holds_without_paid_redispatch():
    with lab() as (home, _):
        completed_session(home)
        episode = selection(home)['episodes'][0]
        record_assessment(home, episode, 'limited', limitations=('intent unavailable',))
        held = selection(home, cutoff=201)
        assert episode['episode_id'] not in {e['episode_id'] for e in held['episodes']}
        assert any(h['episode_id'] == episode['episode_id'] for h in held['held'])


def test_new_feedback_or_explicit_detail_reopens_hold():
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='feedback')
        episode = next(e for e in selection(home, cutoff=time.time() + 100)['episodes']
                       if e.get('task_id') == task['id'])
        record_assessment(home, episode, 'limited', limitations=('feedback unavailable',))
        detail = selection(home, cutoff=time.time() + 101, detail=(episode['episode_id'],))
        assert episode['episode_id'] in {e['episode_id'] for e in detail['episodes']}
        question = tasks.task_file(home, task['id']).parent / 'question.txt'
        question.write_text('Which?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
        supervision.reconcile(home)
        answer = question.parent / 'answer.txt'
        answer.write_text('New linked feedback.\n')
        supervision.resolve(home, task['id'], 'choice', answer)
        changed = selection(home, cutoff=time.time() + 102)
        reopened = next(e for e in changed['episodes'] if e.get('task_id') == task['id'])
        assert reopened['episode_id'] == episode['episode_id']
        assert reopened['basis'] != episode['basis']


def test_manifest_candidate_cap_reports_omitted_pending_work():
    with lab() as (home, _):
        completed_session(home, start_at=10)
        completed_session(home, start_at=40)
        selected = selection(home, max_candidates=1)
        assert len(selected['episodes']) == 1
        assert selected['omitted_pending'] == 1
        assert len(selection(home, cutoff=201, max_candidates=2)['episodes']) == 2


def test_manifest_refuses_budget_that_cannot_fit_eligible_episode():
    with lab() as (home, _):
        completed_session(home)
        config = reviewer.read_config(home)
        config['limits']['max_input_bytes'] = 128
        with pytest.raises(store.Refusal):
            reviewer.select_manifest(home, cutoff=200, config_snapshot=config)


def test_fixed_cap_rotation_serves_old_reflection_with_continuous_arrivals():
    with lab() as (home, _):
        completed_session(home, start_at=10)
        old = selection(home, max_candidates=1)['episodes'][0]
        record_assessment(home, old, 'screened')
        selected_ids = []
        for number in range(6):
            completed_session(home, start_at=20 + number * 20)
            manifest = selection(home, cutoff=200 + number, max_candidates=1)
            episode = manifest['episodes'][0]
            selected_ids.append(episode['episode_id'])
            record_assessment(home, episode, 'screened')
        assert old['episode_id'] in selected_ids
        assert len({e for e in selected_ids if e != old['episode_id']}) >= 2


def test_explicit_held_detail_is_not_displaced_by_new_work():
    with lab() as (home, _):
        completed_session(home, start_at=10)
        old = selection(home, max_candidates=1)['episodes'][0]
        record_assessment(home, old, 'limited', limitations=('intent unavailable',))
        completed_session(home, start_at=40)
        selected = selection(home, cutoff=201, max_candidates=1, detail=(old['episode_id'],))
        assert [episode['episode_id'] for episode in selected['episodes']] == [old['episode_id']]
        assert selected['omitted_pending'] >= 1


def test_large_native_episode_is_bounded_and_remains_recoverable():
    with lab() as (home, _):
        source, native, _ = completed_session(home, start_at=10)
        rows = [json.loads(line) for line in native.read_text().splitlines()]
        binding_index = next(i for i, row in enumerate(rows) if row.get('customType') == 'agd:episode-binding')
        binding = rows[binding_index]
        parent = binding['parentId']
        inserted = []
        for number in range(180):
            row = message(f'bounded native material {number}', 10.5)
            row.update(id=f'bulk-{number:04d}', parentId=parent)
            parent = row['id']
            inserted.append(row)
        binding['parentId'] = parent
        rows[binding_index:binding_index] = inserted
        native.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        completed_session(home, start_at=40)
        first = selection(home, max_candidates=1)
        episode = first['episodes'][0]
        assert episode['session_id'] == source['session_id']
        assert len(json.dumps(first, ensure_ascii=False, indent=2).encode()) <= 16384
        locator_page = reviewer.read_selection_locators(home, first['manifest_id'], episode['episode_id'])
        assert locator_page['rows'][0]['projection_sha256'] == episode['projection']['sha256']
        completion = next(c for c in reviewer_episodes.completion_records(
            home, 200, {'complete': True, 'unavailable': []}) if c['source_id'] == source['source_id'])
        proof = reviewer_episodes.read_native(native, 200, 262144, completion=completion, metadata_only=True)
        assert {'bulk-0000', 'bulk-0179'} <= {row['message_id'] for row in proof['projection']}
        record_assessment(home, episode, 'reflected')
        following = selection(home, cutoff=201, max_candidates=1)
        assert following['episodes'][0]['session_id'] != source['session_id']
        detail = selection(home, cutoff=202, max_candidates=1, detail=(episode['episode_id'],))
        assert detail['episodes'][0]['episode_id'] == episode['episode_id']


def test_large_successful_range_history_does_not_block_selection():
    with lab() as (home, _):
        source, _, _ = completed_session(home, texts=('x' * 1000,))
        episode = selection(home, max_candidates=1)['episodes'][0]
        final_id = f"{source['session_id']}-final-0"
        ranges = [{'message_id': final_id, 'start': index * 3, 'end': index * 3 + 2,
                   'text_sha256': hashlib.sha256(('x' * 1000).encode()).hexdigest()}
                  for index in range(250)]
        record_assessment(home, episode, 'screened', ranges=ranges)
        completed_session(home, start_at=40)
        detail = selection(home, cutoff=201, max_candidates=1, detail=(episode['episode_id'],))
        assert detail['episodes'][0]['episode_id'] == episode['episode_id']
        assert len(json.dumps(detail, ensure_ascii=False, indent=2).encode()) <= 16384
        saved = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert saved['ranges'] == ranges


def test_many_held_and_invalid_sources_disclose_counts_and_allow_work():
    with lab() as (home, _):
        held_ids = []
        for number in range(45):
            completed_session(home, start_at=10)
            episode = selection(home, cutoff=200 + number, max_candidates=1)['episodes'][0]
            held_ids.append(episode['episode_id'])
            record_assessment(home, episode, 'limited', limitations=('missing ' + 'x' * 400,))
        sources = reviewer_episodes._episode_paths(home)['sources']
        for number in range(100):
            (sources / f'{number:064x}.json').write_text('{}')
        no_work = selection(home, cutoff=300, max_candidates=1)
        assert no_work['episodes'] == []
        assert no_work['held_count'] == len(held_ids)
        assert no_work['coverage']['unavailable_count'] >= 100
        assert no_work['coverage']['complete'] is False
        assert len(json.dumps(no_work, ensure_ascii=False, indent=2).encode()) <= 16384
        seen, cursor = set(), 0
        while cursor is not None:
            held_page = reviewer.read_selection_disclosures(home, no_work['manifest_id'], 'held', cursor=cursor)
            assert held_page['total'] == len(held_ids)
            assert len(json.dumps(held_page, ensure_ascii=False, indent=2).encode()) <= 16384
            seen.update(row['episode_id'] for row in held_page['rows'])
            cursor = held_page['next_cursor']
        assert seen == set(held_ids)
        completed_session(home, start_at=40)
        actionable = selection(home, cutoff=301, max_candidates=1)
        assert len(actionable['episodes']) == 1
        assert actionable['held_count'] == len(held_ids)
        recovered = selection(home, cutoff=302, max_candidates=1, detail=(held_ids[-1],))
        assert recovered['episodes'][0]['episode_id'] == held_ids[-1]


def test_successful_detail_with_new_ranges_clears_no_progress_hold():
    with lab() as (home, _):
        source, _, _ = completed_session(home, texts=('outcome with more material',))
        episode = selection(home, max_candidates=1)['episodes'][0]
        record_assessment(home, episode, 'limited', limitations=('outcome unavailable',))
        detail = selection(home, cutoff=201, max_candidates=1, detail=(episode['episode_id'],))['episodes'][0]
        record_assessment(home, detail, 'screened')
        assert selection(home, cutoff=201.5, max_candidates=1)['episodes'] == []
        exposed = {'message_id': f"{source['session_id']}-final-0", 'start': 0, 'end': 7,
                   'text_sha256': hashlib.sha256(b'outcome with more material').hexdigest()}
        record_assessment(home, detail, 'screened', ranges=[exposed])
        resumed = selection(home, cutoff=202, max_candidates=1)
        assert resumed['episodes'][0]['episode_id'] == episode['episode_id']
        assert resumed['episodes'][0]['progress']['reflected'] is False
        saved = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert exposed in saved['ranges'] and saved['hold'] is None


def test_historical_assigned_catalog_does_not_exhaust_current_budget():
    with lab() as (home, _):
        source, native, task = completed_session(home, role='worker', task_id='catalog-template', alive=True)
        initial = selection(home, max_candidates=1)['episodes'][0]
        record_assessment(home, initial, 'reflected')
        completed_session(home, start_at=40)
        config = reviewer.read_config(home)
        config['limits'].update(max_candidates=1, max_input_bytes=8192)
        baseline = reviewer.select_manifest(home, cutoff=200, config_snapshot=config)
        current = baseline['episodes'][0]
        assert current['role'] == 'central'
        episode_paths = reviewer_episodes._episode_paths(home)
        rows_template = [json.loads(line) for line in native.read_text().splitlines()]
        binding = next(row for row in rows_template if row.get('customType') == 'agd:episode-binding')
        interaction = binding['details']['interaction_id']
        original = json.loads((episode_paths['completions'] / source['source_id'] /
                               f'{interaction}.json').read_text())
        for number in range(150):
            task_id = f'history-{number:03d}'
            incarnation = str(uuid.uuid4())
            session_id = str(uuid.uuid4())
            folder = home / 'data' / 'tasks' / task_id
            folder.mkdir(parents=True)
            historical_task = {**task, 'id': task_id, 'incarnation': incarnation}
            (folder / 'task.json').write_text(json.dumps(historical_task))
            (folder / 'brief.md').write_text('Historical task intent.\n')
            session = home / 'data' / 'observability' / 'tasks' / task_id / 'omp' / incarnation / f'{session_id}.jsonl'
            session.parent.mkdir(parents=True)
            historical_source = {**source, 'task_id': task_id, 'incarnation': incarnation,
                                 'session_id': session_id, 'path': str(session.relative_to(home))}
            historical_source['source_id'] = reviewer_episodes.source_fingerprint(historical_source)
            rows = json.loads(json.dumps(rows_template))
            next(row for row in rows if row.get('type') == 'session')['id'] = session_id
            next(row for row in rows if row.get('customType') == 'agd:episode-binding')['details']['source_id'] = historical_source['source_id']
            session.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            completion = {**original, 'source_id': historical_source['source_id']}
            reviewer_episodes._publish_immutable(
                episode_paths['sources'] / f"{historical_source['source_id']}.json", historical_source)
            reviewer_episodes._publish_immutable(
                episode_paths['completions'] / historical_source['source_id'] / f'{interaction}.json', completion)
            attached = {**completion, 'source': historical_source}
            native_proof = reviewer_episodes.read_native(session, 200, 8192, completion=attached, metadata_only=True)
            item = {'completion': attached, 'task': historical_task, 'task_dir': folder,
                    'event': {'incarnation': incarnation}, 'closed_at': completion['recorded_at']}
            historical = reviewer._selection_episode(
                home, item, session, native_proof, config, 200,
                {'complete': True, 'unavailable': [], 'withheld': []})
            record_assessment(home, historical, 'reflected')
        selected = reviewer.select_manifest(home, cutoff=201, config_snapshot=config)
        assert [e['episode_id'] for e in selected['episodes']] == [current['episode_id']]
        catalog = json.loads((reviewer.paths(home)['catalogs'] / f"{selected['catalog_id']}.json").read_text())
        assert not any(row['task_id'].startswith('history-') for row in catalog['assigned'])
        record_assessment(home, selected['episodes'][0], 'reflected')
        no_work = reviewer.select_manifest(home, cutoff=202, config_snapshot=config)
        assert no_work['episodes'] == []


def test_whole_fix_selection_git_deadline_reaps_child(monkeypatch):
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='deadline-git', alive=True)
        tools = home / 'deadline-bin'
        tools.mkdir()
        git = tools / 'git'
        pid_file = home / 'selection-git.pid'
        git.write_text('#!/usr/bin/python3\nimport os, time\n'
                       'open(os.environ["SELECTION_GIT_PID"], "w").write(str(os.getpid()))\n'
                       'time.sleep(10)\n')
        git.chmod(0o755)
        monkeypatch.setenv('SELECTION_GIT_PID', str(pid_file))
        monkeypatch.setenv('PATH', str(tools) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'))
        started = time.monotonic()
        with pytest.raises(store.Refusal):
            reviewer.select_manifest(home, cutoff=200, config_snapshot=reviewer.read_config(home),
                                     deadline_epoch_ms=int((time.time() + 1) * 1000))
        assert time.monotonic() - started < 4
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_whole_fix_selection_lock_wait_obeys_deadline():
    with lab() as (home, _):
        completed_session(home)
        bin_path = str(Path(__file__).resolve().parents[1] / 'bin')
        program = ('import sys\nfrom pathlib import Path\n'
                   f'sys.path.insert(0, {bin_path!r})\nfrom agd_lib import store\n'
                   'with store.lock(Path(sys.argv[1])):\n'
                   ' print("locked", flush=True)\n sys.stdin.read()\n')
        holder = subprocess.Popen([sys.executable, '-c', program, str(home)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        result = {}
        def select():
            try:
                reviewer.select_manifest(home, cutoff=200, config_snapshot=reviewer.read_config(home),
                                         deadline_epoch_ms=int((time.time() + 1) * 1000))
            except Exception as error:
                result['error'] = error
        selector = threading.Thread(target=select)
        try:
            assert holder.stdout.readline().strip() == 'locked'
            selector.start()
            selector.join(4)
            timely = not selector.is_alive()
        finally:
            holder.stdin.close()
            holder.wait(timeout=5)
            holder.stdout.close()
            holder.stderr.close()
            if selector.ident is not None:
                selector.join(5)
        assert timely, 'selection waited beyond its deadline'
        assert not selector.is_alive()
        assert isinstance(result.get('error'), store.Refusal)


def test_whole_fix_catalog_acquisition_does_not_block_mutation_or_publish_stale_progress(monkeypatch):
    with lab() as (home, _):
        completed_session(home)
        episode = selection(home)['episodes'][0]
        entered, release = threading.Event(), threading.Event()
        original = reviewer._selection_catalog
        result = {}

        def paused(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        def select():
            try:
                result['manifest'] = selection(home, cutoff=201)
            except Exception as error:
                result['error'] = error

        def mutate():
            try:
                record_assessment(home, episode, 'reflected')
                result['mutated'] = True
            except Exception as error:
                result['mutation_error'] = error

        monkeypatch.setattr(reviewer, '_selection_catalog', paused)
        selector = threading.Thread(target=select)
        mutator = threading.Thread(target=mutate)
        selector.start()
        try:
            assert entered.wait(5)
            mutator.start()
            mutator.join(2)
            assert not mutator.is_alive(), 'catalog acquisition held the mutation lock'
            assert result.get('mutated'), result
        finally:
            release.set()
            selector.join(10)
            if mutator.ident is not None:
                mutator.join(10)
        assert not selector.is_alive()
        assert not mutator.is_alive()
        if 'manifest' in result:
            assert episode['episode_id'] not in {item['episode_id'] for item in result['manifest']['episodes']}
        else:
            assert isinstance(result.get('error'), store.Refusal)


def test_whole_fix_selection_artifact_scan_stops_at_deadline_without_whole_file_read(monkeypatch):
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='deadline-artifact', alive=True)
        brief = tasks.task_file(home, task['id']).parent / 'brief.md'
        brief.write_bytes(b'x' * (4 * 1024 * 1024))
        identity = brief.stat()
        entered, release = threading.Event(), threading.Event()
        result, observed = {}, {'bytes': 0}
        original_fdopen, original_read = os.fdopen, os.read
        original_open, original_io_open = builtins.open, io.open

        def is_brief(fd):
            try:
                current = os.fstat(fd)
                return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
            except OSError:
                return False

        def account(data):
            observed['bytes'] += len(data)
            if data and not entered.is_set():
                entered.set()
                assert release.wait(10)
            return data

        class ObservedStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def read(self, *args, **kwargs):
                return account(self.stream.read(*args, **kwargs))

        def wrap(stream):
            if isinstance(stream, ObservedStream):
                return stream
            try:
                selected = is_brief(stream.fileno()) and 'b' in stream.mode
            except (OSError, ValueError, AttributeError):
                selected = False
            return ObservedStream(stream) if selected else stream

        def fdopen(fd, *args, **kwargs):
            return wrap(original_fdopen(fd, *args, **kwargs))

        def builtin_open(*args, **kwargs):
            return wrap(original_open(*args, **kwargs))

        def io_open(*args, **kwargs):
            return wrap(original_io_open(*args, **kwargs))

        def read(fd, size):
            data = original_read(fd, size)
            return account(data) if is_brief(fd) else data

        def select():
            try:
                result['manifest'] = reviewer.select_manifest(
                    home, cutoff=200, config_snapshot=config, deadline_epoch_ms=deadline)
            except Exception as error:
                result['error'] = error

        config = reviewer.read_config(home)
        deadline = int((time.time() + 4) * 1000)
        monkeypatch.setattr(os, 'fdopen', fdopen)
        monkeypatch.setattr(os, 'read', read)
        monkeypatch.setattr(builtins, 'open', builtin_open)
        monkeypatch.setattr(io, 'open', io_open)
        selector = threading.Thread(target=select)
        selector.start()
        try:
            assert entered.wait(8), 'artifact scan did not reach the controlled read'
            threading.Event().wait(max(0, deadline / 1000 - time.time()) + 0.05)
        finally:
            release.set()
            selector.join(10)
        assert not selector.is_alive()
        assert isinstance(result.get('error'), store.Refusal)
        assert 0 < observed['bytes'] < identity.st_size


def test_whole_fix_selection_refuses_fifo_artifact_without_waiting():
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='fifo-artifact', alive=True)
        brief = tasks.task_file(home, task['id']).parent / 'brief.md'
        brief.unlink()
        os.mkfifo(brief)
        bin_path = str(Path(__file__).resolve().parents[1] / 'bin')
        program = ('import json, sys, time\nfrom pathlib import Path\n'
                   f'sys.path.insert(0, {bin_path!r})\nfrom agd_lib import reviewer, store\n'
                   'home = Path(sys.argv[1])\n'
                   'try:\n'
                   ' result = reviewer.select_manifest(home, cutoff=200, config_snapshot=reviewer.read_config(home),'
                   ' deadline_epoch_ms=int((time.time()+1)*1000))\n'
                   ' print(json.dumps(result))\n'
                   'except store.Refusal:\n print("refused")\n')
        child = subprocess.Popen([sys.executable, '-c', program, str(home)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            try:
                stdout, stderr = child.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                pytest.fail('selection blocked opening a nonregular artifact')
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        if stdout.strip() != 'refused':
            selected = json.loads(stdout)
            assert not selected['coverage']['complete']
            assert all(locator.get('kind') != 'intent' for episode in selected['episodes']
                       for locator in episode['locators'])


def test_whole_fix_selected_assignment_acquisition_does_not_block_or_publish_stale_snapshot(monkeypatch):
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='assignment-race', alive=True)
        task_dir = tasks.task_file(home, task['id']).parent
        assigned = worker_skills._manifest(home, task)['skills'][0]
        skill = Path(assigned['snapshot']) / 'SKILL.md'
        old_hash = hashlib.sha256(skill.read_bytes()).hexdigest()
        entered, release = threading.Event(), threading.Event()
        original = reviewer._manifest
        baseline = original(home, task_dir, task, {'complete': True, 'unavailable': [], 'withheld': []})
        assert any(row['snapshot_sha256'] == old_hash for row in baseline)
        result = {}

        def paused(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[2]['id'] == task['id']:
                entered.set()
                assert release.wait(5)
            return value

        def select():
            try:
                result['manifest'] = selection(home)
            except Exception as error:
                result['error'] = error

        def mutate():
            try:
                with store.lock(home):
                    skill.write_text(skill.read_text() + '\nchanged assignment snapshot\n')
                result['mutated'] = True
            except Exception as error:
                result['mutation_error'] = error

        monkeypatch.setattr(reviewer, '_manifest', paused)
        selector = threading.Thread(target=select)
        mutator = threading.Thread(target=mutate)
        selector.start()
        try:
            assert entered.wait(5), 'selected assignment was not acquired'
            mutator.start()
            mutator.join(2)
            assert not mutator.is_alive(), 'assignment acquisition held the mutation lock'
            assert result.get('mutated'), result
        finally:
            release.set()
            selector.join(10)
            if mutator.ident is not None:
                mutator.join(10)
        assert not selector.is_alive() and not mutator.is_alive()
        if 'manifest' in result:
            manifest = result['manifest']
            if manifest['catalog_id']:
                catalog = json.loads((reviewer.paths(home)['catalogs'] /
                                      (manifest['catalog_id'] + '.json')).read_text())
                rows = [row for group in catalog['assigned'] if group['task_id'] == task['id']
                        for row in group['skills']]
                assert all(row['snapshot_sha256'] != old_hash for row in rows)
                if not rows:
                    assert not manifest['coverage']['complete']
            else:
                assert not manifest['coverage']['complete']
        else:
            assert isinstance(result.get('error'), store.Refusal)


@pytest.mark.parametrize('expired', ['run', 'freshness'])
def test_selection_expiring_during_publication_check_preserves_state(monkeypatch, expired):
    from agd_lib import reviewer_inspection
    with lab() as (home, _):
        completed_session(home)
        selection(home)
        folders = reviewer.paths(home)
        schedule = folders['progress'] / 'schedule.json'
        before_schedule = schedule.read_bytes()

        def publications():
            return {str(path): path.read_bytes()
                    for name in ('catalogs', 'selection_details', 'selections')
                    for path in folders[name].glob('*.json')}

        before = publications()
        wall, monotonic = [time.time()], [time.monotonic()]
        deadline = int((wall[0] + 30) * 1000)
        checking = [False]
        fingerprint = reviewer_inspection._fingerprint
        check = reviewer_inspection._check_watches

        def delayed_fingerprint(*args, **kwargs):
            value = fingerprint(*args, **kwargs)
            if checking[0]:
                if expired == 'run':
                    wall[0] += 60
                else:
                    monotonic[0] += 2
            return value

        def publication_check(*args, **kwargs):
            checking[0] = True
            try:
                return check(*args, **kwargs)
            finally:
                checking[0] = False

        with monkeypatch.context() as patch:
            patch.setattr(time, 'time', lambda: wall[0])
            patch.setattr(time, 'monotonic', lambda: monotonic[0])
            patch.setattr(reviewer_inspection, '_fingerprint', delayed_fingerprint)
            patch.setattr(reviewer_inspection, '_check_watches', publication_check)
            with pytest.raises(store.Refusal):
                reviewer.select_manifest(home, cutoff=201, config_snapshot=reviewer.read_config(home),
                                         deadline_epoch_ms=deadline)
        assert publications() == before
        assert schedule.read_bytes() == before_schedule
        with store.lock(home, deadline_epoch_ms=int((time.time() + 1) * 1000)):
            assert schedule.read_bytes() == before_schedule
