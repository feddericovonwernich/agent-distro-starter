import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer, reviewer_runner
from support import lab


def test_native_runner_persists_no_work_without_capability_probe(monkeypatch):
    with lab() as (home, _):
        checkpoint = reviewer.paths(home)['checkpoint']; checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes(b'{"anchor":0,"last_due":0}')
        before = checkpoint.read_bytes()
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', lambda *_: (_ for _ in ()).throw(AssertionError('capability called')))
        result = reviewer_runner.run_native_reviewer(
            home, reviewer.enqueue_intent(home, kind='manual'))
        stored = reviewer._receipt(home, result['review_run_id'])
        assert result['state'] == stored['state'] == 'no-work'
        assert stored['dispatch_state'] == 'final' and checkpoint.read_bytes() == before


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
        assert captured['argv'][0] == '--no-env-file'
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
