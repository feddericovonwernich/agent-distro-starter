import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import store
from support import command, run, uninstall_scope

ROOT = Path(__file__).resolve().parents[1]


def state():
    from agd_lib import uninstall_state
    return uninstall_state


@pytest.fixture
def home(tmp_path):
    value = tmp_path / 'home'
    command(value, 'init', '--backend', 'tmux')
    return value


def preview(home):
    identity = home.stat()
    target = {'canonical_home': str(home.resolve()),
              'home_id': json.loads((home / 'data/home.json').read_text())['home_id'],
              'uid': os.getuid(), 'device': identity.st_dev, 'inode': identity.st_ino}
    resource = {'key': f'home:{home.resolve()}', 'kind': 'home', 'identity': target,
                'presence': 'present', 'ownership': 'owned', 'blockers': []}
    value = {'schema': 1, 'target': target, 'resources': [resource], 'work_loss': [],
             'retained': [], 'blockers': [], 'warnings': [], 'outcome': 'ready',
             'discard_digest': None}
    value['digest'] = state().fingerprint({'target': target, 'resources': [resource], 'work_loss': []})
    return value




def test_retirement_blocks_creation_without_recreating_home(home):
    module = state()
    with uninstall_scope(home, preview(home)['resources']):
        shutil.rmtree(home)
        with pytest.raises(store.Refusal):
            module.require_active(home)
        result = run([ROOT / 'bin/agd', '--home', home, 'init', '--backend', 'tmux'], expected=3)
        assert result.returncode == 3
        assert not home.exists()


