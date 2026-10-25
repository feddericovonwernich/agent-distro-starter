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


if __name__ == '__main__':
    test_distinct_leases_and_preserved_project()
