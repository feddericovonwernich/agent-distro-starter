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
                      f'distro-{name}\n{name}\n'.encode() + updates + b'exit\n', detach=False)
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
