import sys
from pathlib import Path

from support import lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import harnesses


def test_omp_argv_uses_explicit_native_session_directory():
    with lab() as (home, project):
        argv = harnesses.launch_argv(
            {'id': 'native', 'harness': 'omp', 'worktree': str(project)}, home / 'brief.md',
            session_dir=home / 'native-session')

    assert argv[argv.index('--session-dir') + 1] == str(home / 'native-session')
    assert '--continue' not in argv
    assert '--resume' not in argv
