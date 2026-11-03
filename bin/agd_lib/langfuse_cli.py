"""Optional, home-scoped official Langfuse CLI runtime."""
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tarfile
import tempfile
import uuid
from pathlib import Path

from . import store

PACKAGE = '@langfuse/cli'
VERSION = '1.2.4'
ARCHIVE_URL = 'https://registry.npmjs.org/@langfuse/cli/-/cli-1.2.4.tgz'
ARCHIVE_SHA256 = '0fc4e39b38bfa45d77f045a2d3a79e165411e4de0737eb7893bec57b246e574f'
MIN_NODE = (20, 0, 0)
# Tests may provide a synthetic verified asset map; production derives this from
# the pinned archive, never from an untrusted package tree.
PINNED_ASSET_HASHES = None


def _paths(home):
    root = Path(home) / 'data' / 'langfuse-cli'
    return {'root': root, 'runtime': root / 'runtime', 'bin': root / 'bin',
            'launcher': root / 'bin' / 'langfuse', 'receipt': root / 'installation.json'}


def _regular(path, *, executable=False):
    path = Path(path)
    try:
        mode = path.lstat()
    except FileNotFoundError as error:
        raise store.Refusal(f'Langfuse path is unavailable: {path.name}') from error
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_nlink != 1:
        raise store.Refusal('Langfuse path ownership is unproven')
    if mode.st_mode & 0o022:
        raise store.Refusal('Langfuse path is writable by another user')
    if executable and not mode.st_mode & 0o111:
        raise store.Refusal('Langfuse launcher is not executable')
    return path

def _runtime_executable(path):
    path = Path(path)
    try:
        mode = path.lstat()
    except FileNotFoundError as error:
        raise store.Refusal(f'Langfuse runtime path is unavailable: {path.name}') from error
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid not in {os.getuid(), 0}:
        raise store.Refusal('Langfuse runtime ownership is unproven')
    if mode.st_mode & 0o022:
        raise store.Refusal('Langfuse runtime is writable by another user')
    if not mode.st_mode & 0o111:
        raise store.Refusal('Langfuse runtime is not executable')
    return path


