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
        command(home, 'init')
        command(home, 'project', 'add', 'sample', '--source', str(origin))
        brief = home/'brief.md'
        brief.write_text('Change the file and report the checks.\n')
        a = command(home, 'task', 'create', 'one', '--project', 'sample', '--brief', str(brief))
        b = command(home, 'task', 'create', 'two', '--project', 'sample', '--brief', str(brief))
        assert a['lease_id'] != b['lease_id']
        assert a['worktree'] != b['worktree']
        (Path(a['worktree'])/'value.txt').write_text('one only\n')
        assert (Path(b['worktree'])/'value.txt').read_text() == 'base\n'
        assert (home/'projects'/'sample'/'value.txt').read_text() == 'base\n'
        command(home, 'task', 'create', 'one', '--project', 'sample', '--brief', str(brief), expected=3)
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
