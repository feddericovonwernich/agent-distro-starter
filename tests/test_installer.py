import hashlib
from functools import partial
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

from agd_lib import installer
from agd_lib.installer import (Refusal, _extract_treehouse, install_herdr, install_omp,
                               install_treehouse, probe_herdr, probe_omp, read_dependencies,
                               setup, validate_dependencies, write_dependencies)


def result(stdout='', stderr='', returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def fake_run(argv, check=False, **_kwargs):
    if argv[1:] == ['--version']:
        return result('v3.1.6\n' if Path(argv[0]).name == 'treehouse' else 'omp/18.14.0\n')
    if argv[1:] == ['models', '--json']:
        return result('[]')
    if argv[1:] == ['login', '--help']:
        return result('usage')
    if argv[1:] == ['config', 'init-xdg']:
        return result()
    if argv[1:] == ['session', 'list', '--json']:
        return result('[]')
    if argv[-2:] == ['status', 'server']:
        return result('status: not running\nsocket: /tmp/herdr.sock\n')
    raise AssertionError(argv)


def release(name, body=b'executable', **asset):
    return {'draft': False, 'prerelease': False, 'tag_name': 'v1.2.3', 'assets': [{
        'name': name, 'browser_download_url': 'https://example.test/' + name,
        'digest': 'sha256:' + hashlib.sha256(body).hexdigest(), **asset,
    }]}


def stream(body):
    def stream_url(url):
        yield body
    return stream_url


def make_archive(path, members):
    with tarfile.open(path, 'w:gz') as archive:
        for member in members:
            info = tarfile.TarInfo(member[0])
            if len(member) == 2:
                info.size = len(member[1])
                archive.addfile(info, io.BytesIO(member[1]))
            else:
                info.type, info.linkname = member[1:]
                archive.addfile(info)


def test_treehouse_extracts_only_the_pinned_regular_member(tmp_path, monkeypatch):
    archive = tmp_path/'treehouse.tgz'
    destination = tmp_path/'treehouse'
    make_archive(archive, [('treehouse', b'executable')])
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return result('v3.1.6\n')

    monkeypatch.setattr(installer.store, 'run', run)
    _extract_treehouse(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), destination)
    assert destination.read_bytes() == b'executable'
    assert destination.stat().st_mode & 0o777 == 0o755
    assert calls == [[str(destination.parent / calls[0][0].split('/')[-1]), '--version']]


def test_treehouse_extract_refuses_unsafe_members_or_wrong_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.store, 'run', lambda *args, **kwargs: result('v3.1.6\n'))
    cases = [
        [('treehouse', b'x'), ('other', b'x')],
        [('treehouse', tarfile.SYMTYPE, 'other')],
        [('/treehouse', b'x')],
        [('dir/../treehouse', b'x')],
    ]
    for number, members in enumerate(cases):
        archive = tmp_path/f'unsafe-{number}.tgz'
        make_archive(archive, members)
        with pytest.raises(Refusal, match='archive'):
            _extract_treehouse(archive, hashlib.sha256(archive.read_bytes()).hexdigest(),
                               tmp_path/f'treehouse-{number}')
    archive = tmp_path/'valid.tgz'
    make_archive(archive, [('treehouse', b'x')])
    with pytest.raises(Refusal, match='digest'):
        _extract_treehouse(archive, '0' * 64, tmp_path/'wrong-digest')


