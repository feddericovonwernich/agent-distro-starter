import fcntl
import os
import pty
import select
import json
import signal
import subprocess
import sys
import tempfile
import termios
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))


def make_fixture(root):
    fixture = root / 'fixture'
    (fixture / 'bin').mkdir(parents=True)
    site = root / 'site'
    site.mkdir()
    # Only external HTTP readiness is simulated; config, ownership and CLI are real.
    (site / 'sitecustomize.py').write_text(
        'from agd_lib import observability\n'
        'observability._ready = lambda *_args: True\n')
    (fixture / 'bin' / 'agd').write_text('''#!/bin/bash
set -eu
printf 'agd %s\n' "$*" >> "$AGD_LOG"
if [ "${AGD_FAIL_SETUP:-}" = 1 ] && [ "$3" = installer ]; then
  mkdir -p "$2/.omp/plugins" "$2/data/omp/xdg/data/omp/plugins/cache/plugins/ponytail-partial"
  printf partial > "$2/.omp/plugins/installed_plugins.json"
  exit 1
fi
if [ "$3" = init ]; then
  /bin/mkdir -m 700 -p "$2/data"
  printf '{"schema":1,"home_id":"%s"}' "$AGD_INSTALL_HOME_ID" > "$2/data/home.json"
fi
if [ "$3" = observability ]; then
  if [ "${AGD_TELEMETRY:-}" = status ] && [ "$4" = status ]; then exit 3; fi
  exec /usr/bin/python3 "$AGD_SOURCE/bin/agd" "$@"
fi
if [ "$3" = installer ] && [ "$4" = shell ]; then exec "$SHELL" -i; fi
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
        'docker': '''#!/bin/bash
if [ "$1" = compose ]; then
  case "$6" in
    up)
      [ "${AGD_TELEMETRY:-}" != daemon ] || exit 1
      printf allocated > "$AGD_RESOURCE"
      if [ "${AGD_TELEMETRY:-}" = interrupt ]; then
        printf '%s' "$$" > "$AGD_RESOURCE.pid"
        exec /bin/sleep 30
      fi
      [ "${AGD_TELEMETRY:-}" != partial ] && [ "${AGD_TELEMETRY:-}" != status ] || exit 1
      ;;
    ps) printf '[{"Service":"collector","State":"running"}]\\n';;
  esac
  exit 0
fi
[ "$1" = ps ] && exit 0
exit 1
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


def run_install(root, tools, answers, *, fail_setup=False, path=None, telemetry='', on_output=None, shell=None):
    env = dict(os.environ, HOME=str(root / 'home'), PATH=path or str(tools), AGD_LOG=str(root / 'log'),
               AGD_FIXTURE=str(root / 'fixture'), AGD_TOOLS=str(tools), SHELL=shell or str(tools / 'sh'),
               AGD_FAIL_SETUP='1' if fail_setup else '', AGD_TELEMETRY=telemetry,
               AGD_SOURCE=str(ROOT), AGD_RESOURCE=str(root / 'resource'), AGD_INSTALL_HOME_ID=str(uuid.uuid4()),
               PYTHONPATH=f'{root / "site"}:{ROOT / "bin"}')
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
        if on_output:
            on_output(master, process, output)
    while select.select([master], [], [], 0)[0]:
        try:
            output.extend(os.read(master, 4096))
        except OSError:
            break
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
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
        result = run_install(root, tools, f'distro-{backend}\n{backend}\nn\n')
        assert result.returncode == 0, (result.stdout, result.stderr)
        log = (root / 'log').read_text().splitlines()
        init = f'agd --home {root}/distro-{backend} init --backend {backend}'
        setup = f'agd --home {root}/distro-{backend} installer setup --backend {backend}'
        assert log.index(init) < log.index(setup)
        assert any(line.startswith(f'shell cwd={root}/distro-{backend} ') for line in log)


def test_failed_owned_clone_with_partial_plugin_state_is_removed():
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
        result = run_install(root, tools, 'distro\ntmux\ny\nn\n')
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
        result = run_install(root, tools, 'distro\nherdr\nn\n')
        assert result.returncode == 0, result.stdout
        log = (root / 'log').read_text().splitlines()
        assert any(line.startswith(f'shell cwd={root}/distro ') for line in log)
        assert not any(line.startswith('omp ') for line in log)
        assert ('\x1b[' in result.stdout) is colored


