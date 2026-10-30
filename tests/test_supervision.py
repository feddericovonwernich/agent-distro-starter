import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from test_projects import CLI, run
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
from agd_lib.supervision import process_start
from agd_lib import supervision

from support import lab, command


def test_acknowledging_wake_does_not_answer_decision():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Investigate value.txt.\n')
        command(home, 'task', 'create', 'question', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        message = home/'question.md'; message.write_text('Which value is intended?\n')
        command(home, 'worker', 'event', 'question', '--incarnation', 'central',
                '--kind', 'decision', '--key', 'value', '--message', str(message))
        command(home, 'session', 'reconcile')
        event = command(home, 'events', 'list')[0]
        command(home, 'events', 'acknowledge', event['id'])
        state = command(home, 'task', 'status', 'question')
        assert state['open_decisions'][0]['key'] == 'value'
        assert {'task': 'question', 'key': 'value'} in command(home, 'session', 'status')['openDecisions']
        assert command(home, 'events', 'list') == []

def test_event_survives_reconcile_and_ack_is_idempotent():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Investigate value.txt.\n')
        command(home, 'task', 'create', 'durable', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        message = home/'question.md'; message.write_text('Choose a value.\n')
        command(home, 'worker', 'event', 'durable', '--incarnation', 'central',
                '--kind', 'decision', '--key', 'value', '--message', str(message))
        command(home, 'session', 'reconcile')
        first = command(home, 'events', 'list')
        command(home, 'session', 'reconcile')
        assert command(home, 'events', 'list') == first
        command(home, 'events', 'acknowledge', first[0]['id'])
        command(home, 'events', 'acknowledge', first[0]['id'])
        command(home, 'session', 'reconcile')
        assert command(home, 'events', 'list') == []
        assert command(home, 'task', 'status', 'durable')['open_decisions'][0]['status'] == 'open'


def test_live_owner_refuses_foreign_mutations():
    with lab() as (home, project):
        owner = os.getpid()
        token = 'known-owner-token'
        watcher = subprocess.Popen(
            [str(CLI), '--home', str(home), 'session', 'start', '--owner-pid', str(owner),
             '--owner-start', process_start(owner), '--generation', 'one'],
            env=dict(os.environ, AGD_OWNER=token), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            assert json.loads(watcher.stdout.readline())['type'] == 'ready'
            brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
            command(home, 'task', 'create', 'foreign', '--project', 'sample',
                    '--brief', str(brief), expected=3)
            assert not (home/'data'/'tasks'/'foreign').exists()
            env = dict(os.environ, AGD_OWNER=token, TREEHOUSE_ROOT=str(home.parent/'pool'))
            result = run([CLI, '--home', home, 'task', 'create', 'owned',
                          '--project', 'sample', '--brief', brief, '--kind', 'scout'], env=env)
            assert json.loads(result.stdout)['id'] == 'owned'
            command(home, 'worker', 'run', 'owned', '--incarnation', 'foreign', expected=3)
        finally:
            watcher.terminate()
            watcher.wait(timeout=5)


def test_dirty_ready_workspace_cannot_emit_completion():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'changed', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        report = home/'report.md'; report.write_text('Observed base in value.txt.\n')
        command(home, 'task', 'ready', 'changed', '--report', str(report))
        (Path(task['worktree'])/'value.txt').write_text('unreviewed change\n')
        command(home, 'session', 'reconcile')
        assert not any(item['kind'] == 'ready' for item in command(home, 'events', 'list'))
        assert (home/'data'/'tasks'/'changed'/'report.md').read_text() == report.read_text()



def test_missing_backend_keeps_worker_workspace_unknown():
    from agd_lib import tasks
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'outage', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', phase='running', incarnation='first',
                    harness='omp', backend='tmux', worker_alive=None,
                    endpoint={'prefix': ['tmux', '-L', 'agd-unavailable-probe'],
                              'server_pid': '1', 'pane': '%1', 'token': 'owned'})
        tasks.save(home, task)
        command(home, 'session', 'reconcile')
        assert any(event['kind'] == 'blocked' and event['task'] == 'outage'
                   for event in command(home, 'events', 'list'))
        assert tasks.read(home, 'outage')['worker_alive'] is None
        assert Path(task['worktree']).is_dir()


def test_resolve_decision_records_answer_without_erasing_wake():
    from agd_lib import tasks
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'choice', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', phase='running', incarnation='first')
        tasks.save(home, task)
        question = home/'question.md'; question.write_text('Which value?\n')
        command(home, 'worker', 'event', 'choice', '--incarnation', 'first',
                '--kind', 'decision', '--key', 'value', '--message', str(question))
        command(home, 'session', 'reconcile')
        answer = home/'answer.md'; answer.write_text('Use base.\n')
        result = command(home, 'decision', 'resolve', 'choice', 'value', '--answer', str(answer))
        assert result['status'] == 'resolved'
        assert command(home, 'worker', 'inbox', 'choice')[0]['body'] == 'Use base.\n'
        assert command(home, 'task', 'status', 'choice')['open_decisions'] == []
        assert any(e['kind'] == 'decision' for e in command(home, 'events', 'list'))


def test_unfinished_clean_exit_is_actionable():
    from agd_lib import tasks
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'exited', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', phase='running', incarnation='first', worker_alive=False)
        tasks.save(home, task)
        note = home/'exit.md'; note.write_text('worker process exited 0\n')
        command(home, 'worker', 'event', 'exited', '--incarnation', 'first',
                '--kind', 'idle', '--key', 'runner-exit', '--message', str(note))
        command(home, 'session', 'reconcile')
        assert any(row['kind'] == 'blocked' and row['task'] == 'exited'
                   for row in command(home, 'events', 'list'))


def test_failed_completion_emits_one_correlated_blocked_event():
    from unittest.mock import patch
    from agd_lib import store, tasks, workers

    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'reply', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(
            executor='worker',
            incarnation='first',
            phase='running',
            worker_alive=True,
            endpoint={'identity': 'worker-endpoint'},
            backend='tmux',
        )
        tasks.save(home, task)
        with store.lock(home):
            request = workers.enqueue_locked(
                home, task, 'Question.\n')
        diagnostic = 'steering turn ended without assistant text'
        with patch.dict(os.environ, {
                'AGD_TASK_ID': 'reply',
                'AGD_INCARNATION': 'first',
        }):
            workers.complete(
                home, 'reply', request.stem, 'first', 'failed', diagnostic)
        current = tasks.read(home, 'reply')
        current['worker_alive'] = False
        tasks.save(home, current)

        command(home, 'session', 'reconcile')
        command(home, 'session', 'reconcile')
        replies = [
            event for event in command(home, 'events', 'list')
            if event['key'] == request.stem
        ]
        assert len(replies) == 1
        assert replies[0]['kind'] == 'blocked'
        assert replies[0]['details'] == diagnostic


def test_recovered_ready_check_rearms_next_failure():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'rearm', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        report = home/'report.md'; report.write_text('Observed base.\n')
        command(home, 'task', 'ready', 'rearm', '--report', str(report))
        work_file = Path(task['worktree'])/'value.txt'
        work_file.write_text('first dirty edit\n')
        command(home, 'session', 'reconcile')
        first = next(e for e in command(home, 'events', 'list') if e['kind'] == 'blocked')
        command(home, 'events', 'acknowledge', first['id'])
        work_file.write_text('base\n')
        command(home, 'session', 'reconcile')
        work_file.write_text('second dirty edit\n')
        command(home, 'session', 'reconcile')
        assert any(e['kind'] == 'blocked' and e['id'] != first['id']
                   for e in command(home, 'events', 'list'))


def test_watcher_runs_central_review_tick_after_reconcile(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    monkeypatch.setenv('AGD_OWNER', 'owner-token')
    monkeypatch.setattr(supervision, 'process_start', lambda _pid: 'start')
    calls = []
    monkeypatch.setattr(supervision, 'reconcile', lambda _home: calls.append('reconcile') or {'pending': []})
    monkeypatch.setattr('agd_lib.relay_reviews.tick', lambda _home: calls.append('tick'))
    monkeypatch.setattr('agd_lib.supervision._stop_owned_relay_services', lambda _home: calls.append('stop'))
    monkeypatch.setattr(supervision.time, 'sleep',
                        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        supervision.watch(home, 1, 'start', 'generation')
    assert calls == ['reconcile', 'tick', 'stop']


def test_owner_exit_stops_only_regular_owned_relay_service(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    good = home / 'state' / 'relay' / 'projects' / 'sample'
    good.mkdir(parents=True)
    (good / 'service.json').write_text(json.dumps({'schema': 1, 'project': 'sample', 'root': str(good)}))
    unsafe = home / 'state' / 'relay' / 'projects' / 'unsafe'
    unsafe.mkdir()
    (unsafe / 'service.json').symlink_to(tmp_path / 'outside.json')
    stopped = []
    monkeypatch.setattr('agd_lib.relay.stop_project_service',
                        lambda received_home, project, reason: stopped.append((received_home, project, reason)))

    supervision._stop_owned_relay_services(home)

    assert stopped == [(home, 'sample', 'central owner exited')]

if __name__ == '__main__':
    test_acknowledging_wake_does_not_answer_decision()
    test_event_survives_reconcile_and_ack_is_idempotent()
    test_live_owner_refuses_foreign_mutations()
    test_dirty_ready_workspace_cannot_emit_completion()
    test_missing_backend_keeps_worker_workspace_unknown()
    test_resolve_decision_records_answer_without_erasing_wake()
    test_unfinished_clean_exit_is_actionable()
    test_recovered_ready_check_rearms_next_failure()
