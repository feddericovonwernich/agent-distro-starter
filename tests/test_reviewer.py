import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from support import command, lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, reviewer, tasks, workers


def ended_worker(home, task_id, incarnation, text, status):
    brief = home / f'{task_id}.md'
    brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief))
    task.update(executor='worker', incarnation=incarnation, phase='running', worker_alive=False)
    tasks.save(home, task)
    binding = observability.worker_binding(home, task_id, incarnation)
    session_id, timestamp = str(uuid.uuid4()), '2026-01-01T00:00:00.000Z'
    rows = [
        {'type': 'session', 'version': 3, 'id': session_id, 'timestamp': timestamp, 'cwd': str(home)},
        {'type': 'message', 'id': str(uuid.uuid4()), 'parentId': None, 'timestamp': timestamp,
         'message': {'role': 'user', 'content': text, 'timestamp': 1767225600000}},
    ]
    Path(binding['session_dir'], f'{session_id}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    note = tasks.task_file(home, task_id).parent / 'runner-exit.txt'
    note.write_text(f'worker process exited {status}')
    workers.event(home, task_id, incarnation, 'failed' if status else 'idle', 'runner-exit', note)
    return task


def test_packet_has_distinct_failure_and_counterexample_sources():
    with lab() as (home, _):
        failed = ended_worker(home, 'failed', str(uuid.uuid4()), 'failure', 1)
        succeeded = ended_worker(home, 'success', str(uuid.uuid4()), 'counterexample', 0)

        packet = reviewer.select_packet(home, cutoff=time.time(), config_snapshot=reviewer.read_config(home))

        native = [source for source in packet['sources'] if source['kind'] == 'native-jsonl']
        assert {episode['task_id'] for episode in packet['episodes']} == {failed['id'], succeeded['id']}
        assert len({source['source_id'] for source in native}) == 2
        assert {excerpt['text'] for excerpt in packet['excerpts']} == {'failure', 'counterexample'}


def test_packet_accepts_closed_prior_incarnation_but_not_live_current_one():
    with lab() as (home, _):
        old = str(uuid.uuid4())
        ended_worker(home, 'retried', old, 'closed attempt', 1)
        task = tasks.read(home, 'retried')
        task.update(incarnation=str(uuid.uuid4()), worker_alive=True)
        tasks.save(home, task)

        packet = reviewer.select_packet(home, cutoff=time.time(), config_snapshot=reviewer.read_config(home))

        assert [(episode['task_id'], episode['incarnation']) for episode in packet['episodes']] == [('retried', old)]


def test_packet_freezes_bounded_excerpts_and_unknown_skill_usage():
    with lab() as (home, _):
        task = ended_worker(home, 'bounded', str(uuid.uuid4()), 'x' * 100, 1)
        task['worker_skills'] = {'schema': 1, 'skills': [{'id': 'assigned', 'files': []}]}
        tasks.save(home, task)
        config = reviewer.read_config(home)
        config['limits']['max_input_bytes'] = 64

        packet = reviewer.select_packet(home, cutoff=time.time(), config_snapshot=config)

        assert len(packet['excerpts'][0]['text'].encode()) <= 64
        assert packet['skill_provenance'][0] == {'task_id': 'bounded', 'assigned': ['assigned'], 'usage': 'unknown'}
        assert packet['coverage']['langfuse_index'] == 'unused'


def test_reserved_receipt_freezes_snapshots_and_begin_only_completes_it():
    with lab() as (home, _):
        intent = reviewer.enqueue_intent(home, kind='default')
        config = reviewer.read_config(home)
        run = reviewer.reserve_run(home, intent, config_snapshot=config, instruction_snapshot=b'frozen',
                                   started_at=100.0, deadline_epoch_ms=200000)
        packet = {'packet_id': 'a' * 64, 'sources': [], 'episodes': [], 'excerpts': [], 'coverage': {}}

        begun = reviewer.begin_run(home, run, packet, model='native-model')

        assert begun['review_run_id'] == run['review_run_id']
        assert begun['started_at'] == 100.0
        assert begun['deadline_epoch_ms'] == 200000
        assert begun['counters'] == {'model_calls': 0}
        assert begun['packet_id'] == 'a' * 64
        with pytest.raises(Exception):
            reviewer.begin_run(home, begun, packet, model='other-model')


def test_reviewer_binding_uses_native_session_id_without_worker_environment(monkeypatch):
    with lab() as (home, _):
        monkeypatch.setenv('AGD_TASK_ID', 'unrelated')
        monkeypatch.setenv('AGD_INCARNATION', str(uuid.uuid4()))

        binding = observability.reviewer_binding(home, str(uuid.uuid4()), 'native-session')

        assert binding['purpose'] == 'background-skill-review'
        assert binding['native_session_id'] == 'native-session'
        assert Path(binding['session_dir']).name == 'omp'
        assert Path(binding['session_dir']).parent.name == binding['review_run_id']


def test_runner_reserves_before_collecting_and_uses_frozen_instruction(monkeypatch):
    from agd_lib import reviewer_runner

    with lab() as (home, _):
        intent = reviewer.enqueue_intent(home, kind='manual')
        config = reviewer.read_config(home)
        config['review_model'] = 'chosen-model'
        monkeypatch.setattr(reviewer_runner.reviewer, 'read_config', lambda _: config)
        monkeypatch.setattr(reviewer_runner.reviewer, 'select_packet',
                            lambda *_args, **kwargs: {'packet_id': 'b' * 64, 'sources': [], 'episodes': [],
                                                      'excerpts': [], 'coverage': {}, 'config_snapshot_sha256': kwargs['config_snapshot']})

        run = reviewer_runner.launch(home, intent)

        receipt = json.loads((reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json").read_text())
        assert receipt['dispatch_state'] == 'begun'
        assert receipt['model'] == 'chosen-model'
        assert json.loads((reviewer.paths(home)['manifests'] / f"{receipt['instruction_snapshot_ref']}.json").read_text())['text'].startswith('# Background') 
