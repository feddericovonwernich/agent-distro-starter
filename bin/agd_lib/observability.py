import base64
import fcntl
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
import urllib.request
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
        'processes': state / 'processes',
        'compose': state / 'compose',
        'queue': state / 'collector' / 'queue',
        'trace_archive': state / 'archives' / 'traces',
        'log_archive': state / 'archives' / 'logs',
        'metric_archive': state / 'archives' / 'metrics',
    }


def run_control(command, stage):
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=120)
    except FileNotFoundError:
        return {'stage': stage, 'category': 'missing-executable', 'exit_code': None,
                'message': f'{stage} failed: executable unavailable'}
    except subprocess.TimeoutExpired:
        return {'stage': stage, 'category': 'timeout', 'exit_code': None,
                'message': f'{stage} failed: timed out'}
    if completed.returncode:
        return {'stage': stage, 'category': 'command-failed', 'exit_code': completed.returncode,
                'message': f'{stage} failed: command failed'}
    return None


def render_collector(home, lock, secrets_value=None):
    home = _home(home)
    _validate_lock(lock)
    locations = paths(home)
    _runtime_directories(home)
    value = secrets_value or _secrets(home, _allocation(home))
    text = _render(_template('collector.yaml.tmpl'), {
        'QUEUE': locations['queue'], 'TRACE_ARCHIVE': locations['trace_archive'] / 'traces.json',
        'LOG_ARCHIVE': locations['log_archive'] / 'logs.json',
        'METRIC_ARCHIVE': locations['metric_archive'] / 'metrics.json',
        'OTLP_ENDPOINT': 'http://langfuse-web:3000/api/public/otel/v1/traces',
        'OTLP_AUTHORIZATION': 'Basic ' + base64.b64encode(
            f"{value['public_key']}:{value['secret_key']}".encode()).decode(),
    })
    path = locations['compose'] / 'collector.yaml'
    _private_write(path, text)
    return path


def render_stack(home, lock=None):
    home = _home(home)
    lock = _load_lock() if lock is None else lock
    _validate_lock(lock)
    locations = paths(home)
    _runtime_directories(home)
    allocation = _allocation(home)
    values = _secrets(home, allocation)
    collector = render_collector(home, lock, values)
    mapping = dict(lock['images'])
    mapping.update({
        'PROJECT': allocation['project'], 'HOME_ID': _home_id(home), 'WEB_PORT': allocation['web_port'],
        'MINIO_PORT': allocation['minio_port'], 'OTLP_HTTP_PORT': allocation['otlp_http_port'],
        'OTLP_GRPC_PORT': allocation['otlp_grpc_port'], 'COLLECTOR_HEALTH_PORT': allocation['collector_health_port'],
        'COLLECTOR_METRICS_PORT': allocation['collector_metrics_port'], 'COLLECTOR_CONFIG': collector,
        'QUEUE': locations['queue'], 'TRACE_ARCHIVE': locations['trace_archive'],
        'LOG_ARCHIVE': locations['log_archive'], 'METRIC_ARCHIVE': locations['metric_archive'],
        'UID': os.getuid(), 'GID': os.getgid(), 'ORG_ID': values['org_id'],
        'PROJECT_ID': values['project_id'], 'PUBLIC_KEY': values['public_key'],
        'SECRET_KEY': values['secret_key'], 'POSTGRES_PASSWORD': values['postgres_password'],
        'CLICKHOUSE_PASSWORD': values['clickhouse_password'], 'REDIS_PASSWORD': values['redis_password'],
        'MINIO_USER': values['minio_user'], 'MINIO_PASSWORD': values['minio_password'],
        'SALT': values['salt'], 'ENCRYPTION_KEY': values['encryption_key'],
        'NEXTAUTH_SECRET': values['nextauth_secret'], 'ADMIN_PASSWORD': values['admin_password'],
        'COLLECTOR': mapping.pop('collector'), 'LANGFUSE': mapping.pop('langfuse'),
        'LANGFUSE_WORKER': mapping.pop('langfuse_worker'), 'POSTGRES': mapping.pop('postgres'),
        'CLICKHOUSE': mapping.pop('clickhouse'), 'MINIO': mapping.pop('minio'), 'REDIS': mapping.pop('redis'),
    })
    compose = locations['compose'] / 'compose.yaml'
    _private_write(compose, _render(_template('compose.yaml.tmpl'), mapping))
    _private_write(locations['compose'] / 'release-lock.json', json.dumps(lock, sort_keys=True) + '\n')
    return dict(allocation, compose=compose, collector=collector, lock=lock, project_id=values['project_id'],
                home_id=_home_id(home))