def test_admission_drains_before_retirement(home):
    module = state()
    program = (
        'import sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0, {str(ROOT / "bin")!r})\n'
        'from agd_lib import uninstall_state\n'
        'with uninstall_state.admission(Path(sys.argv[1])):\n'
        ' print("entered", flush=True)\n'
        ' sys.stdin.read(1)\n'
        'print("released", flush=True)\n')
    child = subprocess.Popen([sys.executable, '-c', program, str(home)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'entered'
        with pytest.raises(store.Refusal):
            with module.retirement(home, preview(home), discard_digest=None):
                pytest.fail('retirement entered while a creator was admitted')
        assert not module.operation_dir(home).exists()
        child.stdin.write('x')
        child.stdin.flush()
        assert child.stdout.readline().strip() == 'released'
        assert child.wait(timeout=10) == 0
        with uninstall_scope(home, preview(home)['resources']):
            with pytest.raises(store.Refusal):
                with module.admission(home):
                    (home / 'late-child').write_text('must not start')
        assert not (home / 'late-child').exists()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()


def test_admission_does_not_deadlock_nested_store_mutation(home):
    state()
    program = (
        'import sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0, {str(ROOT / "bin")!r})\n'
        'from agd_lib import store, uninstall_state\n'
        'home=Path(sys.argv[1])\n'
        'with uninstall_state.admission(home):\n'
        ' with store.lock(home):\n'
        '  (home/"receipt.txt").write_text("registered")\n')
    subprocess.run([sys.executable, '-c', program, str(home)], check=True, timeout=10)
    assert (home / 'receipt.txt').read_text() == 'registered'


def test_replaced_home_never_inherits_old_scope(home):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        key = f'home:{home}'
        assert module.require_scope(home, operation, key)['key'] == key
        old = home.with_name('original')
        home.rename(old)
        home.mkdir()
        (home / 'sentinel').write_text('new home must survive')
        with pytest.raises(store.Refusal):
            module.require_scope(home, operation, key)
        assert (home / 'sentinel').read_text() == 'new home must survive'
        assert (old / 'data/home.json').is_file()


@pytest.mark.parametrize('fault', ['malformed', 'identity', 'inventory'])
def test_corrupt_or_conflicting_operation_retains_evidence(home, fault):
    module = state()
    with uninstall_scope(home, preview(home)['resources']):
        journal = module.operation_dir(home) / 'operation.json'
        value = json.loads(journal.read_text())
        if fault == 'malformed':
            body = b'{broken\n'
        else:
            if fault == 'identity':
                value['target']['canonical_home'] = str(home.parent / 'other')
            else:
                value['inventory']['resources'][0]['identity']['inode'] += 1
            body = json.dumps(value).encode()
        journal.write_bytes(body)
        with pytest.raises(store.Refusal):
            module.read_operation(home)
        with pytest.raises(store.Refusal):
            module.require_active(home)
        assert journal.read_bytes() == body


@pytest.mark.parametrize('irreversible', [False, True])
def test_cancelled_and_irreversible_operations_have_distinct_admission(home, irreversible):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        if irreversible:
            operation = module.checkpoint(home, operation, phase='removing', irreversible=True)
            with pytest.raises(store.Refusal):
                module.checkpoint(home, operation, phase='cancelled')
            with pytest.raises(store.Refusal):
                with module.admission(home):
                    (home / 'new-work').write_text('unsafe')
            assert not (home / 'new-work').exists()
        else:
            module.checkpoint(home, operation, phase='cancelled')
            with module.admission(home):
                (home / 'new-work').write_text('explicit restart')
            assert (home / 'new-work').read_text() == 'explicit restart'


def test_absent_operation_inspection_writes_nothing(home):
    module = state()
    before = set(home.parent.rglob('*'))
    assert module.read_operation(home) is None
    module.require_active(home)
    assert set(home.parent.rglob('*')) == before


def test_unrelated_operation_directory_is_not_repaired(home):
    module = state()
    directory = module.operation_dir(home)
    directory.mkdir(mode=0o700)
    sentinel = directory / 'personal-file'
    sentinel.write_text('leave me alone')
    with pytest.raises(store.Refusal):
        module.require_active(home)
    assert sentinel.read_text() == 'leave me alone'
    assert sorted(path.name for path in directory.iterdir()) == ['personal-file']


@pytest.mark.parametrize('fault', ['changed', 'symlink'])
def test_executor_corruption_cannot_gain_teardown_authority(home, fault):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        script = module.operation_dir(home) / 'bin/agd'
        if fault == 'changed':
            script.write_text('not the approved executor')
        else:
            script.unlink()
            script.symlink_to(ROOT / 'bin/agd')
        with pytest.raises(store.Refusal):
            module.require_scope(home, operation, f'home:{home}')
        assert (home / 'data/home.json').is_file()



@pytest.mark.parametrize('relative', ['bin/agd_lib/unexpected.py', 'bin/agd_lib/__pycache__/store.pyc'])
def test_unbound_import_input_cannot_gain_teardown_authority(home, relative):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        extra = module.operation_dir(home) / relative
        extra.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        extra.write_bytes(b'unapproved import input')
        with pytest.raises(store.Refusal):
            module.require_scope(home, operation, f'home:{home}')
        assert (home / 'data/home.json').is_file()

def test_approval_does_not_authorize_an_unlisted_resource(home):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        with pytest.raises(store.Refusal):
            module.require_scope(home, operation, 'home:/a-different-home')
        assert (home / 'data/home.json').is_file()


def test_retirement_prevents_direct_project_clone(home, tmp_path):
    from agd_lib import projects
    source = tmp_path / 'source'
    run(['git', 'init', '-b', 'main', source])
    run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
         'commit', '--allow-empty', '-m', 'seed'], cwd=source)
    with uninstall_scope(home, preview(home)['resources']):
        with pytest.raises(store.Refusal):
            projects.add(home, 'late-project', str(source))
        assert not (home / 'projects/late-project').exists()
        assert not (home / 'data/projects/late-project.json').exists()