def test_treehouse_install_uses_public_pinned_archive_and_records_hashes(tmp_path, monkeypatch):
    assert installer._TREEHOUSE_URL == (
        'https://github.com/kunchenguid/treehouse/releases/download/v3.1.6/'
        'treehouse-v3.1.6-linux-amd64.tar.gz')
    assert installer._TREEHOUSE_DIGEST == 'e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1'
    archive = tmp_path/'treehouse.tgz'
    make_archive(archive, [('treehouse', b'executable')])
    monkeypatch.setattr(installer, '_TREEHOUSE_DIGEST',
                        hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(installer.store, 'run', lambda *args, **kwargs: result('v3.1.6\n'))
    urls = []

    def stream_url(url):
        urls.append(url)
        yield from [archive.read_bytes()]

    installed = install_treehouse(tmp_path, stream_url)
    assert urls == [installer._TREEHOUSE_URL]
    assert installed['artifact_sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert installed['installed_sha256'] == hashlib.sha256(b'executable').hexdigest()
    write_dependencies(tmp_path, {'schema': 1, 'treehouse': installed})
    assert validate_dependencies(
        tmp_path, lambda argv, check=False: result('v3.1.6\n')
        if argv[1:] == ['--version'] else (_ for _ in ()).throw(AssertionError(argv))
    )['treehouse']['version'] == 'v3.1.6'


def test_treehouse_managed_receipt_requires_fixed_pin(tmp_path):
    executable = tmp_path/'treehouse'
    executable.write_bytes(b'bytes')
    executable.chmod(0o755)
    entry = {'source': 'managed', 'path': str(executable), 'version': 'v3.1.6',
             'capabilities': ['version'], 'asset': installer._TREEHOUSE_ASSET, 'tag': 'v3.1.6',
             'artifact_sha256': installer._TREEHOUSE_DIGEST,
             'installed_sha256': hashlib.sha256(b'bytes').hexdigest()}
    for change in ({'tag': 'v3.1.5'}, {'artifact_sha256': '0' * 64}):
        with pytest.raises(Refusal, match='receipt'):
            write_dependencies(tmp_path, {'schema': 1, 'treehouse': {**entry, **change}})


def test_omp_rejects_missing_api_digest(tmp_path):
    selected = {'draft': False, 'prerelease': False, 'tag_name': 'v1',
                'assets': [{'name': 'omp-linux-x64', 'browser_download_url': 'https://example.test/omp'}]}
    with pytest.raises(Refusal, match='digest'):
        install_omp(tmp_path, lambda _: selected, stream(b'x'), fake_run)


def test_installers_require_exact_stable_asset_and_preserve_old_binary_on_bad_digest(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    target = tmp_path/'.local/bin/omp'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'old')
    unstable = release('omp-linux-x64'); unstable['draft'] = True
    with pytest.raises(Refusal, match='stable'):
        install_omp(tmp_path, lambda _: unstable, stream(b'executable'), fake_run)
    wrong = release('omp-linux-x64', b'expected')
    with pytest.raises(Refusal, match='artifact digest'):
        install_omp(tmp_path, lambda _: wrong, stream(b'other'), fake_run)
    assert target.read_bytes() == b'old'
    with pytest.raises(Refusal, match='asset'):
        install_herdr(tmp_path, lambda _: release('wrong-name'), stream(b'executable'), fake_run)


def test_installs_probe_chmodded_temporary_file_before_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    target = tmp_path/'.local/bin/omp'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'old')
    calls = []

    def rejecting_run(argv, check=False, **kwargs):
        calls.append(argv)
        if argv[1:] == ['--version']:
            assert Path(argv[0]).stat().st_mode & 0o777 == 0o755
            return result(returncode=1)
        return fake_run(argv, check, **kwargs)

    with pytest.raises(Refusal, match='OMP version'):
        install_omp(tmp_path, lambda _: release('omp-linux-x64'), stream(b'executable'), rejecting_run)
    assert target.read_bytes() == b'old'
    assert calls[0][0] != str(target)


def test_omp_uses_strict_release_checksum_when_api_digest_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    body = b'executable'
    digest = hashlib.sha256(body).hexdigest()
    selected = {'draft': False, 'prerelease': False, 'tag_name': 'v1', 'assets': [
        {'name': 'omp-linux-x64', 'browser_download_url': 'https://example.test/omp'},
        {'name': 'SHA256SUMS.txt', 'browser_download_url': 'https://example.test/sums'},
    ]}

    def stream_url(url):
        yield (f'{digest}  omp-linux-x64\n'.encode() if url.endswith('sums') else body)

    installed = install_omp(tmp_path, lambda _: selected, stream_url, fake_run)
    assert installed['tag'] == 'v1'
    assert installed['artifact_sha256'] == digest
    assert installed['installed_sha256'] == digest


def test_probes_require_documented_machine_output(tmp_path):
    omp = tmp_path/'omp'; omp.write_text('x')
    herdr = tmp_path/'herdr'; herdr.write_text('x')
    assert probe_omp(omp, fake_run)['capabilities'] == ['models-json', 'login']
    assert probe_herdr(herdr, 'agd-test', fake_run)['capabilities'] == ['session-list-json', 'named-status']

    def status_runner(output):
        def run(argv, check=False):
            return result(output) if argv[1:] == ['--session', 'agd-test', 'status', 'server'] else fake_run(argv, check)
        return run

    legacy = 'status: running\nversion: 0.8.2\nprotocol: 20\ncompatible: yes\nsocket: /tmp/herdr.sock\n'
    current = ('status: running\nversion: v0.9.5\nendpoint_compatible: yes\n'
               'private_protocol: 20\nprivate_protocol_compatible: yes\nsocket: /tmp/herdr.sock\n')
    assert probe_herdr(herdr, 'agd-test', status_runner(legacy))['capabilities'] == ['session-list-json', 'named-status']
    assert probe_herdr(herdr, 'agd-test', status_runner(current))['capabilities'] == ['session-list-json', 'named-status']

    def bad_models(argv, check=False):
        return result('not json') if argv[1:] == ['models', '--json'] else fake_run(argv, check)

    incompatible = current.replace('private_protocol_compatible: yes', 'private_protocol_compatible: no')
    duplicate = current + 'socket: /tmp/other.sock\n'
    malformed = 'status: running\nprotocol: \ncompatible: yes\nsocket: relative.sock\n'
    whitespace_legacy = legacy.replace('protocol: 20', 'protocol:   ')
    whitespace_current = current.replace('version: v0.9.5', 'version:   ')
    with pytest.raises(Refusal, match='models'):
        probe_omp(omp, bad_models)
    for output in (incompatible, duplicate, malformed, whitespace_legacy, whitespace_current):
        with pytest.raises(Refusal, match='status'):
            probe_herdr(herdr, 'agd-test', status_runner(output))


def test_receipt_schema_rejects_unknown_sources_incomplete_and_extra_entries(tmp_path):
    executable = tmp_path/'omp'; executable.write_bytes(b'bytes'); executable.chmod(0o755)
    existing = {'schema': 1, 'omp': {'source': 'existing', 'path': str(executable), 'version': 'v1',
                'capabilities': ['models-json', 'login']}}
    write_dependencies(tmp_path, existing)
    assert read_dependencies(tmp_path) == existing
    for entry in (
        {**existing['omp'], 'source': 'path'},
        {key: value for key, value in existing['omp'].items() if key != 'version'},
        {**existing['omp'], 'asset': 'omp-linux-x64'},
        {'source': 'managed', 'path': str(executable), 'version': 'v1',
         'capabilities': ['models-json', 'login']},
    ):
        with pytest.raises(Refusal, match='receipt'):
            write_dependencies(tmp_path, {'schema': 1, 'omp': entry})
    with pytest.raises(Refusal, match='receipt'):
        write_dependencies(tmp_path, {'schema': 1, 'unknown': existing['omp']})


def test_receipt_rehashes_managed_and_reprobes_existing_entries(tmp_path):
    omp = tmp_path/'omp'; omp.write_bytes(b'bytes'); omp.chmod(0o755)
    receipt = {'schema': 1, 'omp': {'source': 'managed', 'path': str(omp), 'version': 'v1',
               'capabilities': ['models-json', 'login'], 'asset': 'omp-linux-x64', 'tag': 'v1',
               'artifact_sha256': hashlib.sha256(b'bytes').hexdigest(),
               'installed_sha256': hashlib.sha256(b'bytes').hexdigest()}}
    write_dependencies(tmp_path, receipt)
    assert read_dependencies(tmp_path) == receipt
    assert validate_dependencies(tmp_path, fake_run)['omp']['version'] == 'omp/18.14.0'
    omp.write_bytes(b'changed')
    with pytest.raises(Refusal, match='installed digest'):
        validate_dependencies(tmp_path, fake_run)


def test_install_rejects_non_https_assets_and_never_rehashes_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    selected = release('omp-linux-x64')
    selected['assets'][0]['browser_download_url'] = 'http://example.test/omp'
    with pytest.raises(Refusal, match='HTTPS'):
        install_omp(tmp_path, lambda _: selected, stream(b'executable'), fake_run)

    original = installer._sha256
    target = tmp_path/'.local/bin/omp'

    def hash_temporary_only(path):
        assert Path(path) != target
        return original(path)

    monkeypatch.setattr(installer, '_sha256', hash_temporary_only)
    installed = install_omp(tmp_path, lambda _: release('omp-linux-x64'), stream(b'executable'), fake_run)
    assert target.read_bytes() == b'executable'
    assert installed['installed_sha256'] == hashlib.sha256(b'executable').hexdigest()


def test_omp_rejects_non_https_checksum_asset(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    body = b'executable'
    selected = {'draft': False, 'prerelease': False, 'tag_name': 'v1', 'assets': [
        {'name': 'omp-linux-x64', 'browser_download_url': 'https://example.test/omp'},
        {'name': 'SHA256SUMS.txt', 'browser_download_url': 'http://example.test/sums'},
    ]}
    with pytest.raises(Refusal, match='HTTPS'):
        install_omp(tmp_path, lambda _: selected, stream(body), fake_run)


def test_setup_persists_compatible_declined_dependencies_and_preserves_prior_on_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    omp = tmp_path/'omp'; omp.write_text('x'); omp.chmod(0o755)
    treehouse = tmp_path/'treehouse'; treehouse.write_text('x'); treehouse.chmod(0o755)
    selected = release('omp-linux-x64')
    before = {'schema': 1, 'omp': {'source': 'existing', 'path': str(omp), 'version': 'old',
              'capabilities': ['models-json', 'login']}}
    write_dependencies(tmp_path, before)
    got = setup(tmp_path, 'tmux', lambda _: False, lambda _: selected, stream(b'executable'), fake_run,
                install_plugins=lambda *_args, **_kwargs: {})
    assert got['omp']['source'] == 'existing'
    assert got['treehouse']['source'] == 'existing'
    assert read_dependencies(tmp_path) == got
    with pytest.raises(Refusal, match='requirement'):
        setup(tmp_path, 'herdr', lambda _: False, lambda _: selected, stream(b'executable'), fake_run)
    assert read_dependencies(tmp_path) == got


def test_setup_preserves_valid_managed_receipt_when_update_declined(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    omp = tmp_path / 'omp'
    omp.write_bytes(b'bytes')
    omp.chmod(0o755)
    treehouse = tmp_path / 'treehouse'
    treehouse.write_bytes(b'bytes')
    treehouse.chmod(0o755)
    digest = hashlib.sha256(b'bytes').hexdigest()
    managed = {'source': 'managed', 'path': str(omp), 'version': 'old',
               'capabilities': ['models-json', 'login'], 'asset': 'omp-linux-x64',
               'tag': 'v1', 'artifact_sha256': '0' * 64, 'installed_sha256': digest}
    write_dependencies(tmp_path, {'schema': 1, 'omp': managed})
    got = setup(tmp_path, 'tmux', lambda _: False, lambda _: release('omp-linux-x64'),
                stream(b'executable'), fake_run, install_plugins=lambda *_args, **_kwargs: {})
    assert got['omp'] == {**managed, 'version': 'omp/18.14.0'}


def test_setup_refuses_managed_receipt_tampering_or_path_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    omp = tmp_path / 'omp'
    omp.write_bytes(b'bytes')
    omp.chmod(0o755)
    treehouse = tmp_path / 'treehouse'
    treehouse.write_bytes(b'bytes')
    treehouse.chmod(0o755)
    digest = hashlib.sha256(b'bytes').hexdigest()
    managed = {'source': 'managed', 'path': str(omp), 'version': 'old',
               'capabilities': ['models-json', 'login'], 'asset': 'omp-linux-x64',
               'tag': 'v1', 'artifact_sha256': '0' * 64, 'installed_sha256': digest}
    for change in ({'installed_sha256': '1' * 64}, {'path': str(tmp_path / 'elsewhere')}):
        if change['path'] if 'path' in change else False:
            other = Path(change['path']); other.write_bytes(b'bytes'); other.chmod(0o755)
        write_dependencies(tmp_path, {'schema': 1, 'omp': {**managed, **change}})
        with pytest.raises(Refusal, match='managed'):
            setup(tmp_path, 'tmux', lambda _: False, lambda _: release('omp-linux-x64'),
                  stream(b'executable'), fake_run)


def test_validate_dependencies_probes_omp_inside_local_runtime(tmp_path):
    executable = tmp_path / 'omp'
    executable.write_bytes(b'executable')
    executable.chmod(0o755)
    write_dependencies(tmp_path, {'schema': 1, 'omp': {
        'source': 'existing', 'path': str(executable), 'version': 'v18.14.0',
        'capabilities': ['models-json', 'login'],
    }})
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:] == ['--version']:
            return result('omp v18.14.0\n')
        if argv[1:] == ['models', '--json']:
            return result('[]')
        if argv[1:] == ['login', '--help']:
            return result('usage')
        raise AssertionError(argv)

    validate_dependencies(tmp_path, run)

    assert calls
    for _argv, kwargs in calls:
        assert kwargs['cwd'] == tmp_path
        assert kwargs['env']['PI_CODING_AGENT_DIR'] == str(tmp_path / 'data/omp/home/.omp/agent')
        assert kwargs['env']['OMP_PROFILE'] == 'default'


def test_setup_initializes_native_local_omp_directories(tmp_path, monkeypatch):
    binaries = {}
    for name in ('omp', 'treehouse'):
        path = tmp_path / name
        path.write_bytes(b'executable')
        path.chmod(0o755)
        binaries[name] = path
    monkeypatch.setattr(installer.shutil, 'which',
                        lambda name: str(binaries[name]) if name in binaries else None)
    calls = []
    plugin_calls = []
    monkeypatch.setattr(
        installer.omp_runtime, 'install_plugins',
        lambda home, executable, **kwargs: plugin_calls.append(
            (home, executable, kwargs)) or {'installed': True})

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:] == ['--version']:
            return result('v3.1.6\n' if Path(argv[0]).name == 'treehouse' else 'omp v18.14.0\n')
        if argv[1:] == ['models', '--json']:
            return result('[]')
        if argv[1:] == ['login', '--help']:
            return result('usage')
        if argv[1:] == ['config', 'init-xdg']:
            return result()
        raise AssertionError(argv)

    setup(tmp_path, 'tmux', lambda _: False, lambda _: release('omp-linux-x64'),
          stream(b'executable'), run)

    initialization = [(argv, kwargs) for argv, kwargs in calls
                      if argv[1:] == ['config', 'init-xdg']]
    assert len(initialization) == 1
    assert initialization[0][1]['cwd'] == tmp_path
    assert initialization[0][1]['env']['OMP_PROFILE'] == 'default'
    assert all(path.is_dir() for path in installer.omp_runtime.runtime_directories(tmp_path))
    assert plugin_calls == [(tmp_path, str(binaries['omp']), {'run': run})]


def test_probe_omp_requires_native_plugin_capability_floor():
    def run(argv, **_kwargs):
        if argv[1:] == ['--version']:
            return result('omp/18.13.3\n')
        return fake_run(argv)

    with pytest.raises(Refusal, match='18.14.0'):
        probe_omp('/opt/omp', run)


@pytest.fixture
def reviewer_onboarding(tmp_path, monkeypatch):
    from agd_lib import omp_runtime, reviewer_timer
    home = tmp_path / 'home'
    (home / 'data').mkdir(parents=True)
    (home / 'data/home.json').write_text('{"schema":1,"home_id":"123e4567-e89b-12d3-a456-426614174000"}')
    omp_runtime.agent_dir(home).mkdir(parents=True)
    xdg = tmp_path / 'xdg'
    control = {'failure': None}
    def systemctl(argv):
        if control['failure'] in argv:
            raise installer.store.DependencyError('native timer unavailable')
    monkeypatch.setattr(reviewer_timer, 'enable',
                        partial(reviewer_timer.enable, xdg_config_home=xdg, command_runner=systemctl))
    return home, xdg, control


@pytest.mark.parametrize('answers,state', [
    ([''], 'skipped'), (['n'], 'skipped'), ([None], 'skipped'),
    (['yes', '0'], 'cancelled'), (['yes', None], 'cancelled'),
    (['yes', '4', '03:45', None], 'cancelled'), (['yes', '5', None], 'cancelled'),
])
def test_reviewer_onboarding_decline_and_cancel_have_no_state(reviewer_onboarding, answers, state):
    from agd_lib import reviewer
    home, xdg, _ = reviewer_onboarding
    values = iter(answers)
    got = installer.setup_reviewer(home, lambda _: next(values, None), lambda _: None)
    assert got['state'] == state
    assert not reviewer.paths(home)['config'].exists()
    assert not list(xdg.glob('systemd/user/*.timer'))


@pytest.mark.parametrize('answers,schedule', [
    (['y', ''], {'mode': 'calendar', 'calendar': '02:00', 'timezone': 'UTC', 'interval_hours': None}),
    (['YES', '2'], {'mode': 'interval', 'calendar': None, 'timezone': None, 'interval_hours': 6}),
    (['y', '3'], {'mode': 'interval', 'calendar': None, 'timezone': None, 'interval_hours': 1}),
    (['y', '4', '03:45', 'Europe/Berlin'], {'mode': 'calendar', 'calendar': '03:45', 'timezone': 'Europe/Berlin', 'interval_hours': None}),
    (['y', '5', '9'], {'mode': 'interval', 'calendar': None, 'timezone': None, 'interval_hours': 9}),
])
def test_reviewer_onboarding_presets_and_customization(reviewer_onboarding, answers, schedule):
    from agd_lib import reviewer, reviewer_timer
    home, xdg, _ = reviewer_onboarding
    before = reviewer.read_config(home)
    before['limits']['max_model_calls'] = 7
    before['review_model'] = 'operator-selected-model'
    reviewer._private(reviewer.paths(home)['config'].parent)
    installer.store.atomic_json(reviewer.paths(home)['config'], before)
    values = iter(answers)
    got = installer.setup_reviewer(home, lambda _: next(values, None), lambda _: None)
    assert got['state'] == 'enabled'
    config = reviewer.read_config(home)
    assert config['schedule'] == schedule
    assert config['review_model'] == 'operator-selected-model'
    assert config['limits']['max_model_calls'] == 7
    assert reviewer_timer._read_record(home)['schedule'] == schedule
    assert list(xdg.glob('systemd/user/*.timer'))
    assert not list(reviewer.paths(home)['receipts'].glob('*.json'))


@pytest.mark.parametrize('invalid', [
    ['4', '25:61', 'UTC'], ['4', '03:45', '../invalid'], ['5', '1.5'], ['5', '-2'], ['not-a-choice'],
])
def test_reviewer_onboarding_corrects_invalid_input_without_partial_config(reviewer_onboarding, invalid):
    from agd_lib import reviewer
    home, _, _ = reviewer_onboarding
    values = iter(['y', *invalid, '5', '9'])
    errors = []
    def ask(_):
        assert not reviewer.paths(home)['config'].exists()
        return next(values, None)
    got = installer.setup_reviewer(home, ask, errors.append)
    assert got['state'] == 'enabled'
    assert reviewer.read_config(home)['schedule']['interval_hours'] == 9
    assert any('invalid' in message.lower() for message in errors)


@pytest.mark.parametrize('failure', ['runtime', 'show', 'is-active'])
def test_reviewer_onboarding_retains_schedule_when_activation_unavailable(reviewer_onboarding, monkeypatch, failure):
    from agd_lib import reviewer, reviewer_runner, reviewer_timer
    home, xdg, control = reviewer_onboarding
    if failure == 'runtime':
        def unavailable(*args, **kwargs):
            raise installer.store.DependencyError('installed OMP package root is unavailable')
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', unavailable)
    else:
        control['failure'] = failure
    values = iter(['y', '2'])
    messages = []
    got = installer.setup_reviewer(home, lambda _: next(values, None), messages.append)
    assert got['state'] == 'configured'
    assert reviewer.read_config(home)['schedule']['interval_hours'] == 6
    assert any('not enabled' in message.lower() for message in messages)
    record = reviewer_timer._read_record(home)
    assert record is None or record['state'] == 'failed'
    if failure != 'is-active':
        assert not list(xdg.glob('systemd/user/*.timer'))
    else:
        reviewer_timer.disable(home, xdg_config_home=xdg, command_runner=lambda _: None)
        assert reviewer_timer._read_record(home)['state'] == 'disabled'


@pytest.mark.parametrize('state', ['enabled', 'failed', 'enabling', 'disabling'])
def test_reviewer_onboarding_refuses_unsafe_reconfiguration(reviewer_onboarding, state):
    from agd_lib import reviewer, reviewer_timer
    home, _, _ = reviewer_onboarding
    reviewer._private(reviewer_timer._timer_record(home).parent)
    installer.store.atomic_json(reviewer_timer._timer_record(home), {'state': state})
    before = reviewer.read_config(home)
    reviewer._private(reviewer.paths(home)['config'].parent)
    installer.store.atomic_json(reviewer.paths(home)['config'], before)
    frozen = reviewer.paths(home)['config'].read_bytes()
    values = iter(['y', '3'])
    with pytest.raises(installer.store.Refusal, match='disable'):
        installer.setup_reviewer(home, lambda _: next(values, None), lambda _: None)
    assert reviewer.paths(home)['config'].read_bytes() == frozen


def test_reviewer_onboarding_respects_active_run_lease(reviewer_onboarding):
    from agd_lib import reviewer
    home, _, _ = reviewer_onboarding
    values = iter(['y', '3'])
    with reviewer.run_lease(home), pytest.raises(installer.store.Refusal, match='reviewer run'):
        installer.setup_reviewer(home, lambda _: next(values, None), lambda _: None)
    assert not reviewer.paths(home)['config'].exists()


def test_reviewer_onboarding_cli_preserves_live_central_owner(reviewer_onboarding):
    from agd_lib import reviewer
    home, xdg, _ = reviewer_onboarding
    pid = os.getpid()
    start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    owner = home / 'state/owner.json'
    installer.store.atomic_json(owner, {'owner_pid': pid, 'owner_start': start, 'token': 'central-only'})
    frozen = owner.read_bytes()
    env = {key: value for key, value in os.environ.items() if key != 'AGD_OWNER'}
    env.update(PATH='', XDG_CONFIG_HOME=str(xdg))
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'bin/agd'), '--home', str(home)]
    observed = subprocess.run([*command, 'installer', 'reviewer'], input='y\n2\n',
                              capture_output=True, text=True, env=env, timeout=20)
    assert observed.returncode == 0, observed.stderr
    assert reviewer.read_config(home)['schedule']['interval_hours'] == 6
    assert owner.read_bytes() == frozen
    guarded = subprocess.run([*command, 'installer', 'setup', '--backend', 'herdr'],
                             capture_output=True, text=True, env=env, timeout=20)
    assert guarded.returncode == 3
    assert 'live central' in guarded.stderr
    assert owner.read_bytes() == frozen
