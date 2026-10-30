"""Native OMP storage and execution boundary for one distro."""
import json
import os
import stat
from pathlib import Path
from typing import Mapping

from . import store

PLUGIN_SPECS = {
    'ponytail@ponytail': {
        'marketplace': 'ponytail',
        'source': 'DietrichGebert/ponytail',
        'package': '@dietrichgebert/ponytail',
    },
    'superpowers@superpowers-dev': {
        'marketplace': 'superpowers-dev',
        'source': 'obra/superpowers',
        'package': 'superpowers',
    },
}


def _root(home) -> Path:
    root = store.home(home)
    current = root
    for part in ('data', 'omp'):
        current /= part
        try:
            identity = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
            raise store.Refusal('OMP runtime root is unsafe')
    return root / 'data' / 'omp'


def _data_root(home) -> Path:
    return _root(home) / 'xdg' / 'data' / 'omp'


def _plugins(home) -> Path:
    return _data_root(home) / 'plugins'


def agent_dir(home) -> Path:
    return _root(home) / 'home' / '.omp' / 'agent'


def runtime_directories(home) -> tuple[Path, ...]:
    runtime = _root(home)
    return (
        agent_dir(home),
        runtime / 'xdg' / 'data' / 'omp',
        runtime / 'xdg' / 'state' / 'omp',
        runtime / 'xdg' / 'cache' / 'omp',
    )


def environment(home, inherited: Mapping[str, str]) -> dict[str, str]:
    """Return inherited with every OMP storage control fixed to this distro."""
    root = store.home(home)
    runtime = _root(root)
    for directory in runtime_directories(root):
        current = root
        for part in directory.relative_to(root).parts:
            current /= part
            try:
                identity = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
                raise store.Refusal('OMP runtime root is unsafe')
    result = dict(inherited)
    result.update(
        HOME=str(runtime / 'home'),
        PI_CONFIG_DIR='.omp',
        PI_CODING_AGENT_DIR=str(agent_dir(root)),
        OMP_PROFILE='default',
        PI_PROFILE='default',
        XDG_DATA_HOME=str(runtime / 'xdg' / 'data'),
        XDG_STATE_HOME=str(runtime / 'xdg' / 'state'),
        XDG_CACHE_HOME=str(runtime / 'xdg' / 'cache'),
    )
    account_home = Path(inherited.get('HOME', ''))
    if account_home.is_absolute():
        result.setdefault('XDG_CONFIG_HOME', str(account_home / '.config'))
    return result


def _private_directory(home: Path, destination: Path) -> None:
    current = home
    for part in destination.relative_to(home).parts:
        current /= part
        try:
            identity = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            identity = current.lstat()
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
            raise store.Refusal('OMP runtime directory is unsafe')
        os.chmod(current, 0o700)


def _initialize_git_config(home: Path, inherited: Mapping[str, str]) -> None:
    account_home = Path(inherited.get('HOME', ''))
    if not account_home.is_absolute() or inherited.get('GIT_CONFIG_GLOBAL'):
        return
    source = account_home / '.gitconfig'
    if not source.is_file():
        return
    destination = _root(home) / 'home' / '.gitconfig'
    content = f'[include]\n\tpath = {json.dumps(str(source))}\n'
    try:
        identity = destination.lstat()
    except FileNotFoundError:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return
    if (not stat.S_ISREG(identity.st_mode) or destination.read_text() != content):
        raise store.Refusal('OMP Git configuration bridge is unsafe')


def initialize(home, executable: str, *, inherited: Mapping[str, str] | None = None, run=store.run) -> None:
    root = store.home(home)
    for directory in runtime_directories(root):
        _private_directory(root, directory)
    effective_inherited = os.environ if inherited is None else inherited
    _initialize_git_config(root, effective_inherited)
    marketplace_registry = _data_root(root) / 'marketplaces.json'
    try:
        identity = marketplace_registry.lstat()
    except FileNotFoundError:
        store.atomic_json(marketplace_registry, {'version': 1, 'marketplaces': []})
    else:
        if not stat.S_ISREG(identity.st_mode):
            raise store.Refusal('OMP marketplace registry is unsafe')
    run([executable, 'config', 'init-xdg'], cwd=root,
        env=environment(root, effective_inherited))


