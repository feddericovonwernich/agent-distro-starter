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

    assert env['HOME'] == str(home / 'data/omp/home')
    assert env['OMP_PROFILE'] == env['PI_PROFILE'] == 'default'
    assert env['PI_CONFIG_DIR'] == '.omp'
    assert (Path(env['HOME']) / env['PI_CONFIG_DIR']).resolve() == home / 'data/omp/home/.omp'
    assert env['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')
    assert env['XDG_DATA_HOME'] == str(home / 'data/omp/xdg/data')
    assert env['XDG_STATE_HOME'] == str(home / 'data/omp/xdg/state')
    assert env['XDG_CACHE_HOME'] == str(home / 'data/omp/xdg/cache')
    assert env['PATH'] == '/safe/bin'
    assert env['OPENAI_API_KEY'] == 'preserve-provider-value'
    assert inherited == before


def test_local_environment_keeps_user_and_project_state_inside_distro(tmp_path):
    from agd_lib import omp_runtime

    home = tmp_path / 'distro'
    worktree = tmp_path / 'project' / 'worktree'
    home.mkdir()
    worktree.mkdir(parents=True)

    env = omp_runtime.environment(home, {'HOME': str(tmp_path / 'account')})

    assert (Path(env['HOME']) / env['PI_CONFIG_DIR']).resolve().is_relative_to(home)
    assert (home / env['PI_CONFIG_DIR']).resolve().is_relative_to(home)
    assert (worktree / env['PI_CONFIG_DIR']).resolve().is_relative_to(worktree)


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
    assert observed['env']['HOME'] == str(home / 'data/omp/home')
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
    assert observed['env']['HOME'] == str(home / 'data/omp/home')
    assert observed['env']['OMP_PROFILE'] == 'default'
    assert observed['env']['PI_CODING_AGENT_DIR'] == str(home / 'data/omp/agent')


def test_managed_omp_environments_include_fixed_local_storage(monkeypatch):
    from support import lab
    from agd_lib import observability

    monkeypatch.setenv('HOME', '/preserved/account')
    monkeypatch.setenv('PI_CONFIG_DIR', '.hostile')
    with lab() as (home, _):
        worker, _, _ = observability.managed_environment(
            home, 'worker', {'id': 'task-a', 'worktree': str(home)},
            '00000000-0000-4000-8000-000000000001')
        reviewer, _, _ = observability.reviewer_environment(
            home, '00000000-0000-4000-8000-000000000002')
        central_argv, central, _ = observability.central_argv(home, [])

    for env in (worker, reviewer, central):
        assert env['HOME'] == str(home / 'data/omp/home')
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


PLUGIN_SPECS = {
    'ponytail@ponytail': ('@dietrichgebert/ponytail', '5.3.3'),
    'superpowers@superpowers-dev': ('superpowers', '7.0.1'),
}


def write_native_plugins(home, *, enabled_values=None):
    enabled_values = enabled_values or {}
    registry = {'version': 2, 'plugins': {}}
    lock = {'plugins': {}, 'settings': {}}
    marketplaces = {'version': 1, 'marketplaces': []}
    for plugin_id, (package, version) in PLUGIN_SPECS.items():
        publisher = 'DietrichGebert/ponytail' if plugin_id == 'ponytail@ponytail' else 'obra/superpowers'
        market_name = plugin_id.split('@', 1)[1]
        payload = home / 'data/omp/home/.omp/plugins/cache/plugins' / f'{market_name}-{version}'
        skills = payload / 'skills'
        skills.mkdir(parents=True)
        (skills / 'sample').mkdir()
        (skills / 'sample' / 'SKILL.md').write_text('---\\nname: sample\\ndescription: sample\\n---\\n')
        (payload / 'package.json').write_text(json.dumps({
            'name': package, 'version': version, 'pi': {'skills': ['./skills']},
        }))
        entry = {'scope': 'project', 'installPath': str(payload), 'version': version,
                 'installedAt': '2026-01-01T00:00:00Z', 'lastUpdated': '2026-01-01T00:00:00Z'}
        if plugin_id in enabled_values:
            entry['enabled'] = enabled_values[plugin_id]
        registry['plugins'][plugin_id] = [entry]
        lock['plugins'][package] = {'version': version, 'enabledFeatures': None, 'enabled': True}
        catalog = home / 'data/omp/home/.omp/plugins/cache/marketplaces' / market_name / 'marketplace.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text('{}')
        marketplaces['marketplaces'].append({
            'name': market_name, 'sourceType': 'github', 'sourceUri': publisher,
            'catalogPath': str(catalog), 'addedAt': '2026-01-01T00:00:00Z',
            'updatedAt': '2026-01-01T00:00:00Z',
        })
    config = home / 'data/omp/home/.omp'
    project_plugins = home / '.omp/plugins'
    project_plugins.mkdir(parents=True, exist_ok=True)
    registry_path = project_plugins / 'installed_plugins.json'
    registry_path.write_text(json.dumps(registry))
    (project_plugins / 'omp-plugins.lock.json').write_text(json.dumps(lock))
    (config / 'plugins' / 'project_registries.json').write_text(json.dumps({
        'version': 1, 'registries': [str(registry_path)],
    }))
    (config / 'marketplaces.json').write_text(json.dumps(marketplaces))
    return registry, lock, marketplaces


