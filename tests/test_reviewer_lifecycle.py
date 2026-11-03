import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer, reviewer_runner, store
from support import lab


def _reserved_reviewer_run(home, *, work=True, deadline=None):
    from test_reviewer import ended_worker, message
    if work:
        ended_worker(home, 'native-reviewer', str(__import__('uuid').uuid4()), [message('evidence')])
    intent = reviewer.enqueue_intent(home, kind='manual')
    config = reviewer.read_config(home)
    started = __import__('time').time()
    run = reviewer.reserve_run(
        home, intent, config_snapshot=config,
        instruction_snapshot=reviewer.INSTRUCTION_FILE.read_bytes(),
        started_at=started,
        deadline_epoch_ms=int((started + (deadline or config['limits']['max_runtime_seconds'])) * 1000))
    packet = reviewer.select_packet(home, cutoff=started + 1, config_snapshot=config) if work else None
    if packet is not None:
        with store.lock(home):
            receipt = reviewer._receipt(home, run['review_run_id'])
            receipt['packet_id'] = packet['packet_id']
            store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
    return run, packet


def _write_native_request(home, run, packet):
    runtime = reviewer.paths(home)['runtime'] / run['review_run_id']
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / 'request.json').write_text(json.dumps({
        'schema': 1, 'review_run_id': run['review_run_id'],
        'home': str(home.resolve()), 'cwd': str(runtime),
        'packet_id': packet['packet_id'],
        'config_snapshot_ref': run['config_snapshot_ref'],
        'instruction_snapshot_ref': run['instruction_snapshot_ref'],
        'report_path': str(runtime / 'report-draft.json'),
        'allowed_scope': {'source_ids': sorted(source['source_id'] for source in packet['sources'])},
    }))
    return runtime


def _run_child(home, monkeypatch, run, packet, mode='success'):
    fixture = _fake_omp(home)
    runtime = _write_native_request(home, run, packet)
    monkeypatch.setenv('AGD_HOME', str(home))
    monkeypatch.setenv('AGD_CLI', str(Path(__file__).resolve().parents[1] / 'bin' / 'agd'))
    monkeypatch.setenv('AGD_FAKE_OMP_MODE', mode)
    monkeypatch.setattr(reviewer_runner.omp_runtime, 'executable', lambda _home: str(fixture))
    pid = os.fork()
    if pid == 0:
        try:
            os._exit(int(reviewer_runner.execute(home, run['review_run_id'])))
        except BaseException:
            os._exit(70)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), runtime


def _collect(home, run):
    return reviewer_runner._collect_native_result(home, run['review_run_id'])


def _fake_omp(home):
    path = home / 'fake-omp'
    path.write_text(
        '#!/usr/bin/env python3\n'
        'import json, os, subprocess, sys, time\n'
        'from pathlib import Path\n'
        'home = Path(os.environ["AGD_HOME"])\n'
        'run_id = Path.cwd().name\n'
        'runtime = home / "state/reviewer/runtime" / run_id\n'
        'mode = os.environ.get("AGD_FAKE_OMP_MODE", "success")\n'
        '(runtime / "child.pid").write_text(str(os.getpid()))\n'
        'count = runtime / "dispatch-count"\n'
        'count.write_text(str(int(count.read_text()) + 1) if count.exists() else "1")\n'
        'session_dir = home / "data/observability/reviewer" / run_id / "omp"\n'
        'session_dir.mkdir(parents=True, exist_ok=True)\n'
        'session_file = session_dir / "fixture-session.jsonl"\n'
        'session_file.write_text("\\n".join([json.dumps({"type":"title","v":1,"title":"fixture"}), json.dumps({"type":"session","version":3,"id":"fixture-session"})]) + "\\n")\n'
        'subprocess.run([sys.executable, os.environ["AGD_CLI"], "--home", str(home), "reviewer",\n'
        '                "session-bind", run_id, "--session-id", "fixture-session",\n'
        '                "--session-file", str(session_file)], check=True)\n'
        'if mode == "missing": sys.exit(0)\n'
        'if mode == "invalid": (runtime / "report-draft.json").write_text("{\\"schema\\":1}")\n'
        'else: (runtime / "report-draft.json").write_text(json.dumps({"schema":1,"summary":"fixture","findings":[]}))\n'
        'if mode == "nonzero":\n'
        ' sys.stderr.write("fixture nonzero diagnostic\\\\n"); sys.exit(7)\n'
        'if mode == "timeout":\n'
        ' sys.stderr.write("fixture timeout diagnostic\\\\n"); sys.stderr.flush(); time.sleep(30)\n'
        'if mode == "crash-after-exit": sys.exit(0)\n'
        'sys.exit(0)\n')
    path.chmod(0o700)
    return path