def test_retirement_prevents_native_central_spawn(home, monkeypatch):
    from agd_lib import launcher
    marker = home / 'native-endpoint'
    monkeypatch.setattr(launcher.observability, 'central_argv', lambda *_: (['/bin/true'], {}, None))
    def create(*_args):
        marker.write_text('spawned')
        raise store.Refusal('fixture backend failed after spawning')
    with uninstall_scope(home, preview(home)['resources']):
        with pytest.raises(store.Refusal):
            launcher.prepare_start(home, 'tmux', {}, create, lambda *_: {}, lambda *_: [])
        assert not marker.exists()
        assert not (home / 'state/central-launcher.json').exists()


def test_retirement_prevents_late_worker_session_binding(home):
    from agd_lib import observability
    with uninstall_scope(home, preview(home)['resources']):
        with pytest.raises(store.Refusal):
            observability.worker_binding(home, 'late-task', '12345678-1234-4234-8234-123456789abc')
        assert not (home / 'data/observability').exists()
        assert not (home / 'state/observability').exists()


def test_retirement_prevents_timer_intent_after_quiescing(home):
    from agd_lib import reviewer_timer
    timer = home / 'state/reviewer/checkpoints/timer.json'
    timer.parent.mkdir(parents=True)
    timer.write_text(json.dumps({
        'schema': 1, 'home_id': json.loads((home / 'data/home.json').read_text())['home_id'],
        'uid': os.getuid(), 'state': 'enabled', 'anchor': 0,
        'schedule': {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 1},
        'checkpoint': {'anchor': 0, 'last_due': 0},
    }))
    with uninstall_scope(home, preview(home)['resources']):
        with pytest.raises(store.Refusal):
            reviewer_timer.timer_tick(home, now=3600)
        assert not (home / 'state/reviewer/intents').exists()
        assert not (home / 'state/reviewer/pending').exists()


def test_retirement_prevents_manual_intent_creation(home):
    from agd_lib import reviewer
    with uninstall_scope(home, preview(home)['resources']):
        with pytest.raises(store.Refusal):
            reviewer.enqueue_intent(home, kind='manual')
        assert not reviewer.paths(home)['intents'].exists()
        assert not reviewer.paths(home)['pending'].exists()


def test_retirement_prevents_manual_reviewer_registration(home):
    from agd_lib import reviewer, reviewer_runner
    intent = reviewer.enqueue_intent(home, kind='manual')
    with uninstall_scope(home, preview(home)['resources']):
        with pytest.raises(store.Refusal):
            reviewer_runner.run_native_reviewer(home, intent)
        assert not reviewer.paths(home)['receipts'].exists()
        assert not reviewer.paths(home)['lock'].exists()


