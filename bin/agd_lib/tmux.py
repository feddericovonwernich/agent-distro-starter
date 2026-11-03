from collections.abc import Mapping
import hashlib
import os
import shlex
import stat
import uuid
from pathlib import Path
from . import store, supervision


def prefix(home):
    name = 'agd-' + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]
    return ['tmux', '-L', name]


def call(endpoint, *args, check=True):
    return store.run([*endpoint['prefix'], *args], check=check, timeout=10)


def create(home: Path, task_id: str, incarnation: str, cwd: Path, argv: list[str]) -> dict:
    cmd, session, server = prefix(home), 'agd', _ensure_server(home)
    server_pid = str(server['process']['pid'])
    token = str(uuid.uuid4())
    receipt = home/'state'/'tasks'/task_id/'endpoint.json'
    reservation = dict(prefix=cmd, session=session, server_pid=server_pid,
                       token=token, task=task_id, incarnation=incarnation, server=server,
                       target=supervision.runtime_home_identity(home))
    store.atomic_json(receipt, reservation)
    pane = store.run([*cmd, 'new-window', '-d', '-P', '-F', '#{pane_id}', '-t', session,
                      '-n', task_id, '-c', str(cwd), shlex.join(argv)]).stdout.strip()
    endpoint = dict(reservation, pane=pane)
    store.atomic_json(receipt, endpoint)
    try:
        call(endpoint, 'set-option', '-p', '-t', pane, '@agd_owner', token)
    except Exception:
        # Never destroy a newly created pane on uncertain failure: retain its identity.
        raise store.Refusal(f'pane started but owner token unconfirmed: {endpoint}')
    return endpoint


def owned(endpoint):
    result = call(endpoint, 'display-message', '-p', '#{pid}', check=False)
    if result.returncode or result.stdout.strip() != endpoint['server_pid']:
        raise store.Refusal('tmux server missing or replaced; endpoint identity unknown')
    token = call(endpoint, 'show-options', '-p', '-v', '-t', endpoint['pane'], '@agd_owner', check=False)
    if token.returncode:
        pane = call(endpoint, 'list-panes', '-F', '#{pane_id}', '-t', endpoint['pane'], check=False)
        if pane.returncode:
            return False
        raise store.Refusal('pane exists without confirmed owner token')
    if token.stdout.strip() != endpoint['token']:
        raise store.Refusal('pane belongs to a different task/incarnation')
    return True


def inspect(endpoint):
    if not owned(endpoint):
        return {'presence': 'missing'}
    result = call(endpoint, 'list-panes', '-F', '#{pane_pid} #{pane_dead}', '-t', endpoint['pane'])
    return {'presence': 'present', 'process': result.stdout.strip(), 'source': 'tmux'}


def capture(endpoint, lines=120):
    if not owned(endpoint):
        raise store.Refusal('pane missing')
    return call(endpoint, 'capture-pane', '-p', '-S', f'-{lines}', '-t', endpoint['pane']).stdout


def send_text(endpoint, text):
    if not owned(endpoint):
        raise store.Refusal('pane missing')
    buffer = 'agd-' + uuid.uuid4().hex
    call(endpoint, 'set-buffer', '-b', buffer, '--', text)
    try:
        call(endpoint, 'paste-buffer', '-d', '-b', buffer, '-t', endpoint['pane'])
    finally:
        call(endpoint, 'delete-buffer', '-b', buffer, check=False)
    return {'delivery': 'unconfirmed'}


def send_key(endpoint, key):
    if not owned(endpoint):
        raise store.Refusal('pane missing')
    if key not in ('enter', 'escape'):
        raise ValueError('unsupported key')
    call(endpoint, 'send-keys', '-t', endpoint['pane'], 'Enter' if key == 'enter' else 'Escape')
    return {'delivery': 'unconfirmed'}


def close(endpoint):
    if not owned(endpoint):
        return {'presence': 'missing'}
    call(endpoint, 'kill-pane', '-t', endpoint['pane'])
    return {'presence': inspect(endpoint)['presence']}




