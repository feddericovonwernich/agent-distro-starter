import tempfile
from contextlib import contextmanager
from pathlib import Path
from test_projects import command, run


@contextmanager
def lab():
    with tempfile.TemporaryDirectory(prefix='agd-lab-') as tmp:
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
        project = home/'projects'/'sample'
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=project)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=project)
        yield home, project
