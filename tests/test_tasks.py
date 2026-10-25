import os
import tempfile
from pathlib import Path
from test_projects import run, command


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


if __name__ == '__main__':
    test_distinct_leases_and_preserved_project()
    test_lease_survives_missing_environment_override()
    test_cleaned_history_does_not_block_unregister()