def _foreground(pid):
    argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
    return supervision.process_start(pid), [arg.decode() for arg in argv], os.readlink(f'/proc/{pid}/fd/0')


def _central_endpoint(receipt):
    endpoint = receipt.get('endpoint')
    if (not isinstance(endpoint, dict)
            or not {'server_pid', 'window', 'pane', 'tty', 'token', 'launch_id'} <= endpoint.keys()
            or not set(endpoint) <= {'server_pid', 'window', 'pane', 'tty', 'token', 'launch_id', 'server'}
            or not all(isinstance(endpoint.get(key), str) and endpoint[key]
                       for key in ('server_pid', 'window', 'pane', 'tty', 'token', 'launch_id'))):
        raise store.Refusal('tmux central endpoint is malformed')
    if endpoint['token'] != receipt.get('endpoint_token') or endpoint['launch_id'] != receipt.get('launch_id'):
        raise store.Refusal('tmux central endpoint identity differs')
    return endpoint


def create_central(home: Path, launch_id: str, endpoint_token: str, managed_argv: list[str],
                   launch_env: dict[str, str], expected_omp_argv: list[str]) -> dict:
    if (not all(isinstance(arg, str) for arg in managed_argv)
            or not all(isinstance(arg, str) for arg in expected_omp_argv)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in launch_env.items())):
        raise store.Refusal('tmux central command is malformed')
    cmd, session, server = prefix(home), 'agd', _ensure_server(home)
    server_pid = str(server['process']['pid'])
    command = shlex.join(['exec', 'env', *[f'{key}={value}' for key, value in sorted(launch_env.items())], *managed_argv])
    window, pane, tty = store.run([*cmd, 'new-window', '-d', '-P', '-F', '#{window_id} #{pane_id} #{pane_tty}', '-t', session,
                                   '-n', 'central', '-c', str(home), command]).stdout.strip().split()
    endpoint = {'server_pid': server_pid, 'window': window, 'pane': pane, 'tty': tty,
                'token': endpoint_token, 'launch_id': launch_id, 'server': server}
    store.atomic_json(home / 'state' / 'central-tmux-pending.json', endpoint)
    return endpoint


def finalize_central(home: Path, receipt: dict) -> None:
    endpoint = _central_endpoint(receipt)
    try:
        store.run([*prefix(home), 'set-option', '-p', '-t', endpoint['pane'],
                   '@agd_central_token', endpoint['token']], timeout=10)
    except Exception as error:
        raise store.Refusal(f'tmux central pane started but token is unconfirmed: {endpoint}') from error


def inspect_central(home: Path, receipt: dict) -> dict:
    endpoint = _central_endpoint(receipt)
    cmd = prefix(home)
    server = store.run([*cmd, 'display-message', '-p', '#{pid}'], check=False)
    if server.returncode or server.stdout.strip() != endpoint['server_pid']:
        raise store.Refusal('tmux central server is missing or replaced')
    token = store.run([*cmd, 'show-options', '-p', '-v', '-t', endpoint['pane'],
                       '@agd_central_token'], check=False)
    if token.returncode or token.stdout.strip() != endpoint['token']:
        raise store.Refusal('tmux central pane token differs')
    pane = store.run([*cmd, 'list-panes', '-F',
                      '#{window_id} #{pane_id} #{pane_tty} #{pane_pid} #{pane_dead}',
                      '-t', endpoint['window']], check=False)
    if pane.returncode:
        raise store.Refusal('tmux central window is missing')
    try:
        window, pane_id, tty, pid, dead = pane.stdout.strip().split()
        if window != endpoint['window'] or pane_id != endpoint['pane'] or tty != endpoint['tty']:
            raise store.Refusal('tmux central pane membership differs')
        if dead != '0':
            raise ValueError
        pid = int(pid)
        start, argv, process_tty = _foreground(pid)
        if process_tty != tty:
            raise store.Refusal('tmux central foreground tty differs')
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise store.Refusal('tmux central foreground is unavailable') from error
    return {'endpoint': endpoint, 'foreground': {'launch_id': receipt['launch_id'],
            'endpoint_token': receipt['endpoint_token'], 'pid': pid,
            'start': start, 'argv': argv, 'tty': tty}}


