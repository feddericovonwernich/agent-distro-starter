import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin' / 'agd'


def run(args, cwd=None, env=None, expected=0):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result


def command(home, *args, expected=0):
    env = dict(os.environ, AGD_HOME=str(home), TREEHOUSE_ROOT=str(home.parent/'pool'))
    result = run([CLI, '--home', home, *args], env=env, expected=expected)
    return json.loads(result.stdout) if expected == 0 else result


def test_project_lifecycle():
    with tempfile.TemporaryDirectory(prefix='agd-project-') as tmp:
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
        info = command(home, 'project', 'add', 'sample', '--source', str(origin), '--base', 'main')
        project = home/'projects'/'sample'
        assert info['path'] == str(project)
        assert command(home, 'project', 'inspect', 'sample')['base'] == 'main'
        assert len(command(home, 'project', 'list')) == 1
        (project/'value.txt').write_text('personal edit\n')
        command(home, 'project', 'refresh', 'sample', expected=3)
        assert (project/'value.txt').read_text() == 'personal edit\n'
        command(home, 'project', 'remove', 'sample')
        assert (project/'.git').exists()
        assert command(home, 'project', 'list') == []
        command(home, 'project', 'register', 'sample', '--path', str(project), '--base', 'main')
        instruction = home/'consent.txt'
        instruction.write_text('I trust this exact clone for Cursor.\n')
        assert command(home, 'project', 'trust', 'sample', '--instruction', str(instruction))['trusted']
        command(home, 'project', 'add', '../escape', '--source', str(origin), expected=2)
        assert not (root/'escape').exists()
        (project/'value.txt').write_text('base\n')
        deletion = home/'delete-consent.txt'
        deletion.write_text('Delete only this disposable sample clone.\n')
        approval = command(home, 'project', 'approve', 'sample', '--action', 'delete-clone',
                           '--instruction', str(deletion))
        with (project/'.git'/'info'/'exclude').open('a') as stream:
            stream.write('\nprivate-note.txt\n')
        private = project/'private-note.txt'
        private.write_text('keep this ignored material\n')
        command(home, 'project', 'remove', 'sample', '--delete-clone',
                '--approval', approval['id'], expected=3)
        assert private.read_text() == 'keep this ignored material\n'
        private.unlink()
        assert command(home, 'project', 'remove', 'sample', '--delete-clone',
                       '--approval', approval['id'])['clone_deleted']
        assert not project.exists()




if __name__ == '__main__':
    test_project_lifecycle()


def test_clean_published_clone_needs_only_home_confirmation():
    import sys
    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import projects
    from support import lab
    with lab() as (home, project):
        clean = projects.checkout_inventory(project)
        assert clean['work_loss'] == []
        (project / 'private.env').write_text('owned local value\n')
        with (project / '.git/info/exclude').open('a') as stream:
            stream.write('\nprivate.env\n')
        (project / 'value.txt').write_text('unpublished value\n')
        dirty = projects.checkout_inventory(project)
        assert any(item.get('path') == 'private.env' for item in dirty['work_loss'])
        assert any(item.get('path') == 'value.txt' for item in dirty['work_loss'])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=project)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=project)
        run(['git', 'add', 'value.txt'], cwd=project)
        run(['git', 'commit', '-m', 'unpublished'], cwd=project)
        committed = projects.checkout_inventory(project)
        head = run(['git', 'rev-parse', 'HEAD'], cwd=project).stdout.strip()
        assert any(head in item.get('commits', []) for item in committed['work_loss'])
        assert (project / 'private.env').read_text() == 'owned local value\n'


def test_linked_source_removal_preserves_common_repo_and_sibling():
    import sys
    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import projects
    from support import lab, uninstall_scope
    with lab() as (home, project):
        source, sibling = home.parent / 'linked-source', home.parent / 'sibling'
        run(['git', 'worktree', 'add', '-b', 'source-fixture', source, 'main'], cwd=project)
        run(['git', 'worktree', 'add', '-b', 'sibling-fixture', sibling, 'main'], cwd=project)
        before = (sibling / 'value.txt').read_bytes()
        common = project / '.git'
        refs = run(['git', 'show-ref'], cwd=project).stdout
        resource = projects.checkout_inventory(source)
        with uninstall_scope(home, [resource], discard=bool(resource['work_loss'])) as operation:
            projects.remove_checkout(home, resource, operation)
        assert not source.exists()
        assert str(source) not in run(['git', 'worktree', 'list', '--porcelain'], cwd=project).stdout
        assert common.is_dir()
        assert (sibling / 'value.txt').read_bytes() == before
        assert run(['git', 'show-ref'], cwd=project).stdout == refs


