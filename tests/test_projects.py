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
        command(home, 'init')
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
