import ctypes
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))


def fixture_home(parent, name='distro'):
    home = parent / name
    subprocess.run([sys.executable, str(ROOT / 'bin/agd'), '--home', str(home), 'init', '--backend', 'herdr'],
                   check=True, capture_output=True)
    shutil.copytree(ROOT / 'bin', home / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
    asset = home / 'bundled/observability/release-lock.json'
    asset.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'bundled/observability/release-lock.json', asset)
    return home


def snapshot(home):
    result = {}
    for path in home.rglob('*'):
        mode = path.lstat()
        result[str(path.relative_to(home))] = (
            mode.st_ino, mode.st_mode, mode.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None)
    return result


def terminal(argv, *, cwd, home, answers='approve', env=None, on_output=None, timeout=40):
    master, slave = pty.openpty()
    def setup():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = subprocess.Popen([str(arg) for arg in argv], cwd=cwd, env=env,
                               stdin=slave, stderr=slave, stdout=subprocess.PIPE,
                               text=True, preexec_fn=setup)
    os.close(slave)
    output, sent, deadline = bytearray(), 0, time.monotonic() + timeout
    standard = bytearray()
    discards = 0
    try:
        while process.poll() is None:
            assert time.monotonic() < deadline, output.decode(errors='replace')
            ready = select.select([master, process.stdout], [], [], .02)[0]
            if process.stdout in ready:
                standard.extend(os.read(process.stdout.fileno(), 65536))
            if master in ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            text = output.decode(errors='replace')
            pending = text[sent:]
            if 'Confirm home:' in pending:
                answer = str(home) if answers != 'decline' else 'no'
                os.write(master, b'\x04' if answers == 'eof' else (answer + '\n').encode())
                sent = len(text)
            elif re.search(r'Type (DISCARD [0-9a-f]{16}): ', pending):
                match = re.search(r'Type (DISCARD [0-9a-f]{16}): ', pending)
                answer = match[1] if answers == 'approve' or answers == 'approve-first' and discards == 0 else 'no'
                discards += 1
                os.write(master, (answer + '\n').encode())
                sent = len(text)
            if on_output:
                on_output(process, text)
        process.wait(timeout=10)
        while select.select([master], [], [], 0)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
        standard.extend(process.stdout.read().encode())
        return process.returncode, standard.decode(errors='replace'), output.decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close()
        os.close(master)


def stage_only(home):
    program = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agd_lib import uninstall, uninstall_state
home = Path(sys.argv[2]); preview = uninstall.inventory(home)
with uninstall_state.retirement(home, preview, discard_digest=preview['discard_digest'] if preview['work_loss'] else None) as operation:
    uninstall.stage_executor(home, operation)
