import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from support import command, lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, reviewer_runner, store, supervision, tasks, workers


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


def test_packet_freezes_catalog_and_assigned_manifest_without_claiming_usage():
    with lab() as (home, _):
        task, _, _ = ended_worker(home, 'skills', str(uuid.uuid4()), [message('evidence')])
        skill = home / 'config' / 'worker-skills' / 'agd-test'; skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: agd-test\ndescription: Test skill\n---\n')
        task.update(harness='omp')
        tasks.save(home, task)
        worker_skills = __import__('agd_lib.worker_skills', fromlist=['prepare'])
        worker_skills.prepare(home, task, {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-test']})
        selected = packet(home)
        assert any(row['id'] == 'agd-test' for row in selected['catalog'])
        assert selected['skill_provenance'][0]['assigned'][0]['id'] == 'agd-test'
        assert selected['skill_provenance'][0]['usage'] == 'unknown'


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


def test_retrospective_requires_closed_prior_receipt_and_excludes_later_runs():
    with lab() as (home, _):
        prior, later = str(uuid.uuid4()), str(uuid.uuid4())
        receipts = reviewer.paths(home)['receipts']; receipts.mkdir(parents=True)
        store.atomic_json(receipts / f'{prior}.json', {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': prior, 'dispatch_state': 'closed', 'closed_at': 100.0})
        store.atomic_json(receipts / f'{later}.json', {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': later, 'dispatch_state': 'closed', 'closed_at': 300.0})
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


def test_runner_persists_reservation_before_resolver_failure(monkeypatch):
    with lab() as (home, _):
        config = reviewer.read_config(home); config['review_model'] = 'configured'; store.atomic_json(reviewer.paths(home)['config'], config)
        intent = reviewer.enqueue_intent(home, kind='manual')
        monkeypatch.setattr(reviewer_runner, 'resolve_model', lambda *_: (_ for _ in ()).throw(store.Refusal('no model')))
        with pytest.raises(store.Refusal, match='no model'):
            reviewer_runner.launch(home, intent)
        receipts = list(reviewer.paths(home)['receipts'].glob('*.json'))
        assert len(receipts) == 1 and json.loads(receipts[0].read_text())['dispatch_state'] == 'reserved'


def test_reserved_default_intent_is_consumed_before_next_identical_trigger():
    with lab() as (home, _):
        intent = reviewer.enqueue_intent(home, kind='default')
        reviewer.reserve_run(home, intent, config_snapshot=reviewer.read_config(home), instruction_snapshot=b'frozen',
                             started_at=1.0, deadline_epoch_ms=2)
        assert reviewer.enqueue_intent(home, kind='default')['intent_id'] != intent['intent_id']
