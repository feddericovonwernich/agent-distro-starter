import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin' / 'agd'


def command(home, *args, expected=0):
    result = subprocess.run([str(CLI), '--home', str(home), *args],
                            env=dict(os.environ, AGD_HOME=str(home)), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result


def test_init_writes_safe_selected_config(tmp_path):
    home = tmp_path / 'h'
    command(home, 'init', '--backend', 'herdr')
    got = json.loads((home / 'data' / 'launcher.json').read_text())
    assert got['schema'] == 1 and got['backend'] == 'herdr'
    assert re.fullmatch(r'agd-[0-9a-f]{32}', got['herdr_session'])


def test_init_writes_herdr_session_for_tmux(tmp_path):
    home = tmp_path / 'h'
    command(home, 'init', '--backend', 'tmux')
    got = json.loads((home / 'data' / 'launcher.json').read_text())
    assert got['backend'] == 'tmux'
    assert re.fullmatch(r'agd-[0-9a-f]{32}', got['herdr_session'])


def test_init_refuses_malformed_launcher_config_without_replacement(tmp_path):
    home = tmp_path / 'h'
    config = home / 'data' / 'launcher.json'
    config.parent.mkdir(parents=True)
    config.write_text('{not json')
    command(home, 'init', '--backend', 'tmux', expected=3)
    assert config.read_text() == '{not json'


def test_init_refuses_symlinked_launcher_config_without_replacement(tmp_path):
    home = tmp_path / 'h'
    target = tmp_path / 'target.json'
    target.write_text('unchanged')
    config = home / 'data' / 'launcher.json'
    config.parent.mkdir(parents=True)
    config.symlink_to(target)
    command(home, 'init', '--backend', 'tmux', expected=3)
    assert config.is_symlink()
    assert target.read_text() == 'unchanged'


def test_init_refuses_nonfresh_home_without_replacement(tmp_path):
    home = tmp_path / 'h'
    marker = home / 'existing'
    home.mkdir()
    marker.write_text('keep')
    command(home, 'init', '--backend', 'tmux', expected=3)
    assert marker.read_text() == 'keep'


def test_prepare_start_correlates_only_exact_ready_endpoint(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import launcher, observability, store, supervision

    home = tmp_path / 'h'
    home.mkdir()
    expected_argv = ['/opt/omp', '--cwd', str(home), '--session-dir', str(home / 'sessions')]
    launcher.initialize(home, 'tmux')
    monkeypatch.setattr(observability, 'central_argv',
                        lambda actual_home, args: (expected_argv, {'AGD_HOME': str(actual_home)}, None))
    seen = {}

    def fake_create(backend, actual_home, launch_id, endpoint_token, managed_command, launch_env, argv):
        seen.update(backend=backend, launch_id=launch_id, endpoint_token=endpoint_token, managed_command=managed_command,
                    launch_env=launch_env, argv=argv)
        start = supervision.process_start(os.getpid())
        store.atomic_json(actual_home / 'state' / 'owner.json', {
            'owner_pid': os.getpid(), 'owner_start': start, 'token': endpoint_token,
            'watcher_pid': os.getpid(), 'watcher_start': start, 'heartbeat': __import__('time').time(),
        })
        return {'id': 'central', 'launch_id': launch_id, 'endpoint_token': endpoint_token}

    def fake_inspect(_home, receipt):
        return {'endpoint': {'id': 'central', 'launch_id': receipt['launch_id'],
                             'endpoint_token': receipt['endpoint_token']},
                'foreground': {'launch_id': receipt['launch_id'], 'endpoint_token': receipt['endpoint_token'],
                               'pid': os.getpid(), 'start': supervision.process_start(os.getpid()),
                               'argv': expected_argv}}

    def fake_attach(_home, receipt):
        return ['fake-attach', receipt['endpoint']['id']]

    target = launcher.prepare_start(home, None, {}, fake_create, fake_inspect, fake_attach)
    monkeypatch.setattr(launcher, '_backend_inspect', fake_inspect)
    assert target.attach_argv == ['fake-attach', 'central']
    assert seen['argv'] == expected_argv
    assert seen['managed_command'] == [str(CLI), '--home', str(home), 'observability', 'central']
    assert seen['launch_env']['AGD_LAUNCH_ID'] == seen['launch_id']
    assert seen['launch_env']['AGD_ENDPOINT_TOKEN'] == seen['endpoint_token']
    assert launcher.active_backend(home, target.env) == 'tmux'
    with __import__('pytest').raises(store.Refusal, match='inherited'):
        launcher.active_backend(home, {**target.env, 'AGD_LAUNCH_ID': 'different'})

    receipt_path = home / 'state' / 'central-launcher.json'
    ready = json.loads(receipt_path.read_text())
    for phase in ('reserved', 'spawned'):
        store.atomic_json(receipt_path, {**ready, 'phase': phase})
        with __import__('pytest').raises(store.Refusal, match='interrupted'):
            launcher.prepare_start(home, None, {}, fake_create, fake_inspect, fake_attach)
    store.atomic_json(receipt_path, {**ready, 'endpoint': {'id': 'changed'}})
    with __import__('pytest').raises(store.Refusal, match='continuity'):
        launcher.prepare_start(home, None, {}, fake_create, fake_inspect, fake_attach)

    store.atomic_json(receipt_path, ready)
    for argv in (expected_argv[:-1], ['/wrong', *expected_argv[1:]]):
        def mismatched(_home, receipt, argv=argv):
            return {**fake_inspect(_home, receipt), 'foreground': {**fake_inspect(_home, receipt)['foreground'], 'argv': argv}}
        with __import__('pytest').raises(store.Refusal, match='foreground'):
            launcher.prepare_start(home, None, {}, fake_create, mismatched, fake_attach)

    stale = {**ready, 'owner': {'pid': 1, 'start': 'stale'}}
    store.atomic_json(receipt_path, stale)
    with __import__('pytest').raises(store.Refusal, match='owner snapshot'):
        launcher.prepare_start(home, None, {}, fake_create, fake_inspect, fake_attach)


def test_start_refuses_missing_dependencies_before_reservation(tmp_path):
    home = tmp_path / 'h'
    fake = tmp_path / 'bin'
    fake.mkdir()
    omp = fake / 'omp'
    omp.write_text('#!/bin/sh\nexit 0\n')
    omp.chmod(0o755)
    command(home, 'init', '--backend', 'tmux')
    result = subprocess.run([str(CLI), '--home', str(home), 'start'],
                            env=dict(os.environ, AGD_HOME=str(home), PATH=f'{fake}:{os.environ["PATH"]}'),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
    assert result.returncode == 3
    assert not (home / 'state' / 'central-launcher.json').exists()


def test_start_prefers_proven_active_backend_before_saved_config(tmp_path, monkeypatch):
    import runpy
    import sys
    import pytest

    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import installer, launcher, store, supervision

    module = runpy.run_path(str(CLI), run_name='agd_cli')
    home = tmp_path / 'home'
    home.mkdir()
    launcher.initialize(home, 'tmux')
    start = supervision.process_start(os.getpid())
    endpoint = {'id': 'central'}
    receipt = {
        'schema': 1, 'launch_id': '00000000-0000-0000-0000-000000000001', 'endpoint_token': 'token',
        'backend': 'herdr', 'phase': 'ready', 'endpoint': endpoint, 'managed_command': ['agd'],
        'expected_process_argv': ['/opt/omp'], 'owner': {'pid': os.getpid(), 'start': start},
        'watcher': {'pid': os.getpid(), 'start': start},
    }
    store.atomic_json(home / 'state' / 'central-launcher.json', receipt)
    store.atomic_json(home / 'state' / 'owner.json', {
        'owner_pid': os.getpid(), 'owner_start': start, 'token': 'token',
        'watcher_pid': os.getpid(), 'watcher_start': start, 'heartbeat': __import__('time').time(),
    })
    monkeypatch.setattr(launcher, '_backend_inspect', lambda _, actual_receipt: {
        'endpoint': actual_receipt['endpoint'],
        'foreground': {'launch_id': actual_receipt['launch_id'], 'endpoint_token': actual_receipt['endpoint_token'],
                       'pid': os.getpid(), 'start': start, 'argv': actual_receipt['expected_process_argv']},
    })
    monkeypatch.setattr(launcher, '_backend_attach', lambda _, __: ['attach-herdr'])
    selected = []
    monkeypatch.setattr(installer, 'validate_dependencies',
                        lambda *_: selected.append('validated') or {'schema': 1, 'omp': {}})
    args = module['parser']().parse_args(['--home', str(home), 'start'])
    with pytest.raises(store.Refusal, match='selected launcher dependency'):
        module['main'](args)
    assert selected == ['validated']

    monkeypatch.setattr(installer, 'validate_dependencies',
                        lambda *_: {'schema': 1, 'omp': {}, 'herdr': {}})
    executed = {}
    def fake_exec(*exec_args):
        executed['argv'] = exec_args
        raise AssertionError
    monkeypatch.setattr(module['os'], 'execvpe', fake_exec)
    with pytest.raises(AssertionError):
        module['main'](args)
    assert executed['argv'][1] == ['attach-herdr']

    conflicting = module['parser']().parse_args(['--home', str(home), 'start', '--backend', 'tmux'])
    with pytest.raises(store.Refusal, match='explicit backend differs'):
        module['main'](conflicting)

def test_prepare_start_refuses_unsafe_launcher_config(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import launcher, observability, store

    home = tmp_path / 'h'
    config = home / 'data' / 'launcher.json'
    config.parent.mkdir(parents=True)
    monkeypatch.setattr(observability, 'central_argv', lambda *_: (['/opt/omp'], {}, None))
    for body in ('{', '{}', '{"schema": 2, "backend": "tmux", "herdr_session": "agd-x"}',
                 '{"schema": 1, "backend": "tmux", "herdr_session": "agd-x", "extra": true}'):
        (home / 'state' / 'central-launcher.json').unlink(missing_ok=True)
        config.write_text(body)
        with __import__('pytest').raises(store.Refusal, match='launcher config'):
            launcher.prepare_start(home, None, {}, lambda *_: None, lambda *_: None, lambda *_: [])
    config.unlink()
    target = tmp_path / 'launcher.json'
    target.write_text('{"schema": 1, "backend": "tmux", "herdr_session": "agd-00000000000000000000000000000000"}')
    config.symlink_to(target)
    with __import__('pytest').raises(store.Refusal, match='launcher config'):
        launcher.prepare_start(home, None, {}, lambda *_: None, lambda *_: None, lambda *_: [])
    config.unlink()
    config.symlink_to(tmp_path / 'missing')
    with __import__('pytest').raises(store.Refusal, match='launcher config'):
        launcher.prepare_start(home, None, {}, lambda *_: None, lambda *_: None, lambda *_: [])
