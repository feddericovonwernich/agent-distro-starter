"""Native OMP storage and execution boundary for one distro."""
import os
import stat
from pathlib import Path
from typing import Mapping

from . import store


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


def agent_dir(home) -> Path:
    return _root(home) / 'agent'


def runtime_directories(home) -> tuple[Path, ...]:
    runtime = _root(home)
    return (
        runtime / 'config',
        runtime / 'agent',
        runtime / 'xdg' / 'data',
        runtime / 'xdg' / 'state',
        runtime / 'xdg' / 'cache',
    )


def environment(home, inherited: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of inherited with every OMP storage control made local."""
    runtime = _root(home)
    result = dict(inherited)
    effective_home = Path(result.get('HOME') or Path.home())
    if not effective_home.is_absolute():
        raise store.Refusal('OMP runtime requires an absolute HOME')
    result.setdefault('HOME', str(effective_home))
    config = runtime / 'config'
    relative_config = os.path.relpath(config, effective_home)
    if (effective_home / relative_config).resolve() != config.resolve():
        raise store.Refusal('OMP config root cannot be confined to the distro')
    result.update(
        PI_CONFIG_DIR=relative_config,
        PI_CODING_AGENT_DIR=str(runtime / 'agent'),
        OMP_PROFILE='default',
        PI_PROFILE='default',
        XDG_DATA_HOME=str(runtime / 'xdg' / 'data'),
        XDG_STATE_HOME=str(runtime / 'xdg' / 'state'),
        XDG_CACHE_HOME=str(runtime / 'xdg' / 'cache'),
    )
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


def initialize(home, executable: str, *, inherited: Mapping[str, str] | None = None, run=store.run) -> None:
    root = store.home(home)
    for directory in runtime_directories(root):
        _private_directory(root, directory)
    run([executable, 'config', 'init-xdg'], cwd=root,
        env=environment(root, os.environ if inherited is None else inherited))


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
