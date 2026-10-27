import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import lab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, store


INCARNATION = '00000000-0000-4000-8000-000000000001'


def test_paths_return_private_defaults_without_mutating_unconfigured_home():
    with lab() as (home, _):
        locations = observability.paths(home)
        config = observability.read_config(home)
        status = observability.read_status(home)
        expected_home_id = json.loads((home / 'data' / 'home.json').read_text())['home_id']

        assert locations['config'] == home / 'data' / 'observability' / 'config.json'
        assert locations['status'] == home / 'state' / 'observability' / 'status.json'
        assert locations['task_sessions'] == home / 'data' / 'observability' / 'tasks'
        assert config == {'schema': 1, 'home_id': expected_home_id, 'mode': 'disabled', 'generation': '0'}
        assert status == {'schema': 1, 'home_id': expected_home_id, 'desired': 'disabled', 'runtime': 'disabled', 'generation': '0'}
        assert not locations['config'].exists()
        assert not locations['status'].exists()


def test_uuid_incarnations_get_distinct_private_directories_and_preserve_binding_bytes():
    first_id = INCARNATION
    second_id = '00000000-0000-4000-8000-000000000002'
    with lab() as (home, _):
        first = observability.worker_binding(home, 'task-a', first_id)
        first_receipt = Path(first['session_dir']) / 'binding.json'
        before = first_receipt.read_bytes()
        second = observability.worker_binding(home, 'task-a', second_id)
        repeated = observability.worker_binding(home, 'task-a', first_id)

        assert first['session_dir'] != second['session_dir']
        assert Path(first['session_dir']).is_dir()
        assert first_receipt.is_file()
        assert repeated == first
        assert first_receipt.read_bytes() == before
        assert (Path(first['session_dir']).stat().st_mode & 0o077) == 0


@pytest.mark.parametrize('task_id, incarnation', [
    ('../escape', INCARNATION),
    ('task-a', '../escape'),
    ('task-a', '00000000-0000-4000-8000-000000000001/child'),
    ('task-a', 'not-a-uuid'),
])
def test_binding_rejects_unsafe_task_and_incarnation_paths(task_id, incarnation):
    with lab() as (home, _):
        with pytest.raises((ValueError, store.Refusal)):
            observability.worker_binding(home, task_id, incarnation)


def test_disabled_environment_removes_only_ambient_otel_controls(monkeypatch):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://outside.invalid')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS', 'authorization=secret')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
    monkeypatch.setenv('OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT', 'true')
    monkeypatch.setenv('OTEL_TRACES_EXPORTER', 'otlp')
    monkeypatch.setenv('OTEL_LOGS_EXPORTER', 'otlp')
    monkeypatch.setenv('OTEL_METRICS_EXPORTER', 'otlp')
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES', 'secret=outside')
    monkeypatch.setenv('OTEL_SERVICE_NAME', 'outside')
    monkeypatch.setenv('CONTENT_ROOT', '/safe/content')
    monkeypatch.setenv('APP_HEADER', 'preserve-me')
    monkeypatch.setenv('PRESERVE_ME', 'yes')
    with lab() as (home, _):
        env, generation, warning = observability.managed_environment(
            home, 'worker', {'id': 'task-a'}, INCARNATION)
        observed = json.loads(subprocess.check_output(
            [sys.executable, '-c', 'import json, os; print(json.dumps(dict(os.environ)))'],
            env=env, text=True))

    assert generation == '0'
    assert warning is None
    assert observed['PRESERVE_ME'] == 'yes'
    assert observed['CONTENT_ROOT'] == '/safe/content'
    assert observed['APP_HEADER'] == 'preserve-me'
    assert not any(key.startswith('OTEL_EXPORTER_') or key in {
        'OTEL_TRACES_EXPORTER', 'OTEL_LOGS_EXPORTER', 'OTEL_METRICS_EXPORTER',
        'OTEL_RESOURCE_ATTRIBUTES', 'OTEL_SERVICE_NAME',
    } or (key.startswith('OTEL_') and (
        'PROTOCOL' in key or 'HEADER' in key or 'CONTENT' in key)) for key in observed)
