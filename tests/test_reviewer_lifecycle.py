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
    from test_reviewer import completed_session
    if work:
        completed_session(home, role='worker', task_id='native-reviewer', texts=('evidence',),
                          cutoff=__import__('time').time() + 3600)
    intent = reviewer.enqueue_intent(home, kind='manual')
    config = reviewer.read_config(home)
    started = __import__('time').time()
    run = reviewer.reserve_run(
        home, intent, config_snapshot=config,
        instruction_snapshot=reviewer.INSTRUCTION_FILE.read_bytes(),
        started_at=started,
        deadline_epoch_ms=int((started + (deadline or config['limits']['max_runtime_seconds'])) * 1000))
    manifest = reviewer.select_manifest(home, cutoff=started + 1, config_snapshot=config) if work else None
    return run, manifest


def _write_native_request(home, run, manifest):
    return reviewer_runner._prepare_native_request(home, run, manifest, reviewer.read_config(home))


def _run_child(home, monkeypatch, run, manifest, mode='success'):
    fixture = _fake_omp(home)
    runtime = _write_native_request(home, run, manifest)
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
        'if mode == "invalid": (runtime / "report-draft.json").write_text("{\\"schema\\":2}")\n'
        'else: (runtime / "report-draft.json").write_text(json.dumps({"schema":2,"summary":"fixture","episode_reviews":[],"findings":[]}))\n'
        'if mode == "nonzero":\n'
        ' sys.stderr.write("fixture nonzero diagnostic\\\\n"); sys.exit(7)\n'
        'if mode == "timeout":\n'
        ' sys.stderr.write("fixture timeout diagnostic\\\\n"); sys.stderr.flush(); time.sleep(30)\n'
        'if mode == "crash-after-exit": sys.exit(0)\n'
        'sys.exit(0)\n')
    path.chmod(0o700)
    return path




def _pending_episode_ids(home):
    selected = reviewer.select_manifest(
        home, cutoff=__import__('time').time() + 1,
        config_snapshot=reviewer.read_config(home))
    return tuple(sorted(row['episode_id'] for row in selected['episodes']))

def _real_exit_proof(home, run, manifest, *, keep_supervisor=False, keep_child=False):
    runtime = _write_native_request(home, run, manifest)
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



def test_valid_report_then_nonzero_does_not_commit(monkeypatch):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        before = _pending_episode_ids(home)
        code, _ = _run_child(home, monkeypatch, run, packet, 'nonzero')
        result = _collect(home, run)
        assert code == 7 and result['dispatch_state'] == 'final'
        assert result['outcome'] in ('failed', 'unknown')
        assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        assert _pending_episode_ids(home) == before


@pytest.mark.parametrize('mode', ('missing', 'invalid'))
def test_missing_or_invalid_report_is_retryable(monkeypatch, mode):
    with lab() as (home, _):
        run, packet = _reserved_reviewer_run(home)
        before = _pending_episode_ids(home)
        code, _ = _run_child(home, monkeypatch, run, packet, mode)
        result = _collect(home, run)
        assert code == 0 and result['dispatch_state'] == 'final'
        assert result['outcome'] in ('failed', 'unknown')
        assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        assert _pending_episode_ids(home) == before




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


def test_manifest_no_work_does_not_allocate_backend_model_or_auth(monkeypatch):
    with lab() as (home, _):
        def forbidden(*_args, **_kwargs):
            pytest.fail('no-work allocated backend, model, or authentication')

        monkeypatch.setattr(reviewer_runner, '_reviewer_backend', forbidden)
        monkeypatch.setattr(reviewer_runner.observability, 'reviewer_environment', forbidden)
        monkeypatch.setattr(reviewer_runner.omp_runtime, 'executable', forbidden)
        result = reviewer_runner.run_native_reviewer(
            home, reviewer.enqueue_intent(home, kind='manual'))
        assert result['state'] == 'no-work'
        assert not reviewer.paths(home)['runtime'].exists()


@pytest.mark.parametrize('snapshot', ['instruction', 'config'])
def test_manifest_request_refuses_different_reserved_snapshot(snapshot):
    import time
    from test_reviewer import ended_worker, message

    with lab() as (home, _):
        ended_worker(home, 'snapshot-race', str(__import__('uuid').uuid4()),
                     [message('snapshot race evidence')])
        config = reviewer.read_config(home)
        now = time.time()
        manifest = reviewer.select_manifest(home, cutoff=now + 1, config_snapshot=config)
        assert manifest['episodes']
        reserved_config = dict(config)
        instruction = reviewer.INSTRUCTION_FILE.read_bytes()
        if snapshot == 'instruction':
            instruction += b'\nchanged after selection\n'
        else:
            reserved_config = {**config, 'limits': {**config['limits'],
                               'max_candidates': config['limits']['max_candidates'] - 1}}
        run = reviewer.reserve_run(
            home, reviewer.enqueue_intent(home, kind='manual'),
            config_snapshot=reserved_config, instruction_snapshot=instruction,
            started_at=now, deadline_epoch_ms=int((now + 1800) * 1000))
        with pytest.raises(store.Refusal):
            reviewer_runner._prepare_native_request(home, run, manifest, reserved_config)
        receipt = reviewer._receipt(home, run['review_run_id'])
        assert receipt.get('manifest_id') is None
        assert not (reviewer.paths(home)['runtime'] / run['review_run_id'] / 'request.json').exists()


