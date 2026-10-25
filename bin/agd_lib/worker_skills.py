import json
import os
import stat
from pathlib import Path

from . import store


_CONFIG_KEYS = {'default_profile', 'profiles'}
_PROFILE_KEYS = {'skills'}


def _refusal(message):
    raise store.Refusal(f'worker skills: {message}')


def _private_path(home, *parts):
    path = Path(home)
    try:
        identity = os.lstat(path)
    except OSError as error:
        raise store.Refusal('worker skills: private home is unavailable') from error
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
        _refusal('private home must be a real directory')
    for index, part in enumerate(parts):
        path /= part
        try:
            identity = os.lstat(path)
        except FileNotFoundError:
            return path.joinpath(*parts[index + 1:])
        except OSError as error:
            raise store.Refusal(f'worker skills: cannot inspect {path}') from error
        if stat.S_ISLNK(identity.st_mode):
            _refusal(f'private-home path is symlinked: {path}')
        if index < len(parts) - 1 and not stat.S_ISDIR(identity.st_mode):
            _refusal(f'private-home path component must be a real directory: {path}')
    return path


def _json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _refusal(f'duplicate JSON key in {path}')
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(), object_pairs_hook=pairs)
    except (OSError, json.JSONDecodeError) as error:
        raise store.Refusal(f'worker skills: invalid JSON in {path}') from error


def _id(value, what):
    if not isinstance(value, str):
        _refusal(f'{what} must be an ID')
    try:
        return store.validate_id(value)
    except ValueError as error:
        raise store.Refusal(f'{what} has an invalid ID') from error


def _frontmatter(path, skill_id):
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise store.Refusal(f'worker skills: cannot read {path}') from error
    if not lines or lines[0] != '---':
        _refusal(f'{skill_id}: SKILL.md needs simple frontmatter')
    try:
        end = lines.index('---', 1)
    except ValueError:
        _refusal(f'{skill_id}: SKILL.md frontmatter is unterminated')
    values = {}
    for line in lines[1:end]:
        if ': ' not in line:
            _refusal(f'{skill_id}: unsupported frontmatter')
        key, value = line.split(': ', 1)
        if key not in ('name', 'description') or key in values or not value:
            _refusal(f'{skill_id}: unsupported frontmatter')
        if key == 'name':
            if value != value.strip() or value.startswith('"'):
                _refusal(f'{skill_id}: name must be a plain ID')
            values[key] = _id(value, f'{skill_id} name')
        elif value.startswith('"'):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as error:
                raise store.Refusal(f'worker skills: {skill_id}: invalid description') from error
            if not isinstance(decoded, str):
                _refusal(f'{skill_id}: description must be a string')
            values[key] = decoded
        else:
            values[key] = value
    if set(values) != {'name', 'description'} or not values['description'].strip() or '\n' in values['description']:
        _refusal(f'{skill_id}: frontmatter needs a nonempty single-line name and description')
    if values['name'] != skill_id:
        _refusal(f'{skill_id}: catalog directory and frontmatter name differ')
    return values['description']


def _bundle(path, skill_id):
    try:
        root = os.lstat(path)
    except OSError as error:
        raise store.Refusal(f'worker skills: cannot stat {skill_id}') from error
    if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode):
        _refusal(f'{skill_id}: bundle must be a real directory')
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise store.Refusal(f'worker skills: cannot inspect {skill_id}') from error
        for entry in entries:
            if entry.name == '.git':
                _refusal(f'{skill_id}: .git content is forbidden')
            identity = os.lstat(entry)
            if stat.S_ISLNK(identity.st_mode):
                _refusal(f'{skill_id}: symlinks are forbidden')
            if stat.S_ISDIR(identity.st_mode):
                stack.append(entry)
            elif stat.S_ISREG(identity.st_mode):
                if identity.st_nlink != 1:
                    _refusal(f'{skill_id}: hard-linked files are forbidden')
            else:
                _refusal(f'{skill_id}: bundle contains a non-regular file')
    skill = path/'SKILL.md'
    if not skill.is_file() or skill.is_symlink():
        _refusal(f'{skill_id}: bundle lacks a regular SKILL.md')
    return _frontmatter(skill, skill_id)