def attach_argv(home: Path, receipt: dict) -> list[str]:
    endpoint = _central_endpoint(receipt)
    cmd = prefix(home)
    store.run([*cmd, 'select-window', '-t', endpoint['window']], timeout=10)
    store.run([*cmd, 'select-pane', '-t', endpoint['pane']], timeout=10)
    return [*cmd, 'switch-client' if os.environ.get('TMUX') else 'attach-session', '-t', 'agd']


def _server_evidence(home):
    import stat
    cmd = prefix(home)
    pid = int(store.run([*cmd, 'display-message', '-p', '#{pid}']).stdout.strip())
    path = Path(store.run([*cmd, 'display-message', '-p', '#{socket_path}']).stdout.strip())
    if not path.is_absolute():
        raise store.Refusal('tmux socket path is unproven')
    mode = path.lstat()
    if not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid():
        raise store.Refusal('tmux socket ownership is unproven')
    identity = supervision.process_identity(pid)
    identity.pop('state')
    if identity['uid'] != os.getuid():
        raise store.Refusal('tmux server user differs')
    return {'target': supervision.runtime_home_identity(home), 'process': identity,
            'socket': {'path': str(path), 'device': mode.st_dev, 'inode': mode.st_ino,
                       'uid': mode.st_uid}}


def _ensure_server(home):
    from . import uninstall_state
    path = home / 'state/tmux-server.json'
    previous = uninstall_state.read_evidence(path, private=True) if os.path.lexists(path) else None
    if previous and not _uninstall_server(home, previous):
        raise store.Refusal('previous tmux server is absent; evidence retained')
    cmd, anchor = prefix(home), None
    if store.run([*cmd, 'has-session', '-t', 'agd'], check=False).returncode:
        pane = store.run([*cmd, 'new-session', '-d', '-P', '-F', '#{pane_id}', '-s', 'agd',
                          '-n', 'anchor', '-c', str(home)], timeout=10).stdout.strip()
        if not pane.startswith('%') or not pane[1:].isdigit():
            raise store.Refusal('tmux anchor pane identity is unproven')
        token = uuid.uuid4().hex
        store.run([*cmd, 'set-option', '-p', '-t', pane, '@agd_anchor_owner', token])
        anchor = {'pane': pane, 'token_sha256': hashlib.sha256(token.encode()).hexdigest()}
    current = _server_evidence(home)
    if previous:
        if (previous['target'] != current['target'] or previous['socket'] != current['socket']
                or any(previous['process'][name] != current['process'][name] for name in ('pid', 'start', 'uid'))):
            raise store.Refusal('tmux server changed; previous evidence retained')
        current['anchor'] = anchor or previous.get('anchor')
    else:
        current['anchor'] = anchor
    store.atomic_json(path, current)
    return current


def _uninstall_server(home, server):
    import stat
    target = server['target']
    if (target.get('canonical_home') != str(home) or target.get('uid') != os.getuid()
            or os.path.lexists(home) and target != supervision.runtime_home_identity(home)):
        raise store.Refusal('tmux server home binding differs')
    path = Path(server['socket']['path'])
    if not supervision.process_presence(server['process']):
        if not os.path.lexists(path):
            return False
        raise store.Refusal('tmux socket remains after recorded server exit')
    mode = path.lstat()
    if (not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid()
            or (mode.st_dev, mode.st_ino) != (server['socket']['device'], server['socket']['inode'])):
        raise store.Refusal('tmux socket was replaced')
    pid = store.run([*prefix(home), 'display-message', '-p', '#{pid}']).stdout.strip()
    if pid != str(server['process']['pid']):
        raise store.Refusal('tmux server PID differs')
    return True