def executable(home) -> str:
    from . import installer
    entry = installer.read_dependencies(home).get('omp')
    if entry is None:
        raise store.DependencyError('OMP dependency receipt is unavailable')
    return entry['path']


def validate_native_args(arguments) -> None:
    forbidden = ('--profile', '--alias', '--cwd')
    for argument in arguments:
        if argument == '--':
            break
        if argument in forbidden or any(argument.startswith(flag + '=') for flag in forbidden):
            raise store.Refusal('fixed distro OMP runtime refuses profile or project escape controls')


def _read_json(path: Path, label: str, *, optional=False):
    try:
        identity = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise store.Refusal(f'native plugin {label} is missing')
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode):
        raise store.Refusal(f'native plugin {label} is unsafe')
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise store.Refusal(f'native plugin {label} is malformed') from error


def _confined(path_value, root: Path, label: str, *, directory=True) -> Path:
    if not isinstance(path_value, str):
        raise store.Refusal(f'native plugin {label} is malformed')
    path = Path(path_value)
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise store.Refusal(f'native plugin {label} escapes the distro') from error
    if path != resolved:
        raise store.Refusal(f'native plugin {label} uses a non-canonical path')
    current = root.resolve(strict=True)
    for part in relative.parts:
        current /= part
        identity = current.lstat()
        if stat.S_ISLNK(identity.st_mode):
            raise store.Refusal(f'native plugin {label} uses a symlink')
    identity = resolved.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(identity.st_mode):
        raise store.Refusal(f'native plugin {label} has the wrong type')
    return resolved

def _confined_json(path: Path, root: Path, label: str, *, optional=False):
    if optional and not path.exists() and not path.is_symlink():
        return None
    _confined(str(path), root, label, directory=False)
    return _read_json(path, label, optional=optional)




def _marketplaces(home: Path, required_ids) -> dict[str, dict]:
    value = _confined_json(
        _data_root(home) / 'marketplaces.json', _root(home),
        'marketplace registry')
    if (not isinstance(value, dict) or set(value) != {'version', 'marketplaces'}
            or value.get('version') != 1 or not isinstance(value['marketplaces'], list)):
        raise store.Refusal('native plugin marketplace registry is malformed')
    by_name = {}
    for row in value['marketplaces']:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            raise store.Refusal('native plugin marketplace registry is malformed')
        by_name.setdefault(row['name'], []).append(row)
    result = {}
    for plugin_id in required_ids:
        spec = PLUGIN_SPECS[plugin_id]
        rows = by_name.get(spec['marketplace'], [])
        if len(rows) != 1:
            raise store.Refusal(f'native plugin marketplace {spec["marketplace"]} is missing or ambiguous')
        row = rows[0]
        if row.get('sourceType') != 'github' or row.get('sourceUri') != spec['source']:
            raise store.Refusal(f'native plugin marketplace {spec["marketplace"]} has a conflicting publisher')
        _confined(row.get('catalogPath'), _root(home), 'marketplace catalog', directory=False)
        result[plugin_id] = row
    return result


def _registry(home: Path, *, optional=False):
    root = store.home(home)
    plugins = root / '.omp' / 'plugins'
    registry_path = plugins / 'installed_plugins.json'
    value = _confined_json(registry_path, root, 'project registry', optional=optional)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'version', 'plugins'}
            or value.get('version') != 2 or not isinstance(value['plugins'], dict)):
        raise store.Refusal('native plugin project registry is malformed')
    index = _confined_json(_plugins(root) / 'project_registries.json',
                           root, 'project registry index')
    if (not isinstance(index, dict) or set(index) != {'version', 'registries'}
            or index.get('version') != 1
            or index.get('registries') != [str(registry_path)]):
        raise store.Refusal('native plugin project registry index is malformed')
    return value


