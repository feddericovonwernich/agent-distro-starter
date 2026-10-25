from pathlib import Path
from support import lab, command, run


def test_ready_is_not_cleanup_authority():
    with lab() as (home, project):
        brief = home/'brief.md'
        brief.write_text('Update value.txt.\n')
        task = command(home, 'task', 'create', 'change', '--project', 'sample', '--brief', str(brief))
        work = Path(task['worktree'])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=work)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=work)
        (work/'value.txt').write_text('changed\n')
        run(['git', 'add', 'value.txt'], cwd=work)
        run(['git', 'commit', '-m', 'change'], cwd=work)
        report = home/'result.md'
        report.write_text('Updated value.txt. Check: value is changed. Limit: no product tests.\n')
        ready = command(home, 'task', 'ready', 'change', '--report', str(report))
        command(home, 'task', 'cleanup', 'change', expected=3)
        assert (work/'value.txt').read_text() == 'changed\n'
        assert run(['git', 'rev-parse', 'HEAD'], cwd=work).stdout.strip() == ready['ready_head']
        instruction = home/'approval.txt'
        instruction.write_text('Merge task change into sample main locally.\n')
        approval = command(home, 'task', 'approve', 'change', '--action', 'local-merge', '--target', 'sample:main', '--instruction', str(instruction))
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin'))
        from agd_lib import tasks
        active = tasks.read(home, 'change')
        active.update(executor='worker', worker_alive=True, incarnation='live')
        tasks.save(home, active)
        command(home, 'task', 'land', 'change', '--approval', approval['id'], expected=3)
        assert (project/'value.txt').read_text() == 'base\n'
        active.update(executor='central')
        tasks.save(home, active)
        command(home, 'task', 'land', 'change', '--approval', approval['id'])
        assert (project/'value.txt').read_text() == 'changed\n'
        command(home, 'task', 'cleanup', 'change')
        assert command(home, 'task', 'status', 'change')['phase'] == 'cleaned'


def test_dirty_and_published_work_remain_preserved():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'pending', '--project', 'sample', '--brief', str(brief))
        work = Path(task['worktree'])
        (work/'untracked.txt').write_text('must survive\n')
        command(home, 'task', 'cleanup', 'pending', expected=3)
        assert (work/'untracked.txt').read_text() == 'must survive\n'


def test_stale_approval_does_not_merge_new_commits():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Update value.txt.\n')
        task = command(home, 'task', 'create', 'stale', '--project', 'sample', '--brief', str(brief))
        work = Path(task['worktree'])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=work)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=work)
        (work/'value.txt').write_text('approved\n')
        run(['git', 'add', 'value.txt'], cwd=work)
        run(['git', 'commit', '-m', 'approved'], cwd=work)
        report = home/'report.md'; report.write_text('Changed value; checked commit; no tests.\n')
        command(home, 'task', 'ready', 'stale', '--report', str(report))
        instruction = home/'approval.md'; instruction.write_text('Merge approved task.\n')
        approval = command(home, 'task', 'approve', 'stale', '--action', 'local-merge',
                           '--target', 'sample:main', '--instruction', str(instruction))
        (work/'value.txt').write_text('unapproved\n')
        run(['git', 'add', 'value.txt'], cwd=work)
        run(['git', 'commit', '-m', 'unapproved'], cwd=work)
        command(home, 'task', 'land', 'stale', '--approval', approval['id'], expected=3)
        assert (project/'value.txt').read_text() == 'base\n'
        assert (work/'value.txt').read_text() == 'unapproved\n'

def test_published_head_is_not_a_landing():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'published', '--project', 'sample', '--brief', str(brief))
        work = Path(task['worktree'])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=work)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=work)
        (work/'value.txt').write_text('new\n')
        run(['git', 'add', 'value.txt'], cwd=work)
        run(['git', 'commit', '-m', 'publish'], cwd=work)
        report = home/'result.md'; report.write_text('Changed value. Checked commit. No product tests.\n')
        command(home, 'task', 'ready', 'published', '--report', str(report))
        run(['git', 'push', 'origin', 'HEAD:refs/heads/agd/published'], cwd=work)
        command(home, 'task', 'cleanup', 'published', expected=3)
        assert (work/'value.txt').read_text() == 'new\n'

def test_publication_record_requires_head_bound_approval():
    with lab() as (home, project):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'review', '--project', 'sample', '--brief', str(brief))
        work = Path(task['worktree'])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=work)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=work)
        (work/'value.txt').write_text('review\n')
        run(['git', 'add', 'value.txt'], cwd=work)
        run(['git', 'commit', '-m', 'review'], cwd=work)
        report = home/'result.md'; report.write_text('Changed value. Checked commit. No product tests.\n')
        command(home, 'task', 'ready', 'review', '--report', str(report))
        run(['git', 'push', 'origin', 'HEAD:refs/heads/agd/review'], cwd=work)
        evidence = home/'forge.txt'; evidence.write_text('Review creation requested\n')
        command(home, 'task', 'publication-record', 'review', '--approval', 'unknown',
                '--remote', 'origin', '--branch', 'agd/review',
                '--url', 'https://example.invalid/pull/1', '--evidence', str(evidence), expected=3)
        assert command(home, 'task', 'status', 'review')['phase'] == 'ready'
        instruction = home/'publish-approval.md'; instruction.write_text('Publish review for this exact branch.\n')
        approval = command(home, 'task', 'approve', 'review', '--action', 'publish',
                           '--target', 'origin:agd/review', '--instruction', str(instruction))
        command(home, 'task', 'publication-record', 'review', '--approval', approval['id'],
                '--remote', 'origin', '--branch', 'agd/review',
                '--url', 'https://example.invalid/pull/1', '--evidence', str(evidence), expected=3)
        assert 'publication' not in command(home, 'task', 'status', 'review')

if __name__ == '__main__':
    test_ready_is_not_cleanup_authority()
    test_dirty_and_published_work_remain_preserved()
    test_stale_approval_does_not_merge_new_commits()
    test_published_head_is_not_a_landing()
    test_publication_record_requires_head_bound_approval()