def _uninstall_probe(home, identity):
    endpoint = identity['endpoint']
    try:
        server = endpoint['server']
        if not _uninstall_server(home, server):
            return {'presence': 'absent', 'ownership': 'owned', 'blockers': []}
        if endpoint.get('prefix', prefix(home)) != prefix(home):
            raise store.Refusal('tmux endpoint namespace differs')
        cmd = prefix(home)
        panes = store.run([*cmd, 'list-panes', '-a', '-F', '#{pane_id}']).stdout.splitlines()
        if endpoint['pane'] not in panes:
            return {'presence': 'absent', 'ownership': 'owned', 'blockers': []}
        name = '@agd_owner' if identity.get('task_id') else '@agd_central_token'
        token = store.run([*cmd, 'show-options', '-p', '-v', '-t', endpoint['pane'], name]).stdout.strip()
        if hashlib.sha256(token.encode()).hexdigest() != endpoint['token_sha256']:
            raise store.Refusal('tmux pane owner token differs')
        if 'window' in endpoint:
            actual = store.run([*cmd, 'display-message', '-p', '-t', endpoint['pane'],
                                '#{window_id} #{pane_tty}']).stdout.strip().split()
            if actual != [endpoint['window'], endpoint['tty']]:
                raise store.Refusal('tmux central pane membership differs')
        return {'presence': 'present', 'ownership': 'owned', 'blockers': []}
    except (OSError, ValueError, KeyError, TypeError, store.Refusal):
        return {'presence': 'unknown', 'ownership': 'unknown', 'blockers': [
            {'code': 'tmux-endpoint', 'reason': 'tmux process/socket/token continuity is unproven'}]}


def _uninstall_grace(home, identity):
    probe = _uninstall_probe(home, identity)
    if probe['ownership'] != 'owned' or probe['presence'] == 'unknown':
        raise store.Refusal('tmux quit endpoint is unproven')
    if probe['presence'] == 'present':
        pane = identity['endpoint']['pane']
        cmd = prefix(home)
        store.run([*cmd, 'send-keys', '-t', pane, 'Escape'])
        store.run([*cmd, 'send-keys', '-l', '-t', pane, '/quit'])
        store.run([*cmd, 'send-keys', '-t', pane, 'Enter'])


def uninstall_close(home, resource, operation):
    from . import uninstall_state
    approved = uninstall_state.require_scope(home, operation, resource['key'])
    probe = _uninstall_probe(home, approved['identity'])
    if probe['ownership'] != 'owned' or probe['presence'] == 'unknown':
        raise store.Refusal('tmux endpoint cannot be safely closed')
    if probe['presence'] == 'present':
        store.run([*prefix(home), 'kill-pane', '-t', approved['identity']['endpoint']['pane']])
    if _uninstall_probe(home, approved['identity'])['presence'] != 'absent':
        raise store.Refusal('tmux pane absence remains unproven')
    uninstall_state.checkpoint(home, operation, phase='quiescing', completed=approved['key'])


def _reviewer_run_id(value):
    import uuid
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error


