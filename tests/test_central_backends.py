import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))


def test_tmux_central_records_identity_and_attaches_selected_pane(tmp_path, monkeypatch):
    from agd_lib import tmux

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.delenv('TMUX', raising=False)
    calls = []

    def fake_run(argv, check=True, timeout=None):
        calls.append((argv, check, timeout))
        if argv[-3:] == ['has-session', '-t', 'agd']:
            return SimpleNamespace(returncode=1, stdout='')
        if argv[-2:] == ['-p', '#{pid}']:
            return SimpleNamespace(returncode=0, stdout='501\n')
        if 'new-window' in argv:
            return SimpleNamespace(returncode=0, stdout='@9 %10 /dev/pts/10\n')
        if 'show-options' in argv:
            return SimpleNamespace(returncode=0, stdout='token\n')
        if 'list-panes' in argv:
            return SimpleNamespace(returncode=0, stdout='@9 %10 /dev/pts/10 4321 0\n')
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(tmux.store, 'run', fake_run)

    monkeypatch.setattr(tmux, '_foreground', lambda pid: ('start-4321', receipt['expected_process_argv'], '/dev/pts/10'))
    receipt = {'endpoint_token': 'token', 'launch_id': 'launch-id',
               'expected_process_argv': ['/opt/omp', '--cwd', str(home)]}
    endpoint = tmux.create_central(home, 'launch-id', 'token',
                                   ['/opt/agd', '--home', str(home), 'observability', 'central'],
                                   {'AGD_LAUNCH_ID': 'launch-id', 'AGD_ENDPOINT_TOKEN': 'token'},
                                   receipt['expected_process_argv'])

    assert endpoint == {'server_pid': '501', 'window': '@9', 'pane': '%10', 'tty': '/dev/pts/10',
                        'token': 'token', 'launch_id': 'launch-id'}
    assert tmux.inspect_central(home, {**receipt, 'endpoint': endpoint}) == {
        'endpoint': endpoint,
        'foreground': {'launch_id': 'launch-id', 'endpoint_token': 'token',
                       'pid': 4321, 'start': 'start-4321',
                       'argv': receipt['expected_process_argv'], 'tty': '/dev/pts/10'},
    }
    assert tmux.attach_argv(home, {**receipt, 'endpoint': endpoint}) == [*tmux.prefix(home), 'attach-session', '-t', 'agd']
    assert [*tmux.prefix(home), 'select-window', '-t', '@9'] in [argv for argv, _, _ in calls]
    assert [*tmux.prefix(home), 'select-pane', '-t', '%10'] in [argv for argv, _, _ in calls]
    new_window = next(argv for argv, _, _ in calls if 'new-window' in argv)
    assert new_window[:len(tmux.prefix(home))] == tmux.prefix(home)
    assert new_window[new_window.index('-n') + 1] == 'central'
    assert 'AGD_LAUNCH_ID=launch-id' in new_window[-1]
    assert 'AGD_ENDPOINT_TOKEN=token' in new_window[-1]
    assert '/opt/agd' in new_window[-1]


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


def test_tmux_create_retains_endpoint_before_token_write_failure(tmp_path, monkeypatch):
    from agd_lib import store, tmux

    home = tmp_path / 'home'
    home.mkdir()

    def fake_run(argv, check=True, timeout=None):
        if 'has-session' in argv:
            return SimpleNamespace(returncode=0, stdout='')
        if argv[-2:] == ['-p', '#{pid}']:
            return SimpleNamespace(returncode=0, stdout='501\n')
        if 'new-window' in argv:
            return SimpleNamespace(returncode=0, stdout='@9 %10 /dev/pts/9\n')
        if 'set-option' in argv:
            raise store.Refusal('token write failed')
        return SimpleNamespace(returncode=0, stdout='')

    monkeypatch.setattr(tmux.store, 'run', fake_run)
    endpoint = tmux.create_central(home, 'launch-id', 'token', ['agd'], {}, ['omp'])
    with __import__('pytest').raises(store.Refusal, match='token is unconfirmed'):
        tmux.finalize_central(home, {'endpoint_token': 'token', 'launch_id': 'launch-id', 'endpoint': endpoint})
    assert __import__('json').loads((home / 'state' / 'central-tmux-pending.json').read_text()) == {
        'server_pid': '501', 'window': '@9', 'pane': '%10', 'tty': '/dev/pts/9',
        'token': 'token', 'launch_id': 'launch-id'}


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


