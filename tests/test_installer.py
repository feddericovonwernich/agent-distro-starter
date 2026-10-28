import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

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
        return result('not running\n')
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

    def bad_models(argv, check=False):
        return result('not json') if argv[1:] == ['models', '--json'] else fake_run(argv, check)

    with pytest.raises(Refusal, match='models'):
        probe_omp(omp, bad_models)


def test_receipt_rehashes_managed_and_reprobes_existing_entries(tmp_path):
    omp = tmp_path/'omp'; omp.write_bytes(b'bytes')
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


def test_setup_shows_selected_tag_before_consent_and_keeps_compatible_declined_path(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    omp = tmp_path/'omp'; omp.write_text('x'); omp.chmod(0o755)
    shown = []
    selected = release('omp-linux-x64')

    def prompt(message):
        shown.append(message)
        return False

    got = setup(tmp_path, 'tmux', prompt, lambda _: selected, stream(b'executable'), fake_run)
    assert got['omp']['source'] == 'path'
    assert got['omp']['path'] == str(omp)
    assert any('v1.2.3' in message for message in shown)
    assert omp.read_text() == 'x'
    assert not (tmp_path/'data/dependencies.json').exists()