def test_retirement_prevents_watcher_owner_publication(home):
    from agd_lib import supervision
    with uninstall_scope(home, preview(home)['resources']):
        child = subprocess.Popen(
            [ROOT / 'bin/agd', '--home', home, 'session', 'start',
             '--owner-pid', str(os.getpid()), '--owner-start', supervision.process_start(os.getpid()),
             '--generation', 'retirement-test'],
            env=dict(os.environ, AGD_OWNER='fixture-owner'), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            try:
                child.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.communicate(timeout=10)
            assert child.returncode == 3
            assert not (home / 'state/owner.json').exists()
            assert not (home / 'state/session.lock').exists()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def test_watching_does_not_hold_startup_admission_for_its_lifetime(home):
    from agd_lib import supervision
    child = subprocess.Popen(
        [ROOT / 'bin/agd', '--home', home, 'session', 'start',
         '--owner-pid', str(os.getpid()), '--owner-start', supervision.process_start(os.getpid()),
         '--generation', 'lifetime-test'],
        env=dict(os.environ, AGD_OWNER='fixture-owner'), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert json.loads(child.stdout.readline())['type'] == 'ready'
        with state().retirement(home, preview(home), discard_digest=None):
            assert child.poll() is None
    finally:
        child.terminate()
        child.communicate(timeout=10)


def test_retirement_prevents_worker_child_before_registration(tmp_path, monkeypatch):
    from support import lab
    from agd_lib import tasks, workers, observability
    with lab() as (home, _):
        brief = home / 'brief.md'
        brief.write_text('Read the local fixture.')
        task = command(home, 'task', 'create', 'admission', '--project', 'sample',
                       '--brief', str(brief), '--skill-mode', 'none')
        incarnation = '12345678-1234-4234-8234-123456789abc'
        binding = observability.worker_binding(home, task['id'], incarnation)
        task.update(executor='worker', incarnation=incarnation, phase='running', harness='omp',
                    backend='tmux', endpoint={'fixture': True}, unattended=False,
                    observability=binding)
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', incarnation)
        marker = home / 'model-started'
        monkeypatch.setattr(workers.harnesses, 'launch_argv', lambda *_args, **_kwargs: [
            sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text(\"started\")'])
        with uninstall_scope(home, preview(home)['resources']):
            with pytest.raises(store.Refusal):
                workers.run_worker(home, task['id'], incarnation)
            assert not marker.exists()
            assert not (home / 'state/tasks/admission/runner.json').exists()


def test_native_omp_handoff_is_owned_and_releases_admission(home):
    from agd_lib import supervision
    executable = home.parent / 'omp'
    executable.write_text(
        '#!/usr/bin/env python3\n'
        'import sys\n'
        'print("started", flush=True)\n'
        'sys.stdin.read()\n')
    executable.chmod(0o755)
    store.atomic_json(home / 'data/dependencies.json', {'schema': 1, 'omp': {
        'source': 'existing', 'path': str(executable), 'version': 'omp/18.14.0',
        'capabilities': ['models-json', 'login']}})
    child = subprocess.Popen([ROOT / 'bin/agd', '--home', home, 'omp'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline() == 'started\n'
        receipts = list((home / 'state/native-omp').glob('*.json'))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text())
        assert receipt['pid'] == child.pid
        assert receipt['start'] == supervision.process_start(child.pid)
        assert receipt['target'] == state().target_identity(home)
        with state().retirement(home, preview(home), discard_digest=None):
            assert child.poll() is None
    finally:
        child.terminate()
        child.communicate(timeout=10)



def test_cancelled_preparation_releases_admission_without_executor(home):
    module = state()
    with module.retirement(home, preview(home), discard_digest=None) as operation:
        module.checkpoint(home, operation, phase='cancelled')
    with module.admission(home):
        (home / 'new-work').write_text('usable')
    assert (home / 'new-work').read_text() == 'usable'


def test_cancelled_scope_never_authorizes_replacement_but_allows_new_work(home):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        module.checkpoint(home, operation, phase='cancelled')
    home.rename(home.with_name('original'))
    command(home, 'init', '--backend', 'tmux')
    sentinel = home / 'sentinel'
    sentinel.write_text('new home')
    with pytest.raises(store.Refusal):
        module.require_scope(home, operation, f'home:{home}')
    with module.admission(home):
        (home / 'new-work').write_text('usable')
    assert sentinel.read_text() == 'new home'


def test_conflicting_in_home_pointer_is_preserved_and_refused(home):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        pointer = home / 'state/uninstall.json'
        value = json.loads(pointer.read_text())
        value['operation'] = str(home.parent / 'another-operation')
        store.atomic_json(pointer, value)
        original = pointer.read_bytes()
        with pytest.raises(store.Refusal):
            module.require_scope(home, operation, f'home:{home}')
        assert pointer.read_bytes() == original


def test_partial_home_without_state_remains_recoverable_without_recreation(home):
    module = state()
    with uninstall_scope(home, preview(home)['resources']) as operation:
        module.checkpoint(home, operation, phase='removing', irreversible=True)
        shutil.rmtree(home / 'state')
        module.checkpoint(home, operation, phase='blocked', error='fixture interruption')
        assert module.read_operation(home)['phase'] == 'blocked'
        module.checkpoint(home, operation, phase='verifying')
        assert module.require_scope(home, operation, f'home:{home}')['identity'] == operation['target']
        assert not (home / 'state').exists()
