import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agd_lib import observability, store, supervision


@dataclass(frozen=True)
class AttachTarget:
    attach_argv: list[str]
    env: dict[str, str]


def initialize(home: Path, backend: str) -> None:
    config = home / 'data' / 'launcher.json'
    if backend not in {'tmux', 'herdr'} or home.is_symlink() or config.exists() or config.is_symlink():
        raise store.Refusal('fresh non-symlink launcher home required')
    store.atomic_json(config, {
        'schema': 1,
        'backend': backend,
        'herdr_session': f'agd-{uuid.uuid4().hex}',
    })


def resolve_backend(home: Path, explicit_backend: str | None) -> str:
    path = home / 'data' / 'launcher.json'
    if path.is_symlink() or not path.is_file():
        raise store.Refusal('launcher config is unsafe')
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise store.Refusal('launcher config is malformed') from error
    if (not isinstance(config, dict) or set(config) != {'schema', 'backend', 'herdr_session'}
            or config.get('schema') != 1 or config.get('backend') not in {'tmux', 'herdr'}
            or not isinstance(config.get('herdr_session'), str)
            or not re.fullmatch(r'agd-[0-9a-f]{32}', config['herdr_session'])):
        raise store.Refusal('launcher config is malformed')
    return explicit_backend or config['backend']


def _receipt_path(home: Path) -> Path:
    return home / 'state' / 'central-launcher.json'


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def _read_receipt(home: Path) -> tuple[dict, bytes] | tuple[None, None]:
    path = _receipt_path(home)
    if not path.exists():
        return None, None
    if not path.is_file() or path.is_symlink():
        raise store.Refusal('launcher receipt is malformed')
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise store.Refusal('launcher receipt is malformed') from error
    _validate_receipt(value)
    if raw != _bytes(value):
        raise store.Refusal('launcher receipt bytes changed')
    return value, raw


def _validate_receipt(value: object) -> None:
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('backend') not in {'tmux', 'herdr'}:
        raise store.Refusal('launcher receipt is malformed')
    try:
        uuid.UUID(value.get('launch_id'))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('launcher receipt is malformed') from error
    if (not isinstance(value.get('endpoint_token'), str) or not value['endpoint_token']
            or value.get('phase') not in {'reserved', 'spawned', 'ready'}
            or not isinstance(value.get('endpoint'), dict)
            or not all(isinstance(item, str) for item in value.get('managed_command', ()))
            or not all(isinstance(item, str) for item in value.get('expected_process_argv', ()))
            or value.get('owner') is not None and not isinstance(value['owner'], dict)
            or value.get('watcher') is not None and not isinstance(value['watcher'], dict)):
        raise store.Refusal('launcher receipt is malformed')


def _owner(home: Path, receipt: dict, required: bool) -> dict | None:
    path = home / 'state' / 'owner.json'
    if not path.exists():
        if required:
            raise store.Refusal('central owner is missing')
        return None
    if not path.is_file() or path.is_symlink():
        raise store.Refusal('central owner is malformed')
    try:
        owner = json.loads(path.read_text())
        keys = ('owner_pid', 'owner_start', 'token', 'watcher_pid', 'watcher_start', 'heartbeat')
        if not isinstance(owner, dict) or any(key not in owner for key in keys):
            raise ValueError
        if owner['token'] != receipt['endpoint_token']:
            raise store.Refusal('foreign central owner')
        if supervision.process_start(owner['owner_pid']) != owner['owner_start']:
            raise store.Refusal('central owner identity changed')
        if supervision.process_start(owner['watcher_pid']) != owner['watcher_start']:
            raise store.Refusal('central watcher identity changed')
        if not isinstance(owner['heartbeat'], (int, float)) or time.time() - owner['heartbeat'] >= 12:
            raise store.Refusal('central watcher is unhealthy')
        return owner
    except (ValueError, TypeError, KeyError, OSError) as error:
        if isinstance(error, store.Refusal):
            raise
        raise store.Refusal('central owner is malformed') from error


def _advance(home: Path, expected: dict, replacement: dict) -> None:
    _validate_receipt(replacement)
    with store.lock(home):
        current, raw = _read_receipt(home)
        if current != expected or raw != _bytes(expected):
            raise store.Refusal('launcher receipt changed')
        _owner(home, expected, False)
        store.atomic_json(_receipt_path(home), replacement)


def _inheritance(home: Path, receipt: dict, inherited_env: Mapping[str, str] | None) -> dict[str, str]:
    token_hash = hashlib.sha256(receipt['endpoint_token'].encode()).hexdigest()
    result = {'AGD_HOME': str(home), 'AGD_LAUNCHER_BACKEND': receipt['backend'],
              'AGD_LAUNCH_ID': receipt['launch_id'], 'AGD_ENDPOINT_TOKEN_HASH': token_hash}
    if inherited_env:
        for key, value in result.items():
            if key in inherited_env and inherited_env[key] != value:
                raise store.Refusal('inherited launcher environment differs')
    return result