@pytest.mark.parametrize('backend', ['tmux', 'herdr'])
@pytest.mark.parametrize('answer', ['\n', 'no\n', '\x04'])
def test_declined_telemetry_keeps_install_usable(tmp_path, backend, answer):
    fixture = make_fixture(tmp_path)
    tools = make_tools(tmp_path, fixture)
    result = run_install(tmp_path, tools, f'distro\n{backend}\n' + answer)
    assert result.returncode == 0, result.stdout
    assert (tmp_path / 'distro').is_dir()
    assert not (tmp_path / 'distro/data/observability').exists()
    assert not (tmp_path / 'resource').exists()
    assert 'shell cwd=' in (tmp_path / 'log').read_text()


def test_enabled_telemetry_applies_before_first_launch(tmp_path):
    from agd_lib import observability
    fixture = make_fixture(tmp_path)
    (fixture / '.omp').mkdir()
    (fixture / '.omp/settings.json').write_text('{"defaultModel":"preserve-me"}')
    tools = make_tools(tmp_path, fixture)
    result = run_install(tmp_path, tools, 'distro\nherdr\nYeS\n')
    assert result.returncode == 0, result.stdout
    home = tmp_path / 'distro'
    config = observability.read_config(home)
    assert config['mode'] == 'enabled'
    environment, generation, _ = observability.managed_environment(home, 'central', None, None)
    assert generation == '1'
    assert environment['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'] == f'http://127.0.0.1:{config["ports"]["otlp_http_port"]}/v1/traces'
    assert 'shell cwd=' in (tmp_path / 'log').read_text()
    assert (home / '.omp/settings.json').read_text() == '{"defaultModel":"preserve-me"}'
    secrets = json.loads((home / 'data/observability/secrets.json').read_text())
    assert secrets['secret_key'] not in result.stdout
    assert secrets['admin_password'] not in result.stdout


@pytest.mark.parametrize('failure', ['missing', 'daemon', 'partial', 'status'])
def test_failed_telemetry_preserves_home_and_recovery_state(tmp_path, failure):
    fixture = make_fixture(tmp_path)
    tools = make_tools(tmp_path, fixture)
    if failure == 'missing':
        (tools / 'docker').unlink()
    result = run_install(tmp_path, tools, 'distro\nherdr\ny\n', telemetry=failure)
    assert result.returncode == 0, result.stdout
    home = tmp_path / 'distro'
    assert home.is_dir()
    assert 'shell cwd=' in (tmp_path / 'log').read_text()
    assert 'incomplete' in result.stdout.lower()
    assert 'observability enable' in result.stdout
    assert 'observability disable' in result.stdout
    assert (home / 'state/observability/compose/compose.yaml').is_file()
    assert (home / 'data/observability/secrets.json').is_file()
    if failure in ('partial', 'status'):
        assert (tmp_path / 'resource').read_text() == 'allocated'
    assert 'Telemetry: disabled' not in result.stdout


def test_interrupted_telemetry_preserves_resource_owner(tmp_path):
    fixture = make_fixture(tmp_path)
    tools = make_tools(tmp_path, fixture)
    interrupted = False
    def interrupt(_master, process, output):
        nonlocal interrupted
        if not interrupted and (tmp_path / 'resource.pid').exists():
            interrupted = True
            os.killpg(process.pid, signal.SIGINT)
    result = run_install(tmp_path, tools, 'distro\nherdr\ny\n', telemetry='interrupt', on_output=interrupt)
    assert interrupted, result.stdout
    assert result.returncode != 0
    home = tmp_path / 'distro'
    assert (home / 'state/observability/compose/compose.yaml').is_file()
    assert (home / 'data/observability/secrets.json').is_file()
    assert (tmp_path / 'resource').is_file()
    child = Path('/proc') / (tmp_path / 'resource.pid').read_text() / 'stat'
    assert not child.exists() or child.read_text().rsplit(')', 1)[1].split()[0] == 'Z'


def test_telemetry_prompt_preserves_next_shell_command(tmp_path):
    root = tmp_path / 'parent with spaces'
    root.mkdir()
    fixture = make_fixture(root)
    tools = make_tools(root, fixture)
    result = run_install(root, tools, 'distro\nherdr\nn\nprintf marker > shell-marker\nexit\n', shell='/bin/bash')
    assert result.returncode == 0, result.stdout
    assert (root / 'distro/shell-marker').read_text() == 'marker'
