import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin' / 'agd'


def command(home, *args, expected=0):
    result = subprocess.run([str(CLI), '--home', str(home), *args],
                            env=dict(os.environ, AGD_HOME=str(home)), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result


def test_init_writes_safe_selected_config(tmp_path):
    home = tmp_path / 'h'
    command(home, 'init', '--backend', 'herdr')
    got = json.loads((home / 'data' / 'launcher.json').read_text())
    assert got['schema'] == 1 and got['backend'] == 'herdr'
    assert re.fullmatch(r'agd-[0-9a-f]{32}', got['herdr_session'])


def test_init_writes_herdr_session_for_tmux(tmp_path):
    home = tmp_path / 'h'
    command(home, 'init', '--backend', 'tmux')
    got = json.loads((home / 'data' / 'launcher.json').read_text())
    assert got['backend'] == 'tmux'
    assert re.fullmatch(r'agd-[0-9a-f]{32}', got['herdr_session'])


def test_init_refuses_malformed_launcher_config_without_replacement(tmp_path):
    home = tmp_path / 'h'
    config = home / 'data' / 'launcher.json'
    config.parent.mkdir(parents=True)
    config.write_text('{not json')
    command(home, 'init', '--backend', 'tmux', expected=3)
    assert config.read_text() == '{not json'


def test_init_refuses_symlinked_launcher_config_without_replacement(tmp_path):
    home = tmp_path / 'h'
    target = tmp_path / 'target.json'
    target.write_text('unchanged')
    config = home / 'data' / 'launcher.json'
    config.parent.mkdir(parents=True)
    config.symlink_to(target)
    command(home, 'init', '--backend', 'tmux', expected=3)
    assert config.is_symlink()
    assert target.read_text() == 'unchanged'


def test_init_refuses_nonfresh_home_without_replacement(tmp_path):
    home = tmp_path / 'h'
    marker = home / 'existing'
    home.mkdir()
    marker.write_text('keep')
    command(home, 'init', '--backend', 'tmux', expected=3)
    assert marker.read_text() == 'keep'
