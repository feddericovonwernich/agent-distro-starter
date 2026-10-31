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

from agd_lib import installer, observability, store, supervision


@dataclass(frozen=True)
class AttachTarget:
    attach_argv: list[str]
    env: dict[str, str]


def initialize(home: Path, backend: str) -> None:
    config = home / 'data' / 'launcher.json'
    from .uninstall_state import admission
    with admission(home):
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

def _await_initial_owner(home: Path, receipt: dict, inspect) -> dict:
    deadline = time.monotonic() + 10
    while True:
        try:
            return _prove(home, receipt, inspect)
        except store.Refusal as error:
            if (str(error) not in {'central owner is missing', 'Herdr foreground process is unverified'}
                    or time.monotonic() >= deadline):
                raise
            time.sleep(.05)





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


def _prove_herdr_server(endpoint: dict, process) -> dict:
    pid = getattr(process, 'pid', None)
    if not isinstance(pid, int) or pid < 1:
        raise store.Refusal('Herdr server ownership is unproven')
    start = supervision.process_start(pid)
    try:
        socket_inode = next(
            fields[6] for line in Path('/proc/net/unix').read_text().splitlines()[1:]
            if len(fields := line.split(maxsplit=7)) == 8 and fields[7] == endpoint['socket'])
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
        owns_socket = any(os.readlink(fd) == f'socket:[{socket_inode}]'
                          for fd in Path(f'/proc/{pid}/fd').iterdir())
    except (OSError, StopIteration, UnicodeDecodeError) as error:
        raise store.Refusal('Herdr server ownership is unproven') from error
    if (not start or argv != ['herdr', '--session', endpoint['session'], 'server']
            or not owns_socket):
        raise store.Refusal('Herdr server ownership is unproven')
    return {'server_pid': pid, 'server_start': start, 'server_argv': argv}


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
    if result.returncode:
        raise store.Refusal('Herdr session is not running')
    try:
        installer._parse_herdr_status(result.stdout)
    except store.Refusal as error:
        raise store.Refusal('Herdr session is not running') from error
    fields = dict(line.split(': ', 1) for line in result.stdout.splitlines() if ': ' in line)
    if fields.get('status') != 'running':
        raise store.Refusal('Herdr session is not running')
    socket = Path(fields.get('socket', ''))
    if not socket.is_absolute():
        raise store.Refusal('Herdr server socket is unverified')
    identity = _socket_identity(socket)
    return {'session': session, 'socket': str(socket), 'device': identity.st_dev,
            'inode': identity.st_ino, 'protocol': fields.get('protocol') or fields['private_protocol']}


