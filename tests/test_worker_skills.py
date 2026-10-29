import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

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


PONYTAIL_SKILL = b'''---
name: ponytail
description: >-
  Lazy senior developer.
  Prefer the simplest solution.
argument-hint: "[lite|full|ultra]"
license: MIT
---
Instructions stay byte-for-byte upstream.
'''

SYSTEMATIC_SKILL = b'''---
name: systematic-debugging
description: Find the root cause before changing code.
---
Debug carefully.
'''


def native_skills(home, *, ponytail_name='ponytail',
                  superpowers_name='systematic-debugging'):
    from support import write_native_plugins
    write_native_plugins(home, {
        'ponytail@ponytail': {
            ponytail_name: {
                'SKILL.md': PONYTAIL_SKILL.replace(
                    b'name: ponytail', f'name: {ponytail_name}'.encode()),
                'references/mode.md': b'full\n',
            },
        },
        'superpowers@superpowers-dev': {
            superpowers_name: {
                'SKILL.md': SYSTEMATIC_SKILL.replace(
                    b'name: systematic-debugging',
                    f'name: {superpowers_name}'.encode()),
                'root-cause-tracing.md': b'trace backwards\n',
            },
        },
    })


def test_catalog_accepts_original_upstream_metadata():
    from agd_lib import worker_skills
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        native_skills(home)

        rows = {row['id']: row for row in worker_skills.catalog(home)}
        ponytail = rows['ponytail']

        assert ponytail['description'] == (
            'Lazy senior developer. Prefer the simplest solution.')
        assert Path(ponytail['source'], 'SKILL.md').read_bytes() == PONYTAIL_SKILL
        assert Path(ponytail['source'], 'references/mode.md').is_relative_to(
            Path(ponytail['source']))
        assert ponytail['origin'] == {
            'kind': 'plugin', 'plugin_id': 'ponytail@ponytail',
            'version': '5.3.3', 'relative_path': 'skills/ponytail',
        }


def test_catalog_refuses_native_name_collisions_and_unsafe_sources():
    from agd_lib import store, worker_skills

    def duplicate_plugins(home):
        native_skills(home, ponytail_name='shared', superpowers_name='shared')

    def plugin_custom_collision(home):
        native_skills(home)
        add_skill(home, 'ponytail')

    def directory_name_mismatch(home):
        native_skills(home, ponytail_name='different')
        skill = next((home / 'data/omp/home/.omp/plugins/cache/plugins').glob(
            'ponytail*')) / 'skills/different'
        skill.rename(skill.parent / 'ponytail')

    def traversal_name(home):
        native_skills(home)
        skill = next((home / 'data/omp/home/.omp/plugins/cache/plugins').glob(
            'ponytail*')) / 'skills/ponytail/SKILL.md'
        skill.write_text('---\nname: ../escape\ndescription: unsafe\n---\n')

    def symlink_source(home):
        native_skills(home)
        root = next((home / 'data/omp/home/.omp/plugins/cache/plugins').glob(
            'ponytail*')) / 'skills'
        (root / 'ponytail').rename(root / 'real')
        (root / 'ponytail').symlink_to(root / 'real', target_is_directory=True)

    def special_file(home):
        native_skills(home)
        root = next((home / 'data/omp/home/.omp/plugins/cache/plugins').glob(
            'ponytail*')) / 'skills/ponytail'
        os.mkfifo(root / 'unsafe')

    for arrange in (duplicate_plugins, plugin_custom_collision,
                    directory_name_mismatch, traversal_name, symlink_source,
                    special_file):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            arrange(home)
            with pytest.raises(store.Refusal):
                worker_skills.catalog(home)


