import hashlib
import io
import json
import os
import tarfile
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import lab


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
from agd_lib import store
from agd_lib.store import Refusal


def _cli():
    # Keep RED useful while the focused module is introduced in the GREEN step.
    from agd_lib import langfuse_cli
    return langfuse_cli


def _result(stdout='', stderr='', returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, body in files.items():
            data = body.encode() if isinstance(body, str) else body
            info = tarfile.TarInfo(f'package/{name}')
            info.size = len(data)
            info.mode = 0o755 if name.startswith('bin/') else 0o644
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _package_files():
    return {
        'package.json': json.dumps({
            'name': '@langfuse/cli', 'version': '1.2.4',
            'bin': {'langfuse': 'bin/langfuse'},
            'main': 'bin/langfuse', 'dependencies': {},
        }),
        'bin/langfuse': '#!/usr/bin/env node\nconsole.log("langfuse")\n',
    }


def _node_tools(tmp_path):
    node = tmp_path / 'node'
    npm = tmp_path / 'npm'
    for path in (node, npm):
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)
    return node, npm


@contextmanager
def _query_loopback(web_port, collector_port, project_id):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get('Authorization')))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            payload = {'data': [{'id': project_id}]} if self.path.endswith('/projects') else {}
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            pass

    servers = [
        ThreadingHTTPServer(('127.0.0.1', web_port), Handler),
        ThreadingHTTPServer(('127.0.0.1', collector_port), Handler),
    ]
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield requests
    finally:
        for server in servers:
            server.shutdown()
        for thread in threads:
            thread.join(timeout=2)
        for server in servers:
            server.server_close()


def _query_ownership(home_id, project, web_port):
    container = {
        'Config': {'Labels': {
            'agd.home_id': home_id,
            'com.docker.compose.project': project,
            'com.docker.compose.service': 'langfuse-web',
        }},
        'State': {'Running': True},
        'NetworkSettings': {'Ports': {
            '3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(web_port)}],
        }},
    }

    def inspect(argv):
        if argv[:3] in (
                ['docker', 'network', 'inspect'],
                ['docker', 'volume', 'inspect']):
            return SimpleNamespace(
                returncode=0, stdout=json.dumps([{'Labels': {'agd.home_id': home_id}}]),
                stderr='')
        if argv[:3] == ['docker', 'ps', '-aq']:
            return SimpleNamespace(returncode=0, stdout='web-container\n', stderr='')
        if argv[:3] == ['docker', 'container', 'inspect']:
            return SimpleNamespace(returncode=0, stdout=json.dumps([container]), stderr='')
        raise AssertionError(argv)

    return inspect

def test_langfuse_opt_out_has_no_probes_or_writes(tmp_path):
    cli = _cli()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('optional decline must happen before probing')

    result = cli.setup_langfuse(
        tmp_path, input_fn=lambda *_: '', output=lambda *_: None,
        run=forbidden, which=forbidden, fetch_bytes=forbidden,
    )

    assert result['state'] == 'declined'
    assert calls == []


def test_langfuse_reuses_external_official_cli_without_ownership(tmp_path, monkeypatch):
    cli = _cli()
    external = tmp_path / 'shared'
    package = external / 'node_modules/@langfuse/cli'
    (package / 'bin').mkdir(parents=True)
    for name, body in _package_files().items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755 if name.startswith('bin/') else 0o644)
    executable = package / 'bin/langfuse'
    command = external / 'node_modules/.bin/langfuse'
    command.parent.mkdir()
    command.symlink_to('../@langfuse/cli/bin/langfuse')
    node, _npm = _node_tools(tmp_path)
    archive = _archive(_package_files())
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)
    calls = []

    def which(name):
        calls.append(('which', name))
        return str(command) if name == 'langfuse' else str(node) if name == 'node' else None

    def run(argv, **kwargs):
        calls.append(('run', [str(item) for item in argv]))
        if argv[0] == str(node) and argv[1:2] == ['-p']:
            return _result('22.19.0\n')
        if str(argv[0]) == str(executable) and argv[1:] == ['--version']:
            return _result('1.2.4\n')
        if str(argv[0]) == str(executable) and argv[1:] == ['api', 'schema', '--json']:
            return _result('{"commands": {}}\n')
        raise AssertionError(argv)

    result = cli.ensure_installed(
        tmp_path, run=run, which=which, fetch_bytes=lambda _url: iter([archive]),
    )

    assert result['source'] == 'external'
    assert result['owned'] is False
    launcher = tmp_path / 'data/langfuse-cli/bin/langfuse'
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert json.loads((tmp_path / 'data/langfuse-cli/installation.json').read_text())['owned'] is False
    assert command.is_symlink()
    assert executable.read_bytes() == (package / 'bin/langfuse').read_bytes()