def test_manifest_request_exposes_only_metadata_and_sanitized_availability():
    import time
    from test_reviewer import ended_worker, message

    with lab() as (home, _):
        sentinel = 'PRIVATE_NATIVE_BODY_NOT_FOR_INITIAL_REQUEST'
        ended_worker(home, 'metadata-only', str(__import__('uuid').uuid4()), [message(sentinel)])
        config = reviewer.read_config(home)
        now = time.time()
        run = reviewer.reserve_run(
            home, reviewer.enqueue_intent(home, kind='manual'), config_snapshot=config,
            instruction_snapshot=reviewer.INSTRUCTION_FILE.read_bytes(),
            started_at=now, deadline_epoch_ms=int((now + 1800) * 1000))
        manifest = reviewer.select_manifest(home, cutoff=now + 1, config_snapshot=config)
        assert manifest['episodes']
        runtime = reviewer_runner._prepare_native_request(
            home, run, manifest, config,
            langfuse_query={'available': False, 'secret_key': 'private-secret',
                            'public_key': 'private-public', 'base_url': 'private-host'})
        request = json.loads((runtime / 'request.json').read_text())
        selection = json.loads((runtime / 'selection-manifest.json').read_text())
        receipt = reviewer._receipt(home, run['review_run_id'])
        assert request['schema'] == 2
        assert request['manifest_id'] == receipt['manifest_id'] == manifest['manifest_id']
        assert request['report_schema'] == receipt['report_schema'] == 2
        assert selection['manifest_id'] == manifest['manifest_id']
        assert manifest['instruction_sha256'] == run['instruction_snapshot_ref']
        assert manifest['config_snapshot_sha256'] == run['config_snapshot_ref']
        assert reviewer_runner._native_request(home, run['review_run_id']) == (runtime, request)
        assert len((runtime / 'selection-manifest.json').read_bytes()) <= 16384
        exposed = (runtime / 'request.json').read_text() + (runtime / 'selection-manifest.json').read_text()
        assert all(value not in exposed for value in (sentinel, 'private-secret', 'private-public', 'private-host'))
        assert not (runtime / 'packet.json').exists()
        assert not (runtime / 'catalog.json').exists()


def test_manifest_screened_reflection_remains_actionable_before_backend(monkeypatch):
    import time
    import uuid
    from test_reviewer import completed_session
    from test_reviewer_inspection import inspection, stage

    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        source = inspection().inspect(home, run_id, episode['episode_id'], source='native')
        report = {'schema': 2, 'summary': 'Screened only.', 'findings': [],
                  'episode_reviews': [{'episode_id': episode['episode_id'], 'assessment': 'screened',
                                       'intent_sources': [source['source_id']], 'action_sources': [],
                                       'outcome_sources': [], 'limitations': []}]}
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report)
        reviewer.finalize_run(home, run_id, 'exited')
        selected = reviewer.select_manifest(home, cutoff=time.time() + 1,
                                            config_snapshot=reviewer.read_config(home))
        assert episode['episode_id'] in {row['episode_id'] for row in selected['episodes']}

        class SelectionObserved(Exception):
            pass

        original = reviewer.select_manifest

        def observe(*args, **kwargs):
            result = original(*args, **kwargs)
            assert episode['episode_id'] in {row['episode_id'] for row in result['episodes']}
            raise SelectionObserved

        monkeypatch.setattr(reviewer, 'select_manifest', observe)
        with pytest.raises(SelectionObserved):
            reviewer_runner.run_native_reviewer(home, reviewer.enqueue_intent(home, kind='manual'))


