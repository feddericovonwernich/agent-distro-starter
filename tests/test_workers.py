import os
import sys
from pathlib import Path
from support import lab, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
from agd_lib import tasks


def test_old_worker_cannot_change_current_task():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'worker', '--project', 'sample', '--brief', str(brief))
        task.update(executor='worker', incarnation='new-worker', phase='running')
        tasks.save(home, task)
        message = home/'message.md'; message.write_text('old worker failed\n')
        command(home, 'worker', 'event', 'worker', '--incarnation', 'old-worker',
                '--kind', 'failed', '--key', 'exit', '--message', str(message), expected=3)
        current = tasks.read(home, 'worker')
        assert current['incarnation'] == 'new-worker'
        assert current['lease_id'] == task['lease_id']


def test_current_scout_worker_can_report_ready():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'scout', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='current', phase='running')
        tasks.save(home, task)
        report = home/'finding.md'; report.write_text('value.txt contains base; read verified.\n')
        assert command(home, 'task', 'ready', 'scout', '--report', str(report))['phase'] == 'ready'


def test_ready_worker_cannot_release_its_live_workspace():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'live', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='current', phase='running', worker_alive=True)
        tasks.save(home, task)
        report = home/'finding.md'; report.write_text('value.txt contains base; read verified.\n')
        command(home, 'task', 'ready', 'live', '--report', str(report))
        assert command(home, 'session', 'status')['delegatedWork'] is True
        command(home, 'task', 'cleanup', 'live', expected=3)
        assert Path(task['worktree']).is_dir()
        assert tasks.read(home, 'live')['lease_id'] == task['lease_id']


def test_relaunch_refuses_unknown_prior_process():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'restart', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='old', harness='omp', backend='tmux',
                    phase='running', worker_alive=None, endpoint={'presence': 'unknown'})
        tasks.save(home, task)
        command(home, 'worker', 'relaunch', 'restart', expected=3)
        assert tasks.read(home, 'restart')['incarnation'] == 'old'
        assert tasks.read(home, 'restart')['lease_id'] == task['lease_id']


def test_unavailable_backend_does_not_strand_lease():
    from test_projects import CLI, run
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'notstarted', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        env = dict(os.environ)
        env.pop('AGD_HERDR_SESSION', None)
        run([CLI, '--home', home, 'worker', 'launch', 'notstarted', '--backend', 'herdr'],
            env=env, expected=4)
        assert tasks.read(home, 'notstarted')['phase'] == 'prepared'
        assert tasks.read(home, 'notstarted')['lease_id'] == task['lease_id']


def test_worker_cannot_read_other_task_inbox():
    from test_projects import CLI, run
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'private', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='first', phase='running')
        tasks.save(home, task)
        message = home/'private-message.md'; message.write_text('Do not share this.\n')
        command(home, 'worker', 'send', 'private', '--file', str(message))
        env = dict(os.environ, AGD_TASK_ID='different', AGD_INCARNATION='second')
        result = run([CLI, '--home', home, 'worker', 'inbox', 'private'], env=env, expected=3)
        assert 'different task' in result.stderr


def test_crashed_runner_recovered_only_after_process_and_pane_absent():
    import subprocess
    import time
    from agd_lib import store, tmux
    from agd_lib.supervision import process_start
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'crashed', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        endpoint = tmux.create(home, 'crashed', 'first', Path(task['worktree']),
                               ['/bin/sh', '-c', 'sleep 2'])
        try:
            runner = subprocess.Popen(['sleep', '2'])
            start = process_start(runner.pid)
            task.update(executor='worker', incarnation='first', harness='omp',
                        backend='tmux', phase='running', worker_alive=None, endpoint=endpoint)
            tasks.save(home, task)
            store.atomic_json(home/'state'/'tasks'/'crashed'/'runner.json',
                              {'incarnation': 'first', 'pid': runner.pid, 'start': start,
                               'phase': 'spawned', 'child_pid': runner.pid, 'child_start': start})
            command(home, 'session', 'reconcile')
            assert tasks.read(home, 'crashed')['worker_alive'] is None
            runner.wait(timeout=3)
            deadline = time.monotonic() + 5
            while tmux.inspect(endpoint)['presence'] != 'missing' and time.monotonic() < deadline:
                time.sleep(0.1)
            assert tmux.inspect(endpoint)['presence'] == 'missing'
            command(home, 'session', 'reconcile')
            assert tasks.read(home, 'crashed')['worker_alive'] is False
            assert command(home, 'session', 'status')['delegatedWork'] is True
            assert Path(task['worktree']).is_dir()
        finally:
            tmux.close(endpoint)


def test_reconcile_adopts_exact_owned_endpoint_after_publication_gap():
    from agd_lib import tmux
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'adopt', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='first', harness='omp',
                    backend='tmux', phase='running', worker_alive=None)
        tasks.save(home, task)
        endpoint = tmux.create(home, 'adopt', 'first', Path(task['worktree']), ['/bin/sh', '-c', 'sleep 5'])
        try:
            command(home, 'session', 'reconcile')
            recovered = tasks.read(home, 'adopt')
            assert recovered['endpoint'] == endpoint
            assert recovered['lease_id'] == task['lease_id']
        finally:
            tmux.close(endpoint)


def test_proved_absent_unpublished_start_returns_to_prepared():
    import time
    from agd_lib import tmux
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'vanished', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='first', harness='omp',
                    backend='tmux', phase='running', worker_alive=None)
        tasks.save(home, task)
        endpoint = tmux.create(home, 'vanished', 'first', Path(task['worktree']),
                               ['/bin/sh', '-c', 'sleep 0.1'])
        deadline = time.monotonic() + 5
        while tmux.inspect(endpoint)['presence'] != 'missing' and time.monotonic() < deadline:
            time.sleep(0.1)
        assert tmux.inspect(endpoint)['presence'] == 'missing'
        command(home, 'session', 'reconcile')
        recovered = tasks.read(home, 'vanished')
        assert recovered['phase'] == 'prepared'
        assert recovered['lease_id'] == task['lease_id']
        assert Path(task['worktree']).is_dir()

if __name__ == '__main__':
    test_old_worker_cannot_change_current_task()
    test_current_scout_worker_can_report_ready()
    test_ready_worker_cannot_release_its_live_workspace()
    test_relaunch_refuses_unknown_prior_process()
    test_unavailable_backend_does_not_strand_lease()
    test_worker_cannot_read_other_task_inbox()
    test_crashed_runner_recovered_only_after_process_and_pane_absent()
    test_reconcile_adopts_exact_owned_endpoint_after_publication_gap()
    test_proved_absent_unpublished_start_returns_to_prepared()
