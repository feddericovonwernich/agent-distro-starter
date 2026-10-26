import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from . import store

ARCHIVE_SHA = 'dac2865837bcab9444a8636053f4f1406a3f9291'
ARCHIVE_URL = f'https://github.com/feddericovonwernich/open-relay/archive/{ARCHIVE_SHA}.tar.gz'
PACKAGE_NAME = 'open-relay'
PACKAGE_VERSION = '0.1.0'
MIN_NODE = (22, 19)
MARKER = 'relay-install.json'


def runtime_root(home: Path) -> Path:
    return Path(home) / 'relay'


def _lstat(path, kind=None):
    try:
        value = os.lstat(path)
    except OSError as error:
        raise store.Refusal(f'cannot inspect Relay path: {path}') from error
    if stat.S_ISLNK(value.st_mode):
        raise store.Refusal(f'Relay path is symlinked: {path}')
    if kind == 'dir' and not stat.S_ISDIR(value.st_mode):
        raise store.Refusal(f'Relay path must be a real directory: {path}')
    if kind == 'file' and not stat.S_ISREG(value.st_mode):
        raise store.Refusal(f'Relay path must be a regular file: {path}')
    return value


def _private_path(home, *parts, create=False):
    path = Path(home)
    if not os.path.lexists(path):
        if not create:
            return path.joinpath(*parts)
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
    _lstat(path, 'dir')
    for index, part in enumerate(parts):
        path /= part
        if not os.path.lexists(path):
            return path.joinpath(*parts[index + 1:])
        _lstat(path, 'dir' if index < len(parts) - 1 else None)
    return path


def _package(root, private=False):
    roots = (root / 'node_modules' / PACKAGE_NAME, root / 'lib' / 'node_modules' / PACKAGE_NAME)
    found = []
    for package in roots:
        if package.exists():
            if private:
                _private_path(root, *package.relative_to(root).parts)
            if package.is_symlink() or not package.is_dir():
                raise store.Refusal(f'Relay package is unsafe: {package}')
            found.append(package)
    if len(found) != 1:
        raise store.Refusal('Relay runtime is partial')
    return found[0]


def _metadata(package, private=False):
    metadata = package / 'package.json'
    cli = package / 'dist' / 'src' / 'cli.js'
    if private:
        _private_path(package, 'package.json')
        _private_path(package, 'dist', 'src', 'cli.js')
    if metadata.is_symlink() or cli.is_symlink() or not metadata.is_file() or not cli.is_file():
        raise store.Refusal('Relay runtime is partial')
    try:
        text = metadata.read_text(encoding='utf-8')
        value = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise store.Refusal('Relay package metadata is invalid') from error
    if value.get('name') != PACKAGE_NAME or value.get('version') != PACKAGE_VERSION:
        raise store.Refusal('Relay package identity is incompatible')
    return text, value


def _archive_provenance(package):
    lock = package.parent / '.package-lock.json'
    if not lock.is_file() or lock.is_symlink():
        return False
    try:
        data = json.loads(lock.read_text(encoding='utf-8'))
        entry = data.get('packages', {}).get(f'node_modules/{PACKAGE_NAME}', {})
        resolved = entry.get('resolved', '')
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return False
    return resolved == ARCHIVE_URL


def _marker(root, metadata_text, node_version):
    marker = root / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise store.Refusal('Relay runtime provenance is missing')
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise store.Refusal('Relay runtime provenance is invalid') from error
    expected = {'archive_sha': ARCHIVE_SHA, 'archive_url': ARCHIVE_URL,
                'package_version': PACKAGE_VERSION,
                'metadata_digest': hashlib.sha256(metadata_text.encode()).hexdigest()}
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise store.Refusal('Relay runtime provenance differs from the pinned archive')
    recorded = _parse_node(value.get('node_version'))
    if recorded is None or recorded < MIN_NODE or _parse_node(node_version) is None or _parse_node(node_version) < MIN_NODE:
        raise store.Refusal('Relay runtime Node provenance is incompatible')


def _validate(root, node_version, private=False, marker=False):
    if private:
        _private_path(root)
    package = _package(root, private)
    metadata_text, _ = _metadata(package, private)
    if marker:
        _marker(root, metadata_text, node_version)
    elif not private and not _archive_provenance(package):
        raise store.Refusal('Relay system provenance is unverified')
    return root


def _parse_node(value):
    if not isinstance(value, str):
        return None
    try:
        major, minor, *_ = value.strip().split('.')
        return int(major), int(minor)
    except ValueError:
        return None


