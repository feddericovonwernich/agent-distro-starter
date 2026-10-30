import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from support import lab, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
from agd_lib import tasks, workers


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


def test_omp_launch_persists_fresh_native_binding_before_endpoint_creation():
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'native', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        class Endpoint:
            def create(self, _home, task_id, incarnation, _work, _argv):
                saved = tasks.read(home, task_id)
                assert saved['observability']['incarnation'] == incarnation
                assert Path(saved['observability']['session_dir']).is_dir()
                return {'task': task_id, 'incarnation': incarnation}
        with patch.object(workers.shutil, 'which', return_value='/bin/true'), \
             patch.object(workers, 'backend', return_value=Endpoint()):
            launched = workers.launch(home, task['id'])
        saved = tasks.read(home, task['id'])
        assert uuid.UUID(saved['observability']['incarnation'])
        assert saved['incarnation'] == launched['incarnation']



def _proven_central(home, monkeypatch, backend='herdr'):
    import hashlib
    import time
    from agd_lib import launcher, store, supervision

    token = 'worker-inheritance-token'
    start = supervision.process_start(os.getpid())
    receipt = {
        'schema': 1, 'launch_id': str(uuid.uuid4()), 'endpoint_token': token,
        'backend': backend, 'phase': 'ready', 'endpoint': {'id': 'central'},
        'managed_command': ['agd', 'observability', 'central'], 'expected_process_argv': ['/opt/omp'],
        'owner': {'pid': os.getpid(), 'start': start},
        'watcher': {'pid': os.getpid(), 'start': start},
    }
    store.atomic_json(home/'state'/'owner.json', {
        'owner_pid': os.getpid(), 'owner_start': start, 'token': token,
        'watcher_pid': os.getpid(), 'watcher_start': start, 'heartbeat': time.time(),
    })
    store.atomic_json(home/'state'/'central-launcher.json', receipt)

    def inspect(_home, current):
        return {'endpoint': current['endpoint'], 'foreground': {
            'launch_id': current['launch_id'], 'endpoint_token': current['endpoint_token'],
            'pid': os.getpid(), 'start': start, 'argv': current['expected_process_argv']}}

    monkeypatch.setattr(launcher, '_backend_inspect', inspect)
    return receipt, {
        'AGD_HOME': str(home), 'AGD_LAUNCHER_BACKEND': backend,
        'AGD_LAUNCH_ID': receipt['launch_id'],
        'AGD_ENDPOINT_TOKEN_HASH': hashlib.sha256(token.encode()).hexdigest(),
    }


def _prepared_task(home, task_id):
    brief = home / f'{task_id}.md'
    brief.write_text('Inspect value.txt.\n')
    task = command(home, 'task', 'create', task_id, '--project', 'sample',
                   '--brief', str(brief), '--kind', 'scout')
    task['endpoint'] = {'preserved': task_id}
    tasks.save(home, task)
    return tasks.read(home, task_id)


def test_omitted_backend_inherits_proven_central_and_explicit_override_stays_local(monkeypatch):
    from agd_lib import launcher
    with lab() as (home, _):
        inherited = _prepared_task(home, 'inherited')
        explicit = _prepared_task(home, 'explicit')
        receipt, environment = _proven_central(home, monkeypatch)

        class Endpoint:
            def available(self): pass
            def prepare(self, _home): pass
            def create(self, _home, task_id, incarnation, _work, _argv):
                return {'task': task_id, 'incarnation': incarnation}

        for key in environment:
            monkeypatch.setenv(key, environment[key])
        monkeypatch.setattr(workers, 'backend', lambda _task: Endpoint())
        monkeypatch.setattr(workers.shutil, 'which', lambda _name: '/bin/true')
        assert workers.launch(home, 'inherited')['backend'] == 'herdr'
        assert tasks.read(home, 'inherited')['lease_id'] == inherited['lease_id']
        assert (home/'state'/'central-launcher.json').read_bytes() == launcher._bytes(receipt)

        monkeypatch.setenv('AGD_LAUNCH_ID', 'mismatched-but-explicit-override')
        assert workers.launch(home, 'explicit', selected_backend='tmux')['backend'] == 'tmux'
        assert tasks.read(home, 'explicit')['lease_id'] == explicit['lease_id']