def _herdr_call(endpoint: dict, *args, check=True):
    identity = _socket_identity(Path(endpoint['socket']))
    if identity.st_dev != endpoint['device'] or identity.st_ino != endpoint['inode']:
        raise store.Refusal('Herdr server replaced; endpoint identity unknown')
    try:
        env = dict(os.environ, HERDR_CONFIG_PATH=endpoint['config_path']) if endpoint.get('config_path') else None
        result = store.run(['herdr', '--session', endpoint['session'], *args], check=False, timeout=10, env=env)
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
        if not isinstance(foreground, list):
            raise store.Refusal('Herdr foreground process is unverified')
        matched = [item for item in foreground if isinstance(item, dict)
                   and item.get('argv') == receipt['expected_process_argv']]
        if len(matched) != 1:
            raise store.Refusal('Herdr foreground process is unverified')
        pid, argv = matched[0].get('pid'), matched[0]['argv']
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
    process = subprocess.Popen(['herdr', '--session', session, 'server'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                               cwd=home)
    endpoint = None
    for _ in range(30):
        try:
            endpoint = _herdr_status(session)
            break
        except store.Refusal:
            time.sleep(.1)
    if endpoint is None:
        raise store.Refusal('Herdr server did not become available')
    endpoint.update(_prove_herdr_server(endpoint, process) or {})
    from . import herdr
    endpoint['server'] = herdr.server_identity(endpoint)
    endpoint['server']['target'] = supervision.runtime_home_identity(home)
    endpoint['target'] = supervision.runtime_home_identity(home)
    endpoint['config_path'] = herdr.config_path()
    if checkpoint:
        checkpoint(endpoint)
    created_workspace = _herdr_response(_herdr_call(
        endpoint, 'workspace', 'create', '--cwd', str(home), '--label', 'agd-central', '--no-focus'))
    workspace = created_workspace['workspace']
    if isinstance(created_workspace.get('root_pane'), dict):
        endpoint['anchor'] = {name: created_workspace['root_pane'][name] for name in
                              ('pane_id', 'workspace_id', 'tab_id', 'terminal_id')}
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
    command_env = {key: value for key, value in env.items()
                   if key != 'HERDR_ENV' and isinstance(key, str) and isinstance(value, str)}
    command_env.update(AGD_HERDR_SESSION=session, AGD_LAUNCH_ID=launch_id,
                       AGD_ENDPOINT_TOKEN=endpoint_token)
    for key, value in command_env.items():
        _herdr_call(endpoint, 'pane', 'run', endpoint['pane'],
                    f'export {key}={shlex.quote(value)}')
    _herdr_call(endpoint, 'pane', 'run', endpoint['pane'],
                'exec ' + shlex.join(managed))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        process = _herdr_response(_herdr_call(endpoint, 'pane', 'process-info', '--pane', endpoint['pane']))['process_info']
        if process.get('argv') == argv and isinstance(process.get('pid'), int):
            return endpoint
        foreground = process.get('foreground_processes')
        if isinstance(foreground, list) and any(
                isinstance(item, dict) and item.get('argv') == argv for item in foreground):
            return endpoint
        time.sleep(.05)
    raise store.Refusal('Herdr central process did not start')


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
    from .uninstall_state import admission
    with admission(home):
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
                    'expected_process_argv': argv, 'owner': None, 'watcher': None,
                    'target': supervision.runtime_home_identity(home)}
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
        owner = _await_initial_owner(home, spawned, inspect)
        ready = {**spawned, 'phase': 'ready',
                 'owner': {'pid': owner['owner_pid'], 'start': owner['owner_start']},
                 'watcher': {'pid': owner['watcher_pid'], 'start': owner['watcher_start']},
                 'processes': [owner.get('owner_identity'), owner.get('watcher_identity')]}
        _advance(home, spawned, ready)
        return AttachTarget(attach(home, ready), _attach_env(home, ready, inherited_env))


def _uninstall_endpoint(endpoint, target):
    safe = {name: value for name, value in endpoint.items() if name not in {'token', 'server_argv'}}
    safe['token_sha256'] = hashlib.sha256(endpoint.get('token', '').encode()).hexdigest()
    safe['target'] = target
    return safe


def _uninstall_process(pid, start, recorded=None):
    if recorded and recorded.get('pid') == pid and recorded.get('start') == start:
        return recorded
    try:
        current = supervision.process_identity(pid)
        current.pop('state')
        if current['start'] == start and current['uid'] == os.getuid():
            return current
    except (FileNotFoundError, ProcessLookupError):
        pass
    return {'pid': pid, 'start': start, 'uid': os.getuid(),
            'ppid': None, 'pgid': None, 'sid': None, 'argv_sha256': None}