def _directory(path):
    path = Path(path)
    try:
        mode = path.lstat()
    except FileNotFoundError as error:
        raise store.Refusal('Langfuse package directory is unavailable') from error
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o022:
        raise store.Refusal('Langfuse package directory ownership is unproven')
    return path.resolve()


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path):
    _regular(path)
    try:
        with Path(path).open(encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise store.Refusal('Langfuse installation receipt is malformed') from error


def _validate_receipt(value):
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise store.Refusal('Langfuse installation receipt is malformed')
    allowed = {'schema', 'source', 'owned', 'version', 'package', 'entry', 'node',
               'launcher', 'runtime', 'assets', 'archive'}
    if set(value) - allowed or value.get('source') not in {'managed', 'external'}:
        raise store.Refusal('Langfuse installation receipt is malformed')
    if value.get('owned') is not (value['source'] == 'managed') or value.get('version') != VERSION:
        raise store.Refusal('Langfuse installation receipt is malformed')
    for key in ('package', 'entry', 'node', 'launcher'):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise store.Refusal('Langfuse installation receipt is malformed')
    assets = value.get('assets')
    if not isinstance(assets, dict) or not assets or any(
            not isinstance(key, str) or not isinstance(item, str) or
            not re.fullmatch(r'[0-9a-f]{64}', item) for key, item in assets.items()):
        raise store.Refusal('Langfuse installation receipt is malformed')
    if value['source'] == 'managed':
        if not isinstance(value.get('runtime'), str) or not Path(value['runtime']).is_absolute():
            raise store.Refusal('Langfuse installation receipt is malformed')
        archive = value.get('archive')
        if not isinstance(archive, dict) or archive.get('url') != ARCHIVE_URL or archive.get('sha256') != ARCHIVE_SHA256:
            raise store.Refusal('Langfuse installation receipt is malformed')
    elif value.get('runtime') is not None or value.get('archive') is not None:
        raise store.Refusal('Langfuse installation receipt is malformed')
    return value


def read_installation(home):
    path = _paths(home)['receipt']
    if not path.exists() and not path.is_symlink():
        return None
    return _validate_receipt(_read_json(path))


def _node_version(run, node):
    try:
        result = run([str(node), '-p', 'process.versions.node'])
    except (OSError, store.DependencyError) as error:
        raise store.Refusal('Langfuse requires an existing Node.js runtime') from error
    value = (getattr(result, 'stdout', '') or '').strip()
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match or tuple(map(int, match.groups())) < MIN_NODE:
        raise store.Refusal('Langfuse requires Node.js 20 or newer')
    return value


def _package_root(candidate):
    candidate = Path(candidate).resolve()
    for parent in (candidate.parent, *candidate.parents):
        metadata = parent / 'package.json'
        if metadata.is_file():
            return parent
    raise store.Refusal('Langfuse package metadata is unavailable')


def _metadata(package):
    path = _regular(Path(package) / 'package.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise store.Refusal('Langfuse package metadata is malformed') from error
    if (value.get('name'), value.get('version')) != (PACKAGE, VERSION):
        raise store.Refusal('Langfuse package identity is not the pinned official release')
    if value.get('dependencies') not in ({}, None):
        raise store.Refusal('Langfuse package has unexpected runtime dependencies')
    command = value.get('bin')
    if isinstance(command, dict):
        command = command.get('langfuse')
    if not isinstance(command, str) or Path(command).is_absolute() or '..' in Path(command).parts:
        raise store.Refusal('Langfuse package entrypoint is malformed')
    entry = (Path(package) / command).resolve()
    if not entry.is_relative_to(Path(package).resolve()):
        raise store.Refusal('Langfuse package entrypoint is redirected')
    _regular(entry, executable=True)
    return value, entry


def _asset_map(package):
    package = Path(package).resolve()
    result = {}
    for path in package.rglob('*'):
        if path.is_symlink():
            raise store.Refusal('Langfuse package contains a redirected asset')
        if path.is_file():
            _regular(path)
            result[str(path.relative_to(package))] = _sha256(path)
    if 'package.json' not in result:
        raise store.Refusal('Langfuse package metadata is unavailable')
    return result

def _normalize_owned_tree(package):
    """Remove host umask group/world write bits from npm-owned staging bytes."""
    package = Path(package)
    paths = [package, *package.rglob('*')]
    for path in paths:
        mode = path.lstat()
        if mode.st_uid != os.getuid():
            raise store.Refusal('Langfuse staged package ownership is unproven')
        if stat.S_ISLNK(mode.st_mode) or not (stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode)):
            raise store.Refusal('Langfuse staged package contains an unsafe asset')
        if stat.S_ISREG(mode.st_mode) and mode.st_nlink != 1:
            raise store.Refusal('Langfuse staged package ownership is unproven')
        path.chmod(stat.S_IMODE(mode.st_mode) & ~0o022)


def _safe_reason(error):
    reason = ' '.join(str(error).split())
    return reason[:160] or type(error).__name__



def reviewer_environment(home, inherited):
    """Return a credential-free base env plus optional local query settings."""
    environment = {key: value for key, value in dict(inherited).items()
                   if not key.startswith('LANGFUSE_')}
    unavailable = {'available': False, 'reason': None}
    try:
        from . import observability
        context = observability.langfuse_query_context(home)
    except (OSError, ValueError, TypeError, store.Refusal):
        context = None
    if context is None:
        unavailable['reason'] = 'local Langfuse query is unavailable'
        return environment, unavailable

    def private_run(argv, **kwargs):
        options = dict(kwargs)
        options['env'] = environment
        return store.run(argv, **options)

    try:
        validate_installation(home, run=private_run)
    except (OSError, ValueError, TypeError, store.Refusal, store.DependencyError) as error:
        unavailable['reason'] = _safe_reason(error)
        return environment, unavailable
    host = context['base_url']
    launcher = str(_paths(home)['bin'])
    environment['PATH'] = launcher + (os.pathsep + environment['PATH'] if environment.get('PATH') else '')
    environment.update(
        LANGFUSE_HOST=host,
        LANGFUSE_BASE_URL=host,
        LANGFUSE_PROJECT_ID=context['project_id'],
        LANGFUSE_PUBLIC_KEY=context['public_key'],
        LANGFUSE_SECRET_KEY=context['secret_key'],
        LANGFUSE_API_VERSION='auto',
    )
    return environment, {'available': True, 'reason': None}

def _chunks(value):
    value = value() if callable(value) else value
    if isinstance(value, bytes):
        return value
    try:
        return b''.join(value)
    except TypeError as error:
        raise store.Refusal('Langfuse archive response is invalid') from error


def _archive_assets(archive):
    with tempfile.TemporaryDirectory(prefix='agd-langfuse-archive-') as directory:
        path = Path(directory) / 'archive.tgz'
        path.write_bytes(archive)
        try:
            with tarfile.open(path, 'r:gz') as stream:
                members = stream.getmembers()
                for member in members:
                    name = Path(member.name)
                    if name.is_absolute() or '..' in name.parts or member.issym() or member.islnk():
                        raise store.Refusal('Langfuse archive contains an unsafe member')
                stream.extractall(directory, filter='data')
        except (OSError, tarfile.TarError) as error:
            raise store.Refusal('Langfuse archive is invalid') from error
        package = Path(directory) / 'package'
        if not package.is_dir():
            raise store.Refusal('Langfuse archive package is missing')
        _metadata(package)
        return _asset_map(package)


def _verified_assets(archive, fetch_bytes):
    if PINNED_ASSET_HASHES is not None:
        return dict(PINNED_ASSET_HASHES)
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise store.Refusal('Langfuse archive checksum differs from the pinned release')
    return _archive_assets(archive)


def _probe(executable, run):
    try:
        version = run([str(executable), '--version'])
        schema = run([str(executable), 'api', 'schema', '--json'])
    except (OSError, store.DependencyError) as error:
        raise store.Refusal('Langfuse offline validation is unavailable') from error
    if version.returncode or not (version.stdout or version.stderr).strip():
        raise store.Refusal('Langfuse version validation failed')
    if schema.returncode:
        raise store.Refusal('Langfuse offline schema validation failed')
    try:
        parsed = json.loads(schema.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise store.Refusal('Langfuse offline schema validation failed') from error
    if not isinstance(parsed, (dict, list)):
        raise store.Refusal('Langfuse offline schema validation failed')


def _launcher(node, entry):
    return '#!/bin/sh\nexec ' + shlex.quote(str(node)) + ' ' + shlex.quote(str(entry)) + ' "$@"\n'


def _validate_candidate(package, node, run, assets):
    metadata, entry = _metadata(package)
    actual = _asset_map(package)
    if actual != assets:
        raise store.Refusal('Langfuse package assets differ from the verified archive')
    _runtime_executable(node)
    node_version = _node_version(run, node)
    _probe(entry, run)
    return metadata, entry, node_version, actual


def validate_installation(home, *, run=store.run):
    receipt = read_installation(home)
    if receipt is None:
        raise store.Refusal('Langfuse CLI is not installed')
    paths = _paths(home)
    package = _directory(receipt['package'])
    entry = _regular(receipt['entry'], executable=True)
    node = _runtime_executable(receipt['node'])
    launcher = _regular(receipt['launcher'], executable=True)
    if launcher != paths['launcher'] or package != Path(receipt['package']) or entry != Path(receipt['entry']):
        raise store.Refusal('Langfuse installation path changed')
    if receipt['source'] == 'managed':
        runtime = _directory(receipt['runtime'])
        if runtime != paths['runtime'] or not package.is_relative_to(runtime):
            raise store.Refusal('Langfuse managed runtime path changed')
    actual = _asset_map(package)
    if actual != receipt['assets']:
        raise store.Refusal('Langfuse package assets differ from receipt')
    if _sha256(launcher) != hashlib.sha256(_launcher(node, entry).encode()).hexdigest():
        raise store.Refusal('Langfuse launcher differs from receipt')
    _metadata(package)
    node_version = _node_version(run, node)
    _probe(entry, run)
    return {'state': 'installed', 'source': receipt['source'], 'owned': receipt['owned'],
            'version': VERSION, 'path': str(launcher), 'node_version': node_version}


def _candidate(which):
    try:
        value = which('langfuse')
    except (OSError, TypeError):
        return None
    if not value:
        return None
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise store.Refusal('Langfuse command path is unavailable') from error
    _regular(path, executable=True)
    return path


def _write_receipt(path, value):
    store.atomic_json(path, _validate_receipt(value))


@contextlib.contextmanager
def _lock(home):
    Path(home).mkdir(parents=True, exist_ok=True)
    with store.lock(Path(home)):
        yield


def ensure_installed(home, *, run=store.run, which=shutil.which, fetch_bytes):
    home = Path(home)
    paths = _paths(home)
    with _lock(home):
        existing = read_installation(home)
        if existing is not None:
            return validate_installation(home, run=run)
        if paths['root'].exists() or paths['root'].is_symlink():
            raise store.Refusal('existing Langfuse runtime is foreign or incomplete')
        node_name = which('node')
        npm_name = which('npm')
        node = Path(node_name).resolve() if node_name else None
        if node is None:
            raise store.Refusal('existing Node.js 20+ is required; install it separately and retry')
        _runtime_executable(node)
        candidate_error = None
        try:
            candidate = _candidate(which)
        except store.Refusal as error:
            candidate = None
            candidate_error = error
        if candidate is None and not npm_name:
            if candidate_error is not None:
                raise candidate_error
            raise store.Refusal('existing Node.js 20+ and npm are required; install them separately and retry')
        archive = _chunks(fetch_bytes(ARCHIVE_URL))
        assets = _verified_assets(archive, fetch_bytes)
        if candidate is not None:
            try:
                package = _package_root(candidate)
                metadata, entry, node_version, actual = _validate_candidate(package, node, run, assets)
            except store.Refusal as error:
                candidate_error = error
                candidate = None
            else:
                root = paths['root']
                root.mkdir(parents=True, mode=0o700)
                paths['bin'].mkdir(mode=0o700)
                paths['launcher'].write_text(_launcher(node, entry), encoding='utf-8')
                paths['launcher'].chmod(0o700)
                receipt = {'schema': 1, 'source': 'external', 'owned': False, 'version': VERSION,
                           'package': str(package), 'entry': str(entry), 'node': str(node),
                           'launcher': str(paths['launcher']), 'assets': actual}
                _write_receipt(paths['receipt'], receipt)
                return {'state': 'installed', 'source': 'external', 'owned': False,
                        'version': VERSION, 'path': str(paths['launcher']), 'node_version': node_version}
        if not npm_name:
            raise candidate_error or store.Refusal(
                'existing Node.js 20+ and npm are required; install them separately and retry')
        temporary = paths['root'].parent / f'.langfuse-cli-{uuid.uuid4().hex}'
        runtime = temporary / 'runtime'
        runtime.mkdir(parents=True, mode=0o700)
        archive_path = temporary / 'archive.tgz'
        archive_path.write_bytes(archive)
        npm = Path(npm_name).resolve()
        _runtime_executable(npm)
        run([str(npm), 'install', '--global', '--prefix', str(runtime), '--ignore-scripts',
             '--no-audit', '--no-fund', str(archive_path)])
        package = next((path for path in runtime.rglob('package.json')
                        if path.parent.name == 'cli' and path.parent.parent.name == '@langfuse'), None)
        if package is None:
            raise store.Refusal('Langfuse npm installation did not produce the official package')
        package = package.parent
        _normalize_owned_tree(package)
        metadata, entry, node_version, actual = _validate_candidate(package, node, run, assets)
        launcher = temporary / 'bin' / 'langfuse'
        launcher.parent.mkdir(mode=0o700)
        launcher.write_text(_launcher(node, Path(str(entry).replace(str(temporary), str(paths['root'])))), encoding='utf-8')
        launcher.chmod(0o700)
        # Validate the final paths before promotion, while all bytes are staged.
        final_package = Path(str(package).replace(str(temporary), str(paths['root'])))
        final_entry = Path(str(entry).replace(str(temporary), str(paths['root'])))
        receipt = {'schema': 1, 'source': 'managed', 'owned': True, 'version': VERSION,
                   'package': str(final_package), 'entry': str(final_entry), 'node': str(node),
                   'launcher': str(paths['launcher']), 'runtime': str(paths['runtime']),
                   'assets': actual, 'archive': {'url': ARCHIVE_URL, 'sha256': ARCHIVE_SHA256}}
        _write_receipt(temporary / 'installation.json', receipt)
        os.replace(temporary, paths['root'])
        return {'state': 'installed', 'source': 'managed', 'owned': True,
                'version': VERSION, 'path': str(paths['launcher']), 'node_version': node_version}


def setup_langfuse(home, input_fn=input, output=print, *, run=store.run,
                   which=shutil.which, fetch_bytes):
    output('Node.js 20+ and npm are required. The optional CLI can issue credential-capable queries.')
    try:
        answer = input_fn('Install optional Langfuse CLI? [y/N] ')
    except EOFError:
        answer = None
    if (answer or '').strip().lower() not in {'y', 'yes'}:
        return {'state': 'declined'}
    try:
        return ensure_installed(home, run=run, which=which, fetch_bytes=fetch_bytes)
    except (store.Refusal, store.DependencyError, OSError) as error:
        reason = _safe_reason(error)
        output(f'Langfuse CLI setup incomplete: {reason}; retry with `agd --home . installer langfuse`.')
        return {'state': 'incomplete', 'retry': 'agd --home . installer langfuse'}
