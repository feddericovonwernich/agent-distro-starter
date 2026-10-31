import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))





def test_tmux_central_refuses_changed_server_or_token(tmp_path, monkeypatch):
    from agd_lib import store, tmux

    home = tmp_path / 'home'
    home.mkdir()
    receipt = {'launch_id': 'launch-id', 'endpoint_token': 'token',
               'expected_process_argv': ['/opt/omp'],
               'endpoint': {'server_pid': 'old', 'window': '@9', 'pane': '%10', 'tty': '/dev/pts/10',
                            'token': 'token', 'launch_id': 'launch-id'}}

    def fake_run(argv, check=True, timeout=None):
        if argv[-2:] == ['-p', '#{pid}']:
            return SimpleNamespace(returncode=0, stdout='new\n')
        return SimpleNamespace(returncode=0, stdout='')

    monkeypatch.setattr(tmux.store, 'run', fake_run)
    with __import__('pytest').raises(store.Refusal, match='server'):
        tmux.inspect_central(home, receipt)


def test_launcher_delegates_tmux_central_operations(tmp_path, monkeypatch):
    from agd_lib import launcher, tmux

    home = tmp_path / 'home'
    home.mkdir()
    launcher.initialize(home, 'tmux')
    calls = []
    monkeypatch.setattr(tmux, 'create_central', lambda *args: calls.append(args) or {'pane': '%10'})
    monkeypatch.setattr(tmux, 'attach_argv', lambda actual_home, receipt: ['tmux-attach', receipt['endpoint']['pane']])

    assert launcher._backend_create('tmux', home, 'launch-id', 'token', ['agd'], {'X': 'y'}, ['omp']) == {'pane': '%10'}
    assert launcher._backend_attach(home, {'backend': 'tmux', 'endpoint': {'pane': '%10'}}) == ['tmux-attach', '%10']
    assert calls == [(home, 'launch-id', 'token', ['agd'], {'X': 'y'}, ['omp'])]


def test_tmux_attach_switches_existing_client_to_recorded_session(tmp_path, monkeypatch):
    from agd_lib import tmux

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('TMUX', '/tmp/tmux-1/default,1,0')
    receipt = {'launch_id': 'launch-id', 'endpoint_token': 'token',
               'endpoint': {'server_pid': '501', 'window': '@9', 'pane': '%10', 'tty': '/dev/pts/10',
                            'token': 'token', 'launch_id': 'launch-id'}}
    calls = []
    monkeypatch.setattr(tmux.store, 'run',
                        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout=''))

    assert tmux.attach_argv(home, receipt) == [*tmux.prefix(home), 'switch-client', '-t', 'agd']
    assert calls == [
        [*tmux.prefix(home), 'select-window', '-t', '@9'],
        [*tmux.prefix(home), 'select-pane', '-t', '%10'],
    ]


def test_prepare_start_passes_explicit_backend_to_creator(tmp_path, monkeypatch):
    import os
    from agd_lib import launcher, observability, store, supervision

    home = tmp_path / 'home'
    home.mkdir()
    launcher.initialize(home, 'tmux')
    expected = ['/opt/omp', '--cwd', str(home)]
    monkeypatch.setattr(observability, 'central_argv', lambda *_: (expected, {}, None))
    selected = []

    def create(backend, actual_home, launch_id, endpoint_token, *_):
        selected.append(backend)
        start = supervision.process_start(os.getpid())
        store.atomic_json(actual_home / 'state' / 'owner.json', {
            'owner_pid': os.getpid(), 'owner_start': start, 'token': endpoint_token,
            'watcher_pid': os.getpid(), 'watcher_start': start, 'heartbeat': __import__('time').time(),
        })
        return {'backend': backend}

    def inspect(_, receipt):
        start = supervision.process_start(os.getpid())
        return {'endpoint': receipt['endpoint'], 'foreground': {
            'launch_id': receipt['launch_id'], 'endpoint_token': receipt['endpoint_token'],
            'pid': os.getpid(), 'start': start, 'argv': expected}}

    target = launcher.prepare_start(home, 'herdr', {}, create, inspect, lambda *_: ['attach'])
    assert selected == ['herdr']
    assert target.attach_argv == ['attach']





