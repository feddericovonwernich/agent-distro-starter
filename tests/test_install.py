import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://github.com/feddericovonwernich/agent-distro-starter.git'


def test_named_install():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        origin = root/'template.git'
        subprocess.run(['git', 'clone', '--bare', str(ROOT), str(origin)],
                       check=True, capture_output=True)
        (root/'decoy/my-distro').mkdir(parents=True)
        env = dict(os.environ, AGD_HOME=str(root/'unrelated-home'), CDPATH=str(root/'decoy'),
                   GIT_CONFIG_COUNT='1',
                   GIT_CONFIG_KEY_0=f'url.file://{origin}.insteadOf',
                   GIT_CONFIG_VALUE_0=SOURCE)

        def install(name):
            return subprocess.run(
                ['script', '-q', '-e', '-c', f'bash {shlex.quote(str(ROOT/"install.sh"))}', '/dev/null'],
                input=name+'\n', cwd=root, env=env, capture_output=True, text=True, timeout=90)

        first = install('my-distro')
        assert first.returncode == 0, (first.stdout, first.stderr)
        first_home = root/'my-distro'
        first_id = json.loads((first_home/'data/home.json').read_text())['home_id']
        assert not (root/'unrelated-home').exists()
        assert subprocess.check_output(
            ['git', '-C', str(first_home), 'config', '--get', 'remote.origin.url'],
            text=True).strip() == SOURCE
        assert subprocess.check_output(
            ['git', '-C', str(first_home), 'status', '--porcelain=v1'], text=True) == ''
        second = install('another-distro')
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert json.loads((root/'another-distro/data/home.json').read_text())['home_id'] != first_id
        sentinel = first_home/'keep'
        sentinel.write_text('mine')
        assert install('my-distro').returncode != 0
        assert sentinel.read_text() == 'mine'
        assert install('Bad Name').returncode != 0
        assert not (root/'Bad Name').exists()
        (root/'linked').symlink_to(first_home, target_is_directory=True)
        assert install('linked').returncode != 0
        assert (root/'linked').is_symlink()
        no_tty = subprocess.run(['bash', str(ROOT/'install.sh')], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        assert no_tty.returncode != 0


if __name__ == '__main__':
    test_named_install()
