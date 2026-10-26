import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))

from agd_lib import relay, store


def make_relay_runtime(root, version='0.1.0', executable=True):
    package = root / 'node_modules' / 'open-relay'
    (package / 'dist' / 'src').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'name': 'open-relay', 'version': version,
        'repository': {'url': 'https://github.com/feddericovonwernich/open-relay.git'},
    }))
    if executable:
        (package / 'dist' / 'src' / 'cli.js').write_text('console.log("relay")\n')
        binary = root / 'bin' / 'relay'
        binary.parent.mkdir(parents=True)
        binary.symlink_to(package / 'dist' / 'src' / 'cli.js')
    return root


def fake_npm_success(home):
    def run(argv, cwd=None, timeout=30, check=True):
        stage = Path(argv[argv.index('--prefix') + 1])
        make_relay_runtime(stage)
        return subprocess.CompletedProcess(argv, 0, '', '')
    return run


def runtime_has_open_relay_010(runtime):
    package = runtime / 'node_modules' / 'open-relay'
    return (json.loads((package / 'package.json').read_text()) == {
        'name': 'open-relay', 'version': '0.1.0',
        'repository': {'url': 'https://github.com/feddericovonwernich/open-relay.git'},
    } and (package / 'dist' / 'src' / 'cli.js').is_file())


def test_stale_system_candidate_results_in_usable_private_runtime(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    stale = make_relay_runtime(tmp_path / 'stale', version='0.0.9', executable=True)
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    runtime = relay.ensure_runtime(
        home, which=lambda name: str(stale / 'bin' / name), run=fake_npm_success(home))
    assert runtime == home / 'relay'
    assert runtime_has_open_relay_010(runtime)


def test_partial_private_runtime_is_preserved_and_refused(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'relay').mkdir(parents=True)
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    with pytest.raises(store.Refusal, match='partial'):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))
    assert (home / 'relay').is_dir()


def test_private_runtime_marker_must_match_pinned_archive(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    runtime = make_relay_runtime(home / 'relay')
    (runtime / 'relay-install.json').write_text(json.dumps({
        'archive_sha256': 'not-the-pinned-archive', 'package_version': '0.1.0',
        'node_version': '22.19.0', 'metadata_digest': 'wrong',
    }))
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    with pytest.raises(store.Refusal, match='provenance'):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))


def test_symlinked_private_runtime_is_refused(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / 'relay').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    with pytest.raises(store.Refusal, match='symlink'):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))

def test_lock_tolerates_concurrent_state_directory_creation(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    state = home / 'state'
    original_mkdir = Path.mkdir

    def mkdir_raced(path, *args, **kwargs):
        if path == state:
            original_mkdir(path, *args, **kwargs)
            raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', mkdir_raced)
    with relay._lock(home) as locked:
        assert locked == state
    assert (state / 'relay-install.lock').is_file()


def test_project_service_is_private_and_idempotent(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    monkeypatch.setattr(relay, 'ensure_runtime', lambda *_args, **_kwargs: runtime)
    monkeypatch.setenv('AGD_RELAY_GITHUB_READ_TOKEN', 'test-read-token')

    def init(argv, cwd, **_kwargs):
        (cwd/'.relay'/'connectors').mkdir(parents=True)
        (cwd/'.relay'/'connectors'/'github.json').write_text('{"connector":"github","tokenEnv":"GITHUB_TOKEN","repositories":[{"name":"owner/repo","mode":"configured-tools"}],"triggers":[{}, {}, {}],"aggregate":{}}')
    first = relay.ensure_project_service(home, 'sample', 'owner/repo', run=init)
    second = relay.ensure_project_service(home, 'sample', 'owner/repo', run=init)

    assert first == second
    assert first['runtime'] == str(runtime)
    assert first['repository'] == 'owner/repo'
    assert first['root'] == str(home/'state'/'relay'/'projects'/'sample')
    connector = __import__('json').loads((home/'state'/'relay'/'projects'/'sample'/'.relay'/'connectors'/'github.json').read_text())
    assert connector == {'connector': 'github', 'tokenEnv': 'AGD_RELAY_GITHUB_READ_TOKEN',
                         'repositories': [{'name': 'owner/repo', 'mode': 'generic-check-runs'}], 'triggers': []}
    assert not (home/'relay').exists()


def test_project_service_requires_token_before_runtime_or_state(tmp_path, monkeypatch):
    called = False
    def runtime(*_args, **_kwargs):
        nonlocal called
        called = True
        return tmp_path/'runtime'
    monkeypatch.setattr(relay, 'ensure_runtime', runtime)
    from agd_lib import store
    with pytest.raises(store.Refusal, match='AGD_RELAY_GITHUB_READ_TOKEN'):
        relay.ensure_project_service(tmp_path/'home', 'sample', 'owner/repo')
    assert not called and not (tmp_path/'home').exists()



def test_project_service_preserves_unrecorded_relay_state(tmp_path, monkeypatch):
    home = tmp_path/'home'
    root = home/'state'/'relay'/'projects'/'sample'/'.relay'/'connectors'
    root.mkdir(parents=True)
    (root/'github.json').write_text('{"connector":"github","tokenEnv":"GITHUB_TOKEN","repositories":[{"name":"owner/repo","mode":"configured-tools"}],"triggers":[{}, {}, {}],"aggregate":{}}')
    monkeypatch.setenv('AGD_RELAY_GITHUB_READ_TOKEN', 'test-read-token')
    monkeypatch.setattr(relay, 'ensure_runtime', lambda *_args, **_kwargs: tmp_path/'runtime')
    with pytest.raises(store.Refusal, match='without a service record'):
        relay.ensure_project_service(home, 'sample', 'owner/repo')

def test_project_service_rejects_symlinked_state_root(tmp_path, monkeypatch):
    import os
    home = tmp_path/'home'
    outside = tmp_path/'outside'; outside.mkdir()
    parent = home/'state'/'relay'/'projects'; parent.mkdir(parents=True)
    os.symlink(outside, parent/'sample')
    monkeypatch.setattr(relay, 'ensure_runtime', lambda *_args, **_kwargs: tmp_path/'system-relay')
    from agd_lib import store
    try:
        relay.ensure_project_service(home, 'sample', 'owner/repo')
    except store.Refusal:
        return
    raise AssertionError('symlinked project state was accepted')
