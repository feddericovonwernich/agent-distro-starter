import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))


def test_verified_omp_skill_root_is_project_local():
    from agd_lib import harnesses
    assert harnesses.worker_skill_roots('omp') == ('.omp/skills',)


def test_unverified_cursor_has_no_skill_provisioning_root():
    from agd_lib import harnesses, store
    try:
        harnesses.worker_skill_roots('cursor')
    except store.DependencyError:
        return
    raise AssertionError('unverified Cursor skill provisioning was enabled')


if __name__ == '__main__':
    test_verified_omp_skill_root_is_project_local()
    test_unverified_cursor_has_no_skill_provisioning_root()