def test_langfuse_tampered_external_assets_are_not_executed_or_reused(tmp_path, monkeypatch):
    cli = _cli()
    external = tmp_path / 'shared'
    package = external / 'node_modules/@langfuse/cli'
    (package / 'bin').mkdir(parents=True)
    archive_files = _package_files()
    archive = _archive(archive_files)
    for name, body in archive_files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755 if name.startswith('bin/') else 0o644)
    executable = package / 'bin/langfuse'
    executable.write_text('#!/usr/bin/env node\nconsole.log("tampered")\n')
    command = external / 'node_modules/.bin/langfuse'
    command.parent.mkdir()
    command.symlink_to('../@langfuse/cli/bin/langfuse')
    node, _npm = _node_tools(tmp_path)
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)
    calls = []

    def run(argv, **kwargs):
        calls.append([str(item) for item in argv])
        return _result('22.19.0\n')

    with pytest.raises(Refusal):
        cli.ensure_installed(
            tmp_path, run=run,
            which=lambda name: str(command) if name == 'langfuse' else str(node) if name == 'node' else None,
            fetch_bytes=lambda _url: iter([archive]),
        )

    assert not any(row[0] == str(executable) for row in calls)
    assert executable.read_text() == '#!/usr/bin/env node\nconsole.log("tampered")\n'
    assert not (tmp_path / 'data/langfuse-cli/installation.json').exists()


def test_langfuse_verified_private_install_is_atomic(tmp_path, monkeypatch):
    cli = _cli()
    node, npm = _node_tools(tmp_path)
    archive_files = _package_files()
    archive = _archive(archive_files)
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)
    calls = []

    def which(name):
        calls.append(('which', name))
        return str(node) if name == 'node' else str(npm) if name == 'npm' else None

    def run(argv, **kwargs):
        argv = [str(item) for item in argv]
        calls.append(('run', argv))
        if argv[:3] == [str(node), '-p', 'process.versions.node']:
            return _result('22.19.0\n')
        if argv[0] == str(npm):
            prefix = Path(argv[argv.index('--prefix') + 1])
            package = prefix / 'lib/node_modules/@langfuse/cli'
            for name, body in archive_files.items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                path.chmod(0o755 if name.startswith('bin/') else 0o644)
            return _result()
        if argv[-1:] == ['--version']:
            return _result('1.2.4\n')
        if argv[-3:] == ['api', 'schema', '--json']:
            return _result('{"commands": {}}\n')
        raise AssertionError(argv)

    before = (tmp_path / 'data/langfuse-cli').exists()
    result = cli.ensure_installed(
        tmp_path, run=run, which=which, fetch_bytes=lambda _url: iter([archive]),
    )

    assert before is False
    assert result['source'] == 'managed'
    assert result['owned'] is True
    runtime = tmp_path / 'data/langfuse-cli/runtime'
    assert (runtime / 'lib/node_modules/@langfuse/cli/package.json').is_file()
    assert (tmp_path / 'data/langfuse-cli/bin/langfuse').is_file()
    receipt = json.loads((tmp_path / 'data/langfuse-cli/installation.json').read_text())
    assert receipt['archive']['sha256'] == hashlib.sha256(archive).hexdigest()
    assert any(row[0] == 'run' and row[1][0] == str(npm) for row in calls)

