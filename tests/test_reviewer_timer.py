import os
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import omp_runtime, reviewer, reviewer_runner, reviewer_timer, store


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def home(tmp_path):
    root = tmp_path / 'home'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'home.json').write_text('{"schema":1,"home_id":"123e4567-e89b-12d3-a456-426614174000"}\n')
    omp_runtime.agent_dir(root).mkdir(parents=True)
    return root


def interval_config(hours=12):
    return {'schedule': {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': hours},
            'limits': dict(reviewer.DEFAULT_LIMITS)}


def test_interval_hour_anchor_catches_up_once():
    schedule = interval_config()['schedule']
    assert reviewer_timer.calculate_due(schedule, {'anchor': 0., 'last_due': 0.}, 36 * 3600 + 1) == {
        'due_at': 12 * 3600, 'next_due': 48 * 3600,
        'checkpoint': {'anchor': 0., 'last_due': 36 * 3600}}


def test_calendar_gap_and_fold_follow_native_semantics():
    schedule = {'mode': 'calendar', 'timezone': 'Europe/Berlin', 'calendar': '02:00', 'interval_hours': None}
    assert reviewer_timer.native_next_calendar(schedule, utc('2027-03-28T00:59Z')) == utc('2027-03-29T00:00Z')
    assert reviewer_timer.native_next_calendar(schedule, utc('2027-10-30T00:00Z')) == utc('2027-10-31T00:00Z')
    assert reviewer_timer.native_next_calendar(schedule, utc('2027-10-31T00:15Z')) == utc('2027-11-01T01:00Z')


def test_manual_schedule_never_changes_checkpoint():
    checkpoint = {'anchor': 12.0, 'last_due': 12.0}
    schedule = {'mode': 'manual-only', 'timezone': None, 'calendar': None, 'interval_hours': None}
    assert reviewer_timer.calculate_due(schedule, checkpoint, 100.0) == {'due_at': None, 'next_due': None, 'checkpoint': checkpoint}




def test_refuses_symlink_and_foreign_unit_before_command(tmp_path):
    root, xdg = home(tmp_path), tmp_path / 'xdg'; units = xdg / 'systemd' / 'user'; units.mkdir(parents=True)
    foreign = units / reviewer_timer.unit_name(root); foreign.write_text('[Unit]\nDescription=foreign\n')
    calls = []
    with pytest.raises(store.Refusal): reviewer_timer.enable(root, reviewer_timer.default_config(), xdg_config_home=xdg, command_runner=calls.append)
    assert calls == []
    foreign.unlink(); (units / 'systemd-link').symlink_to('/tmp')
    with pytest.raises(store.Refusal): reviewer_timer.enable(root, reviewer_timer.default_config(), xdg_config_home=units / 'systemd-link', command_runner=calls.append)
    assert calls == []


def test_enable_releases_store_lock_and_preserves_progress(tmp_path):
    root, xdg, calls = home(tmp_path), tmp_path / 'xdg', []
    def runner(argv):
        lock = root / 'state' / 'mutation.lock'; lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a+') as stream:
            import fcntl; fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(argv)
    config = interval_config()
    first = reviewer_timer.enable(root, config, xdg_config_home=xdg, command_runner=runner, now=13_001)
    reviewer_timer.timer_tick(root, now=54_001)
    second = reviewer_timer.enable(root, config, xdg_config_home=xdg, command_runner=runner, now=99_999)
    assert first['anchor'] == second['anchor'] == 10_800
    assert second['checkpoint']['last_due'] == 54_000


def test_due_tick_writes_pending_intent_before_checkpoint_and_recovers(tmp_path, monkeypatch):
    root, xdg = home(tmp_path), tmp_path / 'xdg'; reviewer_timer.enable(root, interval_config(), xdg_config_home=xdg, command_runner=lambda _: None, now=1)
    original = store.atomic_json
    def fail_checkpoint(path, value):
        if Path(path) == reviewer_timer._timer_record(root): raise OSError('interrupted')
        return original(path, value)
    monkeypatch.setattr(store, 'atomic_json', fail_checkpoint)
    with pytest.raises(OSError): reviewer_timer.timer_tick(root, now=43_201)
    monkeypatch.setattr(store, 'atomic_json', original)
    assert len(list(reviewer.paths(root)['pending'].glob('*.json'))) == 1
    assert reviewer_timer.timer_tick(root, now=43_201)['intent_id']
    assert reviewer_timer._read_record(root)['checkpoint']['last_due'] == 43_200


def test_disable_is_idempotent_and_recovers_after_reload_failure(tmp_path):
    root, xdg, calls = home(tmp_path), tmp_path / 'xdg', []
    reviewer_timer.enable(root, interval_config(), xdg_config_home=xdg, command_runner=lambda _: None, now=1)
    def failing(argv):
        calls.append(argv)
        if argv[-1] == 'daemon-reload': raise OSError('reload')
    with pytest.raises(OSError): reviewer_timer.disable(root, xdg_config_home=xdg, command_runner=failing)
    assert reviewer_timer._read_record(root)['state'] == 'disabling'
    reviewer_timer.disable(root, xdg_config_home=xdg, command_runner=calls.append)
    reviewer_timer.disable(root, xdg_config_home=xdg, command_runner=calls.append)
    assert reviewer_timer._read_record(root)['state'] == 'disabled'
    assert not list((xdg / 'systemd/user').glob('agd-reviewer-*'))


def test_precise_fold_bracket_refuses_second_native_occurrence(tmp_path, monkeypatch):
    root, xdg, calls = home(tmp_path), tmp_path / 'xdg', []
    schedule = {'mode': 'calendar', 'timezone': 'Europe/Berlin', 'calendar': '02:00', 'interval_hours': None}
    between = utc('2027-10-31T00:30Z')
    assert between in reviewer_timer._transition_bases(schedule, utc('2027-01-01T00:00Z'))
    real = reviewer_timer.native_next_calendar
    monkeypatch.setattr(reviewer_timer, 'native_next_calendar',
                        lambda value, point: utc('2027-10-31T01:00Z') if point == between else reviewer_timer._zoneinfo_next(value, point))
    with pytest.raises(store.Refusal):
        reviewer_timer.enable(root, {'schedule': schedule, 'limits': dict(reviewer.DEFAULT_LIMITS)},
                              xdg_config_home=xdg, command_runner=calls.append, now=utc('2027-01-01T00:00Z'))
    assert not xdg.exists() and calls == []


def test_timer_runner_skips_collection_until_interval_is_due(tmp_path, monkeypatch):
    root, xdg = home(tmp_path), tmp_path / 'xdg'; reviewer_timer.enable(root, interval_config(2), xdg_config_home=xdg, command_runner=lambda _: None, now=3_601)
    monkeypatch.setattr(reviewer_runner, 'run_native_reviewer', lambda *_args, **_kwargs: pytest.fail('collection launched while ineligible'))
    assert reviewer_runner.launch_timer(root, now=3_602) is None



def test_refuses_foreign_owned_xdg_root(tmp_path, monkeypatch):
    root, xdg = home(tmp_path), tmp_path / 'xdg'; xdg.mkdir()
    actual = reviewer_timer.os.lstat
    def foreign(path):
        result = actual(path)
        if Path(path) == xdg:
            return type('Stat', (), {'st_mode': result.st_mode, 'st_uid': os.getuid() + 1})()
        return result
    monkeypatch.setattr(reviewer_timer.os, 'lstat', foreign)
    with pytest.raises(store.Refusal):
        reviewer_timer.enable(root, interval_config(), xdg_config_home=xdg, command_runner=lambda _: None)


@pytest.mark.parametrize('missing', ['omp', 'bun', 'agent-dir'])
def test_enable_refuses_unusable_runtime_before_unit_activation(tmp_path, monkeypatch, missing):
    root, xdg = home(tmp_path), tmp_path / 'xdg'
    actual = shutil.which
    if missing == 'agent-dir':
        omp_runtime.agent_dir(root).rmdir()
    else:
        monkeypatch.setattr(shutil, 'which', lambda name, *args, **kwargs:
                            None if name == missing else actual(name, *args, **kwargs))
    with pytest.raises((store.Refusal, store.DependencyError)):
        reviewer_timer.enable(root, interval_config(), xdg_config_home=xdg, command_runner=lambda _: None)
    assert not list(xdg.glob('systemd/user/*.timer'))
    assert reviewer_timer._read_record(root) is None


def test_runtime_binding_survives_minimal_environment_and_special_paths(tmp_path, monkeypatch):
    root = home(tmp_path)
    directory = tmp_path / "runtime % 'quoted' \"tools\""
    directory.mkdir()
    for name in ('omp', 'bun'):
        (directory / name).symlink_to(shutil.which(name))
    monkeypatch.setenv('PATH', f'{directory}:{os.environ["PATH"]}')
    runtime = reviewer_timer.runtime_environment(root)
    result = subprocess.run(
        [runtime['python'], '-c',
         'import json,shutil;print(json.dumps({name:shutil.which(name) for name in ("omp","bun")}))'],
        cwd='/', env={'PATH': runtime['path']}, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'omp': str(directory / 'omp'), 'bun': str(directory / 'bun')}


def test_native_activation_failure_retains_owned_disable_recovery(tmp_path):
    root, xdg = home(tmp_path), tmp_path / 'xdg'
    def inactive(argv):
        if 'is-active' in argv:
            raise store.DependencyError('timer remained inactive')
    with pytest.raises(store.DependencyError, match='inactive'):
        reviewer_timer.enable(root, interval_config(), xdg_config_home=xdg, command_runner=inactive)
    assert reviewer_timer._read_record(root)['state'] == 'failed'
    assert list((xdg / 'systemd/user').glob('*.timer'))
    reviewer_timer.disable(root, xdg_config_home=xdg, command_runner=lambda _: None)
    assert reviewer_timer._read_record(root)['state'] == 'disabled'
    assert not list((xdg / 'systemd/user').glob('agd-reviewer-*'))


def test_native_service_discards_inherited_credentials(tmp_path):
    try:
        manager = subprocess.run(['systemctl', '--user', 'show', '--property=Version', '--value'],
                                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip('native user-systemd manager unavailable')
    if manager.returncode:
        pytest.skip('native user-systemd manager unavailable')
    root = home(tmp_path)
    identity = str(uuid.uuid4())
    store.atomic_json(root / 'data/home.json', {'schema': 1, 'home_id': identity})
    marker = root / 'environment.json'
    helper = tmp_path / 'environment-probe.py'
    helper.write_text(
        'import json,os\nfrom pathlib import Path\n'
        f'Path({str(marker)!r}).write_text(json.dumps({{key:os.environ.get(key) for key in '
        '("AGD_CREDENTIAL_SENTINEL","OTEL_EXPORTER_OTLP_HEADERS","HOME","PATH")}))\n')
    rendered = reviewer_timer.render_units(
        root, interval_config(), runtime={'python': sys.executable, 'path': os.defpath}, source_cli=str(helper))
    content = rendered['service'].replace(
        '[Service]\n', '[Service]\nEnvironment=AGD_CREDENTIAL_SENTINEL=synthetic-secret\n'
        'Environment=OTEL_EXPORTER_OTLP_HEADERS=synthetic-header\n')
    directory = reviewer_timer._unit_directory(Path.home() / '.config')
    service = directory / rendered['service_name']
    owner = f'X-AGD-Reviewer-Owner={identity}:{os.getuid()}'
    assert not service.exists()
    reviewer_timer._write_unit(service, content, owner)
    try:
        store.run(['systemctl', '--user', 'daemon-reload'])
        store.run(['systemctl', '--user', 'start', '--wait', service.name])
        observed = json.loads(marker.read_text())
        assert observed['AGD_CREDENTIAL_SENTINEL'] is None
        assert observed['OTEL_EXPORTER_OTLP_HEADERS'] is None
        assert observed['HOME'] == str(Path.home())
        assert observed['PATH'] == os.defpath
    finally:
        store.run(['systemctl', '--user', 'stop', service.name])
        assert reviewer_timer._owned(service, owner)
        service.unlink()
        store.run(['systemctl', '--user', 'daemon-reload'])


def test_uninstall_inventory_unconfigured_features_is_read_only(monkeypatch):
    from agd_lib import observability
    from support import lab
    with lab() as (root, _):
        before = {str(p.relative_to(root)): (p.lstat().st_ino, p.lstat().st_mtime_ns, p.lstat().st_mode)
                  for p in root.rglob('*')}
        monkeypatch.setattr(store, 'run', lambda *_a, **_k: pytest.fail('unconfigured inventory invoked a runtime'))
        monkeypatch.setattr(observability, '_ownership_command',
                            lambda *_a: pytest.fail('unconfigured inventory invoked Docker'))
        assert reviewer_timer.uninstall_inventory(root) == []
        assert observability.uninstall_inventory(root) == []
        assert before == {str(p.relative_to(root)): (p.lstat().st_ino, p.lstat().st_mtime_ns, p.lstat().st_mode)
                          for p in root.rglob('*')}


@pytest.mark.parametrize('foreign', ['marker', 'home', 'symlink'])
def test_uninstall_refuses_foreign_fragment_or_service_home(tmp_path, monkeypatch, foreign):
    root = home(tmp_path)
    directory = tmp_path / 'xdg/systemd/user'
    directory.mkdir(parents=True)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    rendered = reviewer_timer.render_units(root, interval_config(),
                                          runtime={'python': sys.executable, 'path': os.environ['PATH']})
    path = directory / rendered['service_name']
    content = rendered['service']
    if foreign == 'marker':
        content = content.replace('X-AGD-Reviewer-Owner=', 'X-Other-Owner=')
    elif foreign == 'home':
        content = content.replace(reviewer_timer._escape(str(root)), reviewer_timer._escape(str(tmp_path / 'other')))
    if foreign == 'symlink':
        target = tmp_path / 'outside.service'
        target.write_text(content)
        path.symlink_to(target)
    else:
        path.write_text(content)
    before = path.read_bytes()
    monkeypatch.setattr(store, 'run', lambda *_a, **_k: pytest.fail('foreign fragment invoked native control'))
    resources = reviewer_timer.uninstall_inventory(root)
    assert resources and any(row['blockers'] and row['ownership'] in {'unknown', 'foreign'} for row in resources)
    assert path.read_bytes() == before
    assert path.is_symlink() == (foreign == 'symlink')


def test_active_reviewer_service_is_stopped_not_only_timer_disabled(tmp_path):
    import time
    from support import lab, uninstall_scope
    manager = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, text=True)
    if manager.stdout.strip() not in {'running', 'degraded'}:
        pytest.skip(f'user manager unavailable: {manager.stdout.strip()}')
    directory = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    fixture = tmp_path / 'reviewer-fixture.py'
    fixture.write_text('''import fcntl, os, signal, sys
from pathlib import Path
home = Path(next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--home=")))
lock = home / "state/reviewer/run.lock"
lock.parent.mkdir(parents=True, exist_ok=True)
with lock.open("a+") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (home / "service-ready").write_text(str(os.getpid()))
    signal.pause()
''')
    units = []
    with lab() as (root, _), lab() as (other, _):
        try:
            for target in (root, other):
                rendered = reviewer_timer.render_units(
                    target, interval_config(), runtime={'python': sys.executable, 'path': os.environ['PATH']},
                    source_cli=fixture)
                for kind in ('timer', 'service'):
                    path = directory / rendered[f'{kind}_name']
                    assert not os.path.lexists(path)
                    content = rendered[kind]
                    if kind == 'timer':
                        content = content.replace('OnCalendar=*-*-* *:00:00 UTC',
                                                  'OnCalendar=2099-01-01 00:00:00 UTC')
                    path.write_text(content); path.chmod(0o600)
                    units.append(path)
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            for target in (root, other):
                timer = reviewer_timer.unit_name(target)
                subprocess.run(['systemctl', '--user', 'enable', '--now', timer], check=True, capture_output=True)
                subprocess.run(['systemctl', '--user', 'start', '--no-block', timer[:-6] + '.service'], check=True)
            deadline = time.monotonic() + 10
            while not all((target / 'service-ready').exists() for target in (root, other)):
                assert time.monotonic() < deadline, 'native services did not acquire their leases'
                time.sleep(.02)
            other_pid = int((other / 'service-ready').read_text())
            resources = reviewer_timer.uninstall_inventory(root)
            assert resources and all(row['ownership'] == 'owned' and not row['blockers'] for row in resources)
            service = resources[0]['identity']
            lease = reviewer_runner._uninstall_lease(root)
            assert service['run_lock'] == lease['identity']
            assert any(item['pid'] in lease['holders'] for item in service['processes'])
            with uninstall_scope(root, resources) as operation:
                reviewer_timer.uninstall_quiesce(root, resources, operation)
                assert reviewer_runner._uninstall_lease(root)['holders'] == []
                reviewer_timer.uninstall_remove(root, resources, operation)
                assert reviewer_timer.uninstall_inventory(root, expected=resources)[0]['presence'] == 'absent'
            assert all(not os.path.lexists(path) for path in units[:2])
            assert all(path.is_file() for path in units[2:])
            os.kill(other_pid, 0)
            assert reviewer_runner._uninstall_lease(other)['holders'] == [other_pid]
            assert not os.path.lexists(directory / 'timers.target.wants' / reviewer_timer.unit_name(root))
        finally:
            for target in (root, other):
                timer = reviewer_timer.unit_name(target)
                subprocess.run(['systemctl', '--user', 'disable', '--now', timer], capture_output=True)
                subprocess.run(['systemctl', '--user', 'stop', timer[:-6] + '.service'], capture_output=True)
                subprocess.run(['systemctl', '--user', 'clean', '--what=state', timer], capture_output=True)
            for path in units:
                if path.exists():
                    path.unlink()
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)