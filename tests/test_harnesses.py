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


def test_omp_argv_disables_ambient_extensions_and_empty_skills():
    from unittest.mock import patch

    with lab() as (home, project), patch.object(
            harnesses.shutil, 'which', return_value='/usr/bin/omp'):
        argv = harnesses.launch_argv(
            {'id': 'none', 'harness': 'omp', 'worktree': str(project)},
            home / 'brief.md')

    assert '--no-extensions' in argv
    assert '--no-skills' in argv
    assert '--skills' not in argv
    assert argv[argv.index('-e') + 1].endswith('lib/omp-worker.ts')


def test_omp_argv_allows_only_manifest_skills():
    from unittest.mock import patch

    with lab() as (home, project), patch.object(
            harnesses.shutil, 'which', return_value='/usr/bin/omp'):
        argv = harnesses.launch_argv({
            'id': 'selected',
            'harness': 'omp',
            'worktree': str(project),
            'worker_skills': {
                'skills': [
                    {'id': 'systematic-debugging',
                     'destination': str(project / '.omp/skills/systematic-debugging')},
                    {'id': 'ponytail',
                     'destination': str(project / '.omp/skills/ponytail')},
                ],
            },
        }, home / 'brief.md')

    assert '--no-extensions' in argv
    assert argv[argv.index('--skills') + 1] == 'ponytail,systematic-debugging'
    assert '--no-skills' not in argv
