import json
import os
import stat
import uuid
from pathlib import Path

from . import store


def paths(home):
    home = _home(home)
    data = home / 'data' / 'observability'
    state = home / 'state' / 'observability'
    return {
        'config': data / 'config.json',
        'secrets': data / 'secrets.json',
        'central_sessions': data / 'central' / 'omp',
        'task_sessions': data / 'tasks',
        'status': state / 'status.json',
        'compose': state / 'compose',
        'queue': state / 'collector' / 'queue',
        'trace_archive': state / 'archives' / 'traces',
        'log_archive': state / 'archives' / 'logs',
        'metric_archive': state / 'archives' / 'metrics',
    }


def read_config(home):
    home = _home(home)
    directory = _existing_private_directory(home, 'data', 'observability')
    value = _read_json(paths(home)['config']) if directory else None
    if value is None:
        return {'schema': 1, 'home_id': _home_id(home), 'mode': 'disabled', 'generation': '0'}
    _validate_record(value, home, 'config', 'mode')
    return value


def read_status(home):
    home = _home(home)
    directory = _existing_private_directory(home, 'state', 'observability')
    value = _read_json(paths(home)['status']) if directory else None
    if value is None:
        return {'schema': 1, 'home_id': _home_id(home), 'desired': 'disabled',
                'runtime': 'disabled', 'generation': '0'}
    _validate_record(value, home, 'status', 'desired')
    if value.get('runtime') not in ('disabled', 'stopped', 'healthy', 'degraded', 'unknown'):
        raise store.Refusal('observability status is malformed')
    return value


def worker_binding_locked(home, task_id, incarnation):
    home = _home(home)
    task_id = store.validate_id(task_id)
    incarnation = _incarnation(incarnation)
    directory = _private_directory(home, 'data', 'observability', 'tasks', task_id, 'omp', incarnation)
    receipt = directory / 'binding.json'
    value = _read_json(receipt)
    expected = {'task_id': task_id, 'incarnation': incarnation,
                'session_dir': str(directory), 'state': 'directory allocated'}
    if value is None:
        store.atomic_json(receipt, expected)
        return expected
    if value != expected:
        raise store.Refusal('observability binding is malformed')
    return value


def worker_binding(home, task_id, incarnation):
    home = _home(home)
    with store.lock(home):
        return worker_binding_locked(home, task_id, incarnation)


def managed_environment(home, role, task, incarnation):
    config = read_config(home)
    if role not in ('central', 'worker'):
        raise ValueError('invalid observability role')
    if role == 'worker':
        if not isinstance(task, dict):
            raise ValueError('invalid task')
        store.validate_id(task.get('id'))
        _incarnation(incarnation)
    env = {key: value for key, value in os.environ.items() if not _ambient_otel(key)}
    return env, config['generation'], None


def _home(value):
    home = store.home(value)
    _directory_identity(home, 'home')
    return home


def _home_id(home):
    if _existing_private_directory(home, 'data') is None:
        raise store.Refusal('home identity is unavailable')
    path = home / 'data' / 'home.json'
    value = _read_json(path)
    if not isinstance(value, dict):
        raise store.Refusal('home identity is unavailable')
    try:
        home_id = str(uuid.UUID(value['home_id']))
    except (KeyError, ValueError, TypeError) as error:
        raise store.Refusal('home identity is malformed') from error
    if value['home_id'] != home_id:
        raise store.Refusal('home identity is malformed')
    return home_id


def _incarnation(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('invalid worker incarnation') from error
    if value != parsed:
        raise ValueError('invalid worker incarnation')
    return parsed


def _ambient_otel(key):
    if not key.startswith('OTEL_'):
        return False
    return (key.startswith('OTEL_EXPORTER_') or key in {
        'OTEL_TRACES_EXPORTER', 'OTEL_LOGS_EXPORTER', 'OTEL_METRICS_EXPORTER',
        'OTEL_RESOURCE_ATTRIBUTES', 'OTEL_SERVICE_NAME',
    } or 'PROTOCOL' in key or 'HEADER' in key or 'CONTENT' in key)


def _private_directory(home, *parts):
    path = home
    private = False
    for part in parts:
        path /= part
        if not os.path.lexists(path):
            path.mkdir(mode=0o700)
        _directory_identity(path, 'observability path')
        private = private or part == 'observability'
        if private and path.stat(follow_symlinks=False).st_mode & 0o077:
            raise store.Refusal('observability path is not private')
    return path


def _existing_private_directory(home, *parts):
    path = home
    private = False
    for part in parts:
        path /= part
        if not os.path.lexists(path):
            return None
        _directory_identity(path, 'observability path')
        private = private or part == 'observability'
        if private and path.stat(follow_symlinks=False).st_mode & 0o077:
            raise store.Refusal('observability path is not private')
    return path


def _directory_identity(path, label):
    try:
        identity = os.lstat(path)
    except OSError as error:
        raise store.Refusal(f'{label} is unavailable') from error
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
        raise store.Refusal(f'{label} is unsafe')


def _read_json(path):
    if not os.path.lexists(path):
        return None
    try:
        identity = os.lstat(path)
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode):
            raise store.Refusal('observability record is unsafe')
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise store.Refusal('observability record is malformed') from error
    except OSError as error:
        raise store.Refusal('observability record is unavailable') from error


def _validate_record(value, home, name, desired_key):
    if (not isinstance(value, dict) or value.get('schema') != 1 or
            value.get('home_id') != _home_id(home) or
            value.get('generation') is None or not isinstance(value['generation'], str) or
            not value['generation'].isdigit() or
            value.get(desired_key) not in ('disabled', 'enabled')):
        raise store.Refusal(f'observability {name} is malformed')