def test_tmux_inspection_rejects_foreign_window_or_tty(tmp_path, monkeypatch):
    from agd_lib import store, tmux

    home = tmp_path / 'home'
    home.mkdir()
    receipt = {'launch_id': 'launch-id', 'endpoint_token': 'token', 'expected_process_argv': ['omp'],
               'endpoint': {'server_pid': '501', 'window': '@9', 'pane': '%10', 'tty': '/dev/pts/9',
                            'token': 'token', 'launch_id': 'launch-id'}}

    def fake_run(argv, check=True, timeout=None):
        if argv[-2:] == ['-p', '#{pid}']:
            return SimpleNamespace(returncode=0, stdout='501\n')
        if 'show-options' in argv:
            return SimpleNamespace(returncode=0, stdout='token\n')
        if 'list-panes' in argv:
            return SimpleNamespace(returncode=0, stdout='@other %10 /dev/pts/other 4321 0\n')
        return SimpleNamespace(returncode=0, stdout='')

    monkeypatch.setattr(tmux.store, 'run', fake_run)
    with __import__('pytest').raises(store.Refusal, match='membership'):
        tmux.inspect_central(home, receipt)







def test_initial_owner_waits_for_transient_herdr_foreground_inspection(monkeypatch):
    from agd_lib import launcher, store

    owner = {'owner_pid': 1}
    outcomes = iter([store.Refusal('Herdr foreground process is unverified'), owner])

    def prove(*_):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(launcher, '_prove', prove)
    assert launcher._await_initial_owner(Path('/'), {}, object()) == owner


def test_herdr_available_accepts_current_status(monkeypatch):
    import stat
    from agd_lib import herdr

    class Socket:
        st_mode, st_dev, st_ino = stat.S_IFSOCK, 9, 10


    class SocketPath:
        def __init__(self, _):
            pass

        def is_absolute(self):
            return True

        def stat(self):
            return Socket()
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('AGD_HERDR_SESSION', 'agd-test')
    monkeypatch.setattr(herdr.store, 'run', lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=('status: running\nversion: 0.9.5\nendpoint_compatible: yes\n'
                              'private_protocol: 22\nprivate_protocol_compatible: yes\nsocket: /tmp/herdr.sock\n')))
    monkeypatch.setattr(herdr, 'Path', SocketPath)
    assert herdr.available()['protocol'] == '22'

def test_current_herdr_status_requires_version(monkeypatch):
    import stat
    from agd_lib import herdr, launcher, store

    class Socket:
        st_mode, st_dev, st_ino = stat.S_IFSOCK, 9, 10

    class SocketPath:
        def __init__(self, _):
            pass

        def is_absolute(self):
            return True

        def stat(self):
            return Socket()

    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('AGD_HERDR_SESSION', 'agd-test')
    monkeypatch.setattr(herdr.store, 'run', lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=('status: running\nendpoint_compatible: yes\nprivate_protocol: 22\n'
                              'private_protocol_compatible: yes\nsocket: /tmp/herdr.sock\n')))
    monkeypatch.setattr(herdr, 'Path', SocketPath)
    monkeypatch.setattr(launcher, 'Path', SocketPath)
    with __import__('pytest').raises(store.DependencyError, match='incompatible'):
        herdr.available()
    with __import__('pytest').raises(store.Refusal, match='not running'):
        launcher._herdr_status('agd-test')

def test_not_running_herdr_status_refuses_cleanly(monkeypatch):
    import stat
    from agd_lib import herdr, launcher, store

    class Socket:
        st_mode, st_dev, st_ino = stat.S_IFSOCK, 9, 10

    class SocketPath:
        def __init__(self, _):
            pass

        def is_absolute(self):
            return True

        def stat(self):
            return Socket()

    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('AGD_HERDR_SESSION', 'agd-test')
    monkeypatch.setattr(herdr.store, 'run', lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout='status: not running\nsocket: /tmp/herdr.sock\n'))
    monkeypatch.setattr(herdr, 'Path', SocketPath)
    monkeypatch.setattr(launcher, 'Path', SocketPath)
    with __import__('pytest').raises(store.DependencyError, match='session is not running'):
        herdr.available()
    with __import__('pytest').raises(store.Refusal, match='session is not running'):
        launcher._herdr_status('agd-test')




def test_herdr_attach_refuses_replaced_socket_without_herdr_commands(tmp_path, monkeypatch):
    import stat
    from agd_lib import launcher, store

    endpoint = {'session': 'agd-session', 'socket': '/tmp/herdr.sock', 'device': 9, 'inode': 10,
                'protocol': '20', 'workspace': 'workspace-1', 'tab': 'tab-1', 'pane': 'pane-1',
                'terminal': 'terminal-1', 'token': 'token', 'launch_id': 'launch-id'}
    receipt = {'backend': 'herdr', 'endpoint': endpoint}
    calls = []

    class Replaced:
        st_mode, st_dev, st_ino = stat.S_IFSOCK, 9, 11

    monkeypatch.setattr(launcher, '_socket_identity', lambda path: Replaced())
    monkeypatch.setattr(launcher.store, 'run', lambda argv, **kwargs: calls.append(argv))

    with __import__('pytest').raises(store.Refusal, match='replaced'):
        launcher._backend_attach(tmp_path, receipt)
    assert calls == []


