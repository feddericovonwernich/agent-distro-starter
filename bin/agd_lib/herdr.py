import json
import os
import re
import shlex
import stat
import subprocess
import uuid
from pathlib import Path
from . import installer, store


def available():
    if os.environ.get('HERDR_ENV') != '1':
        raise store.DependencyError('Herdr controls require a Herdr-managed pane')
    session = os.environ.get('AGD_HERDR_SESSION', '')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', session) or session == 'default':
        raise store.DependencyError('set AGD_HERDR_SESSION to an explicit non-default named session')
    try:
        result = store.run(['herdr', '--session', session, 'status', 'server'], check=False, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise store.DependencyError('selected Herdr server did not respond') from error
    if result.returncode:
        raise store.DependencyError('selected Herdr session is not running')
    try:
        installer._parse_herdr_status(result.stdout)
    except store.Refusal as error:
        raise store.DependencyError('selected Herdr server is incompatible') from error
    fields = dict(line.split(': ', 1) for line in result.stdout.splitlines() if ': ' in line)
    if fields.get('status') != 'running':
        raise store.DependencyError('selected Herdr session is not running')
    socket = Path(fields.get('socket', ''))
    if not socket.is_absolute():
        raise store.DependencyError('selected Herdr server socket is unverified')
    try:
        identity = socket.stat()
    except OSError as error:
        raise store.DependencyError('selected Herdr server socket is unavailable') from error
    if not stat.S_ISSOCK(identity.st_mode):
        raise store.DependencyError('selected Herdr server socket is unverified')
    return {'session': session, 'socket': str(socket), 'device': identity.st_dev,
            'inode': identity.st_ino, 'protocol': fields.get('protocol') or fields['private_protocol'],
            'config_env': config_environment()}


def call(endpoint, *args, check=True):
    if os.environ.get('HERDR_ENV') != '1':
        raise store.Refusal('Herdr control needs a managed pane')
    socket = Path(endpoint['socket'])
    try:
        identity = socket.stat()
    except OSError as error:
        raise store.Refusal('Herdr server socket absent; endpoint identity unknown') from error
    if (not stat.S_ISSOCK(identity.st_mode) or identity.st_dev != endpoint['device']
        or identity.st_ino != endpoint['inode']):
        raise store.Refusal('Herdr server replaced; endpoint identity unknown')
    try:
        result = store.run(['herdr', '--session', endpoint['session'], *args], check=False, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise store.Refusal('Herdr command timed out; endpoint state unknown') from error
    if result.returncode and check:
        raise store.Refusal(f'Herdr command refused: {result.stderr.strip() or result.stdout.strip()}')
    return result


def response(result):
    try:
        body = json.loads(result.stdout)
        if 'error' in body or 'result' not in body:
            raise ValueError('Herdr reported an unsuccessful result')
        return body['result']
    except (ValueError, KeyError) as error:
        raise store.Refusal('Herdr response unverified') from error


def prepare(home: Path) -> tuple[dict, str]:
    server = available()
    workspace_path = home/'state'/'herdr-workspace.json'
    if workspace_path.exists():
        workspace = json.loads(workspace_path.read_text())
        if any(workspace.get(field) != value for field, value in server.items()):
            raise store.Refusal('recorded Herdr workspace belongs to another server')
        workspace_id = workspace['workspace']
        actual = response(call(server, 'workspace', 'get', workspace_id))['workspace']
        if actual['workspace_id'] != workspace_id:
            raise store.Refusal('recorded Herdr workspace identity changed')
    else:
        created = response(call(server, 'workspace', 'create', '--cwd', str(home),
                                '--label', f'agd-{home.name}', '--no-focus'))
        workspace_id = created['workspace']['workspace_id']
        from . import supervision
        anchor = {name: created['root_pane'][name] for name in
                  ('pane_id', 'workspace_id', 'tab_id', 'terminal_id')} if created.get('root_pane') else None
        store.atomic_json(workspace_path, {**server, 'workspace': workspace_id,
            'target': supervision.runtime_home_identity(home), 'anchor': anchor,
            'server': server_identity(server), 'config_env': config_environment()})
    return server, workspace_id


def create(home: Path, task_id: str, incarnation: str, cwd: Path, argv: list[str]) -> dict:
    server, workspace_id = prepare(home)
    from . import supervision
    receipt = home/'state'/'tasks'/task_id/'endpoint.json'
    endpoint = {**server, 'home': str(home), 'workspace': workspace_id,
                'task': task_id, 'incarnation': incarnation, 'token': str(uuid.uuid4()),
                'command': argv[3:] if argv[:1] == ['env'] else argv,
                'target': supervision.runtime_home_identity(home), 'server': server_identity(server)}
    endpoint['config_env'] = config_environment()
    store.atomic_json(receipt, endpoint)
    created = response(call(endpoint, 'tab', 'create', '--workspace', workspace_id,
                            '--cwd', str(cwd), '--label', task_id, '--no-focus'))
    pane = created['root_pane']
    tab = created['tab']
    if pane['workspace_id'] != workspace_id or pane['tab_id'] != tab['tab_id']:
        raise store.Refusal('Herdr tab identity inconsistent; retain created resources')
    endpoint.update(tab=tab['tab_id'], pane=pane['pane_id'], terminal=pane['terminal_id'])
    store.atomic_json(receipt, endpoint)
    call(endpoint, 'pane', 'run', endpoint['pane'],
         'export PATH=' + shlex.quote(os.environ['PATH']) + '; exec ' + shlex.join(argv))
    return endpoint


def owned(endpoint):
    receipt = Path(endpoint['home'])/'state'/'tasks'/endpoint['task']/'endpoint.json'
    if not receipt.is_file() or json.loads(receipt.read_text()) != endpoint:
        raise store.Refusal('Herdr endpoint ownership receipt differs')
    result = call(endpoint, 'pane', 'get', endpoint['pane'], check=False)
    if result.returncode:
        try:
            error = json.loads(result.stderr)['error']
        except (ValueError, KeyError) as failure:
            raise store.Refusal('Herdr pane state unknown') from failure
        if error.get('code') == 'pane_not_found':
            return None
        raise store.Refusal(f'Herdr pane state unknown: {error.get("code")}')
    pane = response(result)['pane']
    if (pane['pane_id'] != endpoint['pane'] or pane['tab_id'] != endpoint['tab']
        or pane['workspace_id'] != endpoint['workspace'] or pane['terminal_id'] != endpoint['terminal']):
        raise store.Refusal('Herdr pane identity differs from recorded endpoint')
    return pane


def inspect(endpoint):
    if owned(endpoint) is None:
        return {'presence': 'missing'}
    process = response(call(endpoint, 'pane', 'process-info', '--pane', endpoint['pane']))['process_info']
    if process['pane_id'] != endpoint['pane']:
        raise store.Refusal('Herdr process identity differs from endpoint')
    return {'presence': 'present', 'source': 'herdr', 'process': process}


def capture(endpoint, lines=120):
    if owned(endpoint) is None:
        raise store.Refusal('Herdr pane missing')
    return call(endpoint, 'pane', 'read', endpoint['pane'], '--source', 'recent-unwrapped',
                '--lines', str(lines)).stdout


def send_text(endpoint, text):
    if owned(endpoint) is None:
        raise store.Refusal('Herdr pane missing')
    call(endpoint, 'pane', 'send-text', endpoint['pane'], text)
    return {'delivery': 'unconfirmed'}


def send_key(endpoint, key):
    if key not in ('enter', 'escape'):
        raise ValueError('unsupported key')
    if owned(endpoint) is None:
        raise store.Refusal('Herdr pane missing')
    call(endpoint, 'pane', 'send-keys', endpoint['pane'], 'esc' if key == 'escape' else 'enter')
    return {'delivery': 'unconfirmed'}


def close(endpoint):
    if owned(endpoint) is None:
        return {'presence': 'missing'}
    call(endpoint, 'pane', 'close', endpoint['pane'])
    return {'presence': inspect(endpoint)['presence']}


def server_identity(endpoint):
    """Bind a listening socket's kernel owner without opening a protocol channel."""
    from . import supervision
    path = Path(endpoint['socket'])
    mode = path.lstat()
    if (not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid()
            or (mode.st_dev, mode.st_ino) != (endpoint['device'], endpoint['inode'])):
        raise store.Refusal('Herdr server socket identity differs')
    sockets = [fields[6] for line in Path('/proc/net/unix').read_text().splitlines()[1:]
               if len(fields := line.split(maxsplit=7)) == 8 and fields[7] == str(path)]
    if len(sockets) != 1:
        raise store.Refusal('Herdr listening socket identity is ambiguous')
    owners = []
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid == os.getuid() and any(
                    os.readlink(fd) == f'socket:[{sockets[0]}]' for fd in (directory / 'fd').iterdir()):
                owners.append(int(directory.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    if len(owners) != 1:
        raise store.Refusal('Herdr socket process ownership is ambiguous')
    identity = supervision.process_identity(owners[0])
    identity.pop('state')
    if identity['uid'] != os.getuid():
        raise store.Refusal('Herdr server user identity differs')
    return {'process': identity, 'socket': {'path': str(path), 'device': mode.st_dev,
                                          'inode': mode.st_ino, 'uid': mode.st_uid}}


def _uninstall_probe(home, identity):
    from . import launcher, supervision
    endpoint = identity['endpoint']
    def refused(reason):
        return {'presence': 'unknown', 'ownership': 'unknown',
                'blockers': [{'code': 'herdr-endpoint', 'reason': reason}]}
    try:
        target = endpoint.get('target')
        if (not isinstance(target, dict) or target.get('canonical_home') != str(home)
                or target.get('uid') != os.getuid()
                or endpoint.get('home', str(home)) != str(home)):
            return refused('Herdr endpoint lacks this home binding')
        if os.path.lexists(home) and target != supervision.runtime_home_identity(home):
            return refused('Herdr endpoint belongs to a different home inode')
        server = endpoint.get('server')
        if server and not supervision.process_presence(server['process']):
            if not os.path.lexists(endpoint['socket']):
                return {'presence': 'absent', 'ownership': 'owned', 'blockers': []}
            return refused('Herdr socket remains after its recorded server exited')
        path = Path(endpoint['socket'])
        mode = path.lstat()
        if (not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid()
                or (mode.st_dev, mode.st_ino) != (endpoint['device'], endpoint['inode'])):
            return refused('Herdr socket was replaced')
        if server:
            actual = server_identity(endpoint)
            if any(actual['process'][name] != server['process'][name] for name in ('pid', 'start', 'uid')):
                return refused('Herdr peer process was replaced')
        result = launcher._herdr_call(endpoint, 'pane', 'get', endpoint['pane'], check=False)
        if result.returncode:
            error = json.loads(result.stderr or result.stdout).get('error', {})
            if error.get('code') == 'pane_not_found':
                return {'presence': 'absent', 'ownership': 'owned', 'blockers': []}
            return refused('Herdr did not prove pane absence')
        pane = response(result)['pane']
        expected = {'pane_id': endpoint['pane'], 'workspace_id': endpoint['workspace'],
                    'tab_id': endpoint['tab'], 'terminal_id': endpoint['terminal']}
        if any(pane.get(name) != value for name, value in expected.items()):
            return refused('Herdr pane identity or membership differs')
        return {'presence': 'present', 'ownership': 'owned', 'blockers': []}
    except (OSError, ValueError, KeyError, TypeError, store.Refusal):
        return refused('Herdr endpoint inspection is unavailable or malformed')


def _uninstall_grace(home, identity):
    from . import launcher
    probe = _uninstall_probe(home, identity)
    if probe['ownership'] != 'owned' or probe['presence'] == 'unknown':
        raise store.Refusal('Herdr quit endpoint is unproven')
    if probe['presence'] == 'present':
        endpoint = identity['endpoint']
        launcher._herdr_call(endpoint, 'pane', 'send-keys', endpoint['pane'], 'esc')
        launcher._herdr_call(endpoint, 'pane', 'send-text', endpoint['pane'], '/quit')
        launcher._herdr_call(endpoint, 'pane', 'send-keys', endpoint['pane'], 'enter')


def uninstall_close(home, resource, operation):
    from . import launcher, uninstall_state
    approved = uninstall_state.require_scope(home, operation, resource['key'])
    identity = approved['identity']
    probe = _uninstall_probe(home, identity)
    if probe['ownership'] != 'owned' or probe['presence'] == 'unknown':
        raise store.Refusal('Herdr endpoint cannot be safely closed')
    if probe['presence'] == 'present':
        if identity.get('task_id'):
            path = home / 'state/tasks' / identity['task_id'] / 'endpoint.json'
            if not os.path.lexists(path):
                raise store.Refusal('Herdr owned endpoint receipt is missing')
            raw = uninstall_state.read_evidence(path, private=True)
            if uninstall_state.fingerprint(raw) != identity['endpoint_digest']:
                raise store.Refusal('Herdr endpoint receipt changed')
        else:
            import hashlib
            raw, _ = launcher._read_receipt(home)
            if (raw is None or raw['launch_id'] != identity['launch_id']
                    or hashlib.sha256(raw['endpoint_token'].encode()).hexdigest()
                    != identity['endpoint']['token_sha256']):
                raise store.Refusal('Herdr central launch receipt changed')
        endpoint = identity['endpoint']
        launcher._herdr_call(endpoint, 'pane', 'close', endpoint['pane'])
    if _uninstall_probe(home, identity)['presence'] != 'absent':
        raise store.Refusal('Herdr pane absence remains unproven')
    uninstall_state.checkpoint(home, operation, phase='quiescing', completed=approved['key'])


def config_environment():
    home = str(Path.home())
    config = os.environ.get('XDG_CONFIG_HOME') or str(Path(home) / '.config')
    return {'HOME': home, 'XDG_CONFIG_HOME': config,
            'HERDR_CONFIG_PATH': os.environ.get('HERDR_CONFIG_PATH') or str(Path(config) / 'herdr/config.toml')}


def control_environment(endpoint):
    value = endpoint.get('config_env')
    if (not isinstance(value, dict) or set(value) != {'HOME', 'XDG_CONFIG_HOME', 'HERDR_CONFIG_PATH'}
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in value.values())):
        raise store.Refusal('Herdr control namespace is missing or unsafe')
    return dict(os.environ, **value)