def test_resolve_records_schema_two_sources_and_supports_none():
    from agd_lib import worker_skills
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        native_skills(home)

        selected = worker_skills.resolve(
            home, skill_mode='curated', skills=('ponytail',))

        assert selected['schema'] == 2
        assert selected['skills'] == ['ponytail']
        assert set(selected['sources']) == {'ponytail'}
        assert selected['sources']['ponytail']['origin']['plugin_id'] == (
            'ponytail@ponytail')
        assert any(row['path'] == 'SKILL.md'
                   for row in selected['sources']['ponytail']['files'])
        assert worker_skills.resolve(home, skill_mode='none') == {
            'schema': 2, 'mode': 'none', 'profile': None,
            'skills': [], 'sources': {},
        }


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
        resolved = worker_skills.resolve(home)
        assert resolved['schema'] == 2
        assert resolved['mode'] == 'profile'
        assert resolved['profile'] == 'implementer'
        assert resolved['skills'] == ['agd-alpha', 'agd-zebra']
        assert set(resolved['sources']) == {'agd-alpha', 'agd-zebra'}
        curated = worker_skills.resolve(
            home, skill_mode='curated', skills=('agd-zebra',))
        assert curated['skills'] == ['agd-zebra']
        assert set(curated['sources']) == {'agd-zebra'}


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
        add_skill(home, 'agd-zebra', 'Zebra instructions.')
        add_skill(home, 'agd-alpha', 'Alpha instructions.')
        write_profiles(home, {'default_profile': 'implementer', 'profiles': {
            'implementer': {'skills': ['agd-zebra', 'agd-alpha']}}})
        rows = command(home, 'worker', 'skills', 'list')
        assert [(row['id'], row['description'], row['origin']) for row in rows] == [
            ('agd-alpha', 'Alpha instructions.', {'kind': 'custom'}),
            ('agd-zebra', 'Zebra instructions.', {'kind': 'custom'})]
        assert command(home, 'worker', 'profiles', 'list') == {
            'default_profile': 'implementer',
            'profiles': {'implementer': {'skills': ['agd-zebra', 'agd-alpha']}}}


def test_catalog_and_config_symlinks_cannot_escape_private_home():
    from agd_lib import worker_skills, store
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
        home = Path(directory)
        target = Path(outside)
        os.symlink(target, home/'config')
        for action in (lambda: worker_skills.catalog(home),
                       lambda: worker_skills.resolve(home)):
            try:
                action()
            except store.Refusal:
                continue
            raise AssertionError('config parent symlink escaped private home')
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        (home/'config').mkdir()
        for name, action in (
                ('workers.json', lambda: worker_skills.resolve(home)),
                ('worker-skills', lambda: worker_skills.catalog(home))):
            os.symlink(home/f'missing-{name}', home/'config'/name)
            try:
                action()
            except store.Refusal:
                (home/'config'/name).unlink()
                continue
            raise AssertionError(f'dangling {name} symlink was treated as absent')


def test_config_parent_must_be_a_real_directory():
    from agd_lib import worker_skills, store
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        (home/'config').write_text('not a directory')
        for action in (lambda: worker_skills.catalog(home),
                       lambda: worker_skills.resolve(home)):
            try:
                action()
            except store.Refusal as error:
                assert 'path component must be a real directory' in str(error)
                continue
            raise AssertionError('regular config parent was treated as missing')


def test_task_create_selection_refuses_before_allocating_a_lease():
    from support import lab, command
    from agd_lib import tasks
    with lab() as (home, project):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        result = command(
            home, 'task', 'create', 'selection', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--profile', 'missing',
            expected=3)
        assert 'unknown profile' in result.stderr
        assert not tasks.task_file(home, 'selection').exists()


def test_prepare_freezes_and_materializes_a_skill_bundle():
    from agd_lib import worker_skills
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)/'home'
        worktree = Path(directory)/'worktree'
        worktree.mkdir()
        run(['git', 'init', '-b', 'main', worktree])
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=worktree)
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=worktree)
        (worktree/'value.txt').write_text('base\n')
        run(['git', 'add', 'value.txt'], cwd=worktree)
        run(['git', 'commit', '-m', 'initial'], cwd=worktree)
        skill = add_skill(home, 'agd-probe')
        resource = skill/'references'/'token.txt'
        resource.parent.mkdir()
        resource.write_text('frozen token\n')
        resource.chmod(0o755)
        task = {'id': 'freeze', 'worktree': str(worktree), 'harness': 'omp',
                'lease_id': 'lease'}
        selection = worker_skills.resolve(
            home, skill_mode='curated', skills=('agd-probe',))
        manifest = worker_skills.prepare(home, task, selection)
        assert manifest['schema'] == 2
        assert manifest['skills'][0]['origin'] == {'kind': 'custom'}
        generated = worktree/'.omp'/'skills'/'agd-probe'/'references'/'token.txt'
        common = (worktree/run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree).stdout.strip()).resolve()
        exclude = common/'info'/'exclude'
        assert '/.omp/skills/agd-probe/\n' in exclude.read_text()
        exclude.write_text(exclude.read_text() + 'user-note\n')
        assert generated.read_text() == 'frozen token\n'
        assert generated.stat().st_mode & 0o111
        resource.write_text('changed source\n')
        worker_skills.ensure(home, task)
        assert generated.read_text() == 'frozen token\n'
        worker_skills.verify(home, task)
        worker_skills.remove(home, task)
        assert not generated.exists()
        assert 'user-note\n' in exclude.read_text()
        assert '/.omp/skills/agd-probe/\n' not in exclude.read_text()
        assert manifest['selection']['skills'] == ['agd-probe']