def test_herdr_refuses_foreign_post_start_socket_before_workspace(tmp_path, monkeypatch):
    import stat
    import subprocess
    import pytest
    from agd_lib import launcher, store

    home = tmp_path / 'home'
    home.mkdir()
    launcher.initialize(home, 'herdr')
    calls = []
    class Socket:
        st_mode, st_dev, st_ino = stat.S_IFSOCK, 9, 10
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ['herdr', 'session', 'list', '--json']:
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')
        if argv[-2:] == ['status', 'server']:
            return SimpleNamespace(returncode=0, stdout='status: running\ncompatible: yes\nprotocol: 20\nsocket: /tmp/foreign.sock\n', stderr='')
        raise AssertionError(argv)
    monkeypatch.setattr(launcher.store, 'run', fake_run)
    monkeypatch.setattr(launcher, '_socket_identity', lambda _: Socket())
    monkeypatch.setattr(launcher, 'subprocess', SimpleNamespace(
        DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
        Popen=lambda *args, **kwargs: SimpleNamespace(pid=123)))
    monkeypatch.setattr(launcher, '_prove_herdr_server',
                        lambda *_: (_ for _ in ()).throw(store.Refusal('Herdr server ownership is unproven')),
                        raising=False)
    with pytest.raises(store.Refusal, match='ownership'):
        launcher._herdr_create(home, 'launch', 'token', ['/opt/agd'], {'PATH': '/bin'}, ['/opt/omp'])
    assert not any(argv[3:5] == ['workspace', 'create'] for argv in calls)


def test_uninstall_refuses_replaced_backend_or_foreign_pane(tmp_path, monkeypatch):
    import hashlib
    import json
    import os
    import socket
    import uuid
    import pytest
    from agd_lib import herdr, store, supervision, uninstall_state
    from support import command, uninstall_scope
    home = tmp_path / 'home'
    command(home, 'init', '--backend', 'herdr')
    socket_path = tmp_path / 'control.sock'
    original = socket.socket(socket.AF_UNIX)
    replacement = socket.socket(socket.AF_UNIX)
    original.bind(str(socket_path))
    mode = socket_path.stat()
    marker = tmp_path / 'unrelated-closed'
    binary = tmp_path / 'herdr'
    binary.write_text(
        '#!/usr/bin/env python3\nimport json, pathlib, sys\n'
        'if "close" in sys.argv:\n'
        f' pathlib.Path({str(marker)!r}).write_text("closed")\n'
        'print(json.dumps({"result":{"pane":{"pane_id":"foreign-pane",'
        '"tab_id":"tab","workspace_id":"workspace","terminal_id":"terminal"}}}))\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    incarnation = str(uuid.uuid4())
    endpoint = {'session': 'fixture-owned', 'socket': str(socket_path),
                'device': mode.st_dev, 'inode': mode.st_ino, 'protocol': '1',
                'home': str(home), 'task': 'worker', 'incarnation': incarnation,
                'token': str(uuid.uuid4()), 'workspace': 'workspace', 'tab': 'tab',
                'pane': 'owned-pane', 'terminal': 'terminal',
                'target': supervision.runtime_home_identity(home), 'command': ['agd']}
    store.atomic_json(home / 'state/tasks/worker/endpoint.json', endpoint)
    safe = {name: value for name, value in endpoint.items() if name not in {'token', 'command'}}
    safe['token_sha256'] = hashlib.sha256(endpoint['token'].encode()).hexdigest()
    resource = {'key': f'worker-endpoint:worker:{incarnation}', 'kind': 'worker-endpoint',
                'identity': {'backend': 'herdr', 'task_id': 'worker', 'incarnation': incarnation,
                             'endpoint': safe, 'endpoint_digest': uninstall_state.fingerprint(endpoint)},
                'presence': 'present', 'ownership': 'owned', 'blockers': []}
    try:
        with uninstall_scope(home, [resource]) as operation:
            with pytest.raises(store.Refusal):
                herdr.uninstall_close(home, resource, operation)
            assert operation['completed'] == [] and not marker.exists()
            socket_path.unlink()
            replacement.bind(str(socket_path))
            with pytest.raises(store.Refusal):
                herdr.uninstall_close(home, resource, operation)
            assert operation['completed'] == [] and not marker.exists()
            replacement.listen()
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(socket_path))
                peer, _ = replacement.accept()
                with peer:
                    client.sendall(b'unrelated')
                    assert peer.recv(32) == b'unrelated'
    finally:
        original.close()
        replacement.close()