def test_failed_managed_stage_is_retained_for_inspection(tmp_path, monkeypatch):
    cli = _cli()
    node, npm = _node_tools(tmp_path)
    archive = _archive(_package_files())
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)

    def run(argv, **kwargs):
        argv = [str(item) for item in argv]
        if argv[0] == str(node):
            return _result('22.19.0\n')
        if argv[0] == str(npm):
            raise Refusal('fixture npm failure')
        raise AssertionError(argv)

    with pytest.raises(Refusal, match='fixture npm failure'):
        cli.ensure_installed(
            tmp_path, run=run,
            which=lambda name: str(node) if name == 'node' else str(npm) if name == 'npm' else None,
            fetch_bytes=lambda _url: iter([archive]),
        )
    assert list((tmp_path / 'data').glob('.langfuse-cli-*'))


def test_langfuse_bad_digest_or_probe_preserves_previous_install(tmp_path, monkeypatch):
    cli = _cli()
    root = tmp_path / 'data/langfuse-cli'
    (root / 'runtime').mkdir(parents=True)
    old = root / 'runtime/old.txt'
    old.write_bytes(b'known-good-runtime')
    receipt = {'schema': 1, 'source': 'managed', 'owned': True, 'version': '1.2.4',
               'runtime': str(root / 'runtime'), 'assets': {'old.txt': hashlib.sha256(old.read_bytes()).hexdigest()}}
    root.joinpath('installation.json').write_text(json.dumps(receipt))
    frozen = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', '0' * 64, raising=False)

    with pytest.raises(Refusal):
        cli.ensure_installed(
            tmp_path, run=lambda *args, **kwargs: _result('22.19.0\n'),
            which=lambda name: str(tmp_path / name),
            fetch_bytes=lambda _url: iter([b'corrupt archive']),
        )

    assert {path: path.read_bytes() for path in root.rglob('*') if path.is_file()} == frozen


def test_langfuse_foreign_or_partial_runtime_is_retained(tmp_path):
    cli = _cli()
    root = tmp_path / 'data/langfuse-cli/runtime'
    marker = root / 'foreign.bin'
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b'foreign bytes')

    with pytest.raises(Refusal):
        cli.ensure_installed(
            tmp_path, run=lambda *args, **kwargs: _result('22.19.0\n'),
            which=lambda _name: None, fetch_bytes=lambda _url: iter([b'archive']),
        )

    assert marker.read_bytes() == b'foreign bytes'
    assert not (tmp_path / 'data/langfuse-cli/installation.json').exists()


def test_langfuse_setup_does_not_install_node_or_npm(tmp_path):
    cli = _cli()
    calls = []

    def which(name):
        calls.append(('which', name))
        return None

    result = cli.setup_langfuse(
        tmp_path, input_fn=lambda *_: 'y', output=lambda *_: None,
        run=lambda *args, **kwargs: calls.append(('run', args)) or _result(),
        which=which, fetch_bytes=lambda _url: calls.append(('fetch', _url)) or iter([b'archive']),
    )

    assert result['state'] == 'incomplete'
    assert not any(row[0] == 'run' for row in calls)
    assert not any(row[0] == 'fetch' for row in calls)
    assert calls[:2] == [('which', 'node'), ('which', 'npm')]