def test_omitted_backend_refusals_preserve_worker_and_central_records(monkeypatch):
    import pytest
    from agd_lib import launcher, store

    def refuse(task_id, change, message):
        with lab() as (home, _):
            task = _prepared_task(home, task_id)
            receipt, environment = _proven_central(home, monkeypatch)
            for key, value in environment.items():
                monkeypatch.setenv(key, value)
            change(home, receipt)
            before_receipt = (home/'state'/'central-launcher.json').read_bytes()
            before_config = (home/'data'/'launcher.json').read_bytes()
            with pytest.raises(store.Refusal, match=message):
                workers.launch(home, task_id)
            assert tasks.read(home, task_id) == task
            assert (home/'state'/'central-launcher.json').read_bytes() == before_receipt
            assert (home/'data'/'launcher.json').read_bytes() == before_config

    def mismatched(home, _receipt):
        (home/'data'/'launcher.json').write_text(
            '{"schema": 1, "backend": "tmux", "herdr_session": "agd-00000000000000000000000000000000"}\n')
        monkeypatch.setenv('AGD_LAUNCH_ID', 'different')
    refuse('mismatched', mismatched, 'inherited launcher environment differs')
    refuse('malformed', lambda home, _receipt: (home/'state'/'central-launcher.json').write_bytes(b'{'),
           'launcher receipt is malformed')
    refuse('interrupted', lambda home, receipt: store.atomic_json(
        home/'state'/'central-launcher.json', {**receipt, 'phase': 'spawned'}),
           'central launch was interrupted')
    refuse('stale-owner', lambda home, _receipt: store.atomic_json(home/'state'/'owner.json', {
        'owner_pid': os.getpid(), 'owner_start': 'stale', 'token': 'worker-inheritance-token',
        'watcher_pid': os.getpid(), 'watcher_start': 'stale', 'heartbeat': 0}),
           'central owner identity changed')

    def replaced(_home, _receipt):
        monkeypatch.setattr(launcher, '_backend_inspect',
                            lambda _home, current: {'endpoint': {'id': 'replaced'}, 'foreground': {
                                'launch_id': current['launch_id'], 'endpoint_token': current['endpoint_token'],
                                'pid': os.getpid(), 'start': current['owner']['start'],
                                'argv': current['expected_process_argv']}})
    refuse('replaced-endpoint', replaced, 'central endpoint continuity is unproven')


def test_omitted_backend_falls_back_to_saved_backend(monkeypatch):
    with lab() as (home, _):
        task = _prepared_task(home, 'fallback')
        (home/'data'/'launcher.json').write_text(
            '{"schema": 1, "backend": "herdr", "herdr_session": "agd-00000000000000000000000000000000"}\n')

        class Endpoint:
            def available(self): pass
            def prepare(self, _home): pass
            def create(self, _home, task_id, incarnation, _work, _argv):
                return {'task': task_id, 'incarnation': incarnation}

        monkeypatch.setattr(workers, 'backend', lambda _task: Endpoint())
        launched = workers.launch(home, 'fallback')
        assert launched['backend'] == 'herdr'
        assert tasks.read(home, 'fallback')['lease_id'] == task['lease_id']


def test_relaunch_keeps_prior_native_data_when_final_executable_is_true(monkeypatch):
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'retained', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        class Endpoint:
            def create(self, _home, _task_id, incarnation, _work, _argv):
                return {'incarnation': incarnation}
            def inspect(self, _endpoint):
                return {'presence': 'missing'}
        monkeypatch.setattr(workers.shutil, 'which', lambda _name: '/bin/true')
        monkeypatch.setattr(workers, 'backend', lambda _task: Endpoint())
        first = workers.launch(home, task['id'])
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', first['incarnation'])
        assert workers.run_worker(home, task['id'], first['incarnation']) == {'exit': 0}
        first_dir = Path(first['observability']['session_dir'])
        (first_dir / 'native.jsonl').write_bytes(b'old transcript\n')
        artifact = first_dir / 'native-artifacts'; artifact.mkdir()
        (artifact / 'blob').write_bytes(b'old artifact')
        current = tasks.read(home, task['id'])
        current['worker_alive'] = False
        tasks.save(home, current)
        second = workers.relaunch(home, task['id'])
        assert second['observability']['session_dir'] != str(first_dir)
        assert (first_dir / 'native.jsonl').read_bytes() == b'old transcript\n'
        assert (artifact / 'blob').read_bytes() == b'old artifact'