def test_shared_common_git_exclusion_waits_for_last_owner():
    from agd_lib import worker_skills
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first, second = root/'first', root/'second'
        first.mkdir()
        run(['git', 'init', '-b', 'main', first])
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=first)
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=first)
        (first/'value.txt').write_text('base\n')
        run(['git', 'add', 'value.txt'], cwd=first)
        run(['git', 'commit', '-m', 'initial'], cwd=first)
        run(['git', 'worktree', 'add', second], cwd=first)
        tasks = []
        for number, worktree in enumerate((first, second), 1):
            home = root/f'home-{number}'
            add_skill(home, 'agd-probe')
            task = {'id': f'task-{number}', 'worktree': str(worktree), 'harness': 'omp',
                    'lease_id': f'lease-{number}'}
            worker_skills.prepare(home, task, {
                'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
            tasks.append((home, task))
        common = (first/run(['git', 'rev-parse', '--git-common-dir'], cwd=first).stdout.strip()).resolve()
        exclude = common/'info'/'exclude'
        exclude.write_text(exclude.read_text() + 'keep-user-entry\n')
        worker_skills.remove(*tasks[0])
        assert '/.omp/skills/agd-probe/\n' in exclude.read_text()
        worker_skills.remove(*tasks[1])
        assert '/.omp/skills/agd-probe/\n' not in exclude.read_text()
        assert 'keep-user-entry\n' in exclude.read_text()


def test_ensure_recovers_missing_artifacts_and_verify_refuses_foreign_or_tracked_content():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir()
        run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        task = {'id': 'recovery', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        generated = worktree/'.omp'/'skills'/'agd-probe'
        import shutil
        shutil.rmtree(generated)
        worker_skills.ensure(home, task)
        (generated/'foreign.txt').write_text('foreign\n')
        try:
            worker_skills.verify(home, task)
        except store.Refusal:
            pass
        else:
            raise AssertionError('foreign artifact passed verification')
        (generated/'foreign.txt').unlink()
        run(['git', 'add', '-f', generated/'SKILL.md'], cwd=worktree)
        try:
            worker_skills.verify(home, task)
        except store.Refusal:
            return
        raise AssertionError('staged worker skill passed verification')



def test_verify_refuses_modified_frozen_snapshot():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir()
        run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        task = {'id': 'snapshot', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        (home/'data'/'tasks'/'snapshot'/'worker-skills'/'agd-probe'/'SKILL.md').write_text('altered\n')
        try:
            worker_skills.verify(home, task)
        except store.Refusal:
            return
        raise AssertionError('modified snapshot passed verification')


def test_native_alias_and_symlinked_skill_parents_refuse_provisioning():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        root, home, worktree = Path(directory), Path(directory)/'home', Path(directory)/'project'/'task'
        worktree.mkdir(parents=True)
        run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        alias = root/'project'/'.omp'/'skills'/'other'
        alias.mkdir(parents=True)
        (alias/'SKILL.md').write_text('---\nname: agd-probe\ndescription: collision\n---\n')
        task = {'id': 'alias', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        try:
            worker_skills.prepare(home, task, {
                'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        except store.Refusal:
            pass
        else:
            raise AssertionError('frontmatter alias collision was accepted')
        shutil = __import__('shutil')
        shutil.rmtree(root/'project'/'.omp')
        os.symlink(root/'elsewhere', root/'project'/'.omp')
        try:
            worker_skills.prepare(home, task, {
                'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        except store.Refusal:
            return
        raise AssertionError('symlinked native skill parent was accepted')


def test_pending_snapshot_retry_refuses_changed_catalog_source():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir()
        run(['git', 'init', '-b', 'main', worktree])
        skill = add_skill(home, 'agd-probe')
        task = {'id': 'pending', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        original = worker_skills._copy_tree
        def interrupted(source, destination):
            original(source, destination)
            if destination.name == 'agd-probe':
                raise RuntimeError('injected snapshot interruption')
        worker_skills._copy_tree = interrupted
        try:
            try:
                worker_skills.prepare(home, task, {
                    'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
            except RuntimeError:
                pass
            else:
                raise AssertionError('snapshot failure injection did not run')
        finally:
            worker_skills._copy_tree = original
        (skill/'SKILL.md').write_text('changed\n')
        with pytest.raises(store.Refusal):
            worker_skills.prepare(home, task, {
                'schema': 1, 'mode': 'curated', 'profile': None,
                'skills': ['agd-probe']})


def test_retry_repairs_exclude_after_registry_write_failure():
    from unittest.mock import patch
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir()
        run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        common = (worktree/run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree).stdout.strip()).resolve()
        exclude = common/'info'/'exclude'; exclude.write_text('user-entry\n')
        task = {'id': 'exclude-retry', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        original = store.atomic_json
        def interrupted(path, value):
            original(path, value)
            if Path(path).name == 'agd-worker-skills.json':
                raise RuntimeError('injected registry interruption')
        with patch.object(store, 'atomic_json', interrupted):
            try:
                worker_skills.prepare(home, task, {
                    'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
            except RuntimeError:
                pass
            else:
                raise AssertionError('registry fault was not injected')
        worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        assert 'user-entry\n' in exclude.read_text()
        assert '/.omp/skills/agd-probe/\n' in exclude.read_text()


def test_remove_retries_after_first_owned_directory_deletion():
    from unittest.mock import patch
    from agd_lib import worker_skills
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir()
        run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-one'); add_skill(home, 'agd-two')
        task = {'id': 'remove-retry', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        selection = {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-one', 'agd-two']}
        worker_skills.prepare(home, task, selection)
        common = (worktree/run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree).stdout.strip()).resolve()
        exclude = common/'info'/'exclude'; exclude.write_text(exclude.read_text() + 'user-entry\n')
        import shutil
        original = shutil.rmtree
        calls = [0]
        def interrupted(path):
            original(path)
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError('injected remove interruption')
        with patch('shutil.rmtree', interrupted):
            try:
                worker_skills.remove(home, task)
            except RuntimeError:
                pass
        worker_skills.remove(home, task)
        assert not (worktree/'.omp'/'skills'/'agd-one').exists()
        assert not (worktree/'.omp'/'skills'/'agd-two').exists()
        assert 'user-entry\n' in exclude.read_text()


def test_tampered_journal_cannot_touch_sibling_paths():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir(); run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        task = {'id': 'tampered', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        sentinel = Path(directory)/'sentinel'; sentinel.mkdir(); (sentinel/'keep').write_text('keep')
        journal = home/'data'/'tasks'/'tampered'/'worker-skills.json'
        data = json.loads(journal.read_text())
        data['skills'][0]['destination'] = str(sentinel)
        journal.write_text(json.dumps(data))
        for action in (lambda: worker_skills.ensure(home, task), lambda: worker_skills.remove(home, task)):
            try:
                action()
            except store.Refusal:
                continue
            raise AssertionError('tampered journal was accepted')
        assert (sentinel/'keep').read_text() == 'keep'



def test_preexisting_identical_exclude_line_is_not_removed():
    from agd_lib import worker_skills
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir(); run(['git', 'init', '-b', 'main', worktree])
        common = (worktree/run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree).stdout.strip()).resolve()
        exclude = common/'info'/'exclude'; original = '/.omp/skills/agd-probe/\nuser\n'; exclude.write_text(original)
        add_skill(home, 'agd-probe')
        task = {'id': 'user-exclude', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        selection = {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']}
        worker_skills.prepare(home, task, selection)
        worker_skills.remove(home, task)
        assert exclude.read_text() == original


def test_ensure_refuses_post_prepare_native_parent_symlink():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree, outside = Path(directory)/'home', Path(directory)/'worktree', Path(directory)/'outside'
        worktree.mkdir(); outside.mkdir(); run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        task = {'id': 'replace', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        worker_skills.prepare(home, task, {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        __import__('shutil').rmtree(worktree/'.omp')
        os.symlink(outside, worktree/'.omp')
        try:
            worker_skills.ensure(home, task)
        except store.Refusal:
            return
        raise AssertionError('post-prepare symlink replacement was accepted')


def test_ensure_refuses_removed_skill_set():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir(); run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        task = {'id': 'removed', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        worker_skills.prepare(home, task, {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        worker_skills.remove(home, task)
        try:
            worker_skills.ensure(home, task)
        except store.Refusal:
            return
        raise AssertionError('removed skill set was resurrected')


def test_retry_after_post_exclude_write_failure_releases_only_owned_line():
    from unittest.mock import patch
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        home, worktree = Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir(); run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        common = (worktree/run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree).stdout.strip()).resolve()
        exclude = common/'info'/'exclude'; exclude.write_text('/.omp/skills/agd-user/\nuser\n')
        task = {'id': 'exclude-final', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        original, count = store.atomic_json, [0]
        def interrupted(path, value):
            original(path, value)
            if Path(path).name == 'agd-worker-skills.json':
                count[0] += 1
                if count[0] == 2:
                    raise RuntimeError('injected post-exclude failure')
        with patch.object(store, 'atomic_json', interrupted):
            try:
                worker_skills.prepare(home, task, {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
            except RuntimeError:
                pass
            else:
                raise AssertionError('post-exclude fault was not injected')
        worker_skills.prepare(home, task, {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        worker_skills.remove(home, task)
        assert '/.omp/skills/agd-probe/\n' not in exclude.read_text()
        assert exclude.read_text() == '/.omp/skills/agd-user/\nuser\n'


def test_prompt_names_assigned_skill_paths():
    from agd_lib import harnesses
    task = {'id': 'prompt', 'worktree': '/tmp', 'harness': 'omp',
            'worker_skills': {'skills': [{'id': 'agd-probe', 'destination': '/tmp/.omp/skills/agd-probe'}]}}
    argv = harnesses.launch_argv(task, Path('/tmp/a/b/c/brief.md'))
    assert 'Assigned worker skills: agd-probe.' in argv[-1]
    assert '/tmp/.omp/skills/agd-probe/SKILL.md' in argv[-1]


def test_create_materializes_curated_skills_before_prepared():
    from support import lab, command
    from agd_lib import tasks
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(
            home, 'task', 'create', 'curated', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout',
            '--skill-mode', 'curated', '--skill', 'agd-probe')
        stored = tasks.read(home, 'curated')
        generated = Path(task['worktree'])/'.omp/skills/agd-probe/SKILL.md'
        snapshot = home/'data/tasks/curated/worker-skills/agd-probe/SKILL.md'
        assert stored['phase'] == 'prepared'
        assert stored['harness'] == 'omp'
        assert stored['worker_skill_selection']['schema'] == 2
        assert generated.read_bytes() == snapshot.read_bytes()
        assert not __import__('subprocess').run(
            ['git', 'status', '--porcelain'], cwd=task['worktree'],
            capture_output=True, text=True, check=True).stdout


def test_create_materializes_native_names_without_aliases():
    from support import lab, command
    with lab() as (home, project):
        default_native_skills(home)
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'native', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        root = Path(task['worktree'])/'.omp/skills'
        assert sorted(path.name for path in root.iterdir()) == DEFAULT_WORKER_SKILLS
        assert not any(path.name.startswith('agd-') for path in root.iterdir())


def test_create_default_profile_and_cursor_nonempty_refusal():
    from support import lab, command
    from agd_lib import tasks
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        write_profiles(home, {'default_profile': 'implementer',
                              'profiles': {'implementer': {'skills': ['agd-probe']}}})
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'profile', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        assert tasks.read(home, 'profile')['phase'] == 'prepared'
        assert (Path(task['worktree'])/'.omp/skills/agd-probe/SKILL.md').is_file()
        command(
            home, 'task', 'create', 'cursor', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--harness', 'cursor',
            '--skill-mode', 'curated', '--skill', 'agd-probe', expected=4)
        assert not tasks.task_file(home, 'cursor').exists()


def test_unchanged_generated_skill_allows_ready_but_modification_refuses():
    from support import lab, command
    from agd_lib import tasks, worker_skills
    with lab() as (home, project):
        os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(
            home, 'task', 'create', 'guarded', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        report = home/'report.md'; report.write_text('value.txt contains base.\n')
        assert command(home, 'task', 'ready', 'guarded', '--report', str(report))['phase'] == 'ready'
        generated = Path(task['worktree'])/'.omp'/'skills'/'agd-probe'/'SKILL.md'
        generated.write_text('changed\n')
        command(home, 'task', 'ready', 'guarded', '--report', str(report), expected=3)


def test_staged_and_historic_generated_skills_refuse_ready():
    from support import lab, command, run
    from agd_lib import tasks, worker_skills
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(
            home, 'task', 'create', 'staged', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        work = Path(task['worktree'])
        run(['git', 'add', '-f', '.omp/skills/agd-probe/SKILL.md'], cwd=work)
        report = home/'report.md'; report.write_text('base inspected\n')
        command(home, 'task', 'ready', 'staged', '--report', str(report), expected=3)
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(
            home, 'task', 'create', 'history', '--project', 'sample',
            '--brief', str(brief), '--skill-mode', 'curated', '--skill',
            'agd-probe')
        work = Path(task['worktree'])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=work); run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=work)
        run(['git', 'add', '-f', '.omp/skills/agd-probe/SKILL.md'], cwd=work); run(['git', 'commit', '-m', 'leak skill'], cwd=work)
        run(['git', 'rm', '-f', '.omp/skills/agd-probe/SKILL.md'], cwd=work); (work/'value.txt').write_text('changed\n')
        run(['git', 'add', 'value.txt'], cwd=work); run(['git', 'commit', '-m', 'delete skill and change'], cwd=work)
        report = home/'report.md'; report.write_text('changed value\n')
        command(home, 'task', 'ready', 'history', '--report', str(report), expected=3)


def test_unsafe_git_exclusion_metadata_refuses_before_prepare_mutates():
    from agd_lib import worker_skills, store
    from test_projects import run
    with tempfile.TemporaryDirectory() as directory:
        root, home, worktree = Path(directory), Path(directory)/'home', Path(directory)/'worktree'
        worktree.mkdir(); run(['git', 'init', '-b', 'main', worktree])
        add_skill(home, 'agd-probe')
        common = Path(run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree).stdout.strip())
        if not common.is_absolute():
            common = worktree/common
        common = common.resolve()
        info = common/'info'
        task = {'id': 'unsafe-exclude', 'worktree': str(worktree), 'harness': 'omp', 'lease_id': 'lease'}
        selection = {'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']}
        for name in ('exclude', 'agd-worker-skills.json'):
            sentinel = root/f'{name}.sentinel'; sentinel.write_text('outside\n')
            path = info/name
            path.unlink(missing_ok=True)
            os.symlink(sentinel, path)
            try:
                worker_skills.prepare(home, task, selection)
            except store.Refusal:
                pass
            else:
                raise AssertionError(f'symlinked {name} was accepted')
            assert sentinel.read_text() == 'outside\n'
            assert not (home/'data'/'tasks'/task['id']/'worker-skills.json').exists()
            assert not (worktree/'.omp').exists()
            path.unlink()


def test_cleanup_retry_after_pre_remove_failure_releases_owned_artifacts():
    from unittest.mock import patch
    from support import lab, command
    from agd_lib import tasks, worker_skills
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(
            home, 'task', 'create', 'remove-window', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        report = home/'report.md'; report.write_text('base inspected\n')
        command(home, 'task', 'ready', 'remove-window', '--report', str(report))
        def interrupted(root, saved):
            raise RuntimeError('injected pre-remove failure')
        with patch.object(worker_skills, 'remove', interrupted):
            try:
                tasks.cleanup(home, 'remove-window')
            except RuntimeError:
                pass
            else:
                raise AssertionError('pre-remove fault was not injected')
        assert tasks.read(home, 'remove-window')['cleanup_phase'] == 'returning'
        generated = Path(task['worktree'])/'.omp'/'skills'/'agd-probe'
        assert generated.is_dir()
        common = Path(tasks.store.git(task['worktree'], 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        assert '/.omp/skills/agd-probe/\n' in (common/'info'/'exclude').read_text()
        assert tasks.cleanup(home, 'remove-window')['phase'] == 'cleaned'
        assert not generated.exists()
        assert '/.omp/skills/agd-probe/\n' not in (common/'info'/'exclude').read_text()


def test_returned_lease_requires_completed_skill_removal_journal():
    from unittest.mock import patch
    from types import SimpleNamespace
    from support import lab, command
    from agd_lib import tasks, worker_skills, store
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(
            home, 'task', 'create', 'returned-window', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        task.update(
            phase='ready', cleanup_phase='returning', cleanup_discard=False,
            cleanup_approval=None)
        tasks.save(home, task)
        original = tasks.store.run
        def absent(argv, **kwargs):
            if 'status' in argv:
                return SimpleNamespace(stdout='[]')
            return original(argv, **kwargs)
        with patch.object(tasks.store, 'run', absent):
            try:
                tasks.cleanup(home, 'returned-window')
            except store.Refusal:
                pass
            else:
                raise AssertionError('returned lease accepted without removed skill journal')
        assert (Path(task['worktree'])/'.omp'/'skills'/'agd-probe').is_dir()
        assert tasks.read(home, 'returned-window')['phase'] == 'ready'


def test_cleanup_retry_after_artifact_removal_before_return():
    from unittest.mock import patch
    from support import lab, command
    from agd_lib import tasks, worker_skills
    from test_projects import run
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(
            home, 'task', 'create', 'cleanup-skill', '--project', 'sample',
            '--brief', str(brief), '--kind', 'scout', '--skill-mode',
            'curated', '--skill', 'agd-probe')
        report = home/'report.md'; report.write_text('base inspected\n')
        command(home, 'task', 'ready', 'cleanup-skill', '--report', str(report))
        original = tasks.store.run
        def interrupted(argv, **kwargs):
            if 'return' in argv:
                raise tasks.store.Refusal('injected return failure')
            return original(argv, **kwargs)
        with patch.object(tasks.store, 'run', interrupted):
            try:
                tasks.cleanup(home, 'cleanup-skill')
            except tasks.store.Refusal:
                pass
        assert tasks.read(home, 'cleanup-skill')['cleanup_phase'] == 'returning'
        assert not (Path(task['worktree'])/'.omp'/'skills'/'agd-probe').exists()
        assert tasks.cleanup(home, 'cleanup-skill')['phase'] == 'cleaned'

def test_reconcile_finishes_same_lease_and_refuses_changed_source(monkeypatch):
    from support import lab
    from agd_lib import store, tasks, worker_skills
    with lab() as (home, project):
        source = add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        original_prepare = worker_skills.prepare

        def interrupted(*_args, **_kwargs):
            raise RuntimeError('interrupt before snapshot')

        monkeypatch.setattr(worker_skills, 'prepare', interrupted)
        with pytest.raises(RuntimeError):
            tasks.create(
                home, 'recover', 'sample', brief, kind='scout',
                skill_mode='curated', skills=('agd-probe',))
        pending = tasks.read(home, 'recover')
        identity = (pending['attempt'], pending['lease_holder'],
                    pending['lease_id'], pending['worktree'])
        assert pending['phase'] == 'preparing'

        monkeypatch.setattr(worker_skills, 'prepare', original_prepare)
        original_run = tasks.store.run

        def no_second_get(argv, **kwargs):
            assert not ('get' in argv and 'treehouse' in str(argv[0]))
            return original_run(argv, **kwargs)

        monkeypatch.setattr(tasks.store, 'run', no_second_get)
        recovered = tasks.reconcile_allocation(home, 'recover')
        assert recovered['phase'] == 'prepared'
        assert (recovered['attempt'], recovered['lease_holder'],
                recovered['lease_id'], recovered['worktree']) == identity

        monkeypatch.setattr(tasks.store, 'run', original_run)
        monkeypatch.setattr(worker_skills, 'prepare', interrupted)
        with pytest.raises(RuntimeError):
            tasks.create(
                home, 'changed', 'sample', brief, kind='scout',
                skill_mode='curated', skills=('agd-probe',))
        monkeypatch.setattr(worker_skills, 'prepare', original_prepare)
        (source/'SKILL.md').write_text('changed after allocation\n')
        with pytest.raises(store.Refusal):
            tasks.reconcile_allocation(home, 'changed')
        assert tasks.read(home, 'changed')['phase'] == 'preparing'


def test_task_snapshot_survives_plugin_removal():
    import shutil
    from support import lab, command
    from agd_lib import tasks, worker_skills
    with lab() as (home, project):
        default_native_skills(home)
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'survivor', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        destination = Path(task['worktree'])/'.omp/skills/systematic-debugging'
        snapshot = home/'data/tasks/survivor/worker-skills/systematic-debugging'
        expected = (snapshot/'SKILL.md').read_bytes()
        shutil.rmtree(home/'.omp/plugins')
        shutil.rmtree(home/'data/omp/home/.omp')
        shutil.rmtree(destination)

        worker_skills.ensure(home, tasks.read(home, 'survivor'))

        assert (destination/'SKILL.md').read_bytes() == expected
        command(home, 'task', 'create', 'new-task', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout', expected=3)
        assert not tasks.task_file(home, 'new-task').exists()


DEFAULT_WORKER_SKILLS = [
    'ponytail',
    'ponytail-audit',
    'ponytail-debt',
    'ponytail-gain',
    'ponytail-help',
    'ponytail-review',
    'systematic-debugging',
    'test-driven-development',
    'verification-before-completion',
]


def default_native_skills(home):
    from support import write_native_plugins
    ponytail = {
        name: {'SKILL.md': (
            f'---\nname: {name}\ndescription: {name} instructions.\n---\n').encode()}
        for name in DEFAULT_WORKER_SKILLS[:6]
    }
    superpowers = {
        name: {'SKILL.md': (
            f'---\nname: {name}\ndescription: {name} instructions.\n---\n').encode()}
        for name in DEFAULT_WORKER_SKILLS[6:]
    }
    write_native_plugins(home, {
        'ponytail@ponytail': ponytail,
        'superpowers@superpowers-dev': superpowers,
    })


def test_init_seeds_default_worker_profile_from_native_catalog():
    from agd_lib import worker_skills
    from test_projects import command
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        command(home, 'init', '--backend', 'tmux')
        default_native_skills(home)
        resolved = worker_skills.resolve(home)
        assert resolved['schema'] == 2
        assert resolved['mode'] == 'profile'
        assert resolved['profile'] == 'default'
        assert resolved['skills'] == DEFAULT_WORKER_SKILLS
        assert set(resolved['sources']) == set(DEFAULT_WORKER_SKILLS)
        assert command(home, 'worker', 'profiles', 'list') == {
            'default_profile': 'default',
            'profiles': {'default': {'skills': DEFAULT_WORKER_SKILLS}}}
        assert [row['id'] for row in command(
            home, 'worker', 'skills', 'list')] == DEFAULT_WORKER_SKILLS


def test_init_seeds_checkout_home_with_tracked_files():
    import shutil
    from agd_lib import worker_skills
    from test_projects import ROOT, run
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)/'clone'
        run(['git', 'clone', '--no-local', ROOT, home])
        shutil.copy2(ROOT/'bin'/'agd', home/'bin'/'agd')
        shutil.copy2(ROOT/'bin'/'agd_lib'/'worker_skills.py', home/'bin'/'agd_lib'/'worker_skills.py')
        env = dict(os.environ, TREEHOUSE_ROOT=str(home.parent/'pool'))
        env.pop('AGD_HOME', None)
        run([home/'bin'/'agd', 'init', '--backend', 'tmux'], cwd=home, env=env)
        default_native_skills(home)
        assert worker_skills.resolve(home)['skills'] == DEFAULT_WORKER_SKILLS




if __name__ == '__main__':
    test_verified_omp_skill_root_is_project_local()
    test_unverified_cursor_has_no_skill_provisioning_root()
    test_explicit_profile_never_falls_back_to_empty()
    test_profile_defaults_and_curated_selection_are_exact_and_sorted()
    test_invalid_worker_skill_configuration_refuses_instead_of_degrading()
    test_catalog_rejects_missing_or_malformed_bundles()
    test_worker_skill_and_profile_lists_are_deterministic_json()
    test_catalog_and_config_symlinks_cannot_escape_private_home()
    test_config_parent_must_be_a_real_directory()
    test_task_create_selection_refuses_before_allocating_a_lease()
    test_prepare_freezes_and_materializes_a_skill_bundle()
    test_shared_common_git_exclusion_waits_for_last_owner()
    test_ensure_recovers_missing_artifacts_and_verify_refuses_foreign_or_tracked_content()
    test_verify_refuses_modified_frozen_snapshot()
    test_native_alias_and_symlinked_skill_parents_refuse_provisioning()
    test_pending_snapshot_retry_refuses_changed_catalog_source()
    test_retry_repairs_exclude_after_registry_write_failure()
    test_remove_retries_after_first_owned_directory_deletion()
    test_tampered_journal_cannot_touch_sibling_paths()
    test_preexisting_identical_exclude_line_is_not_removed()
    test_ensure_refuses_post_prepare_native_parent_symlink()
    test_ensure_refuses_removed_skill_set()
    test_retry_after_post_exclude_write_failure_releases_only_owned_line()
    test_prompt_names_assigned_skill_paths()
    test_create_materializes_curated_skills_before_prepared()
    test_create_materializes_native_names_without_aliases()
    test_create_default_profile_and_cursor_nonempty_refusal()
    test_unchanged_generated_skill_allows_ready_but_modification_refuses()
    test_staged_and_historic_generated_skills_refuse_ready()
    test_cleanup_retry_after_artifact_removal_before_return()
    test_unsafe_git_exclusion_metadata_refuses_before_prepare_mutates()
    test_cleanup_retry_after_pre_remove_failure_releases_owned_artifacts()
    test_returned_lease_requires_completed_skill_removal_journal()
    test_init_seeds_default_worker_profile_from_native_catalog()
    test_task_snapshot_survives_plugin_removal()
    test_init_seeds_checkout_home_with_tracked_files()
