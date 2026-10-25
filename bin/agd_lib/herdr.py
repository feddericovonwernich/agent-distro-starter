import json
import os
import re
import shlex
import stat
import subprocess
import uuid
from pathlib import Path
from . import store


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
    fields = dict(line.split(': ', 1) for line in result.stdout.splitlines() if ': ' in line)
    if fields.get('status') != 'running' or fields.get('compatible') != 'yes' or not fields.get('protocol'):
        raise store.DependencyError('selected Herdr server is incompatible')
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
            'inode': identity.st_ino, 'protocol': fields['protocol']}


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
        store.atomic_json(workspace_path, {**server, 'workspace': workspace_id})
    return server, workspace_id


def create(home: Path, task_id: str, incarnation: str, cwd: Path, argv: list[str]) -> dict:
    server, workspace_id = prepare(home)
    receipt = home/'state'/'tasks'/task_id/'endpoint.json'
    endpoint = {**server, 'home': str(home), 'workspace': workspace_id,
                'task': task_id, 'incarnation': incarnation, 'token': str(uuid.uuid4()),
                'command': argv[3:] if argv[:1] == ['env'] else argv}
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
