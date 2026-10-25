import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


class Refusal(Exception):
    pass


class DependencyError(Exception):
    pass


def home(path=None):
    value = Path(path or os.environ.get('AGD_HOME') or Path(__file__).resolve().parents[2])
    if not value.is_absolute() or value.is_symlink():
        raise ValueError('home must be an absolute non-symlink directory')
    return value.resolve()


def validate_id(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', value):
        raise ValueError('invalid ID')
    return value


def run(argv, cwd=None, timeout=30, check=True):
    try:
        result = subprocess.run([str(x) for x in argv], cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise DependencyError(f'missing executable: {argv[0]}') from error
    if check and result.returncode:
        raise Refusal(f'{argv[0]} refused: {result.stderr.strip() or result.stdout.strip()}')
    return result


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def lock(root):
    path = root/'state'/'mutation.lock'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+') as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def git(cwd, *args, check=True):
    return run(['git', *args], cwd=cwd, check=check).stdout.strip()


def clean(cwd):
    return not git(cwd, 'status', '--porcelain=v1', '--untracked-files=all')
