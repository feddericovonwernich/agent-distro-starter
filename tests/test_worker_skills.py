import json
import os
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


def test_launch_selection_flags_resolve_before_an_endpoint_starts():
    from support import lab, command
    from agd_lib import tasks
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        brief = home/'brief.md'
        brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'selection', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        result = command(home, 'worker', 'launch', 'selection', '--profile', 'missing',
                         expected=3)
        assert 'unknown profile' in result.stderr
        assert tasks.read(home, 'selection')['phase'] == 'prepared'
        assert tasks.read(home, 'selection')['lease_id'] == task['lease_id']


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
        manifest = worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
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
        worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        (skill/'SKILL.md').write_text('changed\n')
        try:
            worker_skills.prepare(home, task, {
                'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        except store.Refusal:
            return
        raise AssertionError('changed source was accepted after pending snapshot')


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


def test_cli_curated_selection_freezes_before_backend_failure():
    from support import lab, command
    from agd_lib import tasks
    with lab() as (home, project):
        os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'curated', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        command(home, 'worker', 'launch', 'curated', '--backend', 'herdr',
                '--skill-mode', 'curated', '--skill', 'agd-probe', expected=4)
        assert tasks.read(home, 'curated')['phase'] == 'prepared'
        assert not (home/'data'/'tasks'/'curated'/'worker-skills.json').exists()
        assert not (Path(task['worktree'])/'.omp').exists()


def test_cli_default_profile_and_cursor_nonempty_refusal_preserve_task():
    from support import lab, command
    from agd_lib import tasks
    with lab() as (home, project):
        os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
        add_skill(home, 'agd-probe')
        write_profiles(home, {'default_profile': 'implementer',
                              'profiles': {'implementer': {'skills': ['agd-probe']}}})
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'profile', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        command(home, 'worker', 'launch', 'profile', '--backend', 'herdr', expected=4)
        assert tasks.read(home, 'profile')['phase'] == 'prepared'
        assert not (home/'data'/'tasks'/'profile'/'worker-skills.json').exists()
        instruction = home/'trust.md'; instruction.write_text('Trust this disposable project.\n')
        command(home, 'project', 'trust', 'sample', '--instruction', str(instruction))
        task = command(home, 'task', 'create', 'cursor', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        command(home, 'worker', 'launch', 'cursor', '--harness', 'cursor',
                '--skill-mode', 'curated', '--skill', 'agd-probe', expected=4)
        assert tasks.read(home, 'cursor')['phase'] == 'prepared'


def test_unchanged_generated_skill_allows_ready_but_modification_refuses():
    from support import lab, command
    from agd_lib import tasks, worker_skills
    with lab() as (home, project):
        os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
        add_skill(home, 'agd-probe')
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = command(home, 'task', 'create', 'guarded', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout')
        task['harness'] = 'omp'
        manifest = worker_skills.prepare(home, task, {
            'schema': 1, 'mode': 'curated', 'profile': None, 'skills': ['agd-probe']})
        task['worker_skills'] = manifest; tasks.save(home, task)
        report = home/'report.md'; report.write_text('value.txt contains base.\n')
        assert command(home, 'task', 'ready', 'guarded', '--report', str(report))['phase'] == 'ready'
        generated = Path(task['worktree'])/'.omp'/'skills'/'agd-probe'/'SKILL.md'
        generated.write_text('changed\n')
        command(home, 'task', 'ready', 'guarded', '--report', str(report), expected=3)
        assert tasks.read(home, 'guarded')['lease_id'] == task['lease_id']

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
    test_launch_selection_flags_resolve_before_an_endpoint_starts()
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
    test_cli_curated_selection_freezes_before_backend_failure()
    test_cli_default_profile_and_cursor_nonempty_refusal_preserve_task()
    test_unchanged_generated_skill_allows_ready_but_modification_refuses()