def _node_version(run):
    result = run(['node', '-p', 'process.versions.node'])
    version = result.stdout.strip()
    if _parse_node(version) is None or _parse_node(version) < MIN_NODE:
        raise store.DependencyError('Node 22.19 or newer is required for Open Relay')
    return version


def _system_candidate(which):
    executable = which('relay')
    if not executable:
        return None
    try:
        cli = Path(executable).resolve(strict=True)
        package = next(parent for parent in (cli.parent, *cli.parents)
                       if parent.name == PACKAGE_NAME and (parent / 'package.json').is_file())
        root = package.parent.parent.parent if package.parent.parent.name == 'lib' else package.parent.parent
        _validate(root, '22.19.0', marker=False)
    except (OSError, StopIteration, store.Refusal):
        return None
    return root


def find_compatible_system() -> Path | None:
    return _system_candidate(shutil.which)


@contextlib.contextmanager
def _lock(home):
    state = _private_path(home, 'state', create=True)
    if not state.exists():
        try:
            state.mkdir(mode=0o700)
        except FileExistsError:
            pass
    _lstat(state, 'dir')
    lock = state / 'relay-install.lock'
    if os.path.lexists(lock):
        _lstat(lock, 'file')
    with lock.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield state


def ensure_runtime(home: Path, run=store.run, which=shutil.which) -> Path:
    node_version = _node_version(run)
    system = _system_candidate(which)
    if system is not None:
        return system
    home = Path(home)
    root = runtime_root(home)
    _private_path(home, create=True)
    with _lock(home) as state:
        if os.path.lexists(root):
            _lstat(root, 'dir')
            try:
                return _validate(root, node_version, private=True, marker=True)
            except store.Refusal as error:
                raise store.Refusal(f'private Relay runtime is partial or unsafe: {error}') from error
        stages = list(state.glob('relay-stage-*'))
        if stages:
            raise store.Refusal('partial Relay staging directory exists; preserving it')
        if not which('npm'):
            raise store.DependencyError('npm is not installed')
        stage = state / f'relay-stage-{uuid.uuid4().hex}'
        stage.mkdir(mode=0o700)
        run(['npm', 'install', '--global', '--prefix', str(stage), '--ignore-scripts', '--no-audit', '--no-fund', ARCHIVE_URL], timeout=120)
        _validate(stage, node_version, private=True)
        package = _package(stage, private=True)
        metadata_text, _ = _metadata(package, private=True)
        store.atomic_json(stage / MARKER, {'archive_sha': ARCHIVE_SHA, 'archive_url': ARCHIVE_URL,
                                           'package_version': PACKAGE_VERSION, 'node_version': node_version,
                                           'metadata_digest': hashlib.sha256(metadata_text.encode()).hexdigest()})
        _validate(stage, node_version, private=True, marker=True)
        os.replace(stage, root)
        return root


def _service_root(home, project_id):
    project_id = store.validate_id(project_id)
    root = Path(home)
    if not os.path.lexists(root):
        root.mkdir(parents=True, mode=0o700)
    _lstat(root, 'dir')
    for part in ('state', 'relay', 'projects', project_id):
        root /= part
        if os.path.lexists(root):
            _lstat(root, 'dir')
        else:
            root.mkdir(mode=0o700)
    return root


def _service_file(home, project_id):
    return _service_root(home, project_id)/'service.json'


def ensure_project_service(home, project_id, repository, run=store.run):
    project_id = store.validate_id(project_id)
    if not isinstance(repository, str) or repository.count('/') != 1 or not all(repository.split('/')):
        raise store.Refusal('Relay project repository must be OWNER/REPOSITORY')
    runtime = ensure_runtime(home)
    with store.lock(home):
        root = _service_root(home, project_id)
        path = root/'service.json'
        if os.path.lexists(path):
            _lstat(path, 'file')
            try:
                record = json.loads(path.read_text())
            except json.JSONDecodeError as error:
                raise store.Refusal('Relay project service record is invalid') from error
            if (not isinstance(record, dict) or record.get('schema') != 1 or
                    record.get('project') != project_id or record.get('repository') != repository or
                    record.get('runtime') != str(runtime) or record.get('root') != str(root) or
                    record.get('health') not in ('starting', 'healthy')):
                raise store.Refusal('Relay project service identity changed')
            return record
        relay_state = root/'.relay'
        if os.path.lexists(relay_state):
            _lstat(relay_state, 'dir')
        else:
            run([str(runtime/'bin'/'relay'), 'init', '--github', repository], cwd=root, timeout=30)
            _lstat(relay_state, 'dir')
        record = {'schema': 1, 'project': project_id, 'repository': repository,
                  'runtime': str(runtime), 'root': str(root), 'health': 'starting'}
        store.atomic_json(path, record)
        return record
