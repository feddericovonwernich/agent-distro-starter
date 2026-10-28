import os
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from support import command, lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, reviewer_runner, store, supervision, tasks, worker_skills, workers


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
        (skill / 'SKILL.md').write_text('---\nname: agd-test\ndescription: Test skill\n---\n' + 'x' * 600)
        task.update(harness='omp')
        tasks.save(home, task)
        worker_skills = __import__('agd_lib.worker_skills', fromlist=['prepare'])
        worker_skills.prepare(home, task, {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-test']})
        selected = packet(home)
        assert any(row['id'] == 'agd-test' for row in selected['catalog'])
        catalog_entry = next(row for row in selected['catalog'] if row['id'] == 'agd-test')
        assert catalog_entry['snapshot_truncated'] is True and catalog_entry['snapshot_total_bytes'] > len(catalog_entry['snapshot_excerpt'].encode())
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
        assert selected['sources'][0]['byte_watermark'] < next(session.glob('*.jsonl')).stat().st_size
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

def test_shared_cap_does_not_consume_clipped_candidate_material():
    with lab() as (home, _):
        ended_worker(home, 'first', str(uuid.uuid4()), [message('a' * 3000, 100)])
        ended_worker(home, 'second', str(uuid.uuid4()), [message('b' * 3000, 100)])
        config = reviewer.read_config(home)
        config['limits'].update(max_candidates=2, max_input_bytes=15000)
        cutoff = time.time() + 100
        first = reviewer.select_packet(home, cutoff=cutoff, config_snapshot=config)
        assert {source['task_id'] for source in first['sources'] if source['kind'] == 'native-jsonl'} == {'first', 'second'}
        assert {source['task_id'] for source in first['sources'] if any(excerpt['source_id'] == source['source_id'] for excerpt in first['excerpts'])} == {'first'}
        reviewer.mark_material_reviewed(home, first['coverage']['followup_id'])
        second = reviewer.select_packet(home, cutoff=cutoff + 1, config_snapshot=config)
        assert {source['task_id'] for source in second['sources'] if source['kind'] == 'native-jsonl'} == {'second'}
        reviewer.mark_material_reviewed(home, second['coverage']['followup_id'])
        assert reviewer.select_packet(home, cutoff=cutoff + 2, config_snapshot=config)['episodes'] == []

def test_shared_cap_revisits_only_clipped_candidate_excerpt_without_starving_next():
    with lab() as (home, _):
        ended_worker(home, 'a', str(uuid.uuid4()), [message('a' * 2500, 100)])
        ended_worker(home, 'b', str(uuid.uuid4()), [message('b1' * 1200, 100), message('b2' * 1200, 101)])
        ended_worker(home, 'c', str(uuid.uuid4()), [message('c' * 2500, 100)])
        config = reviewer.read_config(home); config['limits'].update(max_candidates=3, max_input_bytes=17000)
        first = reviewer.select_packet(home, cutoff=time.time() + 100, config_snapshot=config)
        b_first = [row['text'] for row in first['excerpts'] if any(source['source_id'] == row['source_id'] and source.get('task_id') == 'b' for source in first['sources'])]
        assert b_first == ['b1' * 1200]
        reviewer.mark_material_reviewed(home, first['coverage']['followup_id'])
        second = reviewer.select_packet(home, cutoff=time.time() + 101, config_snapshot=config)
        b_second = [row['text'] for row in second['excerpts'] if any(source['source_id'] == row['source_id'] and source.get('task_id') == 'b' for source in second['sources'])]
        assert b_second == ['b2' * 1200]


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
    return reviewer.begin_run(home, run, selected, model='openai/reviewer-fixture'), selected


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