def reviewer_create(home: Path, review_run_id: str, command: list[str], *, cwd: Path,
                    env: Mapping[str, str]) -> dict:
    run_id = _reviewer_run_id(review_run_id)
    if (not isinstance(command, list) or not command
            or not all(isinstance(item, str) and item for item in command)):
        raise store.Refusal('reviewer command is malformed')
    home, cwd = Path(home).resolve(), Path(cwd).resolve()
    if not cwd.is_relative_to(home) or not cwd.is_dir():
        raise store.Refusal('reviewer cwd is outside its home')
    if not isinstance(env, Mapping) or not all(isinstance(key, str) and key.isidentifier() and isinstance(value, str)
                                               for key, value in env.items()):
        raise store.Refusal('reviewer environment is malformed')
    server = _ensure_server(home)
    token = uuid.uuid4().hex
    pane = None
    env_file = cwd / '.reviewer-env'
    try:
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(f'{key}={shlex.quote(value)}\n' for key, value in sorted(env.items())))
        script = f'set -a; . {shlex.quote(str(env_file))}; set +a; exec {shlex.join(command)}'
        pane = store.run([*prefix(home), 'new-window', '-d', '-P', '-F',
                          '#{pane_id}', '-t', 'agd', '-n', 'reviewer-' + run_id.replace('-', ''),
                          '-c', str(cwd), script]).stdout.strip()
        if not pane.startswith('%'):
            raise store.Refusal('reviewer tmux pane identity is unverified')
        endpoint = {'schema': 1, 'backend': 'tmux', 'review_run_id': run_id,
                    'home': str(home), 'prefix': prefix(home), 'session': 'agd',
                    'pane': pane, 'token': token, 'server': server,
                    'target': supervision.runtime_home_identity(home),
                    'expected_process_argv': list(command), 'env_file': str(env_file)}
        call(endpoint, 'set-option', '-p', '-t', pane, '@agd_reviewer_owner', token)
        return endpoint
    except BaseException:
        if pane:
            try:
                call({'prefix': prefix(home), 'server_pid': str(server['process']['pid']),
                      'pane': pane, 'token': token}, 'kill-pane', '-t', pane)
            except BaseException:
                pass
        try:
            env_file.unlink()
        except OSError:
            pass
        raise


def _reviewer_endpoint(receipt):
    if (not isinstance(receipt, dict) or receipt.get('backend') != 'tmux'
            or not isinstance(receipt.get('review_run_id'), str)
            or not isinstance(receipt.get('home'), str)
            or receipt.get('session') != 'agd'):
        raise store.Refusal('reviewer tmux endpoint is malformed')
    _reviewer_run_id(receipt['review_run_id'])
    return receipt


def _reviewer_target(home, endpoint):
    if os.path.lexists(home) and endpoint.get('target') != supervision.runtime_home_identity(home):
        raise store.Refusal('reviewer tmux endpoint target differs')


def _reviewer_server_live(endpoint):
    server = endpoint.get('server', {})
    process = server.get('process') if isinstance(server, dict) else None
    socket = server.get('socket') if isinstance(server, dict) else None
    socket_path = socket.get('path') if isinstance(socket, dict) else None
    if not isinstance(process, dict) or not isinstance(socket_path, str):
        raise store.Refusal('reviewer tmux server identity is incomplete')
    if not supervision.process_presence(process):
        if os.path.lexists(socket_path):
            raise store.Refusal('reviewer tmux server absence is unproven')
        return False
    path = Path(socket_path)
    try:
        identity = path.lstat()
    except OSError as error:
        raise store.Refusal('reviewer tmux server socket is unavailable') from error
    if (not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.getuid()
            or identity.st_dev != socket.get('device') or identity.st_ino != socket.get('inode')):
        raise store.Refusal('reviewer tmux server socket identity differs')
    return True


def _reviewer_anchor_owned(endpoint):
    anchor = endpoint.get('server', {}).get('anchor')
    if not isinstance(anchor, dict) or not isinstance(anchor.get('pane'), str):
        raise store.Refusal('reviewer tmux server anchor is malformed')
    if not isinstance(anchor.get('token_sha256'), str):
        raise store.Refusal('reviewer tmux server anchor is malformed')
    panes = call(endpoint, 'list-panes', '-a', '-F', '#{pane_id}', check=False)
    if panes.returncode or panes.stdout.splitlines() != [anchor['pane']]:
        raise store.Refusal('reviewer tmux server has unexpected panes')
    token = call(endpoint, 'show-options', '-p', '-v', '-t', anchor['pane'],
                 '@agd_anchor_owner', check=False)
    if (token.returncode
            or hashlib.sha256(token.stdout.strip().encode()).hexdigest() != anchor['token_sha256']):
        raise store.Refusal('reviewer tmux server anchor differs')
    return True


