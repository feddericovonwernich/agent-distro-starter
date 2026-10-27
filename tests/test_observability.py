import json
import os
import socket
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


def test_rendered_stack_keeps_only_browser_collector_and_media_ports_on_loopback():
    with lab() as (home, _):
        lock = {
            'schema': 1,
            'images': {
                'collector': 'example.invalid/collector@sha256:' + '0' * 64,
                'langfuse': 'example.invalid/langfuse@sha256:' + '1' * 64,
                'langfuse_worker': 'example.invalid/worker@sha256:' + '2' * 64,
                'postgres': 'example.invalid/postgres@sha256:' + '3' * 64,
                'clickhouse': 'example.invalid/clickhouse@sha256:' + '4' * 64,
                'minio': 'example.invalid/minio@sha256:' + '5' * 64,
                'redis': 'example.invalid/redis@sha256:' + '6' * 64,
            },
        }
        rendered = observability.render_stack(home, lock)
        model = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(rendered['compose']), 'config', '--format', 'json'],
            text=True, stderr=subprocess.DEVNULL))

    published = {name: service['ports'] for name, service in model['services'].items()
                 if service.get('ports')}
    assert set(published) <= {'langfuse-web', 'collector', 'minio'}
    assert all(port['host_ip'] == '127.0.0.1'
               for ports in published.values() for port in ports)


def test_control_failure_is_sanitized_not_child_output():
    assert observability.run_control(['missing-command'], 'start') == {
        'stage': 'start', 'category': 'missing-executable', 'exit_code': None,
        'message': 'start failed: executable unavailable'}


def test_rendered_langfuse_environment_has_required_migration_and_bucket_settings():
    with lab() as (home, _):
        rendered = observability.render_stack(home)
        model = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(rendered['compose']), 'config', '--format', 'json'],
            text=True, stderr=subprocess.DEVNULL))

    environment = model['services']['langfuse-worker']['environment']
    assert environment['CLICKHOUSE_MIGRATION_URL'] == 'clickhouse://clickhouse:9000'
    assert environment['LANGFUSE_S3_EVENT_UPLOAD_BUCKET'] == 'langfuse'


def test_rendered_langfuse_waits_for_upstream_health_checks():
    with lab() as (home, _):
        rendered = observability.render_stack(home)
        model = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(rendered['compose']), 'config', '--format', 'json'],
            text=True, stderr=subprocess.DEVNULL))

    assert model['services']['langfuse-web']['depends_on']['clickhouse']['condition'] == 'service_healthy'


def test_existing_owned_render_recovers_its_port_allocation():
    with lab() as (home, _):
        first = observability.render_stack(home)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', first['web_port']))
            listener.listen()
            assert observability.render_stack(home)['web_port'] == first['web_port']


def test_rendered_minio_uses_the_pinned_compose_shell_contract():
    with lab() as (home, _):
        rendered = observability.render_stack(home)
        model = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(rendered['compose']), 'config', '--format', 'json'],
            text=True, stderr=subprocess.DEVNULL))

    assert model['services']['minio']['entrypoint'] == ['sh']
    assert model['services']['minio']['command'][0] == '-c'


def test_rendered_clickhouse_uses_pinned_user_contract():
    with lab() as (home, _):
        rendered = observability.render_stack(home)
        model = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(rendered['compose']), 'config', '--format', 'json'],
            text=True, stderr=subprocess.DEVNULL))

    assert model['services']['clickhouse']['environment']['CLICKHOUSE_USER'] == 'clickhouse'


def test_disable_unconfigured_home_is_idempotent():
    with lab() as (home, _):
        assert observability.disable(home)['runtime'] == 'disabled'
        assert observability.disable(home)['runtime'] == 'disabled'
