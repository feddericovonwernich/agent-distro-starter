import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))


def test_tmux_central_records_identity_and_attaches_selected_pane(tmp_path, monkeypatch):
    from agd_lib import tmux

    home = tmp_path / 'home'
    home.mkdir()
    calls = []

    def fake_run(argv, check=True, timeout=None):
        calls.append((argv, check, timeout))
        if argv[-3:] == ['has-session', '-t', 'agd']:
            return SimpleNamespace(returncode=1, stdout='')
        if argv[-2:] == ['-p', '#{pid}']:
            return SimpleNamespace(returncode=0, stdout='501\n')
        if 'new-window' in argv:
            return SimpleNamespace(returncode=0, stdout='@9 %10\n')
        if 'show-options' in argv:
            return SimpleNamespace(returncode=0, stdout='token\n')
        if 'list-panes' in argv:
            return SimpleNamespace(returncode=0, stdout='4321 0\n')
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(tmux.store, 'run', fake_run)

    monkeypatch.setattr(tmux, '_foreground', lambda pid: ('start-4321', receipt['expected_process_argv']))
    receipt = {'endpoint_token': 'token', 'launch_id': 'launch-id',
               'expected_process_argv': ['/opt/omp', '--cwd', str(home)]}
    endpoint = tmux.create_central(home, 'launch-id', 'token',
                                   ['/opt/agd', '--home', str(home), 'observability', 'central'],
                                   {'AGD_LAUNCH_ID': 'launch-id', 'AGD_ENDPOINT_TOKEN': 'token'},
                                   receipt['expected_process_argv'])

    assert endpoint == {'server_pid': '501', 'window': '@9', 'pane': '%10',
                        'token': 'token', 'launch_id': 'launch-id'}
    assert tmux.inspect_central(home, {**receipt, 'endpoint': endpoint}) == {
        'endpoint': endpoint,
        'foreground': {'launch_id': 'launch-id', 'endpoint_token': 'token',
                       'pid': 4321, 'start': 'start-4321',
                       'argv': receipt['expected_process_argv']},
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
               'endpoint': {'server_pid': 'old', 'window': '@9', 'pane': '%10',
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

    assert launcher._backend_create(home, 'launch-id', 'token', ['agd'], {'X': 'y'}, ['omp']) == {'pane': '%10'}
    assert launcher._backend_attach(home, {'backend': 'tmux', 'endpoint': {'pane': '%10'}}) == ['tmux-attach', '%10']
    assert calls == [(home, 'launch-id', 'token', ['agd'], {'X': 'y'}, ['omp'])]