def catalog(home: Path) -> list[dict]:
    root = _private_path(home, 'config', 'worker-skills')
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        _refusal('catalog must be a real directory')
    result = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        skill_id = _id(path.name, 'catalog entry')
        if not skill_id.startswith('agd-'):
            _refusal(f'{skill_id}: provisioned skill IDs must start agd-')
        result.append({'id': skill_id, 'description': _bundle(path, skill_id)})
    return result


def profiles(home: Path) -> dict:
    path = _private_path(home, 'config', 'workers.json')
    if not path.exists():
        return {'default_profile': None, 'profiles': {}}
    data = _json(path)
    if not isinstance(data, dict) or set(data) != _CONFIG_KEYS:
        _refusal('workers.json must contain only default_profile and profiles')
    default = _id(data['default_profile'], 'default_profile')
    if not isinstance(data['profiles'], dict):
        _refusal('profiles must be an object')
    result = {}
    for name, profile in data['profiles'].items():
        name = _id(name, 'profile')
        if not isinstance(profile, dict) or set(profile) != _PROFILE_KEYS or not isinstance(profile['skills'], list):
            _refusal(f'{name}: profile must contain only a skills array')
        skills = [_id(value, f'{name} skill') for value in profile['skills']]
        if len(skills) != len(set(skills)):
            _refusal(f'{name}: duplicate skill')
        result[name] = {'skills': skills}
    if default not in result:
        _refusal('default_profile does not name a profile')
    return {'default_profile': default, 'profiles': result}


