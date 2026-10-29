import fcntl
import os
import pty
import select
import subprocess
import tempfile
import termios
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def make_fixture(root):
    fixture = root / 'fixture'
    (fixture / 'bin').mkdir(parents=True)
    (fixture / 'bin' / 'agd').write_text('''#!/bin/bash
set -eu
printf 'agd %s\n' "$*" >> "$AGD_LOG"
if [ "${AGD_FAIL_SETUP:-}" = 1 ] && [ "$3" = installer ]; then exit 1; fi
''')
    (fixture / 'bin' / 'agd').chmod(0o755)
    return fixture


def make_tools(root, fixture, python_version='3.12', include_git=True, include_tmux=True):
    tools = root / 'tools'
    tools.mkdir()
    scripts = {
        'uname': '''#!/bin/bash
case "$1" in -s) echo Linux;; -m) echo x86_64;; esac
''',
        'sudo': '''#!/bin/bash
printf 'sudo %s\n' "$*" >> "$AGD_LOG"
if [ "$1" != -v ]; then exec "$@"; fi
''',
        'apt': '''#!/bin/bash
printf 'apt %s\n' "$*" >> "$AGD_LOG"
if [ "$1" = install ]; then printf '#!/bin/bash\nexit 0\n' > "$AGD_TOOLS/tmux"; /bin/chmod +x "$AGD_TOOLS/tmux"; fi
''',
        'python3': f'''#!/bin/bash
if [ "$1" = -c ]; then [ "{python_version}" = 3.12 ] && exit 0 || exit 1; fi
exec /usr/bin/python3 "$@"
''',
        'omp': '''#!/bin/bash
printf 'omp %s\\n' "$*" >> "$AGD_LOG"
[ "$1" != auth ]
''',
        'sh': '''#!/bin/bash
printf 'shell cwd=%s path=%s\\n' "$PWD" "$PATH" >> "$AGD_LOG"
''',
    }
    if include_git:
        scripts['git'] = '''#!/bin/bash
printf 'git %s\\n' "$*" >> "$AGD_LOG"
 /bin/cp -a "$AGD_FIXTURE/." "${@: -1}"
'''
    if include_tmux:
        scripts['tmux'] = '#!/bin/bash\nexit 0\n'
    for name, body in scripts.items():
        path = tools / name
        path.write_text(body)
        path.chmod(0o755)
    return tools


def run_install(root, tools, answers, *, fail_setup=False, path=None):
    env = dict(os.environ, HOME=str(root / 'home'), PATH=path or str(tools), AGD_LOG=str(root / 'log'),
               AGD_FIXTURE=str(root / 'fixture'), AGD_TOOLS=str(tools), SHELL=str(tools / 'sh'),
               AGD_FAIL_SETUP='1' if fail_setup else '')
    master, slave = pty.openpty()
    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = subprocess.Popen(['/bin/bash', str(ROOT / 'install.sh')], cwd=root, env=env,
                               stdin=slave, stdout=slave, stderr=slave, preexec_fn=terminal)
    os.close(slave)
    os.write(master, answers.encode())
    output = bytearray()
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        if select.select([master], [], [], .1)[0]:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
    while select.select([master], [], [], 0)[0]:
        try:
            output.extend(os.read(master, 4096))
        except OSError:
            break
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    os.close(master)
    text = output.decode(errors='replace')
    return subprocess.CompletedProcess(process.args, process.returncode, text, text)


@pytest.mark.parametrize('backend', ['tmux', 'herdr'])
def test_guided_install_initializes_before_setup_and_enters_clone_shell(backend):
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture)
        result = run_install(root, tools, f'distro-{backend}\n{backend}\n')
        assert result.returncode == 0, (result.stdout, result.stderr)
        log = (root / 'log').read_text().splitlines()
        init = f'agd --home {root}/distro-{backend} init --backend {backend}'
        setup = f'agd --home {root}/distro-{backend} installer setup --backend {backend}'
        assert log.index(init) < log.index(setup)
        assert any(line.startswith(f'shell cwd={root}/distro-{backend} ') for line in log)


def test_failed_owned_clone_is_removed_after_setup_refusal():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture)
        result = run_install(root, tools, 'distro\ntmux\n', fail_setup=True)
        assert result.returncode != 0
        assert not (root / 'distro').exists()



def test_failed_clone_that_populates_owned_destination_is_removed():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture)
        (tools / 'git').write_text('''#!/bin/bash
/bin/mkdir -p "${@: -1}"
printf partial > "${@: -1}/partial"
exit 1
''')
        (tools / 'git').chmod(0o755)
        result = run_install(root, tools, 'distro\ntmux\n')
        assert result.returncode != 0
        assert not (root / 'distro').exists()

def test_blank_sudo_declines_missing_git_without_elevation():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture, include_git=False)
        result = run_install(root, tools, 'distro\ntmux\n\n')
        assert result.returncode != 0
        assert not (root / 'log').exists()
        assert not (root / 'distro').exists()


def test_bootstrap_installs_only_missing_selected_tmux():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture, include_tmux=False)
        result = run_install(root, tools, 'distro\ntmux\ny\n')
        assert result.returncode == 0, (result.stdout, result.stderr)
        log = (root / 'log').read_text()
        assert 'sudo -v' in log
        assert 'sudo apt update' in log
        assert 'sudo apt install -y tmux' in log
        assert 'git' not in '\n'.join(line for line in log.splitlines() if line.startswith('apt '))
        assert 'python3' not in '\n'.join(line for line in log.splitlines() if line.startswith('apt '))


def test_present_python_311_refuses_without_bootstrap():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture, python_version='3.11')
        result = run_install(root, tools, 'distro\ntmux\n')
        assert result.returncode != 0
        assert not (root / 'log').exists()


@pytest.mark.parametrize('term,no_color,colored', [
    ('xterm-256color', None, True),
    ('xterm-256color', '', False),
    ('dumb', None, False),
])
def test_install_leaves_omp_configuration_alone_and_respects_terminal(monkeypatch, term, no_color, colored):
    monkeypatch.setenv('TERM', term)
    monkeypatch.delenv('NO_COLOR', raising=False)
    if no_color is not None:
        monkeypatch.setenv('NO_COLOR', no_color)
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        fixture = make_fixture(root)
        tools = make_tools(root, fixture)
        result = run_install(root, tools, 'distro\nherdr\n')
        assert result.returncode == 0, result.stdout
        log = (root / 'log').read_text().splitlines()
        assert any(line.startswith(f'shell cwd={root}/distro ') for line in log)
        assert not any(line.startswith('omp ') for line in log)
        assert ('\x1b[' in result.stdout) is colored