def test_plugin_sources_accept_native_enabled_default_and_explicit_true(tmp_path):
    from agd_lib import omp_runtime

    home = tmp_path / 'distro'
    home.mkdir()
    write_native_plugins(home, enabled_values={'superpowers@superpowers-dev': True})

    plugins = omp_runtime.installed_plugins(home)

    assert set(plugins) == set(PLUGIN_SPECS)
    assert plugins['ponytail@ponytail']['version'] == '5.3.3'
    assert plugins['ponytail@ponytail']['path'].is_relative_to(home / 'data/omp')
    assert plugins['superpowers@superpowers-dev']['skill_roots'][0].name == 'skills'


def test_plugin_sources_refuse_unsafe_or_ambiguous_registration(tmp_path):
    from agd_lib import omp_runtime, store

    cases = {}
    for name in ('outside', 'symlink', 'project-registry-symlink', 'duplicate', 'missing',
                 'disabled', 'schema', 'escaped-skills', 'runtime-disabled',
                 'override-disabled', 'publisher'):
        home = tmp_path / name
        home.mkdir()
        registry, lock, marketplaces = write_native_plugins(home)
        cases[name] = (home, registry, lock, marketplaces)

    home, registry, _, _ = cases['outside']
    registry['plugins']['ponytail@ponytail'][0]['installPath'] = str(tmp_path / 'outside-payload')
    (home / '.omp/plugins/installed_plugins.json').write_text(json.dumps(registry))

    home, registry, _, _ = cases['symlink']
    payload = Path(registry['plugins']['ponytail@ponytail'][0]['installPath'])
    moved = home / 'real-payload'
    payload.rename(moved)
    payload.symlink_to(moved, target_is_directory=True)
    home, _, _, _ = cases['project-registry-symlink']
    project_plugins = home / '.omp/plugins'
    moved = tmp_path / 'outside-project-registry'
    project_plugins.rename(moved)
    project_plugins.symlink_to(moved, target_is_directory=True)


    home, registry, _, _ = cases['duplicate']
    registry['plugins']['ponytail@ponytail'].append(dict(registry['plugins']['ponytail@ponytail'][0]))
    (home / '.omp/plugins/installed_plugins.json').write_text(json.dumps(registry))

    home, registry, _, _ = cases['missing']
    payload = Path(registry['plugins']['ponytail@ponytail'][0]['installPath'])
    for child in sorted(payload.rglob('*'), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    payload.rmdir()

    home, registry, _, _ = cases['disabled']
    registry['plugins']['ponytail@ponytail'][0]['enabled'] = False
    (home / '.omp/plugins/installed_plugins.json').write_text(json.dumps(registry))

    home, _, _, _ = cases['schema']
    (home / '.omp/plugins/installed_plugins.json').write_text('{"version":1,"plugins":{}}')

    home, registry, _, _ = cases['escaped-skills']
    payload = Path(registry['plugins']['ponytail@ponytail'][0]['installPath'])
    manifest = json.loads((payload / 'package.json').read_text())
    manifest['pi']['skills'] = ['../../../../../../escaped']
    (payload / 'package.json').write_text(json.dumps(manifest))

    home, _, lock, _ = cases['runtime-disabled']
    lock['plugins']['@dietrichgebert/ponytail']['enabled'] = False
    (home / '.omp/plugins/omp-plugins.lock.json').write_text(json.dumps(lock))

    home, _, _, _ = cases['override-disabled']
    (home / '.omp/plugin-overrides.json').write_text(json.dumps(
        {'disabled': ['@dietrichgebert/ponytail']}))

    home, _, _, marketplaces = cases['publisher']
    marketplaces['marketplaces'][0]['sourceUri'] = 'attacker/ponytail'
    (home / 'data/omp/home/.omp/marketplaces.json').write_text(json.dumps(marketplaces))

    for name, (home, *_unused) in cases.items():
        with pytest.raises(store.Refusal, match='plugin'):
            omp_runtime.installed_plugins(home)


def test_official_plugin_install_sequence_is_idempotent(tmp_path):
    from types import SimpleNamespace
    from agd_lib import omp_runtime

    home = tmp_path / 'distro'
    home.mkdir()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:4] == ['plugin', 'marketplace', 'add']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[1:3] == ['plugin', 'install']:
            if argv[-1] == 'superpowers@superpowers-dev':
                write_native_plugins(home)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(argv)

    installed = omp_runtime.install_plugins(home, '/opt/omp', run=run)
    assert set(installed) == set(PLUGIN_SPECS)
    assert [argv for argv, _kwargs in calls] == [
        ['/opt/omp', 'plugin', 'marketplace', 'add', 'DietrichGebert/ponytail'],
        ['/opt/omp', 'plugin', 'install', '--scope', 'project', 'ponytail@ponytail'],
        ['/opt/omp', 'plugin', 'marketplace', 'add', 'obra/superpowers'],
        ['/opt/omp', 'plugin', 'install', '--scope', 'project', 'superpowers@superpowers-dev'],
    ]
    assert all(kwargs['timeout'] == 300 for _argv, kwargs in calls)
    calls.clear()
    assert set(omp_runtime.install_plugins(home, '/opt/omp', run=run)) == set(PLUGIN_SPECS)
    assert calls == []