def resolve(home: Path, *, profile: str | None = None, skill_mode: str = 'profile',
            skills: tuple[str, ...] = ()) -> dict:
    if skill_mode not in ('profile', 'curated'):
        _refusal('unsupported skill mode')
    if profile is not None and skill_mode != 'profile':
        _refusal('profiles and curated skills cannot be combined')
    if skill_mode == 'profile' and skills:
        _refusal('individual skills require curated mode')
    if skill_mode == 'curated' and not skills:
        _refusal('curated mode requires at least one skill')
    config_path = _private_path(home, 'config', 'workers.json')
    if skill_mode == 'profile' and profile is None and not config_path.exists():
        return {'schema': 1, 'mode': 'none', 'profile': None, 'skills': []}
    available = {entry['id'] for entry in catalog(home)}
    if skill_mode == 'curated':
        selected = [_id(value, 'selected skill') for value in skills]
        if len(selected) != len(set(selected)):
            _refusal('duplicate selected skill')
        missing = set(selected) - available
        if missing:
            _refusal(f'unknown selected skill: {sorted(missing)[0]}')
        return {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': sorted(selected)}
    config = profiles(home)
    requested = _id(profile, 'profile') if profile is not None else config['default_profile']
    if requested not in config['profiles']:
        _refusal(f'unknown profile: {requested}')
    for name, item in config['profiles'].items():
        missing = set(item['skills']) - available
        if missing:
            _refusal(f'{name}: missing catalog skill {sorted(missing)[0]}')
    return {'schema': 1, 'mode': 'profile', 'profile': requested,
            'skills': sorted(config['profiles'][requested]['skills'])}


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _files(root):
    result, stack = [], [root]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            identity = os.lstat(path)
            relative = str(path.relative_to(root))
            if stat.S_ISLNK(identity.st_mode):
                _refusal(f'worker skill tree contains a symlink: {relative}')
            if stat.S_ISDIR(identity.st_mode):
                result.append({'path': relative, 'kind': 'directory', 'mode': identity.st_mode & 0o777})
                stack.append(path)
            elif stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1:
                result.append({'path': relative, 'kind': 'file', 'size': identity.st_size,
                               'mode': identity.st_mode & 0o777, 'sha256': _sha256(path)})
            else:
                _refusal(f'worker skill tree contains an unsafe entry: {relative}')
    return sorted(result, key=lambda item: item['path'])


def _task_dir(home, task):
    return _private_path(home, 'data', 'tasks', task['id'])


def _journal(home, task):
    return _task_dir(home, task)/'worker-skills.json'


def _snapshot(home, task):
    return _task_dir(home, task)/'worker-skills'


def _copy_tree(source, destination):
    import shutil
    destination.mkdir(parents=True)
    stack = [source]
    while stack:
        directory = stack.pop()
        for item in directory.iterdir():
            target = destination/item.relative_to(source)
            identity = os.lstat(item)
            if stat.S_ISDIR(identity.st_mode):
                target.mkdir(exist_ok=True)
                stack.append(item)
            elif stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
            else:
                _refusal(f'worker skill source contains an unsafe entry: {item}')


def _manifest(home, task):
    path = _journal(home, task)
    if not path.is_file() or path.is_symlink():
        _refusal('worker skill journal is missing')
    data = _json(path)
    from . import harnesses
    if (not isinstance(data, dict) or data.get('schema') != 1 or
            data.get('harness') != task.get('harness') or
            data.get('roots') != list(harnesses.worker_skill_roots(task['harness'])) or
            not isinstance(data.get('skills'), list)):
        _refusal('invalid worker skill journal')
    snapshot_root = _snapshot(home, task)
    root = Path(task['worktree'])/data['roots'][0]
    for record in data['skills']:
        if not isinstance(record, dict) or _id(record.get('id'), 'journal skill') != record['id']:
            _refusal('invalid worker skill journal record')
        if Path(record.get('source', '')).resolve() != _private_path(home, 'config', 'worker-skills', record['id']).resolve():
            _refusal('worker skill source path escaped catalog')
        if Path(record.get('snapshot', '')).resolve() != (snapshot_root/record['id']).resolve():
            _refusal('worker skill snapshot path escaped task data')
        if Path(record.get('destination', '')).resolve() != (root/record['id']).resolve():
            _refusal('worker skill destination escaped task worktree')
    return data


def _native_names(root):
    if root.parent.is_symlink():
        _refusal(f'native skill parent is symlinked: {root.parent}')
    if root.is_symlink():
        _refusal(f'native skill root is symlinked: {root}')
    if not root.exists():
        return set()
    names, stack = set(), [root]
    while stack:
        directory = stack.pop()
        for item in directory.iterdir():
            identity = os.lstat(item)
            if stat.S_ISLNK(identity.st_mode):
                _refusal(f'native skill root contains a symlink: {item}')
            if stat.S_ISDIR(identity.st_mode):
                stack.append(item)
            elif item.name == 'SKILL.md' and stat.S_ISREG(identity.st_mode):
                name = item.parent.name
                lines = item.read_text().splitlines()
                if lines and lines[0] == '---':
                    for line in lines[1:]:
                        if line == '---':
                            break
                        if line.startswith('name: '):
                            name = line[6:]
                            break
                names.add(name)
    return names


def prepare(home: Path, task: dict, selection: dict) -> dict:
    if selection.get('schema') != 1 or not isinstance(selection.get('skills'), list):
        _refusal('invalid worker skill selection')
    journal = _journal(home, task)
    if journal.exists():
        existing = _manifest(home, task)
        if existing.get('selection') != selection:
            _refusal('pending worker skill journal selects different skills')
        for record in existing['skills']:
            _check(record, Path(record['source']))
        ensure(home, task)
        return _manifest(home, task)
    frozen = _snapshot(home, task)
    if frozen.exists() or frozen.is_symlink():
        _refusal('worker skill snapshot destination already exists')
    from . import harnesses
    roots = harnesses.worker_skill_roots(task['harness'])
    root = Path(task['worktree'])/roots[0]
    records = []
    available = {row['id'] for row in catalog(home)}
    for skill_id in selection['skills']:
        if skill_id not in available:
            _refusal(f'missing catalog skill {skill_id}')
        ancestor = Path(task['worktree']).resolve()
        while True:
            native = ancestor/roots[0]
            if skill_id in _native_names(native):
                _refusal(f'worker skill collides with native project discovery: {native}')
            if ancestor.parent == ancestor:
                break
            ancestor = ancestor.parent
        source = _private_path(home, 'config', 'worker-skills', skill_id)
        destination = root/skill_id
        if destination.exists() or destination.is_symlink():
            _refusal(f'worker skill destination exists: {destination}')
        frozen_skill = frozen/skill_id
        records.append({'id': skill_id, 'source': str(source), 'snapshot': str(frozen_skill),
                        'destination': str(destination), 'files': _files(source)})
    manifest = {'schema': 1, 'selection': selection, 'harness': task['harness'],
                'roots': list(roots), 'skills': records, 'state': 'pending',
                'excludes': [str(Path(record['destination']).relative_to(task['worktree']))
                             for record in records]}
    store.atomic_json(journal, manifest)
    for index, record in enumerate(records):
        _copy_tree(Path(record['source']), Path(record['snapshot']))
        _check(record, Path(record['snapshot']))
    _exclude(home, task, manifest['excludes'], True)
    for record in records:
        _copy_tree(Path(record['snapshot']), Path(record['destination']))
    manifest['state'] = 'complete'
    store.atomic_json(journal, manifest)
    return manifest


def _check(record, root):
    if _files(root) != record['files']:
        _refusal(f'worker skill artifacts changed: {record["id"]}')


def ensure(home: Path, task: dict) -> None:
    manifest = _manifest(home, task)
    for record in manifest['skills']:
        snapshot = Path(record['snapshot'])
        _check(record, snapshot)
        destination = Path(record['destination'])
        if destination.exists() or destination.is_symlink():
            _check(record, destination)
        else:
            _copy_tree(snapshot, destination)
    if manifest['state'] != 'complete':
        manifest['state'] = 'complete'
        store.atomic_json(_journal(home, task), manifest)


def verify(home: Path, task: dict) -> None:
    for record in _manifest(home, task)['skills']:
        _check(record, Path(record['snapshot']))
        destination = Path(record['destination'])
        if not destination.is_dir() or destination.is_symlink():
            _refusal(f'worker skill artifact is missing: {record["id"]}')
        _check(record, destination)
        relative = str(destination.relative_to(task['worktree']))
        if store.run(['git', 'ls-files', '-z', '--', relative], cwd=task['worktree'], check=False).stdout:
            _refusal(f'worker skill artifact is tracked: {record["id"]}')


def remove(home: Path, task: dict) -> None:
    import shutil
    manifest = _manifest(home, task)
    if manifest.get('state') != 'removing':
        verify(home, task)
        manifest['state'] = 'removing'
        store.atomic_json(_journal(home, task), manifest)
    for record in manifest['skills']:
        destination = Path(record['destination'])
        if destination.exists() or destination.is_symlink():
            _check(record, destination)
            shutil.rmtree(destination)
    _exclude(home, task, manifest.get('excludes', []), False)
    manifest['state'] = 'removed'
    store.atomic_json(_journal(home, task), manifest)


def _exclude(home, task, paths, add):
    import fcntl
    common = Path(store.git(task['worktree'], 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    info = common/'info'
    info.mkdir(exist_ok=True)
    lock = info/'agd-worker-skills.lock'
    registry = info/'agd-worker-skills.json'
    key = f'{_private_path(home, "data", "home.json")}:{task["id"]}:{task.get("lease_id", "")}'
    with lock.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        data = _json(registry) if registry.exists() else {}
        owners = data.setdefault('owners', {})
        managed = set(data.setdefault('managed', []))
        excludes = info/'exclude'
        if add:
            owners[key] = paths
            data['managed'] = sorted(managed)
            store.atomic_json(registry, data)
            text = excludes.read_text() if excludes.exists() else ''
            for path in paths:
                line = f'/{path}/\n'
                if line not in text:
                    text += line
                    managed.add(path)
            excludes.write_text(text)
        else:
            owners.pop(key, None)
            remaining = {path for values in owners.values() for path in values}
            if excludes.exists():
                excludes.write_text(''.join(
                    line for line in excludes.read_text().splitlines(True)
                    if not (line.strip('/\n') in managed - remaining)))
            managed.intersection_update(remaining)
        data['managed'] = sorted(managed)
        store.atomic_json(registry, data)
