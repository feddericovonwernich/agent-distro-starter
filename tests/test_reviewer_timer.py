import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer, reviewer_runner, reviewer_timer, store


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def home(tmp_path):
    root = tmp_path / 'home'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'home.json').write_text('{"schema":1,"home_id":"123e4567-e89b-12d3-a456-426614174000"}\n')
    return root


def test_interval_hour_anchor_catches_up_once():
    schedule = {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 12}
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


def test_rendered_interval_units_are_hourly_private_and_escaped(tmp_path):
    config = {'schedule': {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 12},
              'limits': {'max_runtime_seconds': 321, 'max_candidates': 1, 'max_input_bytes': 1, 'max_model_calls': 1, 'max_output_bytes': 1}}
    rendered = reviewer_timer.render_units(home(tmp_path), config, source_cli='/opt/a b/agd%run')
    assert 'OnCalendar=*-*-* *:00:00 UTC' in rendered['timer']
    assert 'Persistent=true' in rendered['timer']
    assert 'Type=oneshot' in rendered['service']
    assert 'TimeoutStartSec=321' in rendered['service']
    assert '/opt/a\\x20b/agd%%run' in rendered['service']
    assert '--timer' in rendered['service']


def test_refuses_symlink_and_foreign_unit_before_command(tmp_path):
    root, xdg = home(tmp_path), tmp_path / 'xdg'
    units = xdg / 'systemd' / 'user'
    units.mkdir(parents=True)
    foreign = units / reviewer_timer.unit_name(root)
    foreign.write_text('[Unit]\nDescription=foreign\n')
    calls = []
    with pytest.raises(store.Refusal):
        reviewer_timer.enable(root, reviewer_timer.default_config(), xdg_config_home=xdg,
                              command_runner=lambda argv: calls.append(argv))
    assert calls == []
    foreign.unlink()
    (units / 'systemd-link').symlink_to('/tmp')
    with pytest.raises(store.Refusal):
        reviewer_timer.enable(root, reviewer_timer.default_config(), xdg_config_home=units / 'systemd-link',
                              command_runner=lambda argv: calls.append(argv))
    assert calls == []


def test_enable_releases_store_lock_before_runner_and_is_idempotent(tmp_path):
    root, xdg = home(tmp_path), tmp_path / 'xdg'
    calls = []
    def runner(argv):
        lock = root / 'state' / 'mutation.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a+') as stream:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(argv)
    config = {'schedule': {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 12},
              'limits': dict(reviewer.DEFAULT_LIMITS)}
    first = reviewer_timer.enable(root, config, xdg_config_home=xdg, command_runner=runner, now=13_001)
    second = reviewer_timer.enable(root, config, xdg_config_home=xdg, command_runner=runner, now=99_999)
    assert first['anchor'] == second['anchor'] == 10_800
    assert len(calls) == 3
    assert all(command[:2] == ['systemctl', '--user'] for command in calls)


def test_timer_runner_skips_collection_until_interval_is_due(tmp_path, monkeypatch):
    root, xdg = home(tmp_path), tmp_path / 'xdg'
    config = {'schedule': {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 2},
              'limits': dict(reviewer.DEFAULT_LIMITS)}
    reviewer_timer.enable(root, config, xdg_config_home=xdg, command_runner=lambda _: None, now=3_601)
    monkeypatch.setattr(reviewer_runner, 'launch', lambda *_args, **_kwargs: pytest.fail('collection launched while ineligible'))
    assert reviewer_runner.launch_timer(root, observed_omp_version='test', now=3_602) is None