def installed_plugins(home, required_ids=None) -> dict[str, dict]:
    root = store.home(home)
    required = tuple(PLUGIN_SPECS if required_ids is None else required_ids)
    if any(plugin_id not in PLUGIN_SPECS for plugin_id in required):
        raise ValueError('unsupported required plugin')
    _marketplaces(root, required)
    registry = _registry(root)
    plugins = root / '.omp' / 'plugins'
    lock = _confined_json(plugins / 'omp-plugins.lock.json', root,
                          'runtime lockfile')
    if (not isinstance(lock, dict) or set(lock) != {'plugins', 'settings'}
            or not isinstance(lock['plugins'], dict) or not isinstance(lock['settings'], dict)):
        raise store.Refusal('native plugin runtime lockfile is malformed')
    overrides = _confined_json(
        root / '.omp' / 'plugin-overrides.json', root,
        'project overrides', optional=True) or {}
    if (not isinstance(overrides, dict) or set(overrides) - {'disabled', 'features', 'settings'}
            or not isinstance(overrides.get('disabled', []), list)
            or not isinstance(overrides.get('features', {}), dict)
            or not isinstance(overrides.get('settings', {}), dict)):
        raise store.Refusal('native plugin project overrides are malformed')
    result = {}
    for plugin_id in required:
        spec = PLUGIN_SPECS[plugin_id]
        entries = registry['plugins'].get(plugin_id)
        project_entries = ([entry for entry in entries if isinstance(entry, dict)
                            and entry.get('scope') == 'project']
                           if isinstance(entries, list) else [])
        if len(project_entries) != 1 or len(entries) != 1:
            raise store.Refusal(f'native plugin {plugin_id} registration is missing or ambiguous')
        entry = project_entries[0]
        if entry.get('enabled', True) is not True or not isinstance(entry.get('version'), str):
            raise store.Refusal(f'native plugin {plugin_id} is disabled or malformed')
        payload = _confined(entry.get('installPath'), _root(root),
                            f'{plugin_id} payload')
        manifest = _read_json(payload / 'package.json', f'{plugin_id} manifest')
        pi = manifest.get('pi') if isinstance(manifest, dict) else None
        skill_values = pi.get('skills') if isinstance(pi, dict) else None
        if (manifest.get('name') != spec['package'] or manifest.get('version') != entry['version']
                or not isinstance(skill_values, list) or not skill_values
                or any(not isinstance(value, str) for value in skill_values)):
            raise store.Refusal(f'native plugin {plugin_id} manifest is malformed')
        state = lock['plugins'].get(spec['package'])
        if (not isinstance(state, dict) or state.get('version') != entry['version']
                or state.get('enabled') is not True
                or state.get('enabledFeatures') not in (None, [])):
            raise store.Refusal(f'native plugin {plugin_id} runtime state is disabled or malformed')
        if (spec['package'] in overrides.get('disabled', [])
                or overrides.get('features', {}).get(spec['package']) not in (None, [])):
            raise store.Refusal(f'native plugin {plugin_id} is disabled by project overrides')
        skill_roots = tuple(_confined(str((payload / value).resolve()), payload,
                                      f'{plugin_id} skill root')
                            for value in skill_values)
        result[plugin_id] = {
            'id': plugin_id,
            'version': entry['version'],
            'path': payload,
            'skill_roots': skill_roots,
        }
    return result


def install_plugins(home, executable: str, *, run=store.run) -> dict[str, dict]:
    root = store.home(home)
    registry = _registry(root, optional=True)
    registered = set() if registry is None else set(registry['plugins'])
    marketplace_value = _confined_json(
        _data_root(root) / 'marketplaces.json', _root(root),
        'marketplace registry', optional=True)
    existing_markets = {}
    if marketplace_value is not None:
        if (not isinstance(marketplace_value, dict) or marketplace_value.get('version') != 1
                or not isinstance(marketplace_value.get('marketplaces'), list)):
            raise store.Refusal('native plugin marketplace registry is malformed')
        for row in marketplace_value['marketplaces']:
            if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                raise store.Refusal('native plugin marketplace registry is malformed')
            existing_markets.setdefault(row['name'], []).append(row)
    env = environment(root, os.environ)
    for plugin_id, spec in PLUGIN_SPECS.items():
        rows = existing_markets.get(spec['marketplace'], [])
        if rows and (len(rows) != 1 or rows[0].get('sourceType') != 'github'
                     or rows[0].get('sourceUri') != spec['source']):
            raise store.Refusal(f'native plugin marketplace {spec["marketplace"]} has a conflicting publisher')
        if not rows:
            run([executable, 'plugin', 'marketplace', 'add', spec['source']],
                cwd=root, env=env, timeout=300)
        if plugin_id in registered:
            installed_plugins(root, (plugin_id,))
            continue
        run([executable, 'plugin', 'install', '--scope', 'project', plugin_id],
            cwd=root, env=env, timeout=300)
    return installed_plugins(root)