def _uninstall_server_probe(home, identity, *, late=False):
    from . import herdr, tmux
    server, backend = identity['server'], identity['backend']
    process = server['process']
    try:
        if os.path.lexists(home) and server['target'] != supervision.runtime_home_identity(home):
            raise store.Refusal('backend server home inode differs')
        alive = supervision.process_presence(process)
        socket_path = Path(server['socket']['path'])
        if alive:
            if backend == 'tmux':
                tmux._uninstall_server(home, server)
                panes = store.run([*tmux.prefix(home), 'list-panes', '-a', '-F', '#{pane_id}']).stdout.splitlines()
            else:
                peer = herdr.server_identity(identity['endpoint'])
                if any(peer['process'][name] != process[name] for name in ('pid', 'start', 'uid')):
                    raise store.Refusal('backend peer differs')
                panes = [row['pane_id'] for row in _herdr_response(
                    _herdr_call(identity['endpoint'], 'pane', 'list'))['panes']]
            allowed = identity['anchors'] if late else identity['panes']
            if set(panes) - set(allowed):
                raise store.Refusal('backend contains an unrelated or unretired pane')
            if backend == 'tmux' and server.get('anchor'):
                anchor = server['anchor']
                if anchor['pane'] in panes:
                    token = store.run([*tmux.prefix(home), 'show-options', '-p', '-v', '-t',
                                       anchor['pane'], '@agd_anchor_owner']).stdout.strip()
                    if hashlib.sha256(token.encode()).hexdigest() != anchor['token_sha256']:
                        raise store.Refusal('tmux anchor token differs')
        elif os.path.lexists(socket_path):
            mode = socket_path.lstat()
            if (mode.st_uid != os.getuid() or not stat.S_ISSOCK(mode.st_mode)
                    or (mode.st_dev, mode.st_ino) != (server['socket']['device'], server['socket']['inode'])):
                raise store.Refusal('backend socket was replaced after server exit')
        return {'presence': 'present' if alive or os.path.lexists(socket_path) else 'absent',
                'ownership': 'owned', 'blockers': []}
    except (OSError, ValueError, KeyError, TypeError, store.Refusal):
        return {'presence': 'unknown', 'ownership': 'unknown', 'blockers': [
            {'code': 'backend-continuity', 'reason': 'backend ownership, transport or pane continuity is unproven'}]}