def test_manifest_manual_detail_intent_reopens_held_episode(monkeypatch):
    import uuid
    from test_reviewer import completed_session
    from test_reviewer_inspection import inspection, stage

    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        source = inspection().inspect(home, run_id, episode['episode_id'], source='native')
        report = {'schema': 2, 'summary': 'Limited.', 'findings': [],
                  'episode_reviews': [{'episode_id': episode['episode_id'], 'assessment': 'limited',
                                       'intent_sources': [source['source_id']], 'action_sources': [],
                                       'outcome_sources': [], 'limitations': ['Outcome unavailable.']}]}
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report)
        reviewer.finalize_run(home, run_id, 'exited')
        intent = reviewer.enqueue_intent(home, kind='manual', detail_episode_ids=(episode['episode_id'],))
        assert intent['detail_episode_ids'] == [episode['episode_id']]

        class SelectionObserved(Exception):
            pass

        original = reviewer.select_manifest

        def observe(*args, **kwargs):
            assert kwargs['detail_episode_ids'] == (episode['episode_id'],)
            result = original(*args, **kwargs)
            assert episode['episode_id'] in {row['episode_id'] for row in result['episodes']}
            raise SelectionObserved

        monkeypatch.setattr(reviewer, 'select_manifest', observe)
        with pytest.raises(SelectionObserved):
            reviewer_runner.run_native_reviewer(home, intent)


def test_manifest_cli_detail_repeated_manual_only(monkeypatch):
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader

    with lab() as (home, _):
        loader = SourceFileLoader('agd_cli_detail', str(Path(__file__).resolve().parents[1] / 'bin' / 'agd'))
        cli = module_from_spec(spec_from_loader('agd_cli_detail', loader))
        loader.exec_module(cli)
        first, second = 'a' * 64, 'b' * 64
        monkeypatch.setattr(reviewer_runner, 'run_native_reviewer', lambda _home, intent: intent)
        args = cli.parser().parse_args(['--home', str(home), 'reviewer', 'run',
                                        '--detail-episode', first, '--detail-episode', second])
        intent = cli.main(args)
        assert intent['kind'] == 'manual'
        assert intent['detail_episode_ids'] == [first, second]
        assert intent['retrospective_review_run_ids'] == []
        args = cli.parser().parse_args(['--home', str(home), 'reviewer', 'run',
                                        '--timer', '--detail-episode', first])
        with pytest.raises(store.Refusal):
            cli.main(args)


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
        before = _pending_episode_ids(home)
        code, runtime = _run_child(home, monkeypatch, run, packet, 'timeout')
        result = _collect(home, run)
        assert code != 0 and result['dispatch_state'] == 'final'
        assert result['outcome'] == 'unknown'
        assert 'fixture timeout diagnostic' in (runtime / 'stderr').read_text()
        assert _pending_episode_ids(home) == before


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
            json.dumps({'schema': 2, 'summary': 'unproved', 'episode_reviews': [], 'findings': []}))
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


def test_cli_reviewer_triage_preserves_foreign_owner_and_task_bytes():
    from reviewer_history_fixture import historical_final
    from agd_lib import worker_skills
    import os, json, uuid
    from support import command
    with lab() as (home, _):
        run, submitted = historical_final(home)
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
    from importlib.util import module_from_spec, spec_from_loader
    with lab() as (home, _):
        loader = SourceFileLoader('agd_cli', str(Path(__file__).resolve().parents[1] / 'bin' / 'agd'))
        module = module_from_spec(spec_from_loader('agd_cli', loader))
        loader.exec_module(module)
        with pytest.raises(SystemExit):
            module.parser().parse_args(['--home', str(home), 'reviewer', 'run', '--retrospective-review-run', 'not-a-uuid'])
        assert not reviewer.paths(home)['intents'].exists() and not reviewer.paths(home)['receipts'].exists()




def test_cli_manual_no_work_preserves_timer_bytes():
    from support import command
    with lab() as (home, _):
        checkpoint=reviewer.paths(home)['checkpoint']; checkpoint.parent.mkdir(parents=True,exist_ok=True); checkpoint.write_bytes(b'{"anchor":0,"last_due":0}')
        units=home/'private-units'; units.mkdir(); (units/'owned.timer').write_bytes(b'owned')
        before=(checkpoint.read_bytes(),(units/'owned.timer').read_bytes())
        result=command(home,'reviewer','run')
        assert result['state']=='no-work' and before==(checkpoint.read_bytes(),(units/'owned.timer').read_bytes())






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
            'select = reviewer.select_manifest\n'
            'def paused(*args, **kwargs):\n'
            ' manifest = select(*args, **kwargs)\n'
            ' assert manifest["episodes"]\n'
            ' print("collected", flush=True)\n'
            ' sys.stdin.read(1)\n'
            ' return manifest\n'
            'reviewer.select_manifest = paused\n'
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
                reviewer_runner.uninstall_quiesce(home, resources, operation)
                assert coordinator.poll() is not None
                final = json.loads(receipt.read_text())
                assert final['review_run_id'] == run['identity']['review_run_id']
                assert final['dispatch_state'] == 'final'
                assert final['outcome'] == 'unknown'
                assert receipt.read_bytes() != before
                assert run['key'] in operation['completed']
                tasks.verify_workspace(home, task)
        finally:
            if coordinator.poll() is None:
                coordinator.terminate()
            coordinator.communicate(timeout=10)