def test_mandatory_setup_preserves_optional_cli(tmp_path, monkeypatch):
    cli = _cli()
    optional = tmp_path / 'data/langfuse-cli/installation.json'
    optional.parent.mkdir(parents=True)
    optional.write_text('{"schema":1,"source":"managed","version":"1.2.4"}\n')
    frozen = optional.read_bytes()
    monkeypatch.setattr(cli, 'read_installation', lambda home: json.loads(optional.read_text()))
    monkeypatch.setattr('agd_lib.installer.omp_runtime', SimpleNamespace(
        environment=lambda home, environ: dict(environ),
        initialize=lambda *args, **kwargs: None,
    ))
    from agd_lib import installer
    monkeypatch.setattr(installer, 'shutil', SimpleNamespace(which=lambda _name: None))

    # A failed mandatory setup must not perform optional cleanup or rewrite its receipt.
    with pytest.raises(Refusal):
        installer.setup(
            tmp_path, 'tmux', lambda _message: False,
            lambda _url: {}, lambda _url: iter(()),
            lambda *args, **kwargs: _result(), install_plugins=lambda *args, **kwargs: None,
        )
    assert optional.read_bytes() == frozen


def test_modified_external_cli_never_receives_keys(tmp_path, monkeypatch):
    cli = _cli()
    executable = tmp_path / 'langfuse'
    executable.write_text('modified wrapper')
    executable.chmod(0o755)
    keys = {'LANGFUSE_PUBLIC_KEY': 'pk_test', 'LANGFUSE_SECRET_KEY': 'sk_test'}
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _result('1.2.4\n')

    with pytest.raises(Refusal):
        cli.ensure_installed(
            tmp_path, run=run, which=lambda name: str(executable) if name == 'langfuse' else None,
            fetch_bytes=lambda _url: iter([b'archive']),
        )
    assert all(secret not in repr(call) for secret in keys.values() for call in calls)


def test_other_version_is_retained_and_pinned_copy_is_installed(tmp_path, monkeypatch):
    cli = _cli()
    external = tmp_path / 'shared'
    package = external / 'node_modules/@langfuse/cli'
    (package / 'bin').mkdir(parents=True)
    legacy = _package_files()
    legacy['package.json'] = legacy['package.json'].replace('1.2.4', '0.9.0')
    for name, body in legacy.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755 if name.startswith('bin/') else 0o644)
    node, npm = _node_tools(tmp_path)
    archive = _archive(_package_files())
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)
    legacy_entry = package / 'bin/langfuse'

    def which(name):
        return str(legacy_entry) if name == 'langfuse' else str(node) if name == 'node' else str(npm)

    def run(argv, **kwargs):
        argv = [str(item) for item in argv]
        if argv[:3] == [str(node), '-p', 'process.versions.node']:
            return _result('22.19.0\n')
        if argv[0] == str(npm):
            prefix = Path(argv[argv.index('--prefix') + 1])
            for name, body in _package_files().items():
                path = prefix / 'lib/node_modules/@langfuse/cli' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                path.chmod(0o755 if name.startswith('bin/') else 0o644)
            return _result()
        if argv[-1:] == ['--version']:
            return _result('1.2.4\n')
        if argv[-3:] == ['api', 'schema', '--json']:
            return _result('{"commands": {}}\n')
        raise AssertionError(argv)

    result = cli.ensure_installed(
        tmp_path, run=run, which=which, fetch_bytes=lambda _url: iter([archive]),
    )
    assert result['source'] == 'managed'
    assert legacy_entry.read_bytes() == legacy['bin/langfuse'].encode()
    assert (tmp_path / 'data/langfuse-cli/runtime').is_dir()

def test_root_owned_node_and_npm_are_accepted_when_read_only(tmp_path, monkeypatch):
    cli = _cli()
    node, npm = _node_tools(tmp_path)
    node_alias = tmp_path / 'node-alias'
    npm_alias = tmp_path / 'npm-alias'
    os.link(node, node_alias)
    os.link(npm, npm_alias)
    real_lstat = Path.lstat

    def root_owned_lstat(path):
        result = real_lstat(path)
        if path in {node, npm, node_alias, npm_alias}:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, 'lstat', root_owned_lstat)
    archive = _archive(_package_files())
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)

    def which(name):
        return str(node_alias) if name == 'node' else str(npm_alias) if name == 'npm' else None

    def run(argv, **kwargs):
        argv = [str(item) for item in argv]
        if argv[0] == str(node_alias):
            return _result('22.19.0\n')
        if argv[0] == str(npm_alias):
            prefix = Path(argv[argv.index('--prefix') + 1])
            for name, body in _package_files().items():
                path = prefix / 'lib/node_modules/@langfuse/cli' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                path.chmod(0o755 if name.startswith('bin/') else 0o644)
            return _result()
        if argv[-1:] == ['--version']:
            return _result('1.2.4\n')
        if argv[-3:] == ['api', 'schema', '--json']:
            return _result('{"commands": {}}\n')
        raise AssertionError(argv)

    result = cli.ensure_installed(
        tmp_path, run=run, which=which, fetch_bytes=lambda _url: iter([archive]),
    )
    assert result['source'] == 'managed'


