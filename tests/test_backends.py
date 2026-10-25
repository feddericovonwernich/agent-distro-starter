import importlib
import json
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from support import command, lab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'bin'))
from agd_lib.store import Refusal


@contextmanager
def owned_home(name, home):
    try:
        yield home
    finally:
        path = home/'state'/'herdr-workspace.json'
        if name == 'herdr' and path.is_file():
            from agd_lib import herdr
            workspace = json.loads(path.read_text())
            herdr.call(workspace, 'workspace', 'close', workspace['workspace'])


def check_backend(name):
    assert name in ('tmux', 'herdr')
    backend = importlib.import_module('agd_lib.' + name)
    with tempfile.TemporaryDirectory(prefix='agd-pane-') as tmp, owned_home(name, Path(tmp)/'home with space') as home:
        command(home, 'init')
        endpoint = backend.create(home, 'probe', 'incarnation-one', home,
                                  ['/bin/sh', '-c', 'printf \"agd-%s\\n\" live-check; treehouse --version; sleep 30'])
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if 'agd-live-check' in backend.capture(endpoint):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('probe output never appeared')
            assert backend.inspect(endpoint)['presence'] == 'present'
            if name == 'herdr':
                assert 'v3.1.6' in backend.capture(endpoint)
            foreign = dict(endpoint, token='not-the-owner')
            try:
                backend.close(foreign)
            except Refusal:
                pass
            else:
                raise AssertionError('foreign close was accepted')
            assert backend.inspect(endpoint)['presence'] == 'present'
        finally:
            backend.close(endpoint)
        if name in ('tmux', 'herdr'):
            endpoint = backend.create(home, 'input', 'incarnation-two', home,
                                      ['/bin/sh', '-c', 'stty -echo; exec cat'])
            try:
                sentinel = home/'must-not-exist'
                payload = f'literal; touch {sentinel}'
                backend.send_text(endpoint, payload)
                backend.send_key(endpoint, 'enter')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if payload in backend.capture(endpoint):
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError('literal input not delivered')
                assert not sentinel.exists()
            finally:
                backend.close(endpoint)


def check_herdr_recovery():
    from agd_lib import herdr, tasks
    with lab() as (home, project), owned_home('herdr', home):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'recover', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='attempt-one', harness='omp',
                    backend='herdr', phase='running', worker_alive=None)
        tasks.save(home, task)
        endpoint = herdr.create(home, 'recover', 'attempt-one', Path(task['worktree']),
                                ['/bin/sh', '-c', 'printf worker-started; sleep 30'])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                command(home, 'session', 'reconcile')
                if tasks.read(home, 'recover').get('endpoint') == endpoint:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('running Herdr endpoint was not adopted')
        finally:
            herdr.close(endpoint)


def check_herdr_preflight():
    from agd_lib import herdr, store, tasks
    with lab() as (home, project):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'stale', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        store.atomic_json(home/'state'/'herdr-workspace.json',
                          {**herdr.available(), 'workspace': 'not-a-workspace'})
        command(home, 'worker', 'launch', 'stale', '--harness', 'omp',
                '--backend', 'herdr', expected=3)
        assert tasks.read(home, 'stale')['phase'] == 'prepared'


def check_herdr_blank_pane_is_not_worker():
    import subprocess
    from unittest.mock import patch
    from agd_lib import herdr, tasks
    with lab() as (home, project), owned_home('herdr', home):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'blank', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='attempt-one', harness='omp',
                    backend='herdr', phase='running', worker_alive=None)
        tasks.save(home, task)
        actual_call = herdr.call
        def before_run(endpoint, *args, **options):
            if args[:2] == ('pane', 'run'):
                return subprocess.CompletedProcess([], 0, '', '')
            return actual_call(endpoint, *args, **options)
        with patch.object(herdr, 'call', side_effect=before_run):
            endpoint = herdr.create(home, 'blank', 'attempt-one', Path(task['worktree']),
                                    ['/bin/sh', '-c', 'sleep 30'])
        try:
            assert herdr.inspect(endpoint)['presence'] == 'present'
            command(home, 'session', 'reconcile')
            state = tasks.read(home, 'blank')
            assert 'endpoint' not in state
            assert state['lease_id'] == task['lease_id']
            assert any(event['task'] == 'blank' and event['kind'] == 'blocked'
                       for event in command(home, 'events', 'list'))
        finally:
            herdr.close(endpoint)


def check_herdr_missing_socket_is_unavailable():
    import subprocess
    from unittest.mock import patch
    from agd_lib import herdr, store
    with tempfile.TemporaryDirectory(prefix='agd-missing-socket-') as tmp:
        status = (f'status: running\nprotocol: 20\ncompatible: yes\n'
                  f'socket: {tmp}/gone.sock\n')
        with patch.object(herdr.store, 'run',
                          return_value=subprocess.CompletedProcess([], 0, status, '')):
            try:
                herdr.available()
            except store.DependencyError:
                pass
            else:
                raise AssertionError('vanished selected socket was accepted')


def check_herdr_timeout_preserves_unknown_state():
    import subprocess
    from unittest.mock import patch
    from agd_lib import herdr, store
    endpoint = herdr.available()
    with patch.object(herdr.store, 'run',
                      side_effect=subprocess.TimeoutExpired(['herdr'], 10)):
        try:
            herdr.call(endpoint, 'pane', 'get', 'not-a-pane')
        except store.Refusal:
            pass
        else:
            raise AssertionError('timed-out pane query was accepted')
        try:
            herdr.available()
        except store.DependencyError:
            pass
        else:
            raise AssertionError('timed-out server query was accepted')


if __name__ == '__main__':
    check_backend(sys.argv[1])
    if sys.argv[1] == 'herdr':
        check_herdr_recovery()
        check_herdr_preflight()
        check_herdr_blank_pane_is_not_worker()
        check_herdr_missing_socket_is_unavailable()
        check_herdr_timeout_preserves_unknown_state()