def test_two_live_worker_launches_keep_stale_receipts_per_task_incarnation():
    import subprocess
    import time
    from test_projects import CLI
    from agd_lib import observability

    with lab() as (home, _):
        executable = home / 'omp'
        executable.write_text('#!/bin/sh\nsleep 3\n')
        executable.chmod(0o755)
        launched = []
        for task_id in ('first', 'second'):
            brief = home / f'{task_id}.md'
            brief.write_text('Inspect value.txt.\n')
            task = command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief),
                           '--kind', 'scout')
            incarnation = str(uuid.uuid4())
            task.update(executor='worker', incarnation=incarnation, harness='omp', backend='tmux',
                        phase='running', worker_alive=None, unattended=False, endpoint={'incarnation': incarnation},
                        observability=observability.worker_binding(home, task_id, incarnation))
            tasks.save(home, task)
            env = dict(os.environ, PATH=f'{home}:{os.environ["PATH"]}', AGD_TASK_ID=task_id,
                       AGD_INCARNATION=incarnation)
            launched.append(subprocess.Popen(
                [str(CLI), '--home', str(home), 'worker', 'run', task_id, '--incarnation', incarnation],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env))
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if len(observability.status(home)['workers']) == 2:
                    break
                time.sleep(.05)
            observability.record_desired_generation(home, '1', 'enabled')
            receipts = observability.status(home)['workers']
            assert {(row['task_id'], row['incarnation']) for row in receipts} == {
                (task_id, tasks.read(home, task_id)['incarnation']) for task_id in ('first', 'second')}
            assert {row['state'] for row in receipts} == {'stale-restart-required'}
        finally:
            for process in launched:
                assert process.wait(timeout=5) == 0, process.stderr.read()

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


