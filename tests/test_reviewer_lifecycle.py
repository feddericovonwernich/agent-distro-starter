import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer, reviewer_runner
from support import lab


def test_native_runner_persists_no_work_without_capability_or_agent_dir(monkeypatch):
    with lab() as (home, _):
        checkpoint = reviewer.paths(home)['checkpoint']; checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes(b'{"anchor":0,"last_due":0}')
        before = checkpoint.read_bytes()
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', lambda: (_ for _ in ()).throw(AssertionError('capability called')))
        result = reviewer_runner.run_native_reviewer(home, reviewer.enqueue_intent(home, kind='manual'), agent_dir=home / 'missing')
        stored = reviewer._receipt(home, result['review_run_id'])
        assert result['state'] == stored['state'] == 'no-work'
        assert stored['dispatch_state'] == 'final' and checkpoint.read_bytes() == before


def test_reviewer_session_directory_does_not_bind_pending_id():
    with lab() as (home, _):
        run = '11111111-1111-4111-8111-111111111111'
        directory = Path(__import__('agd_lib.observability', fromlist=['reviewer_session_dir']).reviewer_session_dir(home, run))
        assert directory.is_dir() and not (directory.parent / 'binding.json').exists()

def test_nonzero_fake_child_records_safe_terminal_diagnostic(monkeypatch):
    with lab() as (home, _):
        from test_reviewer import ended_worker, message
        import uuid
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')])
        agent = home / 'agent'; agent.mkdir()
        fake = home / 'fake-bun'
        fake.write_text('#!/usr/bin/env python3\nimport sys\nsys.stderr.write(\"SECRET=https://token@example.invalid\\\\n\")\nsys.exit(7)\n')
        fake.chmod(0o700)
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', lambda: {'omp_version':'fake','ai_url':'x','core_url':'x','sdk_url':'x','telemetry_url':'x','resolver_url':'x'})
        monkeypatch.setattr(reviewer_runner.shutil, 'which', lambda _: str(fake))
        result = reviewer_runner.run_native_reviewer(home, reviewer.enqueue_intent(home, kind='manual'), agent_dir=agent)
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
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir()
        monkeypatch.setattr(reviewer_runner, 'assert_runtime_capability', lambda: {'omp_version':'fake','ai_url':'x','core_url':'x','sdk_url':'x','telemetry_url':'x','resolver_url':'x'})
        monkeypatch.setattr(reviewer_runner.observability, 'reviewer_environment', lambda *_: (_ for _ in ()).throw(RuntimeError('secret preparation failure')))
        result=reviewer_runner.run_native_reviewer(home, reviewer.enqueue_intent(home, kind='manual'), agent_dir=agent)
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