def test_external_worktree_blocks_backing_clone_deletion():
    import sys
    import pytest
    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import projects, store
    from support import lab, uninstall_scope
    with lab() as (home, project):
        external = home.parent / 'foreign-linked'
        run(['git', 'worktree', 'add', '-b', 'foreign-fixture', external, 'main'], cwd=project)
        before = (external / 'value.txt').read_bytes()
        resources = projects.uninstall_inventory(home)
        clone = next(row for row in resources if row['identity']['path'] == str(project))
        assert clone['blockers']
        with pytest.raises(store.Refusal):
            with uninstall_scope(home, [clone], discard=True) as operation:
                projects.remove_checkout(home, clone, operation)
        assert (project / '.git').is_dir()
        assert (external / 'value.txt').read_bytes() == before


def test_owned_linked_checkout_on_another_filesystem_is_removed():
    import sys
    import pytest
    sys.path.insert(0, str(ROOT / 'bin'))
    from agd_lib import projects
    from support import lab, uninstall_scope
    with lab() as (home, project):
        shared_memory = Path('/dev/shm')
        if not shared_memory.is_dir() or shared_memory.stat().st_dev == home.stat().st_dev:
            pytest.skip('independent writable /dev/shm filesystem unavailable')
        with tempfile.TemporaryDirectory(prefix='agd-owned-', dir=shared_memory) as temporary:
            path = Path(temporary) / 'linked'
            run(['git', 'worktree', 'add', '-b', 'cross-filesystem-fixture', path, 'main'], cwd=project)
            resource = projects.checkout_inventory(path)
            assert resource['identity']['device'] != home.stat().st_dev
            with uninstall_scope(home, [resource], discard=bool(resource['work_loss'])) as operation:
                projects.remove_checkout(home, resource, operation)
            assert not path.exists()
            assert str(path) not in run(['git', 'worktree', 'list', '--porcelain'], cwd=project).stdout
            assert (project / 'value.txt').read_text() == 'base\n'


def test_mount_roots_and_nested_bind_mounts_are_refused(tmp_path):
    import shutil
    import sys
    import pytest
    unshare = shutil.which('unshare')
    if not unshare:
        pytest.skip('unprivileged mount namespaces unavailable: unshare missing')
    prefix = [unshare, '--user', '--map-root-user', '--mount', '--propagation', 'private']
    capability = subprocess.run([*prefix, sys.executable, '-c', 'pass'], capture_output=True, text=True)
    if capability.returncode:
        pytest.skip('unprivileged mount namespace unavailable: ' + capability.stderr.strip())
    program = '''
import subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agd_lib import projects, store
base = Path(sys.argv[2])
for kind in ("root", "nested"):
    target = base / kind
    target.mkdir()
    if kind == "root":
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", str(target)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(target)], check=True, capture_output=True)
    (target / "value").write_text("owned fixture")
    subprocess.run(["git", "-C", str(target), "add", "value"], check=True)
    subprocess.run(["git", "-C", str(target), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-m", "initial"],
                   check=True, capture_output=True)
    if kind == "nested":
        other = base / "other"; other.mkdir(); (other / "sentinel").write_text("retained")
        mount = target / "mounted"; mount.mkdir()
        subprocess.run(["mount", "--bind", str(other), str(mount)], check=True)
    try:
        projects.checkout_inventory(target)
    except store.Refusal:
        pass
    else:
        raise AssertionError("mounted deletion root was accepted")
    assert (target / "value").read_text() == "owned fixture"
'''
    result = subprocess.run([*prefix, sys.executable, '-B', '-c', program, str(ROOT / 'bin'), str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