def uninstall_inventory(home, *, expected=None):
    from . import herdr, tmux, uninstall_state
    expected = {row['key']: row for row in expected or []}
    rows, servers = {}, {}
    receipt_path = _receipt_path(home)
    target = supervision.runtime_home_identity(home) if os.path.lexists(home) else None
    if os.path.lexists(receipt_path):
        uninstall_state.read_evidence(receipt_path, private=True)
        receipt, _ = _read_receipt(home)
        key = f"central:{receipt['launch_id']}"
        endpoint = dict(receipt['endpoint'])
        binding = receipt.get('target')
        owner_path = home / 'state/owner.json'
        owner = uninstall_state.read_evidence(owner_path, private=True) if os.path.lexists(owner_path) else None
        processes, blockers = [], []
        if owner:
            if owner.get('token') != receipt['endpoint_token']:
                blockers.append({'code': 'central-token', 'reason': 'central owner token differs'})
            for name in ('owner', 'watcher'):
                processes.append(_uninstall_process(
                    owner[f'{name}_pid'], owner[f'{name}_start'], owner.get(f'{name}_identity')))
        else:
            processes = [item for item in receipt.get('processes', []) if item]
            if not processes:
                processes = [_uninstall_process(item['pid'], item['start'])
                             for item in (receipt.get('owner'), receipt.get('watcher')) if item]
        if binding is None and processes:
            try:
                home_index = receipt['managed_command'].index('--home') + 1
                if receipt['managed_command'][home_index] != str(home):
                    raise store.Refusal('copied central home path differs')
                live = [item for item in processes if supervision.process_presence(item)]
                if live:
                    _backend_inspect(home, receipt)
                    mode = Path(f"/proc/{processes[0]['pid']}/cwd").stat()
                    if (mode.st_dev, mode.st_ino) != (target['device'], target['inode']):
                        raise store.Refusal('legacy central cwd inode differs')
                binding = target
            except (OSError, ValueError, KeyError, store.Refusal):
                blockers.append({'code': 'central-legacy', 'reason': 'legacy central home binding is unproven'})
        if binding != target:
            blockers.append({'code': 'central-home', 'reason': 'central belongs to a different home identity'})
        if endpoint and receipt['backend'] == 'herdr' and not endpoint.get('server'):
            pid, start = endpoint.get('server_pid'), endpoint.get('server_start')
            if pid and start and binding == target:
                server_process = _uninstall_process(pid, start)
                endpoint['server'] = {'target': binding, 'process': server_process,
                    'socket': {'path': endpoint['socket'], 'device': endpoint['device'],
                               'inode': endpoint['inode'], 'uid': os.getuid()}}
        safe = _uninstall_endpoint(endpoint, binding)
        identity = {'target': binding, 'backend': receipt['backend'], 'launch_id': receipt['launch_id'],
                    'endpoint': safe, 'processes': processes,
                    'argv_sha256': uninstall_state.fingerprint(receipt['expected_process_argv'])}
        if not endpoint.get('pane'):
            probe = {'presence': 'unknown', 'ownership': 'unknown', 'blockers': [
                {'code': 'central-publication', 'reason': 'interrupted central has no complete endpoint'}]}
        else:
            module = tmux if receipt['backend'] == 'tmux' else herdr
            probe = module._uninstall_probe(home, identity)
        if blockers:
            probe = {'presence': 'unknown', 'ownership': 'unknown', 'blockers': blockers}
        elif any(supervision.process_presence(item) for item in processes) and probe['ownership'] == 'owned':
            probe['presence'] = 'present'
        rows[key] = {'key': key, 'kind': 'central', 'identity': identity, **probe}
        if endpoint.get('server') and endpoint['server'].get('target') == binding:
            server = endpoint['server']
            server_key = f"backend-server:{receipt['backend']}:{server['process']['pid']}:{server['process']['start']}"
            anchors = [endpoint['anchor']['pane_id']] if endpoint.get('anchor') else []
            if receipt['backend'] == 'tmux' and server.get('anchor'):
                anchors.append(server['anchor']['pane'])
            servers[server_key] = {'backend': receipt['backend'], 'server': server, 'endpoint': safe,
                                  'panes': [endpoint['pane']] if endpoint.get('pane') else [],
                                  'anchors': anchors, 'workspaces': [endpoint['workspace']] if endpoint.get('workspace') else []}
    tmux_path = home / 'state/tmux-server.json'
    if os.path.lexists(tmux_path):
        server = uninstall_state.read_evidence(tmux_path, private=True)
        key = f"backend-server:tmux:{server['process']['pid']}:{server['process']['start']}"
        if key not in servers:
            anchors = [server['anchor']['pane']] if server.get('anchor') else []
            servers[key] = {'backend': 'tmux', 'server': server, 'endpoint': {},
                            'panes': [], 'anchors': anchors, 'workspaces': []}
    workspace_path = home / 'state/herdr-workspace.json'
    if os.path.lexists(workspace_path):
        workspace = uninstall_state.read_evidence(workspace_path, private=True)
        key = f"backend-workspace:herdr:{workspace['workspace']}"
        identity = {name: workspace.get(name) for name in (
            'target', 'session', 'socket', 'device', 'inode', 'protocol', 'workspace', 'anchor', 'server', 'config_path')}
        rows[key] = {'key': key, 'kind': 'backend-workspace', 'identity': identity,
                     **_uninstall_workspace_probe(home, identity)}
        for server in servers.values():
            if server['backend'] == 'herdr' and server['endpoint']['socket'] == workspace['socket']:
                server['workspaces'].append(workspace['workspace'])
                if workspace.get('anchor'):
                    server['anchors'].append(workspace['anchor']['pane_id'])
    for path in sorted((home / 'data/tasks').glob('*/task.json')):
        task = uninstall_state.read_evidence(path, private=True)
        for name in ('endpoint', 'prior_endpoint'):
            endpoint = task.get(name)
            if not endpoint:
                continue
            for server in servers.values():
                if (server['backend'] == task.get('backend') and endpoint.get('pane')
                        and endpoint.get('server', {}).get('process', {}).get('pid') == server['server']['process']['pid']):
                    server['panes'].append(endpoint['pane'])
    for key, identity in servers.items():
        identity['panes'] = sorted(set(identity['panes'] + identity['anchors']))
        identity['anchors'] = sorted(set(identity['anchors']))
        identity['workspaces'] = sorted(set(identity['workspaces']))
        rows[key] = {'key': key, 'kind': 'backend-server', 'identity': identity,
                     **_uninstall_server_probe(home, identity)}
    fields = ('pid', 'start', 'uid', 'ppid', 'pgid', 'sid', 'argv_sha256')
    for path in sorted((home / 'state/native-omp').glob('*.json')):
        record = uninstall_state.read_evidence(path, private=True)
        process = {name: record.get(name) for name in fields}
        key = f"native-omp:{process['pid']}:{process['start']}"
        if key in expected and expected[key]['identity']['process']['pid'] == process['pid']:
            process = expected[key]['identity']['process']
        live = supervision.process_presence(process)
        blockers = []
        if live and record['target'] != target:
            blockers.append({'code': 'native-home', 'reason': 'native OMP belongs to another home inode'})
        if live and any(process[name] is None for name in fields):
            process = _uninstall_process(process['pid'], process['start'])
        rows[key] = {'key': key, 'kind': 'native-omp',
                     'identity': {'target': record['target'], 'process': process, 'executable': record['executable']},
                     'presence': 'unknown' if blockers else 'present' if live else 'absent',
                     'ownership': 'unknown' if blockers else 'owned', 'blockers': blockers}
    for key, saved in expected.items():
        if key in rows:
            continue
        identity = saved['identity']
        if saved['kind'] in {'central', 'native-omp'}:
            processes = identity['processes'] if saved['kind'] == 'central' else [identity['process']]
            live = any(supervision.process_presence(item) for item in processes)
            rows[key] = {**saved, 'presence': 'present' if live else 'absent', 'blockers': []}
        elif saved['kind'] == 'backend-server':
            rows[key] = {**saved, **_uninstall_server_probe(home, identity)}
        elif saved['kind'] == 'backend-workspace':
            rows[key] = {**saved, **_uninstall_workspace_probe(home, identity)}
    return list(rows.values())


