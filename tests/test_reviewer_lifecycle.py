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
