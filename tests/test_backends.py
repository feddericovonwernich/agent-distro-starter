import fcntl
import importlib
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
from contextlib import contextmanager
from pathlib import Path
from support import command, lab
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'bin'))
from agd_lib.store import Refusal


@contextmanager
def owned_home(name, home):
    try:
        yield home
    finally:
        path = home/'state'/'herdr-workspace.json'
        if name == 'herdr' and path.is_file():
            from agd_lib import herdr
            workspace = json.loads(path.read_text())
            herdr.call(workspace, 'workspace', 'close', workspace['workspace'])


def check_backend(name):
    assert name in ('tmux', 'herdr')
    backend = importlib.import_module('agd_lib.' + name)
    with tempfile.TemporaryDirectory(prefix='agd-pane-') as tmp, owned_home(name, Path(tmp)/'home with space') as home:
        command(home, 'init', '--backend', 'tmux')
        endpoint = backend.create(home, 'probe', 'incarnation-one', home,
                                  ['/bin/sh', '-c', 'printf \"agd-%s\\n\" live-check; treehouse --version; sleep 30'])
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if 'agd-live-check' in backend.capture(endpoint):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('probe output never appeared')
            assert backend.inspect(endpoint)['presence'] == 'present'
            if name == 'herdr':
                assert 'v3.1.6' in backend.capture(endpoint)
            foreign = dict(endpoint, token='not-the-owner')
            try:
                backend.close(foreign)
            except Refusal:
                pass
            else:
                raise AssertionError('foreign close was accepted')
            assert backend.inspect(endpoint)['presence'] == 'present'
        finally:
            backend.close(endpoint)
        if name in ('tmux', 'herdr'):
            endpoint = backend.create(home, 'input', 'incarnation-two', home,
                                      ['/bin/sh', '-c', 'stty -echo; exec cat'])
            try:
                sentinel = home/'must-not-exist'
                payload = f'literal; touch {sentinel}'
                backend.send_text(endpoint, payload)
                backend.send_key(endpoint, 'enter')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if payload in backend.capture(endpoint):
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError('literal input not delivered')
                assert not sentinel.exists()
            finally:
                backend.close(endpoint)


def check_herdr_recovery():
    from agd_lib import herdr, tasks
    with lab() as (home, project), owned_home('herdr', home):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'recover', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='attempt-one', harness='omp',
                    backend='herdr', phase='running', worker_alive=None)
        tasks.save(home, task)
        endpoint = herdr.create(home, 'recover', 'attempt-one', Path(task['worktree']),
                                ['/bin/sh', '-c', 'printf worker-started; sleep 30'])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                command(home, 'session', 'reconcile')
                if tasks.read(home, 'recover').get('endpoint') == endpoint:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('running Herdr endpoint was not adopted')
        finally:
            herdr.close(endpoint)


def check_herdr_preflight():
    from agd_lib import herdr, store, tasks
    with lab() as (home, project):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'stale', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        store.atomic_json(home/'state'/'herdr-workspace.json',
                          {**herdr.available(), 'workspace': 'not-a-workspace'})
        command(home, 'worker', 'launch', 'stale', '--backend', 'herdr',
                expected=3)
        assert tasks.read(home, 'stale')['phase'] == 'prepared'


def check_herdr_blank_pane_is_not_worker():
    import subprocess
    from unittest.mock import patch
    from agd_lib import herdr, tasks
    with lab() as (home, project), owned_home('herdr', home):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'blank', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task.update(executor='worker', incarnation='attempt-one', harness='omp',
                    backend='herdr', phase='running', worker_alive=None)
        tasks.save(home, task)
        actual_call = herdr.call
        def before_run(endpoint, *args, **options):
            if args[:2] == ('pane', 'run'):
                return subprocess.CompletedProcess([], 0, '', '')
            return actual_call(endpoint, *args, **options)
        with patch.object(herdr, 'call', side_effect=before_run):
            endpoint = herdr.create(home, 'blank', 'attempt-one', Path(task['worktree']),
                                    ['/bin/sh', '-c', 'sleep 30'])
        try:
            assert herdr.inspect(endpoint)['presence'] == 'present'
            command(home, 'session', 'reconcile')
            state = tasks.read(home, 'blank')
            assert 'endpoint' not in state
            assert state['lease_id'] == task['lease_id']
            assert any(event['task'] == 'blank' and event['kind'] == 'blocked'
                       for event in command(home, 'events', 'list'))
        finally:
            herdr.close(endpoint)


