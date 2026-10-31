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


def test_uninstall_stops_owned_process_tree_not_pid_replacement(tmp_path):
    import os
    import select
    import signal
    import subprocess
    import sys
    heartbeat = tmp_path / 'heartbeat'
    child_program = (
        'import pathlib, sys, time\n'
        'path = pathlib.Path(sys.argv[1])\n'
        'path.write_text("started")\n'
        'print("ready", flush=True)\n'
        'while True:\n'
        ' path.write_text(str(time.monotonic_ns()))\n'
        ' time.sleep(.01)\n')
    parent_program = (
        'import signal, subprocess, sys\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'child = subprocess.Popen([sys.executable, "-c", {child_program!r}, sys.argv[1]], stdout=subprocess.PIPE, text=True)\n'
        'assert child.stdout.readline() == "ready\\n"\n'
        'print(child.pid, flush=True)\n'
        'sys.stdin.read()\n')
    parent = subprocess.Popen([sys.executable, '-c', parent_program, str(heartbeat)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                              start_new_session=True)
    sentinel = subprocess.Popen(
        [sys.executable, '-c',
         'import sys\nprint("ready", flush=True)\nfor line in sys.stdin: print("ack", flush=True)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    child_fd = None
    def identity(pid):
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return {'pid': pid, 'start': fields[19], 'uid': Path(f'/proc/{pid}').stat().st_uid,
                'ppid': int(fields[1]), 'pgid': int(fields[2]), 'sid': int(fields[3])}
    try:
        child_pid = int(parent.stdout.readline())
        child_fd = os.pidfd_open(child_pid)
        assert sentinel.stdout.readline() == 'ready\n'
        supervision.stop_owned_processes([{**identity(sentinel.pid), 'start': '0'}], timeout=.2)
        assert sentinel.poll() is None
        supervision.stop_owned_processes([identity(parent.pid)], timeout=2)
        assert parent.wait(timeout=3) != 0
        assert select.select([child_fd], [], [], 0)[0] == [child_fd]
        stamp = heartbeat.stat().st_mtime_ns
        sentinel.stdin.write('ping\n')
        sentinel.stdin.flush()
        assert sentinel.stdout.readline() == 'ack\n'
        assert heartbeat.stat().st_mtime_ns == stamp
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if child_fd is not None:
            if not select.select([child_fd], [], [], 0)[0]:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            os.close(child_fd)
        sentinel.terminate()
        sentinel.wait(timeout=3)
        parent.stdin.close()
        parent.stdout.close()
        sentinel.stdin.close()
        sentinel.stdout.close()


def test_uninstall_process_stop_refuses_bare_pid():
    from agd_lib import store
    import os
    with pytest.raises(store.Refusal):
        supervision.stop_owned_processes([os.getpid()])


def test_uninstall_does_not_adopt_a_detached_user_process():
    import os
    import select
    import signal
    import subprocess
    import sys
    child_program = 'import time\nprint("ready", flush=True)\nwhile True: time.sleep(1)'
    program = (
        'import subprocess, sys\n'
        f'child = subprocess.Popen([sys.executable, "-c", {child_program!r}], stdout=subprocess.PIPE, text=True, start_new_session=True)\n'
        'assert child.stdout.readline() == "ready\\n"\n'
        'print(child.pid, flush=True)\n'
        'sys.stdin.read()\n')
    parent = subprocess.Popen([sys.executable, '-c', program], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    descriptor = None
    try:
        child_pid = int(parent.stdout.readline())
        descriptor = os.pidfd_open(child_pid)
        identity = supervision.process_identity(parent.pid)
        identity.pop('state')
        supervision.stop_owned_processes([identity], timeout=1)
        parent.wait(timeout=3)
        assert not select.select([descriptor], [], [], 0)[0]
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=3)
        if descriptor is not None:
            if not select.select([descriptor], [], [], 0)[0]:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            os.close(descriptor)
