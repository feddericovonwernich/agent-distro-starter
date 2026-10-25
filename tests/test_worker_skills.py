import json
import sys
import tempfile
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


def add_skill(home, name, description='Use this skill.'):
    skill = home/'config'/'worker-skills'/name
    skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\nInstructions.\n')
    return skill


def write_profiles(home, value):
    path = home/'config'/'workers.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_explicit_profile_never_falls_back_to_empty():
    from agd_lib import worker_skills, store
    with tempfile.TemporaryDirectory() as directory:
        try:
            worker_skills.resolve(Path(directory), profile='missing')
        except store.Refusal:
            return
        raise AssertionError('explicit missing profile silently accepted')


def test_profile_defaults_and_curated_selection_are_exact_and_sorted():
    from agd_lib import worker_skills
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        add_skill(home, 'agd-zebra', 'Zebra instructions.')
        add_skill(home, 'agd-alpha', 'Alpha instructions.')
        write_profiles(home, {'default_profile': 'implementer', 'profiles': {
            'implementer': {'skills': ['agd-zebra', 'agd-alpha']}}})
        assert worker_skills.resolve(home) == {
            'schema': 1, 'mode': 'profile', 'profile': 'implementer',
            'skills': ['agd-alpha', 'agd-zebra']}
        assert worker_skills.resolve(home, skill_mode='curated',
                                    skills=('agd-zebra',)) == {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-zebra']}


def test_invalid_worker_skill_configuration_refuses_instead_of_degrading():
    from agd_lib import worker_skills, store
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        add_skill(home, 'agd-one')
        write_profiles(home, {'default_profile': 'missing', 'profiles': {
            'implementer': {'skills': ['agd-one', 'agd-one']}}})
        for kwargs in ({}, {'profile': 'implementer'}, {'skill_mode': 'curated'},
                       {'profile': 'implementer', 'skill_mode': 'curated',
                        'skills': ('agd-one',)}):
            try:
                worker_skills.resolve(home, **kwargs)
            except store.Refusal:
                continue
            raise AssertionError(f'invalid configuration accepted: {kwargs}')


def test_catalog_rejects_missing_or_malformed_bundles():
    from agd_lib import worker_skills, store
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        (home/'config'/'worker-skills'/'agd-missing').mkdir(parents=True)
        try:
            worker_skills.catalog(home)
        except store.Refusal:
            pass
        else:
            raise AssertionError('missing SKILL.md accepted')
        skill = add_skill(home, 'agd-good')
        (skill/'SKILL.md').write_text('---\nname: agd-other\ndescription: x\n---\n')
        try:
            worker_skills.catalog(home)
        except store.Refusal:
            return
        raise AssertionError('mismatched skill frontmatter accepted')


def test_worker_skill_and_profile_lists_are_deterministic_json():
    from test_projects import command
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        command(home, 'init')
        add_skill(home, 'agd-zebra', 'Zebra instructions.')
        add_skill(home, 'agd-alpha', 'Alpha instructions.')
        write_profiles(home, {'default_profile': 'implementer', 'profiles': {
            'implementer': {'skills': ['agd-zebra', 'agd-alpha']}}})
        assert command(home, 'worker', 'skills', 'list') == [
            {'id': 'agd-alpha', 'description': 'Alpha instructions.'},
            {'id': 'agd-zebra', 'description': 'Zebra instructions.'}]
        assert command(home, 'worker', 'profiles', 'list') == {
            'default_profile': 'implementer',
            'profiles': {'implementer': {'skills': ['agd-zebra', 'agd-alpha']}}}


if __name__ == '__main__':
    test_verified_omp_skill_root_is_project_local()
    test_unverified_cursor_has_no_skill_provisioning_root()
    test_explicit_profile_never_falls_back_to_empty()
    test_profile_defaults_and_curated_selection_are_exact_and_sorted()
    test_invalid_worker_skill_configuration_refuses_instead_of_degrading()
    test_catalog_rejects_missing_or_malformed_bundles()
    test_worker_skill_and_profile_lists_are_deterministic_json()
