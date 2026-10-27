import base64
import fcntl
import json
import os
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
        'PROJECT': allocation['project'], 'WEB_PORT': allocation['web_port'],
        'MINIO_PORT': allocation['minio_port'], 'OTLP_HTTP_PORT': allocation['otlp_http_port'],
        'OTLP_GRPC_PORT': allocation['otlp_grpc_port'], 'COLLECTOR_HEALTH_PORT': allocation['collector_health_port'], 'COLLECTOR_CONFIG': collector,
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
    return dict(allocation, compose=compose, collector=collector)


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
    project_id = _secrets(home, _allocation(home))['project_id']
    attributes = {
        'service.name': 'agent-distro', 'agd.home_id': config['home_id'],
        'agd.langfuse_project': project_id, 'agd.role': role,
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
    env, _generation, environment_warning = managed_environment(home, 'central', None, None)
    warning = warning or environment_warning
    _private_directory(home, 'data', 'observability', 'central', 'omp')
    binary = shutil.which('omp')
    if not binary:
        raise store.DependencyError('omp is not installed')
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


def enable(home):
    return _lifecycle(home, 'enable')


def start(home):
    return _lifecycle(home, 'start')


def stop(home):
    return _lifecycle(home, 'stop')


def disable(home):
    return _lifecycle(home, 'disable')


def status(home):
    home = _home(home)
    config, observed = read_config(home), read_status(home)
    if observed['runtime'] in ('disabled', 'stopped'):
        return observed
    compose = paths(home)['compose'] / 'compose.yaml'
    if not compose.exists():
        return dict(observed, runtime='unknown')
    try:
        output = subprocess.run(['docker', 'compose', '-p', config.get('project', ''), '-f', str(compose),
                                 'ps', '--format', 'json'], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=20, check=True).stdout
        rows = json.loads(output) if output.strip().startswith('[') else [json.loads(line) for line in output.splitlines() if line]
        if not any(row.get('Service') == 'collector' and row.get('State') == 'running' for row in rows):
            return dict(observed, runtime='degraded')
        rendered = dict(config['ports'])
        rendered['project'] = config['project']
        if not _ready(rendered, _secrets(home, rendered)):
            return dict(observed, runtime='degraded')
        return dict(observed, runtime='healthy')
    except (KeyError, OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError):
        return dict(observed, runtime='degraded')


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
            rendered = dict(_allocation(home), compose=locations['compose'] / 'compose.yaml')
            _compose(rendered, 'up', '-d'); _wait_ready(rendered, _secrets(home, rendered))
            _save_state(home, 'enabled', 'healthy', rendered); return status(home)
        if action in ('stop', 'disable'):
            desired = 'disabled' if action == 'disable' else config['mode']
            if not (locations['compose'] / 'compose.yaml').exists():
                _save_state(home, desired, 'disabled' if action == 'disable' else 'stopped', _allocation(home))
                return read_status(home)
            rendered = dict(_allocation(home), compose=locations['compose'] / 'compose.yaml')
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
    result = run_control(['docker', 'compose', '-p', rendered['project'], '-f', str(rendered['compose']), *command],
                         command[0])
    if result:
        raise store.Refusal(result['message'])


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
        config = {'schema': 1, 'home_id': _home_id(home), 'mode': desired, 'generation': generation,
                  'project': allocation['project'], 'ports': {k: allocation[k] for k in allocation if k.endswith('_port')}}
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
    if not (paths(home)['compose'] / 'release-lock.json').exists():
        for port in range(base, base + 5):
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    raise store.Refusal('observability loopback port is unavailable')
    return {'project': f'agdobs{token}', 'web_port': base, 'minio_port': base + 1,
            'otlp_http_port': base + 2, 'otlp_grpc_port': base + 3, 'collector_health_port': base + 4}


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