def _packet_ids(home):
    selected = reviewer.select_packet(
        home, cutoff=__import__('time').time() + 1,
        config_snapshot=reviewer.read_config(home))
    return {
        'sources': tuple(sorted(source['source_id'] for source in selected['sources'])),
        'excerpts': tuple(sorted(excerpt['excerpt_id'] for excerpt in selected['excerpts'])),
        'catalog': tuple(sorted((row['id'], row['catalog_sha256']) for row in selected.get('catalog', []))),
    }

def _real_exit_proof(home, run, packet, *, keep_supervisor=False, keep_child=False):
    runtime = _write_native_request(home, run, packet)
    supervisor = os.fork()
    if supervisor == 0:
        child = subprocess.Popen(['sleep', '30'], start_new_session=True)
        from agd_lib.supervision import process_identity
        child_identity = process_identity(child.pid)
        if not keep_child:
            child.terminate()
            child.wait()
        with store.lock(home):
            receipt = reviewer._receipt(home, run['review_run_id'])
            receipt.update(
                dispatch_state='begun', model=None, model_selector=None,
                omp_version=None, supervisor=process_identity(os.getpid()),
                child=child_identity)
            store.atomic_json(
                reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
        reviewer_runner._write_exit(
            runtime, 0, True, False, b'', b'', child=child_identity,
            supervisor=receipt['supervisor'])
        if keep_supervisor:
            while True:
                __import__('time').sleep(1)
        os._exit(0)
    return supervisor, runtime

def test_standalone_omp_needs_no_sdk_or_bun(monkeypatch):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        monkeypatch.setattr(
            reviewer_runner, 'assert_runtime_capability',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('SDK probe used')))
        original_which = reviewer_runner.shutil.which
        monkeypatch.setattr(
            reviewer_runner.shutil, 'which',
            lambda name: (_ for _ in ()).throw(AssertionError('Bun lookup used'))
            if name == 'bun' else original_which(name))
        code, _ = _run_child(home, monkeypatch, run, packet)
        result = _collect(home, run)
        assert code == 0 and result['dispatch_state'] == 'final'
        assert result['outcome'] == 'exited'


def test_valid_report_then_nonzero_does_not_commit(monkeypatch):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        before = _packet_ids(home)
        code, _ = _run_child(home, monkeypatch, run, packet, 'nonzero')
        result = _collect(home, run)
        assert code == 7 and result['dispatch_state'] == 'final'
        assert result['outcome'] in ('failed', 'unknown')
        assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        assert _packet_ids(home) == before


@pytest.mark.parametrize('mode', ('missing', 'invalid'))
def test_missing_or_invalid_report_is_retryable(monkeypatch, mode):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        before = _packet_ids(home)
        code, _ = _run_child(home, monkeypatch, run, packet, mode)
        result = _collect(home, run)
        assert code == 0 and result['dispatch_state'] == 'final'
        assert result['outcome'] in ('failed', 'unknown')
        assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        assert _packet_ids(home) == before


def test_duplicate_execute_does_not_dispatch_again(monkeypatch):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        code, runtime = _run_child(home, monkeypatch, run, packet)
        assert code == 0
        with pytest.raises(store.Refusal):
            reviewer_runner.execute(home, run['review_run_id'])
        assert (runtime / 'dispatch-count').read_text() == '1'