def _prove(home: Path, receipt: dict, inspect) -> dict:
    observed = inspect(home, receipt)
    if not isinstance(observed, dict) or observed.get('endpoint') != receipt['endpoint']:
        raise store.Refusal('central endpoint continuity is unproven')
    foreground = observed.get('foreground')
    if not isinstance(foreground, dict) or any(foreground.get(key) != receipt[key]
                                                for key in ('launch_id', 'endpoint_token')):
        raise store.Refusal('central foreground correlation is unproven')
    owner = _owner(home, receipt, True)
    if (foreground.get('pid') != owner['owner_pid'] or foreground.get('start') != owner['owner_start']
            or foreground.get('argv') != receipt['expected_process_argv']):
        raise store.Refusal('central foreground process differs')
    if receipt['phase'] == 'ready' and (receipt['owner'] != {'pid': owner['owner_pid'], 'start': owner['owner_start']}
                                        or receipt['watcher'] != {'pid': owner['watcher_pid'], 'start': owner['watcher_start']}):
        raise store.Refusal('central owner snapshot differs')
    return owner




def _herdr_session(home: Path) -> str:
    resolve_backend(home, None)
    return json.loads((home / 'data' / 'launcher.json').read_text())['herdr_session']


def _socket_identity(path: Path):
    try:
        identity = path.stat()
    except OSError as error:
        raise store.Refusal('Herdr server socket is unavailable') from error
    if not stat.S_ISSOCK(identity.st_mode):
        raise store.Refusal('Herdr server socket is unverified')
    return identity


def _herdr_response(result) -> dict:
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or 'error' in value or not isinstance(value['result'], dict):
            raise ValueError
        return value['result']
    except (KeyError, TypeError, ValueError) as error:
        raise store.Refusal('Herdr response unverified') from error


