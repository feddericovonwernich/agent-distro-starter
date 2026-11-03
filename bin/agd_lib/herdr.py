from collections.abc import Mapping
import json
import os
import re
import shlex
import stat
import subprocess
import threading
import uuid
import time
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
               if len(fields := line.split(maxsplit=7)) == 8 and fields[7] == str(path)
               and int(fields[3], 16) & 0x10000]  # SO_ACCEPTCON: connected peers can share the path.
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


def _reviewer_run_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error


def _reviewer_command(command):
    if (not isinstance(command, list) or not command
            or not all(isinstance(item, str) and item for item in command)):
        raise store.Refusal('reviewer command is malformed')


def _reviewer_call(endpoint, *args, check=True):
    from . import launcher
    return launcher._herdr_call(endpoint, *args, check=check)
def _reviewer_client_socket(endpoint):
    api = Path(endpoint['socket'])
    if api.suffix != '.sock':
        raise store.Refusal('Herdr client socket path is unavailable')
    return api.with_name(api.stem + '-client.sock')


def _prove_reviewer_startup(endpoint):
    client = _reviewer_client_socket(endpoint)
    try:
        identity = client.lstat()
    except OSError as error:
        raise store.Refusal('Herdr client listener is unavailable') from error
    if not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.getuid():
        raise store.Refusal('Herdr client listener identity is unsafe')
    try:
        inode = next(
            fields[6] for line in Path('/proc/net/unix').read_text().splitlines()[1:]
            if len(fields := line.split(maxsplit=7)) == 8 and fields[7] == str(client)
            and int(fields[3], 16) & 0x10000)
        owners = []
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if any(os.readlink(fd) == f'socket:[{inode}]'
                       for fd in (directory / 'fd').iterdir()):
                    owners.append(int(directory.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    except (OSError, StopIteration) as error:
        raise store.Refusal('Herdr client listener ownership is unavailable') from error
    process = endpoint['server']['process']
    if owners != [process['pid']]:
        raise store.Refusal('Herdr client listener owner differs')
    endpoint['client_socket'] = {
        'path': str(client), 'device': identity.st_dev, 'inode': identity.st_ino,
        'uid': identity.st_uid,
    }

def reviewer_create(home: Path, review_run_id: str, command: list[str], *, cwd: Path,
                    env: Mapping[str, str]) -> dict:
    """Create a disposable, run-owned Herdr endpoint for a reviewer child."""
    from . import launcher, supervision
    run_id = _reviewer_run_id(review_run_id)
    _reviewer_command(command)
    home, cwd = Path(home).resolve(), Path(cwd).resolve()
    if not cwd.is_relative_to(home) or not cwd.is_dir():
        raise store.Refusal('reviewer cwd is outside its home')
    if (not isinstance(env, Mapping) or not all(
            isinstance(key, str) and key.isidentifier() and isinstance(value, str)
            for key, value in env.items())):
        raise store.Refusal('reviewer environment is malformed')
    session = f'agd-reviewer-{run_id.replace("-", "")}'
    config_env = config_environment()
    server_env = dict(os.environ)
    server_env.update(config_env)
    server_env.pop('HERDR_ENV', None)
    stderr_buffer = bytearray()
    process = None
    endpoint = None
    stderr_thread = None
    try:
        process = subprocess.Popen(['herdr', '--session', session, 'server'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   cwd=str(home), env=server_env)
        def capture_stderr():
            for chunk in iter(lambda: process.stderr.read(8192), b''):
                if len(stderr_buffer) < 8192:
                    stderr_buffer.extend(chunk[:8192 - len(stderr_buffer)])
        stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
        stderr_thread.start()
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, 15)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, 9)
                except OSError:
                    pass
            if process.poll() is None:
                raise store.Refusal('reviewer Herdr server cleanup is unproven')
            try:
                launcher._herdr_status(session)
            except store.Refusal:
                pass
            else:
                raise store.Refusal('reviewer Herdr server remains active after cleanup')
        if stderr_thread is not None:
            stderr_thread.join(.5)
        raise
    try:
        for _ in range(100):
            if process.poll() is not None:
                stderr_thread.join(.2)
                detail = stderr_buffer.decode('utf-8', errors='replace').strip()
                raise store.Refusal(
                    'reviewer Herdr server exited during startup'
                    + (f'; server stderr: {detail}' if detail else ''))
            try:
                candidate = launcher._herdr_status(session)
                candidate.update(launcher._prove_herdr_server(candidate, process))
                candidate['server'] = server_identity(candidate)
                candidate['server']['target'] = supervision.runtime_home_identity(home)
                _prove_reviewer_startup(candidate)
                endpoint = candidate
                break
            except store.Refusal:
                time.sleep(.1)
        if endpoint is None:
            detail = stderr_buffer.decode('utf-8', errors='replace').strip()
            raise store.Refusal(
                'reviewer Herdr server did not complete startup'
                + (f'; server stderr: {detail}' if detail else ''))
        endpoint.update({'schema': 1, 'backend': 'herdr', 'review_run_id': run_id,
                         'home': str(home), 'target': supervision.runtime_home_identity(home),
                         'config_env': config_env, 'expected_process_argv': list(command)})
        workspace = response(_reviewer_call(endpoint, 'workspace', 'create', '--cwd', str(cwd),
                                            '--label', session, '--no-focus'))['workspace']
        workspace_id = workspace['workspace_id']
        tab_result = response(_reviewer_call(
            endpoint, 'tab', 'create', '--workspace', workspace_id,
            '--cwd', str(cwd), '--label', 'reviewer', '--no-focus'))
        tab, pane = tab_result['tab'], tab_result['root_pane']
        if (pane.get('workspace_id') != workspace_id or pane.get('tab_id') != tab.get('tab_id')):
            raise store.Refusal('reviewer Herdr endpoint identity is inconsistent')
        endpoint.update(workspace=workspace_id, tab=tab['tab_id'], pane=pane['pane_id'],
                        terminal=pane['terminal_id'])
        env_file = cwd / '.reviewer-env'
        content = ''.join(f'{key}={shlex.quote(value)}\n' for key, value in sorted(env.items()))
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        endpoint['env_file'] = str(env_file)
        script = f"set -a; . {shlex.quote(str(env_file))}; set +a; exec {shlex.join(command)}"
        _reviewer_call(endpoint, 'pane', 'run', endpoint['pane'], script)
        return endpoint
    except BaseException:
        try:
            if endpoint is not None:
                reviewer_close(home, endpoint)
            else:
                os.killpg(process.pid, 15)
        except BaseException:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
        stderr_thread.join(.5)
        raise


def _reviewer_endpoint(endpoint):
    if (not isinstance(endpoint, dict) or endpoint.get('backend') != 'herdr'
            or not isinstance(endpoint.get('review_run_id'), str)
            or not isinstance(endpoint.get('home'), str)
            or not isinstance(endpoint.get('session'), str)):
        raise store.Refusal('reviewer Herdr endpoint is malformed')
    run_id = _reviewer_run_id(endpoint['review_run_id'])
    if endpoint['session'] != f'agd-reviewer-{run_id.replace("-", "")}':
        raise store.Refusal('reviewer Herdr session is not run-scoped')
    return endpoint


def reviewer_capture(home: Path, receipt: dict, lines: int = 120) -> str:
    endpoint = _reviewer_endpoint(receipt)
    if Path(endpoint['home']).resolve() != Path(home).resolve():
        raise store.Refusal('reviewer Herdr endpoint home differs')
    if type(lines) is not int or lines <= 0 or lines > 200:
        raise store.Refusal('reviewer capture limit is invalid')
    result = _reviewer_call(endpoint, 'pane', 'read', endpoint['pane'],
                            '--source', 'recent-unwrapped', '--lines', str(lines))
    return result.stdout[-8192:]

def reviewer_inspect(home: Path, receipt: dict) -> dict:
    from . import supervision
    endpoint = _reviewer_endpoint(receipt)
    if Path(endpoint['home']).resolve() != Path(home).resolve():
        raise store.Refusal('reviewer Herdr endpoint home differs')
    if os.path.lexists(home) and endpoint.get('target') != supervision.runtime_home_identity(home):
        raise store.Refusal('reviewer Herdr endpoint target differs')
    server = endpoint.get('server', {}).get('process')
    if not isinstance(server, dict):
        raise store.Refusal('reviewer Herdr server identity is missing')
    try:
        socket_paths = [endpoint['socket'],
                        endpoint.get('client_socket', {}).get('path')
                        or str(_reviewer_client_socket(endpoint))]
    except (KeyError, TypeError, store.Refusal):
        raise store.Refusal('reviewer Herdr socket identity is missing')
    server_live = supervision.process_presence(server)
    if not server_live:
        if any(os.path.lexists(path) for path in socket_paths):
            raise store.Refusal('reviewer Herdr server absence is unproven')
        return {'presence': 'missing', 'backend': 'herdr', 'endpoint': endpoint}
    result = _reviewer_call(endpoint, 'pane', 'get', endpoint['pane'], check=False)
    if result.returncode:
        try:
            error = json.loads(result.stderr or result.stdout).get('error', {})
        except (ValueError, AttributeError):
            raise store.Refusal('reviewer Herdr pane state is unknown')
        if error.get('code') == 'pane_not_found':
            return {'presence': 'missing', 'backend': 'herdr', 'endpoint': endpoint}
        raise store.Refusal('reviewer Herdr pane state is unknown')
    pane = response(result)['pane']
    expected = {key: endpoint[key] for key in ('pane', 'workspace', 'tab', 'terminal')}
    names = {'pane': 'pane_id', 'workspace': 'workspace_id',
             'tab': 'tab_id', 'terminal': 'terminal_id'}
    if any(pane.get(names[key]) != value for key, value in expected.items()):
        raise store.Refusal('reviewer Herdr endpoint identity differs')
    process = response(_reviewer_call(
        endpoint, 'pane', 'process-info', '--pane', endpoint['pane']))['process_info']
    return {'presence': 'present', 'backend': 'herdr', 'endpoint': endpoint,
            'process': process}


def reviewer_close(home: Path, receipt: dict) -> None:
    from . import supervision
    endpoint = _reviewer_endpoint(receipt)
    if Path(endpoint['home']).resolve() != Path(home).resolve():
        raise store.Refusal('reviewer Herdr endpoint home differs')
    try:
        socket_paths = [endpoint['socket'],
                        endpoint.get('client_socket', {}).get('path')
                        or str(_reviewer_client_socket(endpoint))]
    except (KeyError, TypeError, store.Refusal):
        socket_paths = [endpoint.get('socket', '')]
    try:
        observed = reviewer_inspect(home, endpoint)
    except store.Refusal:
        server = endpoint.get('server', {}).get('process')
        if (isinstance(server, dict) and supervision.process_presence(server)) or any(
                os.path.lexists(path) for path in socket_paths):
            raise
        observed = {'presence': 'missing'}
    if observed['presence'] == 'present':
        _reviewer_call(endpoint, 'pane', 'close', endpoint['pane'])
    server = endpoint.get('server', {}).get('process')
    if not isinstance(server, dict) or not supervision.process_presence(server):
        if any(os.path.lexists(path) for path in socket_paths):
            raise store.Refusal('reviewer Herdr server absence is unproven')
    else:
        try:
            os.killpg(server['pid'], 15)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 10
        while supervision.process_presence(server) and time.monotonic() < deadline:
            time.sleep(.05)
        if supervision.process_presence(server):
            try:
                os.killpg(server['pid'], 9)
            except ProcessLookupError:
                pass
        if supervision.process_presence(server) or any(os.path.lexists(path) for path in socket_paths):
            raise store.Refusal('reviewer Herdr server remains active')
    try:
        Path(endpoint['env_file']).unlink()
    except (KeyError, OSError):
        pass