def test_no_work_allocates_no_endpoint(monkeypatch):
    with lab() as (home, _):
        checkpoint = reviewer.paths(home)['checkpoint']
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes(b'{"anchor":0,"last_due":0}')
        result = reviewer_runner.run_native_reviewer(
            home, reviewer.enqueue_intent(home, kind='manual'))
        assert result['state'] == 'no-work'
        assert not reviewer.paths(home)['runtime'].exists()


def test_reviewer_session_binding_refuses_foreign_directory(tmp_path):
    with lab() as (home, _):
        run, _ = _reserved_reviewer_run(home)
        foreign = tmp_path / 'foreign.jsonl'
        foreign.write_text('{"type":"session","id":"foreign"}\n')
        with pytest.raises(store.Refusal):
            reviewer_runner.bind_session(home, run['review_run_id'], 'foreign', foreign)


def test_timeout_retains_diagnostics_and_unreviewed_seed(monkeypatch):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home, deadline=1)
        before = _packet_ids(home)
        code, runtime = _run_child(home, monkeypatch, run, packet, 'timeout')
        result = _collect(home, run)
        assert code != 0 and result['dispatch_state'] == 'final'
        assert result['outcome'] == 'unknown'
        assert 'fixture timeout diagnostic' in (runtime / 'stderr').read_text()
        assert _packet_ids(home) == before