def check_herdr_missing_socket_is_unavailable():
    import subprocess
    from unittest.mock import patch
    from agd_lib import herdr, store
    with tempfile.TemporaryDirectory(prefix='agd-missing-socket-') as tmp:
        status = (f'status: running\nprotocol: 20\ncompatible: yes\n'
                  f'socket: {tmp}/gone.sock\n')
        with patch.object(herdr.store, 'run',
                          return_value=subprocess.CompletedProcess([], 0, status, '')):
            try:
                herdr.available()
            except store.DependencyError:
                pass
            else:
                raise AssertionError('vanished selected socket was accepted')


def check_herdr_timeout_preserves_unknown_state():
    import subprocess
    from unittest.mock import patch
    from agd_lib import herdr, store
    endpoint = herdr.available()
    with patch.object(herdr.store, 'run',
                      side_effect=subprocess.TimeoutExpired(['herdr'], 10)):
        try:
            herdr.call(endpoint, 'pane', 'get', 'not-a-pane')
        except store.Refusal:
            pass
        else:
            raise AssertionError('timed-out pane query was accepted')
        try:
            herdr.available()
        except store.DependencyError:
            pass
        else:
            raise AssertionError('timed-out server query was accepted')


CANONICAL_REPOSITORY = 'https://github.com/feddericovonwernich/agent-distro-starter.git'


def _pty(argv, cwd, env, initial=b'', detach=True, detacher=None, detach_bytes=b'\x03'):
    master, slave = pty.openpty()

    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                               preexec_fn=terminal)
    slave_name = os.ttyname(slave)
    os.close(slave)
    if initial:
        os.write(master, initial)
    output = bytearray()
    deadline = time.monotonic() + 30
    while process.poll() is None and time.monotonic() < deadline:
        if select.select([master], [], [], .1)[0]:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
        if detach and (Path(env['AGD_HOME']) / 'state' / 'central-launcher.json').is_file():
            receipt = json.loads((Path(env['AGD_HOME']) / 'state' / 'central-launcher.json').read_text())
            if receipt.get('phase') == 'ready':
                if detacher:
                    detach = not detacher(slave_name)
                else:
                    os.write(master, detach_bytes)
                    detach = False
    while process.poll() is None and time.monotonic() < deadline:
        if select.select([master], [], [], .1)[0]:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
    if process.poll() is None:
        process.kill()
        raise AssertionError(f'PTY command did not detach: {argv!r}')
    while select.select([master], [], [], 0)[0]:
        try:
            output.extend(os.read(master, 4096))
        except OSError:
            break
    os.close(master)
    return subprocess.CompletedProcess(argv, process.returncode, output.decode(errors='replace'), '')