def test_tmux_endpoint_journal_survives_spawned_receipt_transition_failure(tmp_path, monkeypatch):
    from agd_lib import launcher, observability, store, tmux

    home = tmp_path / 'home'
    home.mkdir()
    launcher.initialize(home, 'tmux')
    monkeypatch.setattr(observability, 'central_argv', lambda *_: (['omp'], {}, None))
    monkeypatch.setattr(tmux.store, 'run', lambda argv, **kwargs: (
        SimpleNamespace(returncode=0, stdout='') if 'has-session' in argv else
        SimpleNamespace(returncode=0, stdout='501\n') if argv[-2:] == ['-p', '#{pid}'] else
        SimpleNamespace(returncode=0, stdout='@9 %10 /dev/pts/9\n') if 'new-window' in argv else
        SimpleNamespace(returncode=0, stdout='')))
    monkeypatch.setattr(launcher, '_advance', lambda *_: (_ for _ in ()).throw(store.Refusal('receipt changed')))

    with __import__('pytest').raises(store.Refusal, match='receipt changed'):
        launcher.prepare_start(home, None, {}, launcher._backend_create, launcher._backend_inspect, launcher._backend_attach)
    assert __import__('json').loads((home / 'state' / 'central-tmux-pending.json').read_text())['pane'] == '%10'


def test_herdr_central_uses_named_receipted_session_and_restores_focus(tmp_path, monkeypatch):
    import json
    import stat
    from agd_lib import launcher, store

    home = tmp_path / 'home'
    home.mkdir()
    launcher.initialize(home, 'herdr')
    session = json.loads((home / 'data' / 'launcher.json').read_text())['herdr_session']
    calls, focus = [], []

    def fake_run(argv, check=True, timeout=None):
        calls.append(argv)
        if argv == ['herdr', 'session', 'list', '--json']:
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')
        if argv[-2:] == ['status', 'server']:
            return SimpleNamespace(returncode=0, stdout='status: running\ncompatible: yes\nprotocol: 20\nsocket: /tmp/herdr.sock\n', stderr='')
        command = tuple(argv[3:])
        if command == ('workspace', 'focus', 'workspace-1'):
            focus.append(('workspace', 'workspace-1'))
            result = {}
        elif command == ('tab', 'focus', 'tab-1'):
            focus.append(('tab', 'tab-1'))
            result = {}
        elif command == ('workspace', 'create', '--cwd', str(home), '--label', 'agd-central', '--no-focus'):
            result = {'workspace': {'workspace_id': 'workspace-1'}}
        elif command == ('workspace', 'get', 'workspace-1'):
            result = {'workspace': {'workspace_id': 'workspace-1'}}
        elif command == ('tab', 'create', '--workspace', 'workspace-1', '--cwd', str(home), '--label', 'central', '--no-focus'):
            result = {'tab': {'tab_id': 'tab-1'}, 'root_pane': {'pane_id': 'pane-1', 'workspace_id': 'workspace-1',
                                                                 'tab_id': 'tab-1', 'terminal_id': 'terminal-1'}}
        elif command == ('pane', 'get', 'pane-1'):
            result = {'pane': {'pane_id': 'pane-1', 'workspace_id': 'workspace-1', 'tab_id': 'tab-1',
                               'terminal_id': 'terminal-1'}}
        elif command == ('pane', 'process-info', '--pane', 'pane-1'):
            result = {'process_info': {'pane_id': 'pane-1', 'pid': 42, 'start': 'start-42', 'argv': ['/opt/omp']}}
        elif command[:3] == ('pane', 'run', 'pane-1'):
            result = {}
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'result': result}), stderr='')

    class Socket:
        st_mode, st_dev, st_ino = stat.S_IFSOCK, 9, 10

    monkeypatch.setattr(launcher.store, 'run', fake_run)
    monkeypatch.setattr(launcher, '_socket_identity', lambda path: Socket(), raising=False)
    import subprocess
    monkeypatch.setattr(launcher, 'subprocess', SimpleNamespace(DEVNULL=subprocess.DEVNULL,
                        Popen=lambda argv, **kwargs: calls.append((argv, kwargs))), raising=False)

    endpoint = launcher._backend_create('herdr', home, 'launch-id', 'token', ['/opt/agd'],
                                        {'PATH': '/bin'}, ['/opt/omp'])
    receipt = {'backend': 'herdr', 'launch_id': 'launch-id', 'endpoint_token': 'token',
               'expected_process_argv': ['/opt/omp'], 'endpoint': endpoint}
    assert endpoint['session'] == session
    assert endpoint['socket'] == '/tmp/herdr.sock'
    assert (['herdr', '--session', session, 'server'], {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL, 'start_new_session': True}) in calls
    assert calls[0] == ['herdr', 'session', 'list', '--json']
    assert launcher._backend_attach(home, receipt) == ['herdr', 'session', 'attach', session]
    assert focus == [('workspace', 'workspace-1'), ('tab', 'tab-1')]

    env = launcher._attach_env(home, receipt, {'HERDR_ENV': '1', 'PATH': '/bin'})
    assert env['AGD_HERDR_SESSION'] == session
    assert 'HERDR_ENV' not in env
    monkeypatch.setattr(launcher.store, 'run', lambda argv, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps([{'name': session}]), stderr=''))
    with __import__('pytest').raises(store.Refusal, match='unreceipted'):
        launcher._backend_create('herdr', home, 'other', 'token', ['/opt/agd'], {'PATH': '/bin'}, ['/opt/omp'])
