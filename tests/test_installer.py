import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

from agd_lib import installer
from agd_lib.installer import (Refusal, install_herdr, install_omp, probe_herdr,
                               probe_omp, read_dependencies, setup,
                               validate_dependencies, write_dependencies)


def result(stdout='', stderr='', returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def fake_run(argv, check=False):
    if argv[1:] == ['--version']:
        return result('v1.2.3\n')
    if argv[1:] == ['models', '--json']:
        return result('[]')
    if argv[1:] == ['login', '--help']:
        return result('usage')
    if argv[1:] == ['session', 'list', '--json']:
        return result('[]')
    if argv[1:] == ['--session', 'agd-test', 'status', 'server']:
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

    def rejecting_run(argv, check=False):
        calls.append(argv)
        if argv[1:] == ['--version']:
            assert Path(argv[0]).stat().st_mode & 0o777 == 0o755
            return result(returncode=1)
        return fake_run(argv, check)

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

    def running(argv, check=False):
        output = ('status: running\nversion: v0.9.5\nendpoint_compatible: yes\n'
                  'private_protocol: 20\nprivate_protocol_compatible: yes\nsocket: /tmp/herdr.sock\n')
        return result(output) if argv[1:] == ['--session', 'agd-test', 'status', 'server'] else fake_run(argv, check)

    assert probe_herdr(herdr, 'agd-test', running)['capabilities'] == ['session-list-json', 'named-status']

    def bad_models(argv, check=False):
        return result('not json') if argv[1:] == ['models', '--json'] else fake_run(argv, check)

    def malformed_status(argv, check=False):
        return result('not running\n') if argv[1:] == ['--session', 'agd-test', 'status', 'server'] else fake_run(argv, check)

    with pytest.raises(Refusal, match='models'):
        probe_omp(omp, bad_models)
    with pytest.raises(Refusal, match='status'):
        probe_herdr(herdr, 'agd-test', malformed_status)


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
    assert validate_dependencies(tmp_path, fake_run)['omp']['version'] == 'v1.2.3'
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
    selected = release('omp-linux-x64')
    before = {'schema': 1, 'omp': {'source': 'existing', 'path': str(omp), 'version': 'old',
              'capabilities': ['models-json', 'login']}}
    write_dependencies(tmp_path, before)
    got = setup(tmp_path, 'tmux', lambda _: False, lambda _: selected, stream(b'executable'), fake_run)
    assert got['omp']['source'] == 'existing'
    assert read_dependencies(tmp_path) == got
    with pytest.raises(Refusal, match='requirement'):
        setup(tmp_path, 'herdr', lambda _: False, lambda _: selected, stream(b'executable'), fake_run)
    assert read_dependencies(tmp_path) == got