def _install_start(name):
    assert name in ('tmux', 'herdr')
    with tempfile.TemporaryDirectory(prefix='a') as directory:
        root = Path(directory)
        home, tools, site = root / 'home', root / 'tools', root / 'site'
        tools.mkdir()
        site.mkdir()
        (tools / 'tmux').write_text('#!/bin/sh\nexec /usr/bin/tmux "$@"\n')
        (tools / 'tmux').chmod(0o755)
        (tools / 'git').write_text(
            '#!/bin/sh\nset -eu\n'
            '[ "$1" = clone ] && [ "$2" = --quiet ] && [ "$3" = -- ] && [ "$4" = "$AGD_CANONICAL" ]\n'
            '/bin/cp -a "$AGD_SOURCE/." "$5"\n'
            '/bin/rm -rf "$5/.git"\n')
        (tools / 'git').chmod(0o755)
        (site / 'sitecustomize.py').write_text(
            'import io, json, urllib.request\n'
            'def urlopen(url):\n'
            '    if url.endswith("/releases/latest"):\n'
            '        return io.BytesIO(json.dumps({"draft":False,"prerelease":False,"tag_name":"test","assets":[]}).encode())\n'
            '    raise AssertionError(f"unexpected network URL: {url}")\n'
            'urllib.request.urlopen = urlopen\n')
        base = dict(os.environ, HOME=str(home), AGD_HOME=str(root / f'distro-{name}'),
                    AGD_SOURCE=str(ROOT), AGD_CANONICAL=CANONICAL_REPOSITORY,
                    SHELL='/bin/sh', TERM='xterm',
                    PYTHONPATH=str(site), PATH=f'{tools}:/tmp/agd-treehouse-v3.1.6:{os.environ["PATH"]}')
        updates = b'n\n' if name == 'tmux' else b'n\nn\n'
        installed = root / f'distro-{name}'
        result = _pty(['/bin/bash', str(ROOT / 'install.sh')], root, base,
                      f'distro-{name}\n{name}\n'.encode() + updates + b'n\nn\nn\nexit\n', detach=False)
        assert result.returncode == 0, result.stdout
        receipt = json.loads((installed / 'data' / 'dependencies.json').read_text())
        assert receipt['omp']['source'] == receipt['treehouse']['source'] == 'existing'
        if name == 'herdr':
            assert receipt['herdr']['source'] == 'existing'
        def detach_tmux(tty):
            return subprocess.run(['tmux', '-L', 'agd-' + __import__('hashlib').sha256(
                str(installed.resolve()).encode()).hexdigest()[:16], 'detach-client', '-t', tty],
                env=base, capture_output=True).returncode == 0

        try:
            first = _pty([str(installed / 'bin' / 'agd'), '--home', str(installed), 'start'],
                         installed, {**base, 'AGD_HOME': str(installed)},
                         detacher=detach_tmux if name == 'tmux' else None)
            assert first.returncode == (0 if name == 'tmux' else -2), first.stdout
            ready = json.loads((installed / 'state' / 'central-launcher.json').read_text())
            assert ready['phase'] == 'ready' and ready['owner'] and ready['watcher']
            second = _pty([str(installed / 'bin' / 'agd'), '--home', str(installed), 'start'],
                          installed, {**base, 'AGD_HOME': str(installed)},
                          detacher=detach_tmux if name == 'tmux' else None)
            assert second.returncode == (0 if name == 'tmux' else -2), second.stdout
            assert json.loads((installed / 'state' / 'central-launcher.json').read_text()) == ready
            conflict = subprocess.run([str(installed / 'bin' / 'agd'), '--home', str(installed), 'start',
                                       '--backend', 'herdr' if name == 'tmux' else 'tmux'],
                                      cwd=installed, env={**base, 'AGD_HOME': str(installed)},
                                      text=True, capture_output=True)
            assert conflict.returncode == 3
            assert json.loads((installed / 'state' / 'central-launcher.json').read_text()) == ready
        finally:
            if name == 'tmux':
                subprocess.run(['tmux', '-L', 'agd-' + __import__('hashlib').sha256(
                    str(installed.resolve()).encode()).hexdigest()[:16], 'kill-server'], env=base,
                               capture_output=True)
            else:
                session = json.loads((installed / 'data' / 'launcher.json').read_text())['herdr_session']
                stopped = subprocess.run(['herdr', '--session', session, 'server', 'stop'], env=base,
                                         text=True, capture_output=True)
                assert stopped.returncode == 0, stopped.stderr


def check_install_start_tmux():
    _install_start('tmux')


def check_install_start_herdr():
    _install_start('herdr')


if __name__ == '__main__':
    checks = {
        'install-start-tmux': check_install_start_tmux,
        'install-start-herdr': check_install_start_herdr,
    }
    if sys.argv[1] in checks:
        checks[sys.argv[1]]()
    else:
        check_backend(sys.argv[1])
        if sys.argv[1] == 'herdr':
            check_herdr_recovery()
            check_herdr_preflight()
            check_herdr_blank_pane_is_not_worker()
            check_herdr_missing_socket_is_unavailable()
            check_herdr_timeout_preserves_unknown_state()