def read_config(home):
    home = _home(home)
    directory = _existing_private_directory(home, 'data', 'observability')
    value = _read_json(paths(home)['config']) if directory else None
    if value is None:
        return {'schema': 1, 'home_id': _home_id(home), 'mode': 'disabled', 'generation': '0'}
    _validate_record(value, home, 'config', 'mode')
    if 'project' in value:
        if not isinstance(value.get('project_id'), str) or not value['project_id']:
            raise store.Refusal('observability configuration is malformed')
        _validate_lock(value.get('lock'))
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


def reviewer_session_dir(home, review_run_id):
    """Provision only the reviewer native directory; binding awaits the real ID."""
    home = _home(home)
    try: review_run_id = str(uuid.UUID(review_run_id))
    except (ValueError, TypeError, AttributeError) as error: raise ValueError('invalid reviewer run ID') from error
    with store.lock(home):
        return str(_private_directory(_private_directory(home, 'data', 'observability', 'reviewer', review_run_id), 'omp'))

def reviewer_binding(home, review_run_id, native_session_id):
    """Create the reviewer-owned native session receipt after native allocation."""
    home = _home(home)
    try:
        review_run_id = str(uuid.UUID(review_run_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('invalid reviewer run ID') from error
    if not isinstance(native_session_id, str) or not native_session_id or '/' in native_session_id:
        raise ValueError('invalid native reviewer session ID')
    with store.lock(home):
        directory = _private_directory(home, 'data', 'observability', 'reviewer', review_run_id)
        session_dir = _private_directory(directory, 'omp')
        receipt = directory / 'binding.json'
        expected = {'schema': 1, 'purpose': 'background-skill-review', 'review_run_id': review_run_id,
                    'native_session_id': native_session_id, 'session_dir': str(session_dir)}
        value = _read_json(receipt)
        if value is None:
            store.atomic_json(receipt, expected)
            return expected
        if value != expected:
            raise store.Refusal('reviewer binding is immutable')
        return value


def managed_environment(home, role, task, incarnation):
    home = _home(home)
    config = read_config(home)
    if role not in ('central', 'worker'):
        raise ValueError('invalid observability role')
    if role == 'worker':
        if not isinstance(task, dict):
            raise ValueError('invalid task')
        task_id = store.validate_id(task.get('id'))
        incarnation = _incarnation(incarnation)
    env = {key: value for key, value in os.environ.items() if not key.startswith('OTEL_')}
    env['AGD_HOME'] = str(home)
    if role == 'central':
        env['AGD_MANAGED_CENTRAL'] = '1'
    if role == 'worker':
        env.update(AGD_TASK_ID=task_id, AGD_INCARNATION=incarnation)
    if config['mode'] == 'disabled':
        return env, config['generation'], None
    allocation = config.get('ports')
    if not isinstance(allocation, dict) or not isinstance(allocation.get('otlp_http_port'), int):
        raise store.Refusal('observability configuration is malformed')
    project_id = config.get('project_id')
    if not isinstance(project_id, str) or not project_id:
        raise store.Refusal('observability configuration is malformed')
    attributes = {
        'service.name': 'agent-distro', 'agd.home_id': config['home_id'],
        'agd.distro_id': config['home_id'], 'agd.langfuse_project': project_id, 'agd.role': role,
        'agd.session_scope': 'central' if role == 'central' else 'task-incarnation',
    }
    if role == 'worker':
        attributes.update({'agd.task_id': task_id, 'agd.incarnation': incarnation})
    endpoint = f"http://127.0.0.1:{allocation['otlp_http_port']}"
    env.update(
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=endpoint + '/v1/traces',
        OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=endpoint + '/v1/logs',
        OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=endpoint + '/v1/metrics',
        OTEL_EXPORTER_OTLP_TRACES_PROTOCOL='http/protobuf',
        OTEL_EXPORTER_OTLP_LOGS_PROTOCOL='http/protobuf',
        OTEL_EXPORTER_OTLP_METRICS_PROTOCOL='http/protobuf',
        OTEL_TRACES_EXPORTER='otlp', OTEL_LOGS_EXPORTER='otlp', OTEL_METRICS_EXPORTER='otlp',
        OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT='true',
        OTEL_RESOURCE_ATTRIBUTES=','.join(f'{key}={value}' for key, value in attributes.items()),
        OTEL_SERVICE_NAME='agent-distro',
    )
    warning = None if read_status(home)['runtime'] == 'healthy' else 'observability is degraded; continuing without confirmed Collector health'
    return env, config['generation'], warning


def reviewer_environment(home, review_run_id):
    """Return the managed native environment for one reviewer run."""
    home = _home(home)
    try:
        review_run_id = str(uuid.UUID(review_run_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('invalid reviewer run ID') from error
    config = read_config(home)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('OTEL_') and key not in {'AGD_TASK_ID', 'AGD_INCARNATION', 'AGD_MANAGED_CENTRAL', 'AGD_OWNER'}}
    env['AGD_HOME'] = str(home)
    if config['mode'] == 'disabled':
        return env, config['generation'], None
    allocation, project_id = config.get('ports'), config.get('project_id')
    if not isinstance(allocation, dict) or not isinstance(allocation.get('otlp_http_port'), int) or not isinstance(project_id, str) or not project_id:
        raise store.Refusal('observability configuration is malformed')
    endpoint = f"http://127.0.0.1:{allocation['otlp_http_port']}"
    attributes = {'service.name': 'agent-distro', 'agd.home_id': config['home_id'], 'agd.distro_id': config['home_id'],
                  'agd.langfuse_project': project_id, 'agd.role': 'reviewer', 'agd.session_scope': 'background-skill-review',
                  'agd.review_run_id': review_run_id, 'purpose': 'background-skill-review'}
    env.update(OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=endpoint + '/v1/traces', OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=endpoint + '/v1/logs',
               OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=endpoint + '/v1/metrics', OTEL_EXPORTER_OTLP_TRACES_PROTOCOL='http/protobuf',
               OTEL_EXPORTER_OTLP_LOGS_PROTOCOL='http/protobuf', OTEL_EXPORTER_OTLP_METRICS_PROTOCOL='http/protobuf',
               OTEL_TRACES_EXPORTER='otlp', OTEL_LOGS_EXPORTER='otlp', OTEL_METRICS_EXPORTER='otlp',
               OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT='true',
               OTEL_RESOURCE_ATTRIBUTES=','.join(f'{key}={value}' for key, value in attributes.items()),
               OTEL_SERVICE_NAME='agent-distro')
    warning = None if read_status(home)['runtime'] == 'healthy' else 'observability is degraded; continuing without confirmed Collector health'
    return env, config['generation'], warning


def central_argv(home, omp_args):
    home = _home(home)
    forbidden = {'--cwd', '--session-dir', '--continue', '-c', '--resume', '-r', '--session',
                 '--no-session', '--fork', '--from-claude', '--from-codex'}
    for argument in omp_args:
        if argument == '--':
            break
        if argument in forbidden or any(argument.startswith(flag + '=') for flag in forbidden):
            raise store.Refusal('managed central refuses forwarded cwd or native session controls')
    config = read_config(home)
    warning = None
    if config['mode'] == 'enabled':
        try:
            start(home)
        except store.Refusal as error:
            warning = f'observability degraded: {error}'
    env, generation, environment_warning = managed_environment(home, 'central', None, None)
    warning = warning or environment_warning
    _private_directory(home, 'data', 'observability', 'central', 'omp')
    binary = shutil.which('omp')
    if not binary:
        raise store.DependencyError('omp is not installed')
    record_process_generation(home, 'central', generation)
    return [binary, '--cwd', str(home), '--session-dir', str(paths(home)['central_sessions']), *omp_args], env, warning


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


def enable(home):
    return _lifecycle(home, 'enable')


def start(home):
    return _lifecycle(home, 'start')


def stop(home):
    return _lifecycle(home, 'stop')


def disable(home):
    return _lifecycle(home, 'disable')


def record_desired_generation(home, generation, mode):
    home = _home(home)
    if mode not in ('enabled', 'disabled') or not isinstance(generation, str) or not generation.isdigit():
        raise ValueError('invalid observability generation')
    with store.lock(home):
        current = read_config(home)
        actual_generation = generation if current['mode'] != mode else current['generation']
        allocation = _allocation(home)
        config = {'schema': 1, 'home_id': _home_id(home), 'mode': mode, 'generation': actual_generation,
                  'project': allocation['project'],
                  'ports': {key: allocation[key] for key in allocation if key.endswith('_port')},
                  'lock': _load_lock(), 'project_id': str(uuid.uuid4())}
        _private_directory(home, 'data', 'observability')
        _private_directory(home, 'state', 'observability')
        store.atomic_json(paths(home)['config'], config)
        store.atomic_json(paths(home)['status'], {'schema': 1, 'home_id': _home_id(home), 'desired': mode,
                                                  'runtime': 'unknown', 'generation': actual_generation})


def record_process_generation(home, role, generation, task_id=None, incarnation=None, pid=None, start=None):
    home = _home(home)
    if role not in ('central', 'worker') or not isinstance(generation, str) or not generation.isdigit():
        raise ValueError('invalid observability process generation')
    if role == 'worker':
        task_id, incarnation = store.validate_id(task_id), _incarnation(incarnation)
    elif task_id is not None or incarnation is not None:
        raise ValueError('central process cannot have task identity')
    pid = os.getpid() if pid is None else pid
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError('invalid observability process pid')
    start = _process_start(pid) if start is None else start
    if not isinstance(start, str) or not start:
        raise ValueError('invalid observability process start')
    identity = '-'.join((role, task_id or 'central', incarnation or 'session', str(pid), start))
    name = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex + '.json'
    receipt = {'schema': 1, 'role': role, 'generation': generation, 'pid': pid, 'start': start,
               'recorded_at': time.time()}
    if role == 'worker':
        receipt.update(task_id=task_id, incarnation=incarnation)
    with store.lock(home):
        directory = _private_directory(home, 'state', 'observability', 'processes')
        store.atomic_json(directory / name, receipt)
    return receipt


def _process_start(pid):
    from .supervision import process_start
    return process_start(pid)


def _process_gone(pid, start):
    from .supervision import gone
    return gone(pid, start)


def status(home):
    home = _home(home)
    config, observed = read_config(home), read_status(home)
    compose = paths(home)['compose'] / 'compose.yaml'
    if 'project' not in config:
        return _with_process_status(home, config, observed)
    if not compose.exists():
        return _with_process_status(home, config, dict(observed, runtime='unknown'))
    try:
        _require_owned_resources({'project': config['project'], 'home_id': config['home_id']})
        output = subprocess.run(['docker', 'compose', '-p', config['project'], '-f', str(compose),
                                 'ps', '--format', 'json'], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=20, check=True).stdout
        rows = json.loads(output) if output.strip().startswith('[') else [
            json.loads(line) for line in output.splitlines() if line]
        if not any(row.get('Service') == 'collector' and row.get('State') == 'running' for row in rows):
            return _with_process_status(home, config, dict(observed, runtime='stopped'))
        rendered = dict(config['ports'])
        rendered['project'] = config['project']
        if not _ready(rendered, _read_secrets(home)):
            return _with_process_status(home, config, dict(observed, runtime='degraded'))
        return _with_process_status(home, config, dict(observed, runtime='healthy'))
    except (KeyError, OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError, store.Refusal):
        return _with_process_status(home, config, dict(observed, runtime='degraded'))


def _with_process_status(home, config, observed):
    directory = _existing_private_directory(home, 'state', 'observability', 'processes')
    receipts = [] if directory is None else [_read_json(path) for path in sorted(directory.glob('*.json'))]
    central = []
    workers = []
    for receipt in receipts:
        if (not isinstance(receipt, dict) or receipt.get('schema') != 1 or
                not isinstance(receipt.get('recorded_at'), (int, float))):
            raise store.Refusal('observability process receipt is malformed')
        role = receipt.get('role')
        if role not in ('central', 'worker') or not isinstance(receipt.get('generation'), str):
            raise store.Refusal('observability process receipt is malformed')
        value = {key: receipt[key] for key in ('generation', 'pid', 'start', 'recorded_at') if key in receipt}
        try:
            exited = _process_gone(receipt['pid'], receipt['start'])
        except (OSError, ValueError, TypeError):
            value['state'] = 'unknown'
        else:
            value['state'] = 'stopped' if exited else (
                'stale-restart-required' if receipt['generation'] != config['generation'] else observed['runtime'])
        if role == 'central':
            central.append(value)
        else:
            if not isinstance(receipt.get('task_id'), str) or not isinstance(receipt.get('incarnation'), str):
                raise store.Refusal('observability worker process receipt is malformed')
            workers.append(dict(value, task_id=receipt['task_id'], incarnation=receipt['incarnation']))
    bindings = _worker_bindings(home)
    by_identity = {(row['task_id'], row['incarnation']): row for row in bindings}
    for worker in workers:
        binding = by_identity.get((worker['task_id'], worker['incarnation']))
        if binding:
            worker.update({key: value for key, value in binding.items() if key != 'state'})
            worker['native_state'] = binding['state']
    result = dict(observed)
    live = [item for item in central if item['state'] != 'stopped']
    result['central'] = max(live, key=lambda item: item['recorded_at']) if live else (
        max(central, key=lambda item: item['recorded_at']) if central else {'state': 'unknown'})
    result['central_history'] = sorted(central, key=lambda item: item['recorded_at'])
    result['workers'] = workers
    result['bindings'] = bindings
    result['durable'] = _durable_status(home)
    delivery = (_collector_diagnostics(home, config) if config['mode'] == 'enabled' and
                result['runtime'] in ('healthy', 'degraded') else {'state': 'unknown'})
    if result['runtime'] == 'healthy' and delivery.get('permanent_failure') == 'observed':
        result['runtime'] = 'degraded'
    result['delivery'] = delivery
    return result


def _worker_bindings(home):
    root = _existing_private_directory(home, 'data', 'observability', 'tasks')
    if root is None:
        return []
    bindings = []
    for receipt in root.glob('*/omp/*/binding.json'):
        value = _read_json(receipt)
        if not isinstance(value, dict) or value.get('session_dir') != str(receipt.parent):
            raise store.Refusal('observability binding is malformed')
        files = [path for path in receipt.parent.iterdir() if path.name != 'binding.json']
        state = 'native files present' if files else 'native session not materialized'
        bindings.append(dict(task_id=value['task_id'], incarnation=value['incarnation'],
                             session_dir=value['session_dir'], binding_state='directory allocated', state=state))
    return sorted(bindings, key=lambda item: (item['task_id'], item['incarnation']))


def _durable_status(home):
    locations = paths(home)
    entries = {'queue': locations['queue'], 'trace_archive': locations['trace_archive'],
               'log_archive': locations['log_archive'], 'metric_archive': locations['metric_archive']}
    return {name: {'path': str(path), 'bytes': _tree_bytes(path)} for name, path in entries.items()}


def _tree_bytes(path):
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def _collector_diagnostics(home, config):
    storage_failure, permanent_rejection = _collector_log_evidence(home, config)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{config['ports']['collector_metrics_port']}/metrics",
                                    timeout=3) as response:
            if response.status != 200:
                result = {'state': 'unknown', 'storage_failure': storage_failure}
            else:
                result = _parse_collector_metrics(response.read().decode('utf-8', 'replace'))
    except (KeyError, OSError, UnicodeError, urllib.error.URLError):
        result = {'state': 'unknown', 'storage_failure': storage_failure}
    result['storage_failure'] = storage_failure
    if permanent_rejection:
        result['permanent_failure'] = 'observed'
    return result


def _collector_log_evidence(home, config):
    try:
        output = subprocess.run(['docker', 'compose', '-p', config['project'], '-f',
                                 str(paths(home)['compose'] / 'compose.yaml'),
                                 'logs', '--tail', '100', 'collector'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3,
                                check=False).stdout
    except (KeyError, OSError, subprocess.SubprocessError):
        return 'unknown', False
    text = output[-65536:]
    return _classify_collector_errors(text), _has_permanent_rejection(text)


def _classify_collector_errors(text):
    text = text.lower()
    if 'enospc' in text or 'no space left on device' in text:
        return 'host-disk-full'
    if ('max_size' in text or 'max size' in text) and 'storage' in text:
        return 'file-storage-full'
    return 'unknown'

def _has_permanent_rejection(text):
    return bool(re.search(r'(?:status code|status)\s*[:=]?\s*4\d\d\b', text.lower()) and
                'permanent' in text.lower())


def _parse_collector_metrics(text):
    values = {'queue_batches': None, 'queue_capacity_batches': None, 'queue_full': 'unknown',
              'enqueue_failed_spans': None, 'retryable_send_failed_spans': 0, 'permanent_send_failed_spans': 0,
              'unclassified_send_failed_spans': 0, 'sent_spans': None}
    names = {
        'otelcol_exporter_queue_size': 'queue_batches',
        'otelcol_exporter_queue_capacity': 'queue_capacity_batches',
        'otelcol_exporter_enqueue_failed_spans': 'enqueue_failed_spans',
        'otelcol_exporter_sent_spans': 'sent_spans',
    }
    found = False
    for line in text.splitlines():
        match = re.match(r'^(otelcol_exporter_[a-z_]+)(?:\{([^}]*)\})?\s+([0-9.eE+-]+)$', line.strip())
        if not match or 'exporter="otlphttp/langfuse"' not in (match.group(2) or ''):
            continue
        name, labels, number = match.groups()
        try:
            value = int(float(number))
        except ValueError:
            continue
        if name in names:
            values[names[name]] = value
            found = True
        elif name == 'otelcol_exporter_send_failed_spans':
            found = True
            if 'error_permanent="true"' in (labels or '') or 'error.permanent="true"' in (labels or ''):
                values['permanent_send_failed_spans'] += value
            elif 'error_permanent="false"' in (labels or '') or 'error.permanent="false"' in (labels or ''):
                values['retryable_send_failed_spans'] += value
            else:
                values['unclassified_send_failed_spans'] += value
    if values['queue_batches'] is not None and values['queue_capacity_batches'] is not None:
        values['queue_full'] = 'full' if values['queue_batches'] == values['queue_capacity_batches'] else 'not-full'
    return dict(state='known', storage_failure='unknown', **values) if found else {'state': 'unknown'}


def _lifecycle(home, action):
    home = _home(home)
    locations = paths(home)
    _runtime_directories(home)
    fd = os.open(locations['compose'] / 'lifecycle.lock', os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as error:
            raise store.Refusal('observability lifecycle is busy') from error
        _save_stage(home, action)
        if action == 'enable':
            rendered = render_stack(home); _compose(rendered, 'up', '-d')
            _wait_ready(rendered, _secrets(home, rendered)); _save_state(home, 'enabled', 'healthy', rendered)
            return status(home)
        config = read_config(home)
        if action == 'start':
            if config['mode'] != 'enabled' or not (locations['compose'] / 'compose.yaml').exists():
                raise store.Refusal('observability is not enabled')
            rendered = dict(_allocation(home), compose=locations['compose'] / 'compose.yaml',
                            home_id=config['home_id'], lock=config['lock'], project_id=config['project_id'])
            _compose(rendered, 'up', '-d'); _wait_ready(rendered, _read_secrets(home))
            _save_state(home, 'enabled', 'healthy', rendered); return status(home)
        if action in ('stop', 'disable'):
            desired = 'disabled' if action == 'disable' else config['mode']
            if not (locations['compose'] / 'compose.yaml').exists():
                _save_state(home, desired, 'disabled' if action == 'disable' else 'stopped', _allocation(home))
                return read_status(home)
            rendered = dict(_allocation(home), compose=locations['compose'] / 'compose.yaml',
                            home_id=config['home_id'], lock=config['lock'], project_id=config['project_id'])
            if action == 'disable':
                _save_state(home, 'disabled', 'unknown', rendered)
            _compose(rendered, 'stop'); _save_state(home, desired, 'stopped', rendered)
            return read_status(home)
    except store.Refusal:
        if acquired:
            _save_failure(home, action)
        raise
    finally:
        os.close(fd)


def _compose(rendered, *command):
    _require_owned_resources(rendered)
    result = run_control(['docker', 'compose', '-p', rendered['project'], '-f', str(rendered['compose']), *command],
                         command[0])
    if result:
        raise store.Refusal(result['message'])


def _require_owned_resources(rendered):
    home_id = rendered.get('home_id')
    if not home_id:
        return
    names = [('network', rendered['project'] + '_network'),
             *[('volume', rendered['project'] + suffix) for suffix in ('_postgres', '_clickhouse', '_redis', '_minio')]]
    for kind, name in names:
        inspected = _ownership_command(['docker', kind, 'inspect', name])
        if inspected.returncode:
            continue
        try:
            value = json.loads(inspected.stdout)[0]
        except (IndexError, ValueError, TypeError):
            raise store.Refusal('observability ownership is uncertain')
        if value.get('Labels', {}).get('agd.home_id') != home_id:
            raise store.Refusal('observability resource belongs to another home')
    containers = _ownership_command(['docker', 'ps', '-aq', '--filter',
                                     f'label=com.docker.compose.project={rendered["project"]}'])
    if containers.returncode:
        raise store.Refusal('observability container ownership is uncertain')
    for container_id in containers.stdout.split():
        inspected = _ownership_command(['docker', 'container', 'inspect', container_id])
        try:
            value = json.loads(inspected.stdout)[0]
        except (IndexError, ValueError, TypeError):
            raise store.Refusal('observability container ownership is uncertain')
        if value.get('Config', {}).get('Labels', {}).get('agd.home_id') != home_id:
            raise store.Refusal('observability resource belongs to another home')

def _ownership_command(command):
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise store.Refusal('observability ownership inspection is unavailable') from error


def _ready(rendered, value):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{rendered['collector_health_port']}/", timeout=3) as collector:
        if collector.status != 200:
            return False
    with urllib.request.urlopen(
            f"http://127.0.0.1:{rendered['web_port']}/api/public/health", timeout=3) as response:
        if response.status != 200:
            return False
    auth = base64.b64encode(f"{value['public_key']}:{value['secret_key']}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{rendered['web_port']}/api/public/projects",
        headers={'Authorization': 'Basic ' + auth})
    with urllib.request.urlopen(request, timeout=5) as project_response:
        payload = json.loads(project_response.read())
    projects = payload.get('data', payload) if isinstance(payload, dict) else payload
    return isinstance(projects, list) and any(
        isinstance(project, dict) and project.get('id') == value['project_id'] for project in projects)


def _wait_ready(rendered, value):
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            if _ready(rendered, value):
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise store.Refusal('observability start failed: stack did not become ready')

def _save_state(home, desired, runtime, allocation):
    with store.lock(home):
        current = read_config(home)
        generation = str(int(current['generation']) + (current['mode'] != desired))
        lock = allocation.get('lock')
        if not isinstance(lock, dict):
            lock = _load_lock()
        _validate_lock(lock)
        project_id = allocation.get('project_id')
        if not isinstance(project_id, str):
            project_id = _secrets(home, allocation)['project_id']
        config = {'schema': 1, 'home_id': _home_id(home), 'mode': desired, 'generation': generation,
                  'project': allocation['project'], 'ports': {k: allocation[k] for k in allocation if k.endswith('_port')},
                  'lock': lock, 'project_id': project_id}
        status_value = {'schema': 1, 'home_id': _home_id(home), 'desired': desired,
                        'runtime': runtime, 'generation': generation}
        _private_directory(home, 'data', 'observability')
        _private_directory(home, 'state', 'observability')
        store.atomic_json(paths(home)['config'], config)
        store.atomic_json(paths(home)['status'], status_value)




def _save_stage(home, stage):
    with store.lock(home):
        current = read_config(home)
        _private_directory(home, 'state', 'observability')
        store.atomic_json(paths(home)['status'], {'schema': 1, 'home_id': _home_id(home),
                          'desired': current['mode'], 'runtime': 'unknown',
                          'generation': current['generation'], 'stage': stage})
def _save_failure(home, action):
    with store.lock(home):
        current = read_config(home)
        _private_directory(home, 'state', 'observability')
        store.atomic_json(paths(home)['status'], {'schema': 1, 'home_id': _home_id(home),
                          'desired': current['mode'], 'runtime': 'degraded',
                          'generation': current['generation'], 'stage': action})


def _runtime_directories(home):
    for item in ('data', 'observability'), ('state', 'observability'), ('state', 'observability', 'compose'), (
            'state', 'observability', 'collector', 'queue'), ('state', 'observability', 'archives', 'traces'), (
            'state', 'observability', 'archives', 'logs'), ('state', 'observability', 'archives', 'metrics'):
        _private_directory(home, *item)


def _allocation(home):
    token = _home_id(home).replace('-', '')[:12]
    base = 20000 + int(token[:4], 16) % 20000
    metrics_port = 40000 + int(token[4:8], 16) % 20000
    if not (paths(home)['compose'] / 'release-lock.json').exists():
        for port in (*range(base, base + 5), metrics_port):
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    raise store.Refusal('observability loopback port is unavailable')
    return {'project': f'agdobs{token}', 'web_port': base, 'minio_port': base + 1,
            'otlp_http_port': base + 2, 'otlp_grpc_port': base + 3, 'collector_health_port': base + 4,
            'collector_metrics_port': metrics_port}


def _secrets(home, allocation):
    path = paths(home)['secrets']
    existing = _read_json(path)
    if existing is not None:
        if not isinstance(existing, dict) or existing.get('home_id') != _home_id(home):
            raise store.Refusal('observability secrets are malformed')
        return existing
    value = {'schema': 1, 'home_id': _home_id(home), 'org_id': str(uuid.uuid4()),
             'project_id': str(uuid.uuid4()), 'public_key': 'pk-lf-' + secrets.token_urlsafe(24),
             'secret_key': 'sk-lf-' + secrets.token_urlsafe(32), 'postgres_password': secrets.token_urlsafe(24),
             'clickhouse_password': secrets.token_urlsafe(24), 'redis_password': secrets.token_urlsafe(24),
             'minio_user': 'agd' + secrets.token_hex(6), 'minio_password': secrets.token_urlsafe(24),
             'salt': secrets.token_urlsafe(24), 'encryption_key': secrets.token_hex(32),
             'nextauth_secret': secrets.token_urlsafe(32), 'admin_password': secrets.token_urlsafe(24)}
    _private_directory(home, 'data', 'observability')
    store.atomic_json(path, value)
    os.chmod(path, 0o600)
    return value


def _read_secrets(home):
    value = _read_json(paths(home)['secrets'])
    if not isinstance(value, dict) or value.get('home_id') != _home_id(home):
        raise store.Refusal('observability secrets are unavailable')
    return value


def _load_lock():
    try:
        return json.loads((_bundle() / 'release-lock.json').read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise store.Refusal('observability release lock is unavailable') from error


def _validate_lock(lock):
    required = {'collector', 'langfuse', 'langfuse_worker', 'postgres', 'clickhouse', 'minio', 'redis'}
    if not isinstance(lock, dict) or lock.get('schema') != 1 or set(lock.get('images', ())) != required:
        raise store.Refusal('observability release lock is unsupported')
    if any(not isinstance(image, str) or '@sha256:' not in image or len(image.rsplit('@sha256:', 1)[1]) != 64
           for image in lock['images'].values()):
        raise store.Refusal('observability release lock is unsupported')
    if lock != _load_lock():
        raise store.Refusal('observability release lock is unsupported')


def _template(name):
    try:
        return (_bundle() / name).read_text()
    except OSError as error:
        raise store.Refusal('observability template is unavailable') from error


def _bundle():
    return Path(__file__).resolve().parents[2] / 'bundled' / 'observability'


def _render(text, values):
    for key, value in values.items():
        text = text.replace('{{' + key.upper() + '}}', str(value))
    if '{{' in text:
        raise store.Refusal('observability template is unsupported')
    return text


def _private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