def test_root_owned_group_writable_node_or_npm_is_rejected(tmp_path, monkeypatch):
    cli = _cli()
    node, npm = _node_tools(tmp_path)
    npm.chmod(0o775)
    real_lstat = Path.lstat

    def root_owned_lstat(path):
        result = real_lstat(path)
        if path in {node, npm}:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, 'lstat', root_owned_lstat)
    archive = _archive(_package_files())
    monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)

    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return _result('22.19.0\n')

    with pytest.raises(Refusal):
        cli.ensure_installed(
            tmp_path,
            run=run,
            which=lambda name: str(node) if name == 'node' else str(npm) if name == 'npm' else None,
            fetch_bytes=lambda _url: iter([archive]),
        )
    assert calls == []
    assert not (tmp_path / 'data/langfuse-cli/bin/langfuse').exists()
@pytest.mark.parametrize(('runtime', 'available'), [('healthy', True), ('stopped', False)])
def test_reviewer_langfuse_env_uses_only_owned_local_project(tmp_path, monkeypatch, runtime, available):
    cli = _cli()
    from agd_lib import observability

    with lab() as (home, _):
        observability._runtime_directories(home)
        store.atomic_json(observability.paths(home)['compose'] / 'release-lock.json',
                          observability._load_lock())
        rendered = observability.render_stack(home)
        observability._save_state(home, 'enabled', runtime, rendered)
        config = observability.read_config(home)
        secret = observability._read_secrets(home)
        host = f"http://127.0.0.1:{config['ports']['web_port']}"
        monkeypatch.setattr(
            observability, '_ownership_command',
            _query_ownership(rendered['home_id'], rendered['project'],
                             rendered['web_port']))

        external = tmp_path / 'shared'
        package = external / 'node_modules/@langfuse/cli'
        package_files = _package_files()
        (package / 'bin').mkdir(parents=True)
        (package / 'bin').chmod(0o755)
        package.chmod(0o755)
        package.parent.chmod(0o755)
        for name, body in package_files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            path.chmod(0o755 if name.startswith('bin/') else 0o644)
        executable = package / 'bin/langfuse'
        node, _npm = _node_tools(tmp_path)
        archive = _archive(package_files)
        monkeypatch.setattr(cli, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest(), raising=False)

        def run(argv, **kwargs):
            if argv[0] == str(node) and argv[1:2] == ['-p']:
                return _result('22.19.0\n')
            if str(argv[0]) == str(executable) and argv[1:] == ['--version']:
                return _result('1.2.4\n')
            if str(argv[0]) == str(executable) and argv[1:] == ['api', 'schema', '--json']:
                return _result('{"commands": {}}\n')
            raise AssertionError(argv)

        cli.ensure_installed(
            home, run=run,
            which=lambda name: str(executable) if name == 'langfuse' else str(node) if name == 'node' else None,
            fetch_bytes=lambda _url: iter([archive]),
        )
        original_validate = cli.validate_installation
        monkeypatch.setattr(
            cli, 'validate_installation',
            lambda target, **_kwargs: original_validate(target, run=run))

        inherited = {
            'PATH': '/usr/bin',
            'LANGFUSE_HOST': 'https://cloud.invalid',
            'LANGFUSE_BASE_URL': 'https://cloud.invalid',
            'LANGFUSE_PUBLIC_KEY': 'ambient-public',
            'LANGFUSE_SECRET_KEY': 'ambient-secret',
            'LANGFUSE_PROJECT_ID': 'ambient-project',
        }
        with _query_loopback(config['ports']['web_port'],
                             config['ports']['collector_health_port'],
                             secret['project_id']) as requests:
            environment, status = cli.reviewer_environment(home, inherited)

    if available:
        assert status == {'available': True, 'reason': None}
        assert environment['LANGFUSE_HOST'] == host
        assert environment['LANGFUSE_BASE_URL'] == host
        assert environment['LANGFUSE_PROJECT_ID'] == secret['project_id']
        assert environment['LANGFUSE_PUBLIC_KEY'] == secret['public_key']
        assert environment['LANGFUSE_SECRET_KEY'] == secret['secret_key']
        assert environment['LANGFUSE_API_VERSION'] == 'auto'
        assert environment['PATH'].split(':', 1)[0] == str(home / 'data/langfuse-cli/bin')
        assert requests
        assert all(auth is None for _path, auth in requests[:-1])
        assert requests[-1][0].endswith('/projects')
        assert requests[-1][1].startswith('Basic ')
    else:
        assert status['available'] is False
        assert status['reason']
        assert not any(key.startswith('LANGFUSE_') for key in environment)
        assert environment['PATH'] == '/usr/bin'
        assert requests == []


