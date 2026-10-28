"""Explicitly managed reviewer timer scheduling; it never activates itself."""
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import reviewer, store


SCHEDULE_KEYS = {'mode', 'timezone', 'calendar', 'interval_hours'}


def default_config():
    return {'schedule': dict(reviewer.DEFAULT_CONFIG['schedule']), 'limits': dict(reviewer.DEFAULT_LIMITS)}


def validate_schedule(schedule):
    return reviewer.validate_schedule(schedule)


def _native_run(argv):
    return subprocess.run(argv, env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'}, timeout=10,
                          check=True, capture_output=True, text=True)


def native_next_calendar(schedule, base, *, command_runner=_native_run):
    schedule = validate_schedule(schedule)
    if schedule['mode'] != 'calendar':
        raise store.Refusal('native calendar needs calendar schedule')
    base_text = datetime.fromtimestamp(base, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    expression = f"*-*-* {schedule['calendar']}:00 {schedule['timezone']}"
    try:
        output = command_runner(['systemd-analyze', 'calendar', expression, '--base-time', base_text, '--iterations=1']).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise store.Refusal('native calendar unavailable') from error
    matched = re.search(r'^[ \t]*(?:\(in UTC\)|Next elapse):[ \t]*(.+ UTC)[ \t]*$', output, re.MULTILINE)
    if not matched:
        raise store.Refusal('native calendar unavailable')
    try:
        return datetime.strptime(matched.group(1), '%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc).timestamp()
    except ValueError as error:
        raise store.Refusal('native calendar invalid') from error


def _zoneinfo_next(schedule, base):
    zone = ZoneInfo(schedule['timezone'])
    local_base = datetime.fromtimestamp(base, timezone.utc).astimezone(zone)
    hour, minute = map(int, schedule['calendar'].split(':'))
    for offset in range(0, 367):
        naive = datetime.combine(local_base.date() + timedelta(days=offset), datetime.min.time()).replace(hour=hour, minute=minute)
        candidate = naive.replace(tzinfo=zone, fold=0)
        # A round trip rejects spring-forward wall times. fold=0 intentionally runs only once.
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
            continue
        timestamp = candidate.timestamp()
        if timestamp > base:
            return timestamp
    raise store.Refusal('calendar horizon unavailable')


def native_calendar_agrees(schedule, base):
    expected = _zoneinfo_next(schedule, base)
    actual = native_next_calendar(schedule, base)
    if actual != expected:
        raise store.Refusal('native calendar semantics disagree with IANA timezone')
    return actual


def calculate_due(schedule, checkpoint, now_utc):
    schedule = validate_schedule(schedule)
    if not isinstance(checkpoint, dict) or not isinstance(now_utc, (int, float)):
        raise store.Refusal('timer checkpoint')
    if schedule['mode'] == 'manual-only':
        return {'due_at': None, 'next_due': None, 'checkpoint': checkpoint}
    if schedule['mode'] == 'interval':
        anchor, last = checkpoint.get('anchor'), checkpoint.get('last_due')
        if not isinstance(anchor, (int, float)) or not isinstance(last, (int, float)) or last < anchor:
            raise store.Refusal('interval checkpoint')
        step = schedule['interval_hours'] * 3600
        latest = anchor + max(0, int((now_utc - anchor) // step)) * step
        return {'due_at': last + step if latest > last else None, 'next_due': latest + step,
                'checkpoint': {'anchor': float(anchor), 'last_due': max(float(last), float(latest))}}
    due = checkpoint.get('next_due')
    if not isinstance(due, (int, float)):
        raise store.Refusal('calendar checkpoint')
    if now_utc < due:
        return {'due_at': None, 'next_due': due, 'checkpoint': checkpoint}
    next_due = native_next_calendar(schedule, now_utc)
    return {'due_at': due, 'next_due': next_due, 'checkpoint': {'next_due': next_due}}


def _home_id(home):
    path = store.home(home) / 'data' / 'home.json'
    try:
        value = json.loads(path.read_text())
        return str(uuid.UUID(value['home_id']))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise store.Refusal('home identity is unavailable') from error


def unit_name(home):
    return f"agd-reviewer-{_home_id(home).replace('-', '')}.timer"


def _identity(home):
    return _home_id(home), os.getuid()


def _escape(value):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise store.Refusal('timer executable must be absolute')
    return value.replace('%', '%%').replace('\\', '\\\\').replace(' ', '\\x20').replace('\t', '\\x09').replace('\n', '\\x0a')


def render_units(home, config, *, source_cli=None):
    if not isinstance(config, dict) or not isinstance(config.get('limits'), dict):
        raise store.Refusal('timer config')
    schedule = validate_schedule(config.get('schedule'))
    runtime = config['limits'].get('max_runtime_seconds')
    if type(runtime) is not int or runtime <= 0:
        raise store.Refusal('timer runtime')
    home_id, uid = _identity(home)
    timer = unit_name(home)
    service = timer[:-6] + '.service'
    source_cli = str(Path(source_cli or Path(__file__).resolve().parents[1] / 'agd').resolve())
    marker = f'X-AGD-Reviewer-Owner={home_id}:{uid}'
    calendar = '*-*-* *:00:00 UTC' if schedule['mode'] == 'interval' else f"*-*-* {schedule['calendar']}:00 {schedule['timezone']}"
    if schedule['mode'] == 'manual-only':
        raise store.Refusal('manual-only has no timer unit')
    return {'timer_name': timer, 'service_name': service,
            'timer': f"[Unit]\nDescription=AGD reviewer timer\n{marker}\n\n[Timer]\nOnCalendar={calendar}\nPersistent=true\nUnit={service}\n\n[Install]\nWantedBy=timers.target\n",
            'service': f"[Unit]\nDescription=AGD reviewer run\n{marker}\n\n[Service]\nType=oneshot\nTimeoutStartSec={runtime}\nExecStart={_escape(source_cli)} --home={_escape(str(store.home(home)))} reviewer run --timer\n"}


def _safe_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise store.Refusal('XDG_CONFIG_HOME must be absolute')
    chain = []
    current = path
    while not current.exists() and not current.is_symlink():
        chain.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise store.Refusal('unit directory is unsafe')
    for item in reversed(chain):
        item.mkdir(mode=0o700)
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            mode = os.lstat(item).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise store.Refusal('unit directory is unsafe')
        if item == current:
            break
    path.chmod(0o700)
    return path


def _unit_directory(xdg_config_home):
    base = _safe_directory(Path(xdg_config_home or os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'))
    return _safe_directory(base / 'systemd' / 'user')


def _owned(path, marker):
    try:
        mode = os.lstat(path).st_mode
        return stat.S_ISREG(mode) and not stat.S_ISLNK(mode) and marker in path.read_text().splitlines()
    except OSError:
        return False


def _write_unit(path, content, marker):
    if path.exists() or path.is_symlink():
        if not _owned(path, marker):
            raise store.Refusal('existing unit is not owned by this home')
        if path.read_text() == content:
            return False
    fd, temporary = tempfile.mkstemp(prefix='.agd-reviewer-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _timer_record(home):
    return reviewer.paths(home)['checkpoints'] / 'timer.json'


def _read_record(home):
    path = _timer_record(home)
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file(): raise ValueError('unsafe')
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise store.Refusal('timer receipt is invalid') from error


def enable(home, config, *, xdg_config_home=None, command_runner=store.run, source_cli=None, now=None):
    root = store.home(home); schedule = validate_schedule(config.get('schedule'))
    now = time.time() if now is None else float(now)
    if schedule['mode'] == 'manual-only':
        raise store.Refusal('manual-only schedule cannot enable a timer')
    agreed_next = native_calendar_agrees(schedule, now) if schedule['mode'] == 'calendar' else None
    rendered = render_units(root, config, source_cli=source_cli)
    marker = f'X-AGD-Reviewer-Owner={_home_id(root)}:{os.getuid()}'
    with store.lock(root):
        previous = _read_record(root)
        unchanged = previous and previous.get('schedule') == schedule and previous.get('state') == 'enabled'
        anchor = previous.get('anchor') if unchanged and schedule['mode'] == 'interval' else float(int(now // 3600) * 3600)
        record = {'schema': 1, 'home_id': _home_id(root), 'uid': os.getuid(), 'schedule': schedule,
                  'anchor': anchor if schedule['mode'] == 'interval' else None, 'state': 'enabling'}
        reviewer._private(_timer_record(root).parent); store.atomic_json(_timer_record(root), record)
    next_due = agreed_next
    try:
        directory = _unit_directory(xdg_config_home)
        changed = _write_unit(directory / rendered['timer_name'], rendered['timer'], marker)
        changed = _write_unit(directory / rendered['service_name'], rendered['service'], marker) or changed
        if not unchanged or changed:
            command_runner(['systemctl', '--user', 'daemon-reload'])
            command_runner(['systemctl', '--user', 'enable', rendered['timer_name']])
            command_runner(['systemctl', '--user', 'start', rendered['timer_name']])
    except Exception as error:
        with store.lock(root):
            store.atomic_json(_timer_record(root), {**record, 'state': 'failed', 'error': str(error)})
        raise
    with store.lock(root):
        final = {**record, 'state': 'enabled',
                 'checkpoint': {'next_due': next_due} if next_due is not None else {'anchor': anchor, 'last_due': anchor}}
        store.atomic_json(_timer_record(root), final)
    return final


def disable(home, *, xdg_config_home=None, command_runner=store.run):
    root = store.home(home); name = unit_name(root); service = name[:-6] + '.service'; marker = f'X-AGD-Reviewer-Owner={_home_id(root)}:{os.getuid()}'
    directory = _unit_directory(xdg_config_home)
    owned = [directory / item for item in (name, service) if (directory / item).exists()]
    if any(not _owned(path, marker) for path in owned):
        raise store.Refusal('existing unit is not owned by this home')
    command_runner(['systemctl', '--user', 'disable', '--now', name])
    for path in owned: path.unlink()
    command_runner(['systemctl', '--user', 'daemon-reload'])
    with store.lock(root):
        record = _read_record(root) or {'schema': 1, 'home_id': _home_id(root), 'uid': os.getuid()}
        store.atomic_json(_timer_record(root), {**record, 'state': 'disabled'})


def timer_tick(home, *, now=None):
    """Record one eligible timer intent; callers launch it after this returns."""
    root = store.home(home); now = time.time() if now is None else float(now)
    with store.lock(root):
        record = _read_record(root)
        if not record or record.get('state') != 'enabled':
            raise store.Refusal('reviewer timer is not enabled')
        due = calculate_due(record['schedule'], record['checkpoint'], now)
        if due['due_at'] is None:
            return None
        store.atomic_json(_timer_record(root), {**record, 'checkpoint': due['checkpoint']})
    return reviewer.enqueue_intent(root, kind='timer')