def _uninstall_workspace_probe(home, identity):
    try:
        if (not identity.get('target') or os.path.lexists(home)
                and identity['target'] != supervision.runtime_home_identity(home)):
            raise store.Refusal('workspace home binding differs')
        if identity.get('server') and not supervision.process_presence(identity['server']['process']):
            return {'presence': 'absent', 'ownership': 'owned', 'blockers': []}
        result = _herdr_call(identity, 'workspace', 'get', identity['workspace'], check=False)
        if result.returncode:
            if json.loads(result.stderr or result.stdout).get('error', {}).get('code') == 'workspace_not_found':
                return {'presence': 'absent', 'ownership': 'owned', 'blockers': []}
            raise store.Refusal('workspace absence is unproven')
        if _herdr_response(result)['workspace']['workspace_id'] != identity['workspace']:
            raise store.Refusal('workspace identity differs')
        return {'presence': 'present', 'ownership': 'owned', 'blockers': []}
    except (OSError, ValueError, KeyError, TypeError, store.Refusal):
        return {'presence': 'unknown', 'ownership': 'unknown', 'blockers': [
            {'code': 'herdr-workspace', 'reason': 'workspace binding or transport is unproven'}]}


def uninstall_quiesce(home, resources, operation):
    from . import herdr, tmux, uninstall_state
    for resource in resources:
        if resource['kind'] not in {'central', 'native-omp'}:
            continue
        approved = uninstall_state.require_scope(home, operation, resource['key'])
        current = next(row for row in uninstall_inventory(home, expected=[approved]) if row['key'] == approved['key'])
        if current['identity'] != approved['identity'] or current['ownership'] != 'owned' or current['presence'] == 'unknown':
            raise store.Refusal('central/native uninstall identity changed')
        identity = approved['identity']
        processes = identity['processes'] if approved['kind'] == 'central' else [identity['process']]
        if current['presence'] == 'present' and approved['kind'] == 'central':
            module = tmux if identity['backend'] == 'tmux' else herdr
            module._uninstall_grace(home, identity)
            deadline = time.monotonic() + 3
            while any(supervision.process_presence(item) for item in processes) and time.monotonic() < deadline:
                time.sleep(.05)
        live = [item for item in processes if supervision.process_presence(item)]
        if live:
            supervision.stop_owned_processes(live)
        if approved['kind'] == 'central':
            module = tmux if identity['backend'] == 'tmux' else herdr
            module.uninstall_close(home, approved, operation)
        if any(supervision.process_presence(item) for item in processes):
            raise store.Refusal('central/native process absence is unproven')
        uninstall_state.checkpoint(home, operation, phase='quiescing', completed=approved['key'])