def _reviewer_dispose_server(endpoint):
    server = endpoint.get('server')
    if not isinstance(server, dict):
        raise store.Refusal('reviewer tmux server identity is malformed')
    if 'anchor' not in server or server['anchor'] is None:
        return
    if not _reviewer_server_live(endpoint):
        return
    _reviewer_anchor_owned(endpoint)
    call(endpoint, 'kill-pane', '-t', server['anchor']['pane'])
    process = server['process']
    deadline = __import__('time').monotonic() + 10
    while supervision.process_presence(process):
        if __import__('time').monotonic() >= deadline:
            raise store.Refusal('reviewer tmux server remains active')
        __import__('time').sleep(.05)
    socket_path = server['socket']['path']
    if os.path.lexists(socket_path):
        raise store.Refusal('reviewer tmux server socket remains')



def _reviewer_owned(endpoint):
    if not _reviewer_server_live(endpoint):
        return False
    server = endpoint['server']['process']
    result = call(endpoint, 'display-message', '-p', '#{pid}', check=False)
    if result.returncode or result.stdout.strip() != str(server['pid']):
        raise store.Refusal('reviewer tmux server is missing or replaced')
    panes = call(endpoint, 'list-panes', '-F', '#{pane_id}', '-t', endpoint['pane'],
                 check=False)
    if panes.returncode:
        return False
    token = call(endpoint, 'show-options', '-p', '-v', '-t', endpoint['pane'],
                 '@agd_reviewer_owner', check=False)
    if token.returncode:
        raise store.Refusal('reviewer tmux pane owner is unconfirmed')
    if token.stdout.strip() != endpoint['token']:
        raise store.Refusal('reviewer tmux pane owner differs')
    return True


def reviewer_capture(home: Path, receipt: dict, lines: int = 120) -> str:
    endpoint = _reviewer_endpoint(receipt)
    if Path(endpoint['home']).resolve() != Path(home).resolve():
        raise store.Refusal('reviewer tmux endpoint home differs')
    _reviewer_target(home, endpoint)
    if type(lines) is not int or lines <= 0 or lines > 200:
        raise store.Refusal('reviewer capture limit is invalid')
    if not _reviewer_owned(endpoint):
        raise store.Refusal('reviewer tmux pane is missing')
    return call(endpoint, 'capture-pane', '-p', '-S', f'-{lines}',
                '-t', endpoint['pane']).stdout[-8192:]

def reviewer_inspect(home: Path, receipt: dict) -> dict:
    endpoint = _reviewer_endpoint(receipt)
    if Path(endpoint['home']).resolve() != Path(home).resolve():
        raise store.Refusal('reviewer tmux endpoint home differs')
    _reviewer_target(home, endpoint)
    if not _reviewer_owned(endpoint):
        return {'presence': 'missing', 'backend': 'tmux', 'endpoint': endpoint}
    pane = call(endpoint, 'list-panes', '-F', '#{pane_id} #{pane_pid} #{pane_dead}',
                '-t', endpoint['pane'])
    fields = pane.stdout.strip().split()
    if len(fields) != 3 or fields[0] != endpoint['pane']:
        raise store.Refusal('reviewer tmux pane identity differs')
    return {'presence': 'present', 'backend': 'tmux', 'endpoint': endpoint,
            'process': {'pid': int(fields[1]), 'dead': fields[2]}}


def reviewer_close(home: Path, receipt: dict) -> None:
    endpoint = _reviewer_endpoint(receipt)
    if Path(endpoint['home']).resolve() != Path(home).resolve():
        raise store.Refusal('reviewer tmux endpoint home differs')
    _reviewer_target(home, endpoint)
    observed = reviewer_inspect(home, endpoint)
    if observed['presence'] == 'present':
        call(endpoint, 'kill-pane', '-t', endpoint['pane'])
    deadline = __import__('time').monotonic() + 10
    while True:
        try:
            current = reviewer_inspect(home, endpoint)
        except store.Refusal:
            current = {'presence': 'unknown'}
        if current['presence'] == 'missing':
            break
        if __import__('time').monotonic() >= deadline:
            raise store.Refusal('reviewer tmux pane remains active')
        __import__('time').sleep(.05)
    _reviewer_dispose_server(endpoint)
    try:
        Path(endpoint['env_file']).unlink()
    except (KeyError, OSError):
        pass
