"""Home-scoped retirement admission and recoverable uninstall authority."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

_PHASES = {'preparing', 'quiescing', 'removing', 'verifying', 'completed', 'cancelled', 'blocked'}
_TERMINAL = {'completed', 'cancelled'}
_HELD = {}


def _refuse(message):
    from .store import Refusal
    raise Refusal(message)


def _path(home):
    value = Path(home)
    if not value.is_absolute():
        _refuse('uninstall home must be absolute')
    value = Path(os.path.abspath(value))
    return value.parent.resolve() / value.name


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def operation_dir(home):
    root = _path(home)
    key = hashlib.sha256(str(root).encode()).hexdigest()[:32]
    return root.parent / f'.agd-uninstall-{key}'


def _directory(path, *, private=False):
    try:
        value = os.lstat(path)
    except OSError as error:
        _refuse(f'directory is unavailable: {path}: {error.strerror}')
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
        _refuse(f'directory identity is unsafe: {path}')
    if private and stat.S_IMODE(value.st_mode) != 0o700:
        _refuse(f'uninstall operation directory is not private: {path}')
    return value


def _json(path, *, private=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8') as stream:
            value = os.fstat(stream.fileno())
            if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                    or value.st_nlink != 1 or private and value.st_mode & 0o077):
                _refuse(f'uninstall identity file is unsafe: {path}')
            return json.load(stream)
    except (OSError, ValueError, UnicodeError) as error:
        _refuse(f'uninstall identity file is unreadable or malformed: {path}: {type(error).__name__}')


def target_identity(home):
    root = _path(home)
    if root in {Path('/'), Path.home().resolve(), Path('/tmp'), Path('/var'), Path('/run'), Path('/home')}:
        _refuse('refusing dangerous uninstall target')
    identity = _directory(root)
    if os.path.ismount(root):
        _refuse('uninstall target is a mount point')
    _directory(root / 'data')
    marker = _json(root / 'data/home.json')
    try:
        valid = (isinstance(marker, dict) and marker.get('schema') == 1
                 and str(uuid.UUID(marker['home_id'])) == marker['home_id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        _refuse('uninstall home identity is invalid')
    return {'canonical_home': str(root), 'home_id': marker['home_id'], 'uid': os.getuid(),
            'device': identity.st_dev, 'inode': identity.st_ino}


def _same_target(home, expected, *, allow_missing=False):
    root = _path(home)
    if expected.get('canonical_home') != str(root) or expected.get('uid') != os.getuid():
        _refuse('uninstall operation belongs to a different home or user')
    try:
        current = os.lstat(root)
    except FileNotFoundError:
        if allow_missing:
            return
        _refuse('original uninstall home disappeared before removal')
    if (not stat.S_ISDIR(current.st_mode) or current.st_uid != expected['uid']
            or current.st_dev != expected['device'] or current.st_ino != expected['inode']):
        _refuse('original uninstall home identity changed; replacement retained')
    marker = root / 'data/home.json'
    if not os.path.lexists(marker) and allow_missing:
        return
    if target_identity(root) != expected:
        _refuse('uninstall home marker changed; replacement retained')


def _inventory_digest(inventory):
    return fingerprint({key: inventory[key] for key in ('target', 'resources', 'work_loss')})


def _validate_inventory(value):
    if (not isinstance(value, dict) or value.get('schema') != 1
            or not isinstance(value.get('resources'), list)
            or not isinstance(value.get('work_loss'), list)
            or value.get('digest') != _inventory_digest(value)):
        _refuse('uninstall inventory approval is invalid')
    keys = [row.get('key') for row in value['resources'] if isinstance(row, dict)]
    if (len(keys) != len(value['resources']) or any(not isinstance(key, str) or not key for key in keys)
            or len(set(keys)) != len(keys)):
        _refuse('uninstall inventory resource identities are invalid')


def _validate_executor(root, operation):
    executor = operation.get('executor')
    if not isinstance(executor, dict) or not isinstance(executor.get('files'), dict) or not executor['files']:
        _refuse('uninstall executor is incomplete')
    python = Path(executor.get('python', ''))
    if not python.is_absolute() or python.resolve().is_relative_to(root):
        _refuse('uninstall interpreter is inside the deletion target')
    directory = operation_dir(root)
    for relative, expected in executor['files'].items():
        name = Path(relative)
        if name.is_absolute() or not name.parts or '..' in name.parts:
            _refuse('uninstall staged path escapes its operation directory')
        path = directory
        for part in name.parts[:-1]:
            path /= part
            _directory(path)
        path /= name.name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                mode = os.fstat(stream.fileno())
                if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid():
                    _refuse('uninstall executor file identity is unsafe')
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError:
            _refuse('uninstall executor is unavailable or redirected')
        if actual != expected:
            _refuse('uninstall executor checksum changed')
    approved = {Path(name) for name in executor['files']}
    parents = {parent for name in approved for parent in name.parents if parent != Path('.')}
    for top in {name.parts[0] for name in approved}:
        for path in (directory / top).rglob('*'):
            relative = path.relative_to(directory)
            mode = path.lstat()
            if stat.S_ISLNK(mode.st_mode):
                _refuse('uninstall executor contains an unbound redirect')
            if stat.S_ISDIR(mode.st_mode):
                if relative not in parents:
                    _refuse('uninstall executor contains an unbound import directory')
            elif relative not in approved:
                _refuse('uninstall executor contains an unbound import file')


def read_operation(home):
    root = _path(home)
    directory = operation_dir(root)
    if not os.path.lexists(directory):
        return None
    _directory(directory, private=True)
    value = _json(directory / 'operation.json', private=True)
    try:
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('phase') not in _PHASES
                or not isinstance(value.get('irreversible_started'), bool)
                or not isinstance(value.get('completed'), list)):
            _refuse('uninstall journal schema is invalid')
        _validate_inventory(value['inventory'])
        if value['target'] != value['inventory']['target']:
            _refuse('uninstall target differs from its approval')
        target = value['target']
        if (target['canonical_home'] != str(root) or target['uid'] != os.getuid()
                or not isinstance(target['home_id'], str)
                or str(uuid.UUID(target['home_id'])) != target['home_id']
                or any(type(target[key]) is not int or target[key] < 0 for key in ('device', 'inode'))):
            _refuse('uninstall journal target identity is invalid')
        if (value['coordinator']['uid'] != os.getuid()
                or not isinstance(value['coordinator']['pid'], int)
                or not isinstance(value['coordinator']['start'], str)):
            _refuse('uninstall coordinator identity is invalid')
        keys = {row['key'] for row in value['inventory']['resources']}
        if any(key not in keys for key in value['completed']):
            _refuse('uninstall completion records are outside the approved scope')
        if value['phase'] not in _TERMINAL:
            _same_target(root, target, allow_missing=value['irreversible_started'])
            state = root / 'state'
            if os.path.lexists(state):
                _directory(state)
            pointer = state / 'uninstall.json'
            if os.path.lexists(pointer):
                hint = _json(pointer, private=True)
                if (not isinstance(hint, dict) or hint.get('schema') != 1
                        or hint.get('target') != target
                        or hint.get('operation') != str(directory)
                        or hint.get('phase') not in _PHASES):
                    _refuse('in-home retirement pointer conflicts with its authoritative journal')
            if value['executor'] is not None:
                _validate_executor(root, value)
            elif value['phase'] != 'preparing':
                _refuse('uninstall executor is missing after preparation')
        return value
    except (KeyError, TypeError, ValueError):
        _refuse('uninstall journal is incomplete or malformed')


def require_active(home):
    value = read_operation(home)
    if value and value['phase'] not in _TERMINAL:
        _refuse(f'distro retirement is in progress; resume {operation_dir(home)}')


@contextlib.contextmanager
def _parent_lock(root, exclusive=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(root.parent, flags)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)
    except BlockingIOError:
        _refuse('another startup or uninstall is publishing admission')


def _activity(root, *, initializing=False, exclusive=False):
    if not os.path.lexists(root):
        if not initializing:
            _refuse('distro home is absent; refusing to recreate it')
        root.mkdir(mode=0o700, exist_ok=True)
    _directory(root)
    state = root / 'state'
    if not os.path.lexists(state):
        state.mkdir(mode=0o700, exist_ok=True)
    _directory(state)
    descriptor = None
    try:
        descriptor = os.open(state / 'activity.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        mode = os.fstat(descriptor)
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_nlink != 1:
            os.close(descriptor)
            _refuse('distro admission lock is unsafe')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH)
        return descriptor
    except BlockingIOError:
        if descriptor is not None:
            os.close(descriptor)
        _refuse('distro creation is busy; retry uninstall after it settles')
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        _refuse('distro admission lock is unavailable')


@contextlib.contextmanager
def admission(home, *, initializing=False):
    root = _path(home)
    require_active(root)
    if initializing and not root.parent.exists():
        root.parent.mkdir(parents=True, mode=0o700)
    with _parent_lock(root):
        require_active(root)
        descriptor = _activity(root, initializing=initializing)
        original = os.lstat(root)
    try:
        require_active(root)
        current = os.lstat(root)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            _refuse('distro changed while startup admission was acquired')
        yield
    finally:
        os.close(descriptor)


def _actor():
    from .supervision import process_start
    return {'pid': os.getpid(), 'start': process_start(os.getpid()), 'uid': os.getuid()}


def _write(root, operation):
    from . import store
    store.atomic_json(operation_dir(root) / 'operation.json', operation)
    if root.is_dir() and not root.is_symlink():
        state = root / 'state'
        if not os.path.lexists(state) and operation['irreversible_started']:
            return
        _directory(state)
        store.atomic_json(state / 'uninstall.json', {
            'schema': 1, 'target': operation['target'], 'phase': operation['phase'],
            'operation': str(operation_dir(root)),
        })


@contextlib.contextmanager
def retirement(home, inventory, *, discard_digest):
    from . import store, supervision
    root = _path(home)
    _validate_inventory(inventory)
    if inventory.get('blockers') or any(row.get('ownership') in {'foreign', 'unknown'}
                                       or row.get('blockers') for row in inventory['resources']):
        _refuse('uninstall inventory has unresolved ownership or resource blockers')
    if inventory['work_loss'] and (not discard_digest or discard_digest != inventory.get('discard_digest')):
        _refuse('separate approval is required to discard local work')
    activity = descriptor = None
    directory = operation_dir(root)
    with _parent_lock(root, exclusive=True):
        previous = read_operation(root)
        if previous is None:
            if target_identity(root) != inventory['target']:
                _refuse('uninstall target changed after preview')
        else:
            _same_target(root, previous['target'], allow_missing=previous['irreversible_started'])
            actor = previous['coordinator']
            if actor != _actor() and not supervision.gone(actor['pid'], actor['start']):
                _refuse('another live coordinator owns this uninstall')
        try:
            if root.exists():
                activity = _activity(root, exclusive=True)
            if not directory.exists():
                directory.mkdir(mode=0o700)
            _directory(directory, private=True)
            descriptor = os.open(directory / 'coordinator.lock',
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock_mode = os.fstat(descriptor)
            if not stat.S_ISREG(lock_mode.st_mode) or lock_mode.st_uid != os.getuid():
                _refuse('uninstall coordinator lock is unsafe')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _refuse('another coordinator holds the uninstall lock')
            if str(root) in _HELD:
                _refuse('uninstall operation is already active in this process')
            operation = {
                'schema': 1, 'target': inventory['target'], 'coordinator': _actor(),
                'phase': 'preparing', 'inventory': inventory, 'discard_digest': discard_digest,
                'completed': [], 'irreversible_started': False, 'executor': None, 'failure': None,
            }
            if previous and previous['phase'] not in _TERMINAL:
                if previous['target'] != inventory['target']:
                    _refuse('pending uninstall does not match this home')
                operation.update(phase=previous['phase'], completed=previous['completed'],
                                 irreversible_started=previous['irreversible_started'],
                                 executor=previous['executor'])
            with store.lock(root) if root.exists() else contextlib.nullcontext():
                _write(root, operation)
            _HELD[str(root)] = descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            if activity is not None:
                os.close(activity)
    try:
        yield operation
    finally:
        _HELD.pop(str(root), None)
        os.close(descriptor)


def _owned(root, operation):
    current = read_operation(root)
    if (current is None or current['coordinator'] != _actor() or str(root) not in _HELD
            or current['target'] != operation.get('target')
            or current['inventory']['digest'] != operation.get('inventory', {}).get('digest')):
        _refuse('uninstall authority is not held by this coordinator')
    return current


def require_scope(home, operation, key):
    root = _path(home)
    current = _owned(root, operation)
    if current['phase'] not in {'quiescing', 'removing', 'verifying'}:
        _refuse('uninstall executor has not entered teardown')
    _validate_executor(root, current)
    for resource in current['inventory']['resources']:
        if resource['key'] == key:
            if resource.get('ownership') != 'owned' or resource.get('presence') == 'unknown' or resource.get('blockers'):
                _refuse('resource ownership is not proven')
            return resource
    _refuse('resource was not approved for uninstall')


def checkpoint(home, operation, *, phase, completed=None, irreversible=False, error=None):
    root = _path(home)
    current = _owned(root, operation)
    if phase not in _PHASES:
        _refuse('unknown uninstall phase')
    if phase == 'cancelled' and current['irreversible_started']:
        _refuse('irreversible uninstall cannot be cancelled into a usable home')
    if operation.get('executor') != current['executor']:
        if current['phase'] != 'preparing':
            _refuse('uninstall executor cannot change after preparation')
        current['executor'] = operation.get('executor')
    if phase not in {'preparing', 'cancelled', 'blocked'}:
        _validate_executor(root, current)
    if completed is not None:
        if completed not in {row['key'] for row in current['inventory']['resources']}:
            _refuse('cannot complete a resource outside uninstall scope')
        if completed not in current['completed']:
            current['completed'].append(completed)
    current.update(phase=phase, irreversible_started=current['irreversible_started'] or irreversible,
                   failure=error)
    _write(root, current)
    operation.clear()
    operation.update(current)
    return operation