@pytest.mark.parametrize('name', ['herdr', 'tmux'])
def test_reviewer_backend_rejects_unscoped_endpoint_requests(name, tmp_path):
    backend = importlib.import_module('agd_lib.' + name)
    with pytest.raises(Refusal):
        backend.reviewer_create(
            tmp_path, 'not-a-uuid', ['/bin/true'], cwd=tmp_path, env={})


def test_removed_home_herdr_reviewer_absence_requires_both_sockets(tmp_path):
    import socket
    import uuid
    from agd_lib import herdr, supervision
    home = tmp_path / 'removed-home'
    home.mkdir()
    socket_path = tmp_path / 'reviewer.sock'
    client_socket_path = tmp_path / 'reviewer-client.sock'
    process = subprocess.Popen(['/bin/sleep', '30'])
    try:
        process_identity = supervision.process_identity(process.pid)
        process.terminate()
        process.wait(timeout=5)
        run_id = str(uuid.uuid4())
        endpoint = {
            'backend': 'herdr',
            'review_run_id': run_id,
            'home': str(home),
            'session': f"agd-reviewer-{run_id.replace('-', '')}",
            'target': {'home_id': str(uuid.uuid4())},
            'server': {'process': process_identity},
            'socket': str(socket_path),
            'client_socket': {'path': str(client_socket_path)},
        }
        home.rmdir()
        assert herdr.reviewer_inspect(home, endpoint)['presence'] == 'missing'
        replacement = socket.socket(socket.AF_UNIX)
        try:
            replacement.bind(str(socket_path))
            with pytest.raises(Refusal):
                herdr.reviewer_inspect(home, endpoint)
        finally:
            replacement.close()
            socket_path.unlink(missing_ok=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_reviewer_close_retains_replaced_home_environment(tmp_path):
    import uuid
    from agd_lib import herdr, supervision
    home = tmp_path / 'review-home'
    home.mkdir()
    (home / 'data').mkdir()
    old_id = str(uuid.uuid4())
    (home / 'data' / 'home.json').write_text(json.dumps({'schema': 1, 'home_id': old_id}))
    target = supervision.runtime_home_identity(home)
    process = subprocess.Popen(['/bin/sleep', '30'])
    try:
        process_identity = supervision.process_identity(process.pid)
        process.terminate()
        process.wait(timeout=5)
        run_id = str(uuid.uuid4())
        replacement = tmp_path / 'replacement'
        home.rename(replacement)
        home.mkdir()
        (home / 'data').mkdir()
        (home / 'data' / 'home.json').write_text(
            json.dumps({'schema': 1, 'home_id': str(uuid.uuid4())}))
        env_file = home / 'reviewer.env'
        env_file.write_text('foreign replacement environment\n')
        endpoint = {
            'backend': 'herdr',
            'review_run_id': run_id,
            'home': str(home),
            'session': f"agd-reviewer-{run_id.replace('-', '')}",
            'target': target,
            'server': {'process': process_identity},
            'socket': str(tmp_path / 'reviewer.sock'),
            'client_socket': {'path': str(tmp_path / 'reviewer-client.sock')},
            'env_file': str(env_file),
        }
        with pytest.raises(Refusal):
            herdr.reviewer_close(home, endpoint)
        assert env_file.exists()
        assert env_file.read_text() == 'foreign replacement environment\n'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

def test_tmux_reviewer_close_retains_replaced_home_environment(tmp_path, monkeypatch):
    import uuid
    from types import SimpleNamespace
    from agd_lib import supervision, tmux
    home = tmp_path / 'review-home'
    home.mkdir()
    (home / 'data').mkdir()
    (home / 'data' / 'home.json').write_text(json.dumps({'schema': 1, 'home_id': str(uuid.uuid4())}))
    target = supervision.runtime_home_identity(home)
    process = subprocess.Popen(['/bin/sleep', '30'])
    try:
        process_identity = supervision.process_identity(process.pid)
        replacement = tmp_path / 'replacement'
        home.rename(replacement)
        home.mkdir()
        (home / 'data').mkdir()
        (home / 'data' / 'home.json').write_text(
            json.dumps({'schema': 1, 'home_id': str(uuid.uuid4())}))
        env_file = home / 'reviewer.env'
        env_file.write_text('foreign replacement environment\n')
        endpoint = {
            'schema': 1, 'backend': 'tmux', 'review_run_id': str(uuid.uuid4()),
            'home': str(home), 'prefix': tmux.prefix(home), 'session': 'agd',
            'pane': '%1', 'token': 'token', 'target': target,
            'server': {'process': process_identity},
            'env_file': str(env_file),
        }
        state = {'pane': True}
        def fake_call(_endpoint, *args, **_kwargs):
            if args[0] == 'display-message':
                return SimpleNamespace(returncode=0, stdout=str(process.pid), stderr='')
            if args[0] == 'list-panes':
                return SimpleNamespace(returncode=0 if state['pane'] else 1,
                                       stdout=f'%1 {process.pid} 0\n' if state['pane'] else '', stderr='')
            if args[0] == 'show-options':
                return SimpleNamespace(returncode=0, stdout='token\n', stderr='')
            if args[0] == 'kill-pane':
                state['pane'] = False
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            raise AssertionError(args)
        monkeypatch.setattr(tmux, 'call', fake_call)
        with pytest.raises(Refusal):
            tmux.reviewer_close(home, endpoint)
        assert env_file.exists()
        assert env_file.read_text() == 'foreign replacement environment\n'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_tmux_reviewer_inspect_accepts_saved_dead_server_with_absent_socket_and_home(tmp_path):
    import uuid
    from agd_lib import supervision, tmux
    home = tmp_path / 'review-home'
    home.mkdir()
    (home / 'data').mkdir()
    (home / 'data' / 'home.json').write_text(json.dumps({'schema': 1, 'home_id': str(uuid.uuid4())}))
    target = supervision.runtime_home_identity(home)
    process = subprocess.Popen(['/bin/sleep', '30'])
    try:
        process_identity = supervision.process_identity(process.pid)
        process.terminate()
        process.wait(timeout=5)
        (home / 'data' / 'home.json').unlink()
        (home / 'data').rmdir()
        home.rmdir()
        endpoint = {
            'schema': 1, 'backend': 'tmux', 'review_run_id': str(uuid.uuid4()),
            'home': str(home), 'prefix': tmux.prefix(home), 'session': 'agd',
            'pane': '%1', 'token': 'token', 'target': target,
            'server': {'process': process_identity,
                       'socket': {'path': str(tmp_path / 'tmux.sock')}},
        }
        assert tmux.reviewer_inspect(home, endpoint)['presence'] == 'missing'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize('created', [True, False], ids=['reviewer-created', 'pre-existing-shared'])
def test_tmux_reviewer_close_disposes_created_server_but_preserves_shared_server(
        tmp_path, monkeypatch, created):
    import hashlib
    import socket
    import uuid
    from types import SimpleNamespace
    from agd_lib import supervision, tmux
    home = tmp_path / 'review-home'
    home.mkdir()
    (home / 'data').mkdir()
    (home / 'data' / 'home.json').write_text(json.dumps({'schema': 1, 'home_id': str(uuid.uuid4())}))
    target = supervision.runtime_home_identity(home)
    socket_path = tmp_path / 'tmux.sock'
    server_socket = socket.socket(socket.AF_UNIX)
    server_socket.bind(str(socket_path))
    process = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
    try:
        process_identity = supervision.process_identity(process.pid)
        socket_stat = socket_path.lstat()
        env_file = home / 'reviewer.env'
        env_file.write_text('HOME=/owned\n')
        endpoint = {
            'schema': 1, 'backend': 'tmux', 'review_run_id': str(uuid.uuid4()),
            'home': str(home), 'prefix': tmux.prefix(home), 'session': 'agd',
            'pane': '%1', 'token': 'token', 'target': target,
            'server_created': created,
            'server': {
                'target': target, 'process': process_identity,
                'socket': {'path': str(socket_path), 'device': socket_stat.st_dev,
                           'inode': socket_stat.st_ino},
                'anchor': {'pane': '%0', 'token_sha256': hashlib.sha256(b'anchor').hexdigest()},
            },
            'env_file': str(env_file),
        }
        if not created:
            endpoint['server'].pop('anchor')
        state, calls = {'pane': True}, []
        def fake_call(_endpoint, *args, **_kwargs):
            calls.append(args[0])
            if args[0] == 'display-message':
                return SimpleNamespace(returncode=0, stdout=str(process.pid), stderr='')
            if args[0] == 'list-panes':
                if '-a' in args:
                    return SimpleNamespace(returncode=0, stdout='%0\n', stderr='')
                return SimpleNamespace(returncode=0 if state['pane'] else 1,
                                       stdout=f'%1 {process.pid} 0\n' if state['pane'] else '', stderr='')
            if args[0] == 'show-options':
                token = 'anchor' if '%0' in args else 'token'
                return SimpleNamespace(returncode=0, stdout=token + '\n', stderr='')
            if args[0] == 'kill-pane':
                if '%0' in args:
                    process.terminate()
                    process.wait(timeout=5)
                    server_socket.close()
                    socket_path.unlink(missing_ok=True)
                else:
                    state['pane'] = False
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            if args[0] == 'kill-server':
                raise AssertionError('whole tmux server must not be killed')
            raise AssertionError(args)
        monkeypatch.setattr(tmux, 'call', fake_call)
        tmux.reviewer_close(home, endpoint)
        assert not env_file.exists()
        assert ('kill-pane' in calls) and (calls.count('kill-pane') == (2 if created else 1))
        if created:
            assert process.poll() is not None
            assert not socket_path.exists()
        else:
            assert process.poll() is None
            assert socket_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if server_socket.fileno() != -1:
            server_socket.close()
        socket_path.unlink(missing_ok=True)



@pytest.mark.parametrize('foreign_record', [False, True], ids=['matching-record', 'foreign-record'])
def test_tmux_reviewer_created_server_record_is_retired_for_consecutive_runs(
        tmp_path, monkeypatch, foreign_record):
    import socket
    import uuid
    from types import SimpleNamespace
    from agd_lib import supervision, tmux

    home = tmp_path / 'review-home'
    (home / 'data').mkdir(parents=True)
    (home / 'state').mkdir()
    (home / 'data' / 'home.json').write_text(json.dumps({
        'schema': 1, 'home_id': str(uuid.uuid4())}))
    socket_path = tmp_path / 'tmux.sock'
    server = {'process': None, 'socket': None, 'anchor_token': '', 'pane': None,
              'pane_alive': False, 'next_pane': 1}

    def start_server():
        server_socket = socket.socket(socket.AF_UNIX)
        server_socket.bind(str(socket_path))
        process = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
        server.update(process=process, socket=server_socket, pane_alive=False,
                      anchor_token='', pane=None)

    def fake_run(argv, *args, **kwargs):
        if 'has-session' in argv:
            return SimpleNamespace(returncode=1, stdout='', stderr='')
        if 'new-session' in argv:
            start_server()
            return SimpleNamespace(returncode=0, stdout='%0\n', stderr='')
        if 'new-window' in argv:
            server['pane'] = f"%{server['next_pane']}"
            server['next_pane'] += 1
            server['pane_alive'] = True
            return SimpleNamespace(returncode=0, stdout=server['pane'] + '\n', stderr='')
        if 'set-option' in argv and '@agd_anchor_owner' in argv:
            server['anchor_token'] = argv[-1]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if 'display-message' in argv:
            if argv[-1] == '#{pid}':
                return SimpleNamespace(returncode=0, stdout=str(server['process'].pid), stderr='')
            if argv[-1] == '#{socket_path}':
                return SimpleNamespace(returncode=0, stdout=str(socket_path), stderr='')
        raise AssertionError(argv)

    def fake_call(_endpoint, *args, **_kwargs):
        if args[0] == 'set-option' and '@agd_reviewer_owner' in args:
            server['reviewer_token'] = args[-1]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[0] == 'display-message':
            return SimpleNamespace(returncode=0, stdout=str(server['process'].pid), stderr='')
        if args[0] == 'list-panes':
            if '-a' in args:
                return SimpleNamespace(returncode=0, stdout='%0\n', stderr='')
            if not server['pane_alive']:
                return SimpleNamespace(returncode=1, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout=f"{server['pane']} {server['process'].pid} 0\n", stderr='')
        if args[0] == 'show-options':
            target = args[args.index('-t') + 1]
            if target == '%0':
                value = server['anchor_token']
            else:
                value = server['reviewer_token']
            return SimpleNamespace(returncode=0, stdout=value + '\n', stderr='')
        if args[0] == 'kill-pane':
            target = args[args.index('-t') + 1]
            if target == '%0':
                process = server['process']
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                server['socket'].close()
                socket_path.unlink(missing_ok=True)
            else:
                server['pane_alive'] = False
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(args)

    monkeypatch.setattr(tmux.store, 'run', fake_run)
    monkeypatch.setattr(tmux, 'call', fake_call)
    try:
        first = tmux.reviewer_create(
            home, str(uuid.uuid4()), ['/bin/sh', '-c', 'sleep 30'],
            cwd=home, env={'HOME': str(home)})
        tmux.reviewer_close(home, first)
        second = tmux.reviewer_create(
            home, str(uuid.uuid4()), ['/bin/sh', '-c', 'sleep 30'],
            cwd=home, env={'HOME': str(home)})
        record = home / 'state' / 'tmux-server.json'
        if foreign_record:
            value = json.loads(record.read_text())
            value['target'] = dict(value['target'], home_id=str(uuid.uuid4()))
            record.write_text(json.dumps(value))
            saved = record.read_bytes()
            with pytest.raises(Refusal):
                tmux.reviewer_close(home, second)
            assert record.read_bytes() == saved
        else:
            tmux.reviewer_close(home, second)
            assert not record.exists()
    finally:
        process = server['process']
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server_socket = server['socket']
        if server_socket is not None and server_socket.fileno() != -1:
            server_socket.close()
        socket_path.unlink(missing_ok=True)


def test_tmux_reviewer_preserves_preexisting_agd_server_across_runs(
        tmp_path, monkeypatch):
    import socket
    import uuid
    from types import SimpleNamespace
    from agd_lib import tmux

    home = tmp_path / 'review-home'
    (home / 'data').mkdir(parents=True)
    (home / 'state').mkdir()
    (home / 'data' / 'home.json').write_text(json.dumps({
        'schema': 1, 'home_id': str(uuid.uuid4())}))
    socket_path = tmp_path / 'tmux.sock'
    server = {'process': None, 'socket': None, 'initialized': False,
              'anchor_token': '', 'pane': None, 'pane_alive': False,
              'next_pane': 1}

    def start_server():
        server_socket = socket.socket(socket.AF_UNIX)
        server_socket.bind(str(socket_path))
        process = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
        server.update(process=process, socket=server_socket, initialized=True,
                      anchor_token='', pane=None, pane_alive=False)

    def fake_run(argv, *args, **kwargs):
        if 'has-session' in argv:
            return SimpleNamespace(returncode=0 if server['initialized'] else 1,
                                   stdout='', stderr='')
        if 'new-session' in argv:
            start_server()
            return SimpleNamespace(returncode=0, stdout='%0\n', stderr='')
        if 'new-window' in argv:
            server['pane'] = f"%{server['next_pane']}"
            server['next_pane'] += 1
            server['pane_alive'] = True
            return SimpleNamespace(returncode=0, stdout=server['pane'] + '\n', stderr='')
        if 'set-option' in argv and '@agd_anchor_owner' in argv:
            server['anchor_token'] = argv[-1]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if 'display-message' in argv:
            if argv[-1] == '#{pid}':
                return SimpleNamespace(returncode=0, stdout=str(server['process'].pid), stderr='')
            if argv[-1] == '#{socket_path}':
                return SimpleNamespace(returncode=0, stdout=str(socket_path), stderr='')
        raise AssertionError(argv)

    def fake_call(_endpoint, *args, **_kwargs):
        if args[0] == 'set-option' and '@agd_reviewer_owner' in args:
            server['reviewer_token'] = args[-1]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[0] == 'display-message':
            return SimpleNamespace(returncode=0, stdout=str(server['process'].pid), stderr='')
        if args[0] == 'list-panes':
            if '-a' in args:
                return SimpleNamespace(returncode=0, stdout='%0\n%9\n', stderr='')
            if not server['pane_alive']:
                return SimpleNamespace(returncode=1, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout=f"{server['pane']} {server['process'].pid} 0\n", stderr='')
        if args[0] == 'show-options':
            target = args[args.index('-t') + 1]
            value = server['anchor_token'] if target == '%0' else server['reviewer_token']
            return SimpleNamespace(returncode=0, stdout=value + '\n', stderr='')
        if args[0] == 'kill-pane':
            target = args[args.index('-t') + 1]
            assert target != '%0'
            server['pane_alive'] = False
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(args)

    monkeypatch.setattr(tmux.store, 'run', fake_run)
    monkeypatch.setattr(tmux, 'call', fake_call)
    try:
        tmux._ensure_server(home)
        record = home / 'state' / 'tmux-server.json'
        saved = record.read_bytes()
        pid = server['process'].pid
        first = tmux.reviewer_create(
            home, str(uuid.uuid4()), ['/bin/sh', '-c', 'sleep 30'],
            cwd=home, env={'HOME': str(home)})
        tmux.reviewer_close(home, first)
        assert server['process'].poll() is None
        assert socket_path.exists()
        assert record.read_bytes() == saved
        second = tmux.reviewer_create(
            home, str(uuid.uuid4()), ['/bin/sh', '-c', 'sleep 30'],
            cwd=home, env={'HOME': str(home)})
        tmux.reviewer_close(home, second)
        assert server['process'].poll() is None
        assert server['process'].pid == pid
        assert socket_path.exists()
        assert record.read_bytes() == saved
    finally:
        process = server['process']
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server_socket = server['socket']
        if server_socket is not None and server_socket.fileno() != -1:
            server_socket.close()
        socket_path.unlink(missing_ok=True)


@pytest.mark.parametrize('session', ['default', 'focused'])
def test_reviewer_endpoint_rejects_default_or_focused_session(session):
    import uuid
    from agd_lib import herdr
    run_id = str(uuid.uuid4())
    with pytest.raises(Refusal):
        herdr._reviewer_endpoint({
            'backend': 'herdr',
            'review_run_id': run_id,
            'home': '/tmp/owned-reviewer-home',
            'session': session,
        })



def test_managed_reviewer_herdr_endpoint_is_run_scoped_and_absent_after_close(tmp_path):
    import shutil
    import uuid
    from agd_lib import herdr
    if shutil.which('herdr') is None:
        pytest.skip('Herdr executable unavailable')
    home = tmp_path / 'home'
    command(home, 'init', '--backend', 'tmux')
    cwd = home / 'review-runtime'
    cwd.mkdir()
    run_id = str(uuid.uuid4())
    endpoint = herdr.reviewer_create(
        home, run_id, ['/bin/sh', '-c', 'printf managed-reviewer; sleep 30'],
        cwd=cwd, env={'HOME': str(home), 'PATH': '/usr/bin:/bin'})
    try:
        assert endpoint['session'] == f"agd-reviewer-{run_id.replace('-', '')}"
        assert herdr.reviewer_inspect(home, endpoint)['presence'] == 'present'
        with pytest.raises(Refusal):
            herdr.reviewer_create(
                home, run_id, ['/bin/sh', '-c', 'printf collision; sleep 30'],
                cwd=cwd, env={'HOME': str(home), 'PATH': '/usr/bin:/bin'})
        assert herdr.reviewer_inspect(home, endpoint)['presence'] == 'present'
        foreign = dict(endpoint, target={'canonical_home': str(home), 'home_id': str(uuid.uuid4())})
        with pytest.raises(Refusal):
            herdr.reviewer_inspect(home, foreign)
        assert herdr.reviewer_inspect(home, endpoint)['presence'] == 'present'
    finally:
        herdr.reviewer_close(home, endpoint)
    assert herdr.reviewer_inspect(home, endpoint)['presence'] == 'missing'

