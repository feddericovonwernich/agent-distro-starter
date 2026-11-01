import os
import tempfile
from pathlib import Path
from test_projects import run, command
import pytest



def test_distinct_leases_and_preserved_project():
    with tempfile.TemporaryDirectory(prefix='agd-task-') as tmp:
        root = Path(tmp)
        seed, origin, home = root/'seed', root/'origin.git', root/'home'
        run(['git', 'init', '-b', 'main', seed])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=seed)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=seed)
        (seed/'value.txt').write_text('base\n')
        run(['git', 'add', 'value.txt'], cwd=seed)
        run(['git', 'commit', '-m', 'initial'], cwd=seed)
        run(['git', 'clone', '--bare', seed, origin])
        command(home, 'init', '--backend', 'tmux')
        command(home, 'project', 'add', 'sample', '--source', str(origin))
        brief = home/'brief.md'
        brief.write_text('Change the file and report the checks.\n')
        a = command(home, 'task', 'create', 'one', '--project', 'sample',
                    '--brief', str(brief), '--skill-mode', 'none')
        b = command(home, 'task', 'create', 'two', '--project', 'sample',
                    '--brief', str(brief), '--skill-mode', 'none')
        assert a['lease_id'] != b['lease_id']
        assert a['worktree'] != b['worktree']
        (Path(a['worktree'])/'value.txt').write_text('one only\n')
        assert (Path(b['worktree'])/'value.txt').read_text() == 'base\n'
        assert (home/'projects'/'sample'/'value.txt').read_text() == 'base\n'
        command(home, 'task', 'create', 'one', '--project', 'sample',
                '--brief', str(brief), '--skill-mode', 'none', expected=3)
        assert command(home, 'task', 'status', 'one')['lease_id'] == a['lease_id']

        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
        from agd_lib import tasks
        saved = tasks.read(home, 'two')
        saved['phase'] = 'preparing'
        saved.pop('worktree')
        saved.pop('lease_id')
        tasks.save(home, saved)
        recovered = command(home, 'task', 'reconcile', 'two')
        assert recovered['lease_id'] == b['lease_id']
        assert recovered['worktree'] == b['worktree']


def test_lease_survives_missing_environment_override():
    from support import lab
    from test_projects import CLI
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'standalone', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        report = home/'report.md'; report.write_text('Read value.txt: base. Checked read.\n')
        env = dict(os.environ)
        env.pop('TREEHOUSE_ROOT', None)
        result = run([CLI, '--home', home, 'task', 'ready', 'standalone', '--report', report],
                     env=env)
        assert __import__('json').loads(result.stdout)['phase'] == 'ready'


def test_lease_survives_managed_omp_home_boundary():
    from support import lab
    from test_projects import CLI
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import omp_runtime
    with lab() as (home, project):
        account_home = home.parent/'account'
        account_home.mkdir()
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        managed = omp_runtime.environment(
            home, {**os.environ, 'HOME': str(account_home), 'AGD_HOME': str(home)})
        created = run(
            [CLI, '--home', home, 'task', 'create', 'managed-home',
             '--project', 'sample', '--brief', brief, '--kind', 'scout',
             '--skill-mode', 'none'],
            env=managed)
        assert __import__('json').loads(created.stdout)['phase'] == 'prepared'

        report = home/'report.md'; report.write_text('Read value.txt: base. Checked read.\n')
        account = {**os.environ, 'HOME': str(account_home), 'AGD_HOME': str(home)}
        account.pop('TREEHOUSE_ROOT', None)
        ready = run(
            [CLI, '--home', home, 'task', 'ready', 'managed-home',
             '--report', report],
            env=account)
        assert __import__('json').loads(ready.stdout)['phase'] == 'ready'
        assert __import__('json').loads(created.stdout)['treehouse_root'] == str(
            home/'data/treehouse')


def test_cleaned_history_does_not_block_unregister():
    from support import lab
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'past', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        report = home/'report.md'; report.write_text('value.txt contains base.\n')
        command(home, 'task', 'ready', 'past', '--report', str(report))
        command(home, 'task', 'cleanup', 'past')
        command(home, 'project', 'remove', 'sample')
        assert project.is_dir()
        assert command(home, 'task', 'status', 'past')['phase'] == 'cleaned'