def _herdr_status(session: str) -> dict:
    try:
        result = store.run(['herdr', '--session', session, 'status', 'server'], check=False, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise store.Refusal('Herdr server did not respond') from error
    fields = dict(line.split(': ', 1) for line in result.stdout.splitlines() if ': ' in line)
    if (result.returncode or fields.get('status') != 'running' or fields.get('compatible') != 'yes'
            or not fields.get('protocol')):
        raise store.Refusal('Herdr session is not running')
    socket = Path(fields.get('socket', ''))
    if not socket.is_absolute():
        raise store.Refusal('Herdr server socket is unverified')
    identity = _socket_identity(socket)
    return {'session': session, 'socket': str(socket), 'device': identity.st_dev,
            'inode': identity.st_ino, 'protocol': fields['protocol']}


def _herdr_call(endpoint: dict, *args, check=True):
    identity = _socket_identity(Path(endpoint['socket']))
    if identity.st_dev != endpoint['device'] or identity.st_ino != endpoint['inode']:
        raise store.Refusal('Herdr server replaced; endpoint identity unknown')
    try:
        result = store.run(['herdr', '--session', endpoint['session'], *args], check=False, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise store.Refusal('Herdr command timed out; endpoint state unknown') from error
    if result.returncode and check:
        raise store.Refusal(f'Herdr command refused: {result.stderr.strip() or result.stdout.strip()}')
    return result


def _herdr_inspect(home: Path, receipt: dict) -> dict:
    endpoint = receipt['endpoint']
    pane = _herdr_response(_herdr_call(endpoint, 'pane', 'get', endpoint['pane']))['pane']
    if (pane.get('pane_id') != endpoint['pane'] or pane.get('workspace_id') != endpoint['workspace']
            or pane.get('tab_id') != endpoint['tab'] or pane.get('terminal_id') != endpoint['terminal']):
        raise store.Refusal('Herdr pane identity differs from recorded endpoint')
    process = _herdr_response(_herdr_call(endpoint, 'pane', 'process-info', '--pane', endpoint['pane']))['process_info']
    if process.get('pane_id') != endpoint['pane']:
        raise store.Refusal('Herdr process identity differs from endpoint')
    if isinstance(process.get('pid'), int):
        pid, start, argv = process['pid'], process.get('start'), process.get('argv')
    else:
        foreground = process.get('foreground_processes')
        if not isinstance(foreground, list) or len(foreground) != 1 or not isinstance(foreground[0], dict):
            raise store.Refusal('Herdr foreground process is unverified')
        pid, argv = foreground[0].get('pid'), foreground[0].get('argv')
        start = supervision.process_start(pid) if isinstance(pid, int) else None
    if not isinstance(start, str) or not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise store.Refusal('Herdr foreground process is unverified')
    return {'endpoint': endpoint, 'foreground': {'launch_id': endpoint['launch_id'],
            'endpoint_token': endpoint['token'], 'pid': pid, 'start': start, 'argv': argv,
            'terminal': endpoint['terminal']}}


def _herdr_create(home: Path, launch_id: str, endpoint_token: str, managed: list[str],
                  env: dict[str, str], argv: list[str], checkpoint=None) -> dict:
    session = _herdr_session(home)
    try:
        listed = json.loads(store.run(['herdr', 'session', 'list', '--json'], timeout=10).stdout)
        sessions = listed['sessions'] if isinstance(listed, dict) else listed
    except (KeyError, ValueError, TypeError) as error:
        raise store.Refusal('Herdr session list is unverified') from error
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        raise store.Refusal('Herdr session list is unverified')
    if any(item.get('name') == session for item in sessions):
        raise store.Refusal('unreceipted Herdr session already exists')
    subprocess.Popen(['herdr', '--session', session, 'server'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    endpoint = None
    for _ in range(30):
        try:
            endpoint = _herdr_status(session)
            break
        except store.Refusal:
            time.sleep(.1)
    if endpoint is None:
        raise store.Refusal('Herdr server did not become available')
    if checkpoint:
        checkpoint(endpoint)
    workspace = _herdr_response(_herdr_call(
        endpoint, 'workspace', 'create', '--cwd', str(home), '--label', 'agd-central', '--no-focus'))['workspace']
    workspace_id = workspace.get('workspace_id')
    if not isinstance(workspace_id, str):
        raise store.Refusal('Herdr workspace identity is unverified')
    endpoint['workspace'] = workspace_id
    if checkpoint:
        checkpoint(endpoint)
    checked_workspace = _herdr_response(_herdr_call(endpoint, 'workspace', 'get', workspace_id))['workspace']
    if checked_workspace.get('workspace_id') != workspace_id:
        raise store.Refusal('Herdr workspace identity changed')
    created = _herdr_response(_herdr_call(
        endpoint, 'tab', 'create', '--workspace', workspace_id, '--cwd', str(home), '--label', 'central', '--no-focus'))
    tab, pane = created.get('tab'), created.get('root_pane')
    if not isinstance(tab, dict) or not isinstance(pane, dict):
        raise store.Refusal('Herdr tab identity is unverified')
    endpoint.update(tab=tab.get('tab_id'), pane=pane.get('pane_id'), terminal=pane.get('terminal_id'),
                    token=endpoint_token, launch_id=launch_id)
    if (not all(isinstance(endpoint[key], str) and endpoint[key] for key in ('workspace', 'tab', 'pane', 'terminal'))
            or pane.get('workspace_id') != endpoint['workspace'] or pane.get('tab_id') != endpoint['tab']):
        raise store.Refusal('Herdr tab identity is inconsistent')
    if checkpoint:
        checkpoint(endpoint)
    command_env = {key: value for key, value in {
        'AGD_HERDR_SESSION': session, 'AGD_LAUNCH_ID': launch_id, 'AGD_ENDPOINT_TOKEN': endpoint_token,
        'PATH': env.get('PATH', os.environ.get('PATH', '')),
    }.items()}
    _herdr_call(endpoint, 'pane', 'run', endpoint['pane'],
                'export ' + ' '.join(f'{key}={shlex.quote(value)}' for key, value in command_env.items())
                + '; exec ' + shlex.join(managed))
    return endpoint


def _attach_env(home: Path, receipt: dict, inherited_env: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(inherited_env or os.environ)
    env.pop('HERDR_ENV', None)
    env.update(_inheritance(home, receipt, inherited_env))
    if receipt['backend'] == 'herdr' and isinstance(receipt['endpoint'].get('session'), str):
        env['AGD_HERDR_SESSION'] = receipt['endpoint']['session']
    return env
def _backend_inspect(home: Path, receipt: dict) -> dict:
    if receipt['backend'] == 'herdr':
        return _herdr_inspect(home, receipt)
    module = __import__(f'agd_lib.{receipt["backend"]}', fromlist=['inspect_central'])
    inspect = getattr(module, 'inspect_central', None)
    if not callable(inspect):
        raise store.Refusal('central backend is unavailable')
    return inspect(home, receipt)


def _backend_create(backend: str, home: Path, launch_id: str, endpoint_token: str, managed: list[str],
                    env: dict[str, str], argv: list[str], checkpoint=None) -> dict:
    if backend == 'herdr':
        return _herdr_create(home, launch_id, endpoint_token, managed, env, argv, checkpoint)
    module = __import__(f'agd_lib.{backend}', fromlist=['create_central'])
    create = getattr(module, 'create_central', None)
    if not callable(create):
        raise store.Refusal('central backend is unavailable')
    return create(home, launch_id, endpoint_token, managed, env, argv)


def _backend_finalize(home: Path, receipt: dict) -> None:
    if receipt['backend'] == 'herdr':
        return
    module = __import__(f'agd_lib.{receipt["backend"]}', fromlist=['finalize_central'])
    finalize = getattr(module, 'finalize_central', None)
    if not callable(finalize):
        raise store.Refusal('central backend is unavailable')
    finalize(home, receipt)


def _backend_attach(home: Path, receipt: dict) -> list[str]:
    if receipt['backend'] == 'herdr':
        _herdr_inspect(home, receipt)
        endpoint = receipt['endpoint']
        _herdr_response(_herdr_call(endpoint, 'workspace', 'focus', endpoint['workspace']))
        _herdr_response(_herdr_call(endpoint, 'tab', 'focus', endpoint['tab']))
        _herdr_inspect(home, receipt)
        return ['herdr', 'session', 'attach', endpoint['session']]
    module = __import__(f'agd_lib.{receipt["backend"]}', fromlist=['attach_argv'])
    attach = getattr(module, 'attach_argv', None)
    if not callable(attach):
        raise store.Refusal('central backend is unavailable')
    return attach(home, receipt)


def active_backend(home: Path, inherited_env: Mapping[str, str] | None = None) -> str | None:
    resolve_backend(home, None)
    receipt, _ = _read_receipt(home)
    if receipt is None:
        return None
    if receipt['phase'] != 'ready':
        raise store.Refusal('central launch was interrupted')
    _inheritance(home, receipt, inherited_env)
    _prove(home, receipt, _backend_inspect)
    return receipt['backend']


def prepare_start(home: Path, explicit_backend: str | None, inherited_env: Mapping[str, str] | None,
                  create, inspect, attach) -> AttachTarget:
    selected = resolve_backend(home, explicit_backend)
    receipt, _ = _read_receipt(home)
    if receipt is not None:
        if receipt['phase'] != 'ready':
            raise store.Refusal('central launch was interrupted')
        if selected != receipt['backend']:
            raise store.Refusal('explicit backend differs from active central')
        env = _attach_env(home, receipt, inherited_env)
        _prove(home, receipt, inspect)
        return AttachTarget(attach(home, receipt), env)
    argv, central_env, _warning = observability.central_argv(home, ())
    launch_id, endpoint_token = str(uuid.uuid4()), uuid.uuid4().hex
    managed = [str(Path(__file__).resolve().parents[1] / 'agd'), '--home', str(home), 'observability', 'central']
    reserved = {'schema': 1, 'launch_id': launch_id, 'endpoint_token': endpoint_token, 'backend': selected,
                'phase': 'reserved', 'endpoint': {}, 'managed_command': managed,
                'expected_process_argv': argv, 'owner': None, 'watcher': None}
    with store.lock(home):
        existing, _ = _read_receipt(home)
        if existing is not None:
            raise store.Refusal('launcher receipt appeared during reservation')
        _owner(home, reserved, False)
        store.atomic_json(_receipt_path(home), reserved)
    if selected == 'herdr' and create is _backend_create:
        spawned = None

        def checkpoint(endpoint):
            nonlocal spawned
            replacement = {**(spawned or reserved), 'phase': 'spawned', 'endpoint': dict(endpoint)}
            _advance(home, spawned or reserved, replacement)
            spawned = replacement

        endpoint = create(selected, home, launch_id, endpoint_token, managed,
                          {**central_env, 'AGD_LAUNCH_ID': launch_id, 'AGD_ENDPOINT_TOKEN': endpoint_token}, argv,
                          checkpoint)
        if spawned is None or spawned['endpoint'] != endpoint:
            raise store.Refusal('Herdr endpoint checkpoint is missing')
    else:
        endpoint = create(selected, home, launch_id, endpoint_token, managed,
                          {**central_env, 'AGD_LAUNCH_ID': launch_id, 'AGD_ENDPOINT_TOKEN': endpoint_token}, argv)
        spawned = {**reserved, 'phase': 'spawned', 'endpoint': endpoint}
        _advance(home, reserved, spawned)
    if create is _backend_create:
        _backend_finalize(home, spawned)
    owner = _prove(home, spawned, inspect)
    ready = {**spawned, 'phase': 'ready',
             'owner': {'pid': owner['owner_pid'], 'start': owner['owner_start']},
             'watcher': {'pid': owner['watcher_pid'], 'start': owner['watcher_start']}}
    _advance(home, spawned, ready)
    return AttachTarget(attach(home, ready), _attach_env(home, ready, inherited_env))