def test_relaunch_restores_frozen_skill_after_catalog_changes():
    from unittest.mock import patch
    from agd_lib import worker_skills, workers
    from test_worker_skills import add_skill
    from test_projects import run
    import shutil
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        add_skill(home, 'agd-probe')
        task = command(
            home, 'task', 'create', 'frozen', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        task.update(executor='worker', incarnation='old', backend='tmux',
                    phase='running', worker_alive=False,
                    endpoint={'old': True, 'incarnation': 'old'},
                    unattended=False)
        tasks.save(home, task)
        shutil.rmtree(home/'config'/'worker-skills'/'agd-probe')
        shutil.rmtree(Path(task['worktree'])/'.omp'/'skills'/'agd-probe')
        class FakeBackend:
            def create(self, *args):
                return {'new': True}
        with patch.object(workers, 'inspect', return_value={'endpoint': {'presence': 'missing'}}), \
             patch.object(workers, 'backend', return_value=FakeBackend()):
            result = workers.relaunch(home, 'frozen')
        assert result['endpoint'] == {'new': True}
        assert (Path(task['worktree'])/'.omp'/'skills'/'agd-probe'/'SKILL.md').is_file()




def test_granted_launch_uses_frozen_private_scoped_skill_and_prompt():
    from unittest.mock import patch
    from agd_lib import workers
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(
            home, 'task', 'create', 'granted', '--project', 'sample',
            '--brief', str(brief), '--kind', 'ship', '--skill-mode', 'none')
        instruction = home/'grant.md'
        instruction.write_text('Task granted may publish origin:agd/granted for task granted.\n')
        grant = tasks.grant_pr_publish(home, 'granted', 'origin:agd/granted', instruction)
        class FakeBackend:
            def create(self, *_args): return {'created': True, 'incarnation': _args[2]}
        with patch.object(workers.harnesses, 'launch_argv', return_value=['true']), \
             patch.object(__import__('agd_lib.pr', fromlist=['github_repository']), 'github_repository',
                          return_value='owner/repo'), \
             patch.object(__import__('agd_lib.relay', fromlist=['start_project_service']), 'start_project_service'), \
             patch.object(workers, 'backend', return_value=FakeBackend()), \
             patch.object(workers.shutil, 'which', return_value='/bin/true'):
            launched = workers.launch(home, 'granted', pr_grant=grant['id'])
        manifest = launched['worker_skills']
        assert manifest['skills'][0]['id'] == 'agd-scoped-pr-relay'
        assert not (home/'config'/'worker-skills'/'agd-scoped-pr-relay').exists()
        assert (Path(manifest['skills'][0]['snapshot'])/'SKILL.md').is_file()
        scoped = dict(launched, worker_skills=manifest, pr_grant=grant['id'])
        scoped['incarnation'] = 'worker-incarnation'
        argv = __import__('agd_lib.harnesses', fromlist=['launch_argv']).launch_argv(
            scoped, tasks.task_file(home, 'granted').parent/'brief.md')
        assert '--no-extensions' in argv
        assert argv[argv.index('--skills') + 1] == 'agd-scoped-pr-relay'
        text = argv[-1]
        assert grant['id'] in text and '--incarnation $AGD_INCARNATION' in text
        assert f'worker verify-pr granted' in text and str(home) in text
        reply_path = tasks.task_file(home, 'granted').parent/'relay-reply.json'
        assert reply_path == home/'data'/'tasks'/'granted'/'relay-reply.json'
        assert '--action start' in text and '--action complete' in text and '--action cancelled' in text
        assert 'start|complete|cancelled' not in text
        assert f'--payload {reply_path}' in text
        assert 'feedback received; repair pending' in text
        current = tasks.read(home, 'granted')
        current['worker_alive'] = False
        tasks.save(home, current)
        with patch.object(workers, 'inspect', return_value={'endpoint': {'presence': 'missing'}}), \
             patch.object(workers, 'backend', return_value=FakeBackend()):
            relaunched = workers.relaunch(home, 'granted')
        assert relaunched['incarnation'] != launched['incarnation']
        assert (Path(manifest['skills'][0]['snapshot'])/'SKILL.md').is_file()


def test_granted_launch_refuses_non_github_remote_before_worker_creation():
    import pytest
    from agd_lib import store, workers
    with lab() as (home, _project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        command(
            home, 'task', 'create', 'nongithub', '--project', 'sample',
            '--brief', str(brief), '--kind', 'ship', '--skill-mode', 'none')
        instruction = home/'grant.md'; instruction.write_text('Task nongithub grant origin:agd/nongithub.\n')
        grant = tasks.grant_pr_publish(home, 'nongithub', 'origin:agd/nongithub', instruction)
        with pytest.raises(store.Refusal, match='GitHub'):
            workers.launch(home, 'nongithub', pr_grant=grant['id'])
        assert tasks.read(home, 'nongithub')['phase'] == 'prepared'
        assert (tasks.task_file(home, 'nongithub').parent/'relay-blocked.json').is_file()
def test_concurrent_skill_launch_has_one_winner_without_orphans():
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch
    from agd_lib import workers
    from test_worker_skills import add_skill
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        add_skill(home, 'agd-probe')
        command(
            home, 'task', 'create', 'race', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        class Fake:
            def create(self, *args): return {'incarnation': args[2]}
        def attempt():
            try:
                return workers.launch(home, 'race')
            except Exception as error:
                return error
        with patch.object(workers.harnesses, 'launch_argv', return_value=['true']), \
             patch.object(workers, 'backend', return_value=Fake()), \
             patch.object(workers.shutil, 'which', return_value='/bin/true'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: attempt(), range(2)))
        assert sum(isinstance(row, dict) for row in results) == 1
        assert sum(isinstance(row, Exception) for row in results) == 1

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
    test_relaunch_restores_frozen_skill_after_catalog_changes()
    test_concurrent_skill_launch_has_one_winner_without_orphans()