def test_cleanup_retry_after_return_marks_confirmed_absent_lease_cleaned():
    from unittest.mock import patch
    from support import lab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import tasks
    os.environ['PATH'] = '/tmp/agd-treehouse-v3.1.6:' + os.environ['PATH']
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'returning', '--project', 'sample',
                '--brief', str(brief), '--kind', 'scout')
        report = home/'report.md'; report.write_text('base inspected\n')
        command(home, 'task', 'ready', 'returning', '--report', str(report))
        original = tasks.save
        def interrupted(root, task):
            if task.get('phase') == 'cleaned':
                raise RuntimeError('injected post-return failure')
            original(root, task)
        with patch.object(tasks, 'save', interrupted):
            try:
                tasks.cleanup(home, 'returning')
            except RuntimeError:
                pass
            else:
                raise AssertionError('post-return fault not injected')
        try:
            tasks.cleanup(home, 'returning', discard=True)
        except tasks.store.Refusal:
            pass
        else:
            raise AssertionError('wrong cleanup retry authorization was accepted')
        assert tasks.read(home, 'returning')['cleanup_phase'] == 'returning'
        assert tasks.cleanup(home, 'returning')['phase'] == 'cleaned'

def test_pr_publish_grant_binds_target_and_descendant_history():
    from support import lab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import store, tasks

    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'prgrant', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home/'instruction.txt'; instruction.write_text('Authorize prgrant to publish origin:agd/prgrant.\n')
        grant = tasks.grant_pr_publish(home, 'prgrant', 'origin:agd/prgrant', instruction)
        assert tasks.authorize_pr_publish(home, tasks.read(home, 'prgrant'), grant['id'])['id'] == grant['id']
        run(['git', 'commit', '--allow-empty', '-m', 'repair'], cwd=task['worktree'])
        assert tasks.authorize_pr_publish(home, tasks.read(home, 'prgrant'), grant['id'])['grant_head'] != task['start']
        run(['git', 'reset', '--hard', task['start']], cwd=task['worktree'])
        with pytest.raises(store.Refusal, match='descendant'):
            tasks.authorize_pr_publish(home, tasks.read(home, 'prgrant'), grant['id'])
        with pytest.raises(store.Refusal, match='target'):
            tasks.grant_pr_publish(home, 'prgrant', 'origin:other', instruction)


def test_pr_publish_grant_requires_instruction_to_name_task_and_target():
    from support import lab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import store, tasks

    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        command(home, 'task', 'create', 'instruction-grant', '--project', 'sample', '--brief', str(brief))
        instruction = home/'instruction.txt'
        instruction.write_text('Authorize publication.\n')
        with pytest.raises(store.Refusal, match='identify exact task and target'):
            tasks.grant_pr_publish(home, 'instruction-grant', 'origin:agd/instruction-grant', instruction)
        instruction.write_text('Authorize instruction-grant to publish origin:agd/instruction-grant.\n')
        assert tasks.grant_pr_publish(home, 'instruction-grant', 'origin:agd/instruction-grant',
                                      instruction)['target'] == 'origin:agd/instruction-grant'
        instruction.write_text('Authorize origin:agd/instruction-grant.\n')
        with pytest.raises(store.Refusal, match='independently name exact task'):
            tasks.grant_pr_publish(home, 'instruction-grant', 'origin:agd/instruction-grant', instruction)




def test_ready_refuses_pr_granted_task_without_final_verification():
    from support import lab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import store, tasks

    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'ready-pr', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home/'instruction.txt'
        instruction.write_text('Authorize ready-pr to publish origin:agd/ready-pr.\n')
        tasks.grant_pr_publish(home, task['id'], 'origin:agd/ready-pr', instruction)
        report = home/'report.md'; report.write_text('Verified.\n')
        with pytest.raises(store.Refusal, match='PR verification'):
            tasks.ready(home, task['id'], report)


def test_ready_refuses_manually_written_final_claim_without_github_evidence():
    from support import lab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import store, tasks

    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'forged-final', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home/'instruction.txt'
        instruction.write_text('Authorize forged-final to publish origin:agd/forged-final.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/forged-final', instruction)
        head = run(['git', 'rev-parse', 'HEAD'], cwd=task['worktree']).stdout.strip()
        with pytest.raises(store.Refusal, match='final verification is private'):
            tasks.write_pr_receipt(home, task['id'], {
                'schema': 1, 'grant_id': grant['id'], 'remote': 'origin', 'repository': grant['remote_url'],
                'branch': task['branch'], 'base': task['base'], 'local_head': head, 'remote_head': head,
                'pr': {'id': 'P_1', 'number': 1, 'url': 'https://example.test/owner/repo/pull/1'},
                'verification': {'grant_id': grant['id'], 'remote': 'origin',
                                 'repository': grant['remote_url'], 'branch': task['branch'], 'base': task['base'],
                                 'head': head, 'remote_head': head, 'pr_head': head,
                                 'pr_number': 1, 'pr_id': 'P_1',
                                 'final': {'head_sha': head, 'pr': {'headRefOid': head}}}})
        report = home/'report.md'; report.write_text('Verified.\n')
        with pytest.raises(store.Refusal, match='PR verification'):
            tasks.ready(home, task['id'], report)