'''
    result = subprocess.run([sys.executable, '-B', '-c', program, str(ROOT / 'bin'), str(home)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    from agd_lib import uninstall_state
    return uninstall_state.operation_dir(home)


def test_relative_home_resolved_before_handoff(tmp_path):
    home = fixture_home(tmp_path, 'distro % "quoted"')
    sentinel = tmp_path / 'sibling'; sentinel.write_text('retained')
    code, stdout, stderr = terminal(
        ['/bin/bash', '-c', './bin/agd --home . uninstall && cd .. && printf "\\nPARENT=%s\\n" "$PWD"'],
        cwd=home, home=home)
    assert code == 0, stderr
    result, _ = json.JSONDecoder().raw_decode(stdout.lstrip())
    assert result['outcome'] == 'uninstalled'
    assert result['home'] == str(home)
    assert f'PARENT={tmp_path}\n' in stdout
    assert not home.exists()
    assert sentinel.read_text() == 'retained'


@pytest.mark.parametrize('answers', ['decline', 'eof'])
def test_dry_run_and_decline_write_nothing(tmp_path, answers):
    home = fixture_home(tmp_path)
    secret = 'not-for-the-inventory-' + os.urandom(12).hex()
    private = home / 'data/private.json'; private.write_text(json.dumps({'token': secret}))
    before = snapshot(home)
    result = subprocess.run([str(home / 'bin/agd'), '--home', '.', 'uninstall', '--dry-run'],
                            cwd=home, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview['outcome'] == 'ready'
    assert preview['target']['canonical_home'] == str(home)
    assert result.stdout.startswith('{\n  ')
    assert secret not in result.stdout + result.stderr
    assert snapshot(home) == before
    assert not any(home.rglob('__pycache__'))
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, answers=answers)
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'cancelled'
    assert snapshot(home) == before
    from agd_lib import uninstall_state
    assert not uninstall_state.operation_dir(home).exists()


def test_staged_python_environment_cannot_recreate_target(tmp_path):
    home = fixture_home(tmp_path)
    cache = home / 'poison-cache'
    environment = dict(os.environ, PYTHONPYCACHEPREFIX=str(cache), PYTHONPATH=str(home / 'poison-imports'))
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, env=environment)
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'uninstalled'
    assert not home.exists() and not cache.exists()


def test_owned_work_loss_requires_second_bound_confirmation(tmp_path):
    home = fixture_home(tmp_path)
    work = home / 'personal-work.txt'; work.write_text('retain unpublished work')
    before = snapshot(home)
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, answers='wrong-discard')
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'cancelled'
    assert re.search(r'DISCARD [0-9a-f]{16}', stderr)
    assert work.read_text() == 'retain unpublished work'
    assert snapshot(home) == before


def test_managed_ancestor_is_refused_before_quiescing(tmp_path):
    from agd_lib import supervision
    home = fixture_home(tmp_path)
    identity = supervision.process_identity(os.getpid()); identity.pop('state', None)
    root = home / 'state/native-omp'; root.mkdir(parents=True)
    receipt = root / f"{identity['pid']}-{identity['start']}.json"
    receipt.write_text(json.dumps({'schema': 1, 'target': supervision.runtime_home_identity(home),
                                   'process': identity, 'executable': sys.executable}))
    receipt.chmod(0o600)
    before = snapshot(home)
    result = subprocess.run([str(home / 'bin/agd'), '--home', str(home), 'uninstall', '--dry-run'],
                            capture_output=True, text=True)
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)['outcome'] == 'blocked'
    assert snapshot(home) == before
    os.kill(os.getpid(), 0)


def test_recreated_home_and_corrupt_journal_are_never_adopted(tmp_path):
    for kind in ('replaced', 'corrupt'):
        parent = tmp_path / kind; parent.mkdir()
        home = fixture_home(parent)
        directory = stage_only(home)
        if kind == 'replaced':
            home.rename(parent / 'original')
            home.mkdir(); (home / 'sentinel').write_text('replacement')
        else:
            (directory / 'operation.json').write_bytes(b'{corrupt')
        before, evidence = snapshot(home), (directory / 'operation.json').read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / 'bin/agd'), '--home', str(home),
                                 'uninstall', '--dry-run'], capture_output=True, text=True)
        assert result.returncode == 3, result.stderr
        assert json.loads(result.stdout)['outcome'] == 'blocked'
        assert snapshot(home) == before
        assert (directory / 'operation.json').read_bytes() == evidence


def test_optional_features_never_enabled_need_no_optional_runtime(tmp_path):
    home = fixture_home(tmp_path)
    empty = tmp_path / 'empty-path'; empty.mkdir()
    code, stdout, stderr = terminal([sys.executable, home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, env=dict(os.environ, PATH=str(empty), NO_COLOR='1'))
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'uninstalled'
    assert not home.exists()
    assert '\x1b[' not in stderr


def _crash_and_resume(tmp_path, *, partial):
    from agd_lib import uninstall_state
    home = fixture_home(tmp_path)
    watched = home / 'data/crash-files' if partial else home.parent
    if partial:
        watched.mkdir()
        for index in range(3000):
            (watched / str(index)).write_text('owned crash fixture')
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    assert descriptor >= 0
    watch = libc.inotify_add_watch(descriptor, os.fsencode(watched), 0x200)
    assert watch >= 0
    killed = []
    def interrupt(process, _text):
        if killed or not select.select([descriptor], [], [], 0)[0]:
            return
        events = os.read(descriptor, 65536)
        if not partial and os.fsencode(home.name) + b'\0' not in events:
            return
        os.kill(process.pid, signal.SIGKILL)
        killed.append(True)
    try:
        code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                        cwd=home, home=home, on_output=interrupt)
    finally:
        os.close(descriptor)
    assert killed and code == -signal.SIGKILL, (code, stdout, stderr)
    directory = uninstall_state.operation_dir(home)
    record = json.loads((directory / 'operation.json').read_text())
    assert record['phase'] == 'verifying' and record['irreversible_started']
    if partial:
        assert home.exists() and 0 < len(list(watched.iterdir())) < 3000
        shutil.rmtree(home / 'bin', ignore_errors=True)
    else:
        assert not home.exists()
    recovery = re.search(r'^Recovery: (.+)$', stderr, re.MULTILINE)
    assert recovery, stderr
    started = time.monotonic()
    code, stdout, stderr = terminal(shlex.split(recovery[1].strip()), cwd=home.parent, home=home, timeout=40)
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'uninstalled'
    assert time.monotonic() - started >= 8
    assert not home.exists()
    final = json.loads((directory / 'operation.json').read_text())
    assert final['phase'] == 'completed'
    assert not (directory / 'executor').exists()


def test_crash_during_home_removal_resumes_without_original_cli(tmp_path):
    _crash_and_resume(tmp_path, partial=True)


def test_crash_after_home_unlink_repeats_delayed_verification(tmp_path):
    _crash_and_resume(tmp_path, partial=False)


def test_declining_pending_reversible_operation_clears_retirement(tmp_path):
    home = fixture_home(tmp_path)
    directory = stage_only(home)
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, answers='decline')
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'cancelled'
    assert json.loads((directory / 'operation.json').read_text())['phase'] == 'cancelled'
    result = subprocess.run([home / 'bin/agd', '--home', home, 'session', 'status'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_work_changed_after_approval_requires_refreshed_discard(tmp_path):
    home = fixture_home(tmp_path)
    work = home / 'personal-work'; work.write_text('original unpublished work')
    changed = []
    def change(_process, text):
        if not changed and 'Type DISCARD ' in text:
            work.write_text('new work after the displayed preview')
            changed.append(True)
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'], cwd=home, home=home,
                                    answers='approve-first', on_output=change)
    assert code == 0, stderr
    assert json.loads(stdout)['outcome'] == 'cancelled'
    assert work.read_text() == 'new work after the displayed preview'
    assert len(set(re.findall(r'Type (DISCARD [0-9a-f]{16}): ', stderr))) == 2


def test_unrecorded_foreign_native_lease_is_not_a_plain_git_checkout():
    from support import lab
    from agd_lib import store, uninstall
    with lab() as (home, project):
        root = home / 'data/treehouse'
        allocated = json.loads(store.run(
            ['treehouse', '--root', str(root), 'get', '--lease', '--json', '--no-fetch',
             '--lease-holder', 'independent-owner'], cwd=project).stdout)
        foreign = Path(allocated['path']) / 'foreign-work'
        foreign.write_text('must survive')
        preview = uninstall.inventory(home)
        assert preview['outcome'] == 'blocked'
        assert foreign.read_text() == 'must survive'
        rows = json.loads(store.run(['treehouse', '--root', str(root), 'status', '--json'], cwd=project).stdout)
        assert next(row for row in rows if row['path'] == allocated['path'])['lease_id'] == allocated['lease_id']


def test_linked_home_uses_native_git_removal(tmp_path):
    from smoke_uninstall import checkouts
    backing, (home, sentinel) = checkouts(tmp_path)
    refs = subprocess.run(['git', 'show-ref'], cwd=backing, check=True, capture_output=True, text=True).stdout
    code, stdout, stderr = terminal([sys.executable, home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, timeout=60)
    assert code == 0, (stdout, stderr)
    assert json.loads(stdout)['outcome'] == 'uninstalled'
    assert not home.exists()
    assert (sentinel / 'bin/agd').is_file()
    current = subprocess.run(['git', 'show-ref'], cwd=backing, check=True, capture_output=True, text=True).stdout
    assert current == refs
    listed = subprocess.run(['git', 'worktree', 'list', '--porcelain'], cwd=backing,
                            check=True, capture_output=True, text=True).stdout
    assert str(home) not in listed and str(sentinel) in listed


def test_retirement_pointer_without_external_authority_is_not_overwritten(tmp_path):
    home = fixture_home(tmp_path)
    pointer = home / 'state/uninstall.json'
    pointer.write_bytes(b'{unresolved-pointer')
    before = snapshot(home)
    result = subprocess.run([home / 'bin/agd', '--home', home, 'uninstall', '--dry-run'],
                            capture_output=True, text=True)
    assert result.returncode == 3
    assert json.loads(result.stdout)['outcome'] == 'blocked'
    assert snapshot(home) == before


def test_cancelled_stage_does_not_pin_the_previous_source_version(tmp_path):
    home = fixture_home(tmp_path)
    stage_only(home)
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, answers='decline')
    assert code == 0, stderr
    with (home / 'bin/agd').open('a') as stream:
        stream.write('\n# Fixture source revision after cancellation.\n')
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home)
    assert code == 0, (stdout, stderr)
    assert json.loads(stdout)['outcome'] == 'uninstalled'
    assert not home.exists()


def test_terminal_journal_cannot_authorize_or_block_a_new_home(tmp_path):
    from agd_lib import uninstall_state
    home = fixture_home(tmp_path)
    directory = stage_only(home)
    code, _, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                               cwd=home, home=home, answers='decline')
    assert code == 0, stderr
    old = uninstall_state.target_identity(home)
    shutil.rmtree(home)
    home = fixture_home(tmp_path)
    current = uninstall_state.target_identity(home)
    assert current['home_id'] != old['home_id']
    stage_only(home)
    assert json.loads((directory / 'operation.json').read_text())['target'] == current


def test_missing_required_docker_inspector_uses_dependency_exit(tmp_path, monkeypatch):
    from agd_lib import observability, store, uninstall
    home = fixture_home(tmp_path)
    observability.render_stack(home)
    before = snapshot(home)
    def unavailable(_argv):
        try:
            raise FileNotFoundError('docker')
        except FileNotFoundError as error:
            raise store.Refusal('observability ownership inspection is unavailable') from error
    monkeypatch.setattr(observability, '_ownership_command', unavailable)
    result, code = uninstall.command(home, dry_run=True, ask=lambda _m: pytest.fail('dry-run asked'),
                                     report=lambda _m: pytest.fail('dry-run wrote progress'))
    assert code == 4
    assert result['outcome'] == 'blocked'
    assert snapshot(home) == before


def test_recreated_home_during_delay_is_not_deleted_again(tmp_path):
    home = fixture_home(tmp_path)
    replacement = []
    def recreate(_process, text):
        if not replacement and '3 / 3' in text:
            assert not home.exists()
            home.mkdir()
            (home / 'replacement-work').write_text('preserve the new home')
            replacement.append(snapshot(home))
    code, stdout, stderr = terminal([home / 'bin/agd', '--home', '.', 'uninstall'],
                                    cwd=home, home=home, on_output=recreate)
    assert code == 3, (stdout, stderr)
    assert replacement and json.loads(stdout)['outcome'] == 'blocked'
    assert snapshot(home) == replacement[0]
    result = subprocess.run([sys.executable, ROOT / 'bin/agd', '--home', home, 'uninstall', '--dry-run'],
                            capture_output=True, text=True)
    assert result.returncode == 3
    assert snapshot(home) == replacement[0]
