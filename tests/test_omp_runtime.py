import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))


def test_local_environment_wins_over_inherited_storage(tmp_path):
    from agd_lib import omp_runtime

    home = tmp_path / 'distro'
    account_home = tmp_path / 'account'
    home.mkdir()
    account_home.mkdir()
    inherited = {
        'HOME': str(account_home),
        'PATH': '/safe/bin',
        'OPENAI_API_KEY': 'preserve-provider-value',
        'PI_CONFIG_DIR': '.outside',
        'PI_CODING_AGENT_DIR': '/outside/agent',
        'OMP_PROFILE': 'outside',
        'PI_PROFILE': 'outside',
        'XDG_DATA_HOME': '/outside/data',
        'XDG_STATE_HOME': '/outside/state',
        'XDG_CACHE_HOME': '/outside/cache',
    }
    before = copy.deepcopy(inherited)

    env = omp_runtime.environment(home, inherited)

    assert env['HOME'] == inherited['HOME']
    assert env['OMP_PROFILE'] == env['PI_PROFILE'] == 'default'
    assert (Path(env['HOME']) / env['PI_CONFIG_DIR']).resolve() == home / 'data/omp/config'
    assert env['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')
    assert env['XDG_DATA_HOME'] == str(home / 'data/omp/xdg/data')
    assert env['XDG_STATE_HOME'] == str(home / 'data/omp/xdg/state')
    assert env['XDG_CACHE_HOME'] == str(home / 'data/omp/xdg/cache')
    assert env['PATH'] == '/safe/bin'
    assert env['OPENAI_API_KEY'] == 'preserve-provider-value'
    assert inherited == before


def test_environment_rejects_symlinked_runtime_root(tmp_path):
    from agd_lib import omp_runtime, store

    home = tmp_path / 'distro'
    outside = tmp_path / 'outside'
    home.mkdir()
    outside.mkdir()
    (home / 'data').mkdir()
    (home / 'data' / 'omp').symlink_to(outside, target_is_directory=True)

    with pytest.raises(store.Refusal, match='OMP runtime'):
        omp_runtime.environment(home, {'HOME': str(tmp_path / 'account')})


def test_initialize_creates_private_roots_and_uses_local_environment(tmp_path):
    from agd_lib import omp_runtime

    home = tmp_path / 'distro'
    account_home = tmp_path / 'account'
    home.mkdir()
    account_home.mkdir()
    capture = tmp_path / 'capture.json'
    executable = tmp_path / 'omp'
    executable.write_text(
        '#!/usr/bin/env python3\n'
        'import json, os, pathlib, sys\n'
        f'pathlib.Path({str(capture)!r}).write_text(json.dumps('
        '{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}))\n'
    )
    executable.chmod(0o755)

    omp_runtime.initialize(home, str(executable), inherited={'HOME': str(account_home), 'PATH': os.environ['PATH']})

    observed = json.loads(capture.read_text())
    assert observed['argv'] == ['config', 'init-xdg']
    assert observed['cwd'] == str(home)
    assert observed['env']['HOME'] == str(account_home)
    assert observed['env']['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')
    for path in omp_runtime.runtime_directories(home):
        assert path.is_dir()
        assert path.stat().st_mode & 0o077 == 0


def test_agd_omp_forwards_arguments_in_local_environment(tmp_path):
    home = tmp_path / 'distro'
    account_home = tmp_path / 'account'
    home.mkdir()
    account_home.mkdir()
    capture = tmp_path / 'capture.json'
    executable = tmp_path / 'omp'
    executable.write_text(
        '#!/usr/bin/env python3\n'
        'import json, os, pathlib, sys\n'
        f'pathlib.Path({str(capture)!r}).write_text(json.dumps('
        '{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}))\n'
    )
    executable.chmod(0o755)
    dependencies = home / 'data' / 'dependencies.json'
    dependencies.parent.mkdir()
    dependencies.write_text(json.dumps({'schema': 1, 'omp': {
        'source': 'existing', 'path': str(executable), 'version': 'omp/18.14.0',
        'capabilities': ['models-json', 'login'],
    }}))

    result = subprocess.run(
        [str(ROOT / 'bin' / 'agd'), '--home', str(home), 'omp', 'plugin', 'list', '--json'],
        env={**os.environ, 'HOME': str(account_home), 'OMP_PROFILE': 'hostile'},
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(capture.read_text())
    assert observed['argv'] == ['plugin', 'list', '--json']
    assert observed['cwd'] == str(home)
    assert observed['env']['HOME'] == str(account_home)
    assert observed['env']['OMP_PROFILE'] == 'default'
    assert observed['env']['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')


def test_managed_omp_environments_include_fixed_local_storage(monkeypatch):
    from support import lab
    from agd_lib import observability

    monkeypatch.setenv('HOME', '/preserved/account')
    monkeypatch.setenv('PI_CONFIG_DIR', '.hostile')
    with lab() as (home, _):
        worker, _, _ = observability.managed_environment(
            home, 'worker', {'id': 'task-a'}, '00000000-0000-4000-8000-000000000001')
        reviewer, _, _ = observability.reviewer_environment(
            home, '00000000-0000-4000-8000-000000000002')
        central_argv, central, _ = observability.central_argv(home, [])

    for env in (worker, reviewer, central):
        assert env['HOME'] == '/preserved/account'
        assert env['OMP_PROFILE'] == 'default'
        assert env['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')
    assert '--cwd' in central_argv


def test_central_refuses_profile_escape_before_launch():
    from support import lab
    from agd_lib import observability, store

    with lab() as (home, _):
        with pytest.raises(store.Refusal, match='fixed distro OMP runtime'):
            observability.central_argv(home, ['--profile', 'outside'])


@pytest.mark.parametrize('arguments', [
    ['--profile', 'outside'], ['--profile=outside'], ['--alias', 'outside'],
    ['--cwd', '/tmp'], ['--cwd=/tmp'],
])
def test_agd_omp_refuses_storage_or_project_escape_controls(tmp_path, arguments):
    home = tmp_path / 'distro'
    home.mkdir()
    result = subprocess.run(
        [str(ROOT / 'bin' / 'agd'), '--home', str(home), 'omp', *arguments],
        capture_output=True, text=True,
    )
    assert result.returncode == 3
    assert 'fixed distro OMP runtime' in result.stderr