def test_crash_after_exit_recovers_report_without_redispatch(monkeypatch):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        code, runtime = _run_child(home, monkeypatch, run, packet, 'crash-after-exit')
        assert code == 0
        pid = int((runtime / 'child.pid').read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        original_submit = reviewer.submit_report
        crashed = [True]

        def crash_once(*args, **kwargs):
            if crashed.pop():
                raise RuntimeError('simulated parent crash at report submission')
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(reviewer, 'submit_report', crash_once)
        with pytest.raises(RuntimeError):
            _collect(home, run)
        monkeypatch.setattr(reviewer, 'submit_report', original_submit)
        result = _collect(home, run)
        assert result['dispatch_state'] == 'final'
        assert (runtime / 'dispatch-count').read_text() == '1'
        assert len(list(reviewer.paths(home)['reports'].glob('*.json'))) == 1


def test_missing_exit_proof_cannot_recover_success():
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        runtime = _write_native_request(home, run, packet)
        (runtime / 'report-draft.json').write_text(
            json.dumps({'schema': 1, 'summary': 'unproved', 'findings': []}))
        result = reviewer_runner._collect_native_result(home, run['review_run_id'])
        assert result['dispatch_state'] == 'final'
        assert result['outcome'] in ('failed', 'unknown')
        assert not list(reviewer.paths(home)['reports'].glob('*.json'))


def test_collector_rejects_live_supervisor_after_child_exit():
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        supervisor, _ = _real_exit_proof(home, run, packet, keep_supervisor=True)
        try:
            for _ in range(50):
                if (reviewer.paths(home)['runtime'] / run['review_run_id'] / 'exit.json').exists():
                    break
                __import__('time').sleep(.02)
            result = _collect(home, run)
            assert result['dispatch_state'] == 'final'
            assert result['outcome'] in ('failed', 'unknown')
            assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        finally:
            try:
                os.kill(supervisor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(supervisor, 0)


def test_collector_rejects_surviving_owned_child_group():
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        supervisor, _ = _real_exit_proof(home, run, packet, keep_child=True)
        os.waitpid(supervisor, 0)
        child = reviewer._receipt(home, run['review_run_id'])['child']
        try:
            result = _collect(home, run)
            assert result['dispatch_state'] == 'final'
            assert result['outcome'] in ('failed', 'unknown')
            assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        finally:
            try:
                os.killpg(child['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_reviewer_session_directory_does_not_bind_pending_id():
    with lab() as (home, _):
        run = '11111111-1111-4111-8111-111111111111'
        directory = Path(__import__('agd_lib.observability', fromlist=['reviewer_session_dir']).reviewer_session_dir(home, run))
        assert directory.is_dir() and not (directory.parent / 'binding.json').exists()

def test_native_runner_uses_local_agent_empty_project_and_safe_diagnostic(monkeypatch):
    with lab() as (home, _):
        from test_reviewer import ended_worker, message
        import uuid
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')])
        from agd_lib import omp_runtime
        capture = home / 'capture.json'
        fake = home / 'fake-bun'
        fake.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            f'capture = {str(capture)!r}\n'
            'payload = json.loads(sys.stdin.readline())\n'
            'open(capture, \"w\").write(json.dumps({\"argv\": sys.argv[1:], '
            '\"cwd\": os.getcwd(), \"payload\": payload, '
            '\"home\": os.environ.get(\"HOME\"), '
            '\"agent\": os.environ.get(\"PI_CODING_AGENT_DIR\")}))\n'
            'sys.stderr.write(\"SECRET=https://token@example.invalid\\\\n\")\n'
            'sys.exit(7)\n')
        fake.chmod(0o700)
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', lambda *_args, **_kwargs: {'omp_version':'fake','ai_url':'x','core_url':'x','sdk_url':'x','registry_url':'x','manager_url':'x','session_url':'x','telemetry_url':'x','resolver_url':'x'})
        monkeypatch.setattr(reviewer_runner.shutil, 'which', lambda _: str(fake))
        result = reviewer_runner.run_native_reviewer(
            home, reviewer.enqueue_intent(home, kind='manual'))
        assert capture.exists(), result.get('error')
        captured = json.loads(capture.read_text())
        runtime = home/'state/reviewer/runtime'/result['review_run_id']
        assert captured['cwd'] == captured['payload']['input']['cwd'] == str(runtime)
        assert captured['payload']['input']['agentDir'] == str(omp_runtime.agent_dir(home))
        assert captured['agent'] == str(omp_runtime.agent_dir(home))
        assert captured['home'] == str(home/'data/omp/home')
        assert json.loads((runtime/'.omp/plugins/installed_plugins.json').read_text()) == {
            'version': 2, 'plugins': {}}
        assert json.loads((runtime/'.omp/plugins/omp-plugins.lock.json').read_text()) == {
            'plugins': {}, 'settings': {}}
        raw = json.dumps(result)
        assert result['dispatch_state'] == 'final' and result['error']['code'] == 'nonzero'
        assert 'SECRET' not in raw and 'token@example' not in raw

def test_cli_reviewer_triage_preserves_foreign_owner_and_task_bytes():
    from test_reviewer import produced_run, build_report
    from agd_lib import worker_skills
    import os, json, uuid
    from support import command
    with lab() as (home, _):
        run, packet = produced_run(home)
        submitted = reviewer.submit_report(home, run['review_run_id'], str(uuid.uuid4()), build_report(packet))
        reviewer.finalize_run(home, run['review_run_id'], 'exited')
        owner = home / 'state' / 'owner.json'; owner.write_text(json.dumps({'pid': os.getpid(), 'started': 'foreign', 'token': str(uuid.uuid4())}))
        task_bytes = {str(p.relative_to(home/'data'/'tasks')): p.read_bytes() for p in (home/'data'/'tasks').rglob('*') if p.is_file()}
        before = (owner.read_bytes(), task_bytes, worker_skills.catalog(home), worker_skills.resolve(home))
        report = command(home, 'reviewer', 'reports', 'show', submitted['report_id'])
        proposal = command(home, 'reviewer', 'proposals', 'show', submitted['proposal_ids'][0])
        disposed = command(home, 'reviewer', 'proposals', 'dispose', submitted['proposal_ids'][0], '--status', 'accepted')
        assert report['review_run_id'] == run['review_run_id'] and report['report']['findings']
        assert proposal['history'] == [] and disposed['current'] == 'accepted'
        after_tasks = {str(p.relative_to(home/'data'/'tasks')): p.read_bytes() for p in (home/'data'/'tasks').rglob('*') if p.is_file()}
        assert before == (owner.read_bytes(), after_tasks, worker_skills.catalog(home), worker_skills.resolve(home))

def test_invalid_retrospective_is_rejected_before_reviewer_writes():
    from importlib.machinery import SourceFileLoader
    import pytest
    with lab() as (home, _):
        module = SourceFileLoader('agd_cli', str(Path(__file__).resolve().parents[1] / 'bin' / 'agd')).load_module()
        with pytest.raises(SystemExit):
            module.parser().parse_args(['--home', str(home), 'reviewer', 'run', '--retrospective-review-run', 'not-a-uuid'])
        assert not reviewer.paths(home)['intents'].exists() and not reviewer.paths(home)['receipts'].exists()


def test_preparation_failure_is_terminal_without_child(monkeypatch):
    from test_reviewer import ended_worker, message
    import uuid
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')])
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', lambda *_args, **_kwargs: {'omp_version':'fake','ai_url':'x','core_url':'x','sdk_url':'x','registry_url':'x','manager_url':'x','session_url':'x','telemetry_url':'x','resolver_url':'x'})
        monkeypatch.setattr(reviewer_runner.observability, 'reviewer_environment', lambda *_: (_ for _ in ()).throw(RuntimeError('secret preparation failure')))
        result = reviewer_runner.run_native_reviewer(
            home, reviewer.enqueue_intent(home, kind='manual'))
        assert result['dispatch_state']=='final' and result['outcome']=='failed' and result['error']['code']=='preparation'
        assert 'secret' not in json.dumps(result)


def test_cli_manual_no_work_preserves_timer_bytes():
    from support import command
    with lab() as (home, _):
        checkpoint=reviewer.paths(home)['checkpoint']; checkpoint.parent.mkdir(parents=True,exist_ok=True); checkpoint.write_bytes(b'{"anchor":0,"last_due":0}')
        units=home/'private-units'; units.mkdir(); (units/'owned.timer').write_bytes(b'owned')
        before=(checkpoint.read_bytes(),(units/'owned.timer').read_bytes())
        result=command(home,'reviewer','run')
        assert result['state']=='no-work' and before==(checkpoint.read_bytes(),(units/'owned.timer').read_bytes())




def test_runner_expired_after_collection_skips_capability_and_child(monkeypatch):
    from test_reviewer import ended_worker, message
    import uuid
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')])
        monkeypatch.setattr(reviewer_runner.reviewer, 'select_packet',
                            lambda *_args, **_kwargs: {'packet_id': 'a' * 64, 'excerpts': [{}]})
        monkeypatch.setattr(reviewer_runner.reviewer, 'deadline_remaining_seconds', lambda _: 0)
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability',
                            lambda *_: (_ for _ in ()).throw(AssertionError('capability called')))
        result = reviewer_runner.run_native_reviewer(
            home, reviewer.enqueue_intent(home, kind='manual'))
        assert result['dispatch_state'] == 'final' and result['outcome'] == 'failed' and result['error']['code'] == 'timeout'


def test_capability_timeout_is_bounded_by_deadline(monkeypatch, tmp_path):
    import subprocess, time
    root = tmp_path / 'omp'; (root / 'dist').mkdir(parents=True)
    (root / 'dist' / 'cli.js').write_text('')
    (root / 'package.json').write_text('{"name":"@oh-my-pi/pi-coding-agent","version":"test","bin":{"omp":"dist/cli.js"}}')
    seen = []
    monkeypatch.setattr(reviewer_runner.shutil, 'which', lambda name: str(root / 'dist' / 'cli.js') if name == 'omp' else 'bun')
    def run(*_args, **kwargs):
        seen.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired('bun', kwargs['timeout'])
    monkeypatch.setattr(reviewer_runner.subprocess, 'run', run)
    with __import__('pytest').raises(Exception, match='timed out'):
        reviewer_runner.assert_runtime_capability(int((time.time() + 1) * 1000))
    assert 0 < seen[0] <= 1



def test_capability_uses_omp_package_from_arbitrary_cwd_without_local_modules(monkeypatch, tmp_path):
    import shutil, time
    omp = shutil.which('omp')
    assert omp
    monkeypatch.chdir(tmp_path)
    capability = reviewer_runner.assert_runtime_capability(int((time.time() + 10) * 1000), omp_binary=omp)
    assert set(capability) == {'ai_url', 'core_url', 'sdk_url', 'registry_url', 'manager_url', 'session_url', 'telemetry_url', 'resolver_url', 'omp_version'}
    assert all(capability[key].startswith('file:') for key in capability if key.endswith('_url'))
    assert capability['omp_version']


def test_legacy_busy_reviewer_without_safe_identity_blocks(tmp_path):
    import subprocess
    from support import command
    home = tmp_path / 'home'
    command(home, 'init', '--backend', 'tmux')
    program = (
        'import sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "bin")!r})\n'
        'from agd_lib import reviewer\n'
        'with reviewer.run_lease(Path(sys.argv[1])):\n'
        ' print("held", flush=True)\n'
        ' sys.stdin.read()\n')
    holder = subprocess.Popen([sys.executable, '-c', program, str(home)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == 'held\n'
        before = set(home.rglob('*'))
        resources = reviewer_runner.uninstall_inventory(home)
        assert any(row['presence'] == 'unknown' and row['ownership'] == 'unknown'
                   and row['blockers'] for row in resources)
        assert set(home.rglob('*')) == before
        assert holder.poll() is None
    finally:
        holder.terminate()
        holder.wait(timeout=5)
        holder.stdin.close()
        holder.stdout.close()


def test_manual_reviewer_records_identity_before_spawn_and_preserves_timeout(monkeypatch):
    import subprocess
    import uuid
    import pytest
    from agd_lib import store, tasks, supervision
    from support import uninstall_scope
    from test_reviewer import ended_worker, message
    with lab() as (home, _):
        task, _, _ = ended_worker(home, 'evidence', str(uuid.uuid4()), [message('fixture evidence')])
        program = (
            'import json, sys\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "bin")!r})\n'
            'from agd_lib import reviewer, reviewer_runner\n'
            'select = reviewer.select_packet\n'
            'def paused(*args, **kwargs):\n'
            ' packet = select(*args, **kwargs)\n'
            ' assert packet["excerpts"]\n'
            ' print("collected", flush=True)\n'
            ' sys.stdin.read(1)\n'
            ' return packet\n'
            'reviewer.select_packet = paused\n'
            'home = Path(sys.argv[1])\n'
            'result = reviewer_runner.run_native_reviewer(home, reviewer.enqueue_intent(home, kind="manual"))\n'
            'print(json.dumps(result), flush=True)\n')
        coordinator = subprocess.Popen(
            [sys.executable, '-c', program, str(home)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert coordinator.stdout.readline() == 'collected\n'
            resources = reviewer_runner.uninstall_inventory(home)
            run = next(row for row in resources if row['kind'] == 'reviewer-run')
            assert run['identity']['coordinator']['pid'] == coordinator.pid
            assert run['identity']['coordinator']['start'] == supervision.process_start(coordinator.pid)
            assert run['identity']['child'] is None
            receipt = reviewer.paths(home)['receipts'] / f"{run['identity']['review_run_id']}.json"
            with uninstall_scope(home, resources) as operation:
                before = receipt.read_bytes()
                monkeypatch.setattr(reviewer_runner, 'UNINSTALL_TIMEOUT', 1e-12, raising=False)
                with pytest.raises(store.Refusal):
                    reviewer_runner.uninstall_quiesce(home, resources, operation)
                assert coordinator.poll() is None
                assert receipt.read_bytes() == before
                assert operation['completed'] == []
                tasks.verify_workspace(home, task)
                monkeypatch.setattr(reviewer_runner, 'UNINSTALL_TIMEOUT', 15.0)
                coordinator.stdin.write('continue\n')
                coordinator.stdin.flush()
                stdout, stderr = coordinator.communicate(timeout=20)
                assert coordinator.returncode == 0, stderr
                assert json.loads(stdout)['dispatch_state'] == 'final'
                assert json.loads(receipt.read_text()).get('child') is None
                reviewer_runner.uninstall_quiesce(home, resources, operation)
                assert run['key'] in operation['completed']
        finally:
            if coordinator.poll() is None:
                coordinator.terminate()
            coordinator.communicate(timeout=10)