def uninstall_remove(home, resources, operation):
    from . import tmux, uninstall_state
    for resource in sorted(resources, key=lambda row: row['kind'] == 'backend-server'):
        if resource['kind'] not in {'backend-workspace', 'backend-server'}:
            continue
        approved = uninstall_state.require_scope(home, operation, resource['key'])
        identity = approved['identity']
        if approved['kind'] == 'backend-workspace':
            probe = _uninstall_workspace_probe(home, identity)
            if probe['ownership'] != 'owned' or probe['presence'] == 'unknown':
                raise store.Refusal('workspace cannot be retired')
            if probe['presence'] == 'present':
                panes = _herdr_response(_herdr_call(identity, 'pane', 'list', '--workspace', identity['workspace']))['panes']
                anchor = identity.get('anchor')
                if any(not anchor or pane['pane_id'] != anchor['pane_id'] for pane in panes):
                    raise store.Refusal('workspace contains an unrelated or unretired pane')
                _herdr_call(identity, 'workspace', 'close', identity['workspace'])
            if _uninstall_workspace_probe(home, identity)['presence'] != 'absent':
                raise store.Refusal('workspace remains after close')
        else:
            probe = _uninstall_server_probe(home, identity, late=True)
            if probe['ownership'] != 'owned' or probe['presence'] == 'unknown':
                raise store.Refusal('backend server cannot be retired')
            server = identity['server']
            if supervision.process_presence(server['process']):
                if identity['backend'] == 'tmux':
                    store.run([*tmux.prefix(home), 'kill-server'])
                else:
                    for workspace in identity['workspaces']:
                        result = _herdr_call(identity['endpoint'], 'workspace', 'get', workspace, check=False)
                        if not result.returncode:
                            _herdr_call(identity['endpoint'], 'workspace', 'close', workspace)
                    supervision.stop_owned_processes([server['process']])
            if supervision.process_presence(server['process']):
                raise store.Refusal('backend server remains alive')
            path = Path(server['socket']['path'])
            if os.path.lexists(path):
                mode = path.lstat()
                if (not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid()
                        or (mode.st_dev, mode.st_ino) != (server['socket']['device'], server['socket']['inode'])):
                    raise store.Refusal('backend socket changed before removal')
                uninstall_state.checkpoint(home, operation, phase='removing', irreversible=True)
                path.unlink()
            if identity['backend'] == 'herdr':
                endpoint = identity['endpoint']
                env = dict(os.environ, HERDR_CONFIG_PATH=endpoint['config_path']) if endpoint.get('config_path') else None
                listed = json.loads(store.run(['herdr', 'session', 'list', '--json'], env=env).stdout)
                sessions = listed['sessions'] if isinstance(listed, dict) else listed
                selected = [item for item in sessions if item['name'] == endpoint['session']]
                if selected:
                    if (len(selected) != 1 or selected[0].get('running')
                            or Path(selected[0]['session_dir']) != Path(endpoint['socket']).parent):
                        raise store.Refusal('stopped Herdr session directory differs')
                    uninstall_state.checkpoint(home, operation, phase='removing', irreversible=True)
                    deleted = json.loads(store.run(
                        ['herdr', 'session', 'delete', endpoint['session'], '--json'], env=env).stdout)
                    if deleted.get('deleted') is not True:
                        raise store.Refusal('Herdr session deletion is unconfirmed')
                    remaining = json.loads(store.run(['herdr', 'session', 'list', '--json'], env=env).stdout)
                    remaining = remaining['sessions'] if isinstance(remaining, dict) else remaining
                    if any(item['name'] == endpoint['session'] for item in remaining):
                        raise store.Refusal('Herdr named session remains after deletion')
        uninstall_state.checkpoint(home, operation, phase='removing', completed=approved['key'])