def test_ambient_cloud_keys_removed_when_query_unavailable(monkeypatch):
    cli = _cli()
    inherited = {
        'LANGFUSE_HOST': 'https://cloud.invalid',
        'LANGFUSE_BASE_URL': 'https://cloud.invalid',
        'LANGFUSE_PUBLIC_KEY': 'ambient-public',
        'LANGFUSE_SECRET_KEY': 'ambient-secret',
        'LANGFUSE_PROJECT_ID': 'ambient-project',
    }
    with lab() as (home, _):
        environment, status = cli.reviewer_environment(home, inherited)

    assert not any(key.startswith('LANGFUSE_') for key in environment)
    assert status['available'] is False
    assert status['reason']




def test_reviewer_environment_drops_keys_when_healthy_record_is_stale(monkeypatch):
    cli = _cli()
    from agd_lib import observability

    with lab() as (home, _):
        rendered = observability.render_stack(home)
        observability._save_state(home, 'enabled', 'healthy', rendered)
        home_id = rendered['home_id']
        container = {
            'Config': {'Labels': {
                'agd.home_id': home_id,
                'com.docker.compose.project': rendered['project'],
                'com.docker.compose.service': 'langfuse-web',
            }},
            'State': {'Running': True},
            'NetworkSettings': {'Ports': {
                '3000/tcp': [{'HostIp': '127.0.0.1',
                              'HostPort': str(rendered['web_port'])}],
            }},
        }

        def inspect(argv):
            if argv[:3] in (
                    ['docker', 'network', 'inspect'],
                    ['docker', 'volume', 'inspect']):
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{'Labels': {'agd.home_id': home_id}}]),
                    stderr='')
            if argv[:3] == ['docker', 'ps', '-aq']:
                return SimpleNamespace(returncode=0, stdout='web-container\n', stderr='')
            if argv[:3] == ['docker', 'container', 'inspect']:
                return SimpleNamespace(returncode=0, stdout=json.dumps([container]), stderr='')
            raise AssertionError(argv)

        monkeypatch.setattr(observability, '_ownership_command', inspect)
        monkeypatch.setattr(cli, 'validate_installation', lambda *_a, **_k: None)
        environment, status = cli.reviewer_environment(
            home, {'PATH': '/usr/bin', 'LANGFUSE_SECRET_KEY': 'ambient-secret'})

    assert status['available'] is False
    assert status['reason']
    assert not any(key.startswith('LANGFUSE_') for key in environment)
