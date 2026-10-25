import hashlib
import shlex
import uuid
from pathlib import Path
from . import store


def prefix(home):
    name = 'agd-' + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]
    return ['tmux', '-L', name]


def call(endpoint, *args, check=True):
    return store.run([*endpoint['prefix'], *args], check=check, timeout=10)


def create(home: Path, task_id: str, incarnation: str, cwd: Path, argv: list[str]) -> dict:
    cmd = prefix(home)
    session = 'agd'
    if store.run([*cmd, 'has-session', '-t', session], check=False).returncode:
        store.run([*cmd, 'new-session', '-d', '-s', session, '-n', 'anchor', '-c', str(home)], timeout=10)
    server_pid = store.run([*cmd, 'display-message', '-p', '#{pid}']).stdout.strip()
    token = str(uuid.uuid4())
    receipt = home/'state'/'tasks'/task_id/'endpoint.json'
    reservation = dict(prefix=cmd, session=session, server_pid=server_pid,
                       token=token, task=task_id, incarnation=incarnation)
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