def test_pr_receipt_does_not_transition_task_phase():
    from support import lab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
    from agd_lib import tasks

    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        command(home, 'task', 'create', 'receipt', '--project', 'sample', '--brief', str(brief))
        before = tasks.read(home, 'receipt')['phase']
        tasks.write_pr_receipt(home, 'receipt', {'schema': 1, 'grant_id': 'grant', 'blocked_reason': 'auth'})
        assert tasks.read(home, 'receipt')['phase'] == before


def test_task_approve_pr_publish_records_exact_target():
    from support import lab

    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'cli-grant', '--project', 'sample', '--brief', str(brief))
        instruction = home/'instruction.txt'; instruction.write_text('Authorize cli-grant to publish origin:agd/cli-grant.\n')
        grant = command(home, 'task', 'approve', task['id'], '--action', 'pr-publish',
                        '--target', 'origin:agd/cli-grant', '--instruction', str(instruction))
        assert grant['target'] == 'origin:agd/cli-grant'


if __name__ == '__main__':
    test_distinct_leases_and_preserved_project()
    test_lease_survives_missing_environment_override()
    test_cleaned_history_does_not_block_unregister()
    test_cleanup_retry_after_return_marks_confirmed_absent_lease_cleaned()


def test_return_is_not_destroy_and_skipped_slot_is_not_success(monkeypatch):
    import json
    import pytest
    from types import SimpleNamespace
    from support import lab, command, uninstall_scope
    from agd_lib import tasks, worker_skills, store
    with lab() as (home, project):
        brief = home / 'brief.md'; brief.write_text('Inspect the fixture without a model.\n')
        task = command(home, 'task', 'create', 'purge', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout', '--skill-mode', 'none')
        worker_skills.remove(home, task)
        resources = tasks.uninstall_inventory(home)
        original = store.run
        def skipped(argv, **kwargs):
            if argv[0] == 'treehouse' and 'destroy' in argv:
                return SimpleNamespace(returncode=0, stdout='not removed', stderr='')
            return original(argv, **kwargs)
        with uninstall_scope(home, resources, discard=any(row.get('work_loss') for row in resources)) as operation:
            monkeypatch.setattr(store, 'run', skipped)
            with pytest.raises(store.Refusal):
                tasks.uninstall_remove(home, resources, operation)
            assert Path(task['worktree']).is_dir()
            assert resources[0]['key'] not in operation['completed']
            rows = json.loads(original([*tasks.treehouse_args(task), 'status', '--json'], cwd=project).stdout)
            exact = next(row for row in rows if row['path'] == task['worktree'])
            assert not exact.get('lease_id')
            monkeypatch.setattr(store, 'run', original)
            tasks.uninstall_remove(home, resources, operation)
            assert not Path(task['worktree']).exists()
            assert str(task['worktree']) not in original(['git', 'worktree', 'list', '--porcelain'], cwd=project).stdout
            assert resources[0]['key'] in operation['completed']
        assert tasks.read(home, task['id'])['phase'] == 'prepared'


def test_foreign_or_reassigned_lease_is_preserved(monkeypatch):
    import json
    import pytest
    from support import lab, command, uninstall_scope
    from agd_lib import tasks, worker_skills, store
    with lab() as (home, project):
        brief = home / 'brief.md'; brief.write_text('Inspect the fixture without a model.\n')
        task = command(home, 'task', 'create', 'race', '--project', 'sample',
                       '--brief', str(brief), '--kind', 'scout', '--skill-mode', 'none')
        worker_skills.remove(home, task)
        resources = tasks.uninstall_inventory(home)
        original = store.run
        acquired = []
        def raced(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == 'treehouse' and 'return' in argv and not acquired:
                empty = home / 'empty-include'; empty.write_text('')
                allocation = json.loads(original(
                    [*tasks.treehouse_args(task), 'get', '--lease', '--json', '--no-fetch',
                     '--lease-holder', 'foreign-owner', '--base', 'main', '--include-file', str(empty)],
                    cwd=project).stdout)
                acquired.append(allocation)
                assert allocation['path'] == task['worktree']
                (Path(allocation['path']) / 'foreign.txt').write_text('replacement owner\n')
            return result
        with uninstall_scope(home, resources, discard=any(row.get('work_loss') for row in resources)) as operation:
            monkeypatch.setattr(store, 'run', raced)
            with pytest.raises(store.Refusal):
                tasks.uninstall_remove(home, resources, operation)
            assert resources[0]['key'] not in operation['completed']
            assert (Path(task['worktree']) / 'foreign.txt').read_text() == 'replacement owner\n'
            rows = json.loads(original([*tasks.treehouse_args(task), 'status', '--json'], cwd=project).stdout)
            exact = next(row for row in rows if row['path'] == task['worktree'])
            assert exact['lease_id'] == acquired[0]['lease_id']
            assert exact['lease_holder'] == 'foreign-owner'
