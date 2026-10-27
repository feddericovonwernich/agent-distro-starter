import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from support import command, lab

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



def test_central_uses_private_session_root_and_refuses_native_conflicts():
    with lab() as (home, _):
        argv, env, warning = observability.central_argv(home, [])
        assert Path(argv[argv.index('--session-dir') + 1]).is_dir()
        assert env['AGD_HOME'] == str(home)
        assert warning is None
        with pytest.raises(store.Refusal):
            observability.central_argv(home, ['--no-session'])


@pytest.mark.parametrize('omp_args', [
    ['--cwd', '/tmp'], ['--cwd=/tmp'], ['--session-dir', '/tmp'], ['--session-dir=/tmp'],
    ['--continue'], ['--continue=1'], ['-c'], ['--resume'], ['--resume=session-id'], ['-r'], ['--session'],
    ['--session=session-id'], ['--no-session'], ['--no-session=1'], ['--fork', 'session-id'], ['--fork=session-id'],
    ['--from-claude'], ['--from-claude=1'], ['--from-codex'], ['--from-codex=1'],
])
def test_central_refuses_every_omp_session_source_control(omp_args):
    with lab() as (home, _):
        with pytest.raises(store.Refusal):
            observability.central_argv(home, omp_args)


def test_central_preserves_flag_shaped_prompt_after_omp_separator():
    with lab() as (home, _):
        argv, _env, _warning = observability.central_argv(home, ['--', '--session', 'literal'])
    assert argv[-3:] == ['--', '--session', 'literal']

def test_rendered_stack_keeps_only_browser_collector_and_media_ports_on_loopback():
    with lab() as (home, _):
        rendered = observability.render_stack(home)
        model = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(rendered['compose']), 'config', '--format', 'json'],
            text=True, stderr=subprocess.DEVNULL))

    published = {name: service['ports'] for name, service in model['services'].items()
                 if service.get('ports')}
    assert set(published) <= {'langfuse-web', 'collector', 'minio'}
    assert all(port['host_ip'] == '127.0.0.1'
               for ports in published.values() for port in ports)


def test_render_stack_rejects_an_unpinned_injected_lock():
    with lab() as (home, _):
        lock = observability._load_lock()
        lock['images']['collector'] = 'example.invalid/collector@sha256:' + '0' * 64

        with pytest.raises(store.Refusal):
            observability.render_stack(home, lock)


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


def test_status_requires_collector_and_generated_project_readiness(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"data":[]}'

    with lab() as (home, _):
        rendered = observability.render_stack(home)
        observability._save_state(home, 'enabled', 'healthy', rendered)
        monkeypatch.setattr(
            observability.subprocess, 'run',
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                [], 0, '[{"Service":"collector","State":"running"}]', ''))
        monkeypatch.setattr(observability.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response())

        assert observability.status(home)['runtime'] == 'degraded'


def test_disable_persists_disabled_state_when_stop_fails(monkeypatch):
    with lab() as (home, _):
        rendered = observability.render_stack(home)
        observability._save_state(home, 'enabled', 'healthy', rendered)
        monkeypatch.setattr(
            observability, '_compose',
            lambda *_args: (_ for _ in ()).throw(store.Refusal('stop failed: command failed')))

        with pytest.raises(store.Refusal):
            observability.disable(home)

        assert observability.read_config(home)['mode'] == 'disabled'
        assert observability.read_status(home)['desired'] == 'disabled'
        assert observability.read_status(home)['runtime'] == 'degraded'


def test_live_central_with_old_generation_requires_restart():
    with lab() as (home, _):
        observability.record_process_generation(home, 'central', '1')
        observability.record_desired_generation(home, '2', 'enabled')
        status = observability.status(home)
        assert status['runtime'] == 'unknown'
        assert status['central']['state'] == 'stale-restart-required'


def test_exited_worker_receipt_is_not_reported_stale(monkeypatch):
    with lab() as (home, _):
        monkeypatch.setattr(observability, '_process_gone', lambda *_: True)
        observability.record_process_generation(home, 'worker', '1', 'task-a', INCARNATION,
                                                pid=123, start='old')
        observability.record_desired_generation(home, '2', 'enabled')
        worker = observability.status(home)['workers'][0]
        assert worker['state'] == 'stopped'
        assert worker['generation'] == '1'


def test_collector_diagnostics_separates_queue_admission_and_send_failure_evidence():
    diagnostics = observability._parse_collector_metrics('''
otelcol_exporter_queue_size{exporter="otlphttp/langfuse"} 7
otelcol_exporter_queue_capacity{exporter="otlphttp/langfuse"} 1000
otelcol_exporter_enqueue_failed_spans{exporter="otlphttp/langfuse"} 2
otelcol_exporter_send_failed_spans{exporter="otlphttp/langfuse",error_permanent="false"} 3
otelcol_exporter_send_failed_spans{exporter="otlphttp/langfuse",error_permanent="true"} 5
otelcol_exporter_sent_spans{exporter="otlphttp/langfuse"} 11
''')
    assert diagnostics == {
        'state': 'known', 'queue_batches': 7, 'queue_capacity_batches': 1000, 'queue_full': 'not-full',
        'enqueue_failed_spans': 2, 'retryable_send_failed_spans': 3,
        'permanent_send_failed_spans': 5, 'unclassified_send_failed_spans': 0,
        'sent_spans': 11, 'storage_failure': 'unknown',
    }


def test_collector_error_classification_keeps_disk_and_file_storage_full_distinct():
    assert observability._classify_collector_errors('write queue: no space left on device') == 'host-disk-full'
    assert observability._classify_collector_errors('file_storage: max_size exceeded') == 'file-storage-full'


def test_metrics_outage_retains_sanitized_storage_failure(monkeypatch):
    with lab() as (home, _):
        monkeypatch.setattr(
            observability.urllib.request, 'urlopen',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(observability.urllib.error.URLError('offline')))
        monkeypatch.setattr(
            observability.subprocess, 'run',
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, 'queue write failed: ENOSPC', ''))
        assert observability._collector_diagnostics(home, {
            'project': 'owned', 'ports': {'collector_metrics_port': 1},
        }) == {'state': 'unknown', 'storage_failure': 'host-disk-full'}

def test_collector_diagnostics_reports_sanitized_permanent_rejection_from_logs(monkeypatch):
    with lab() as (home, _):
        monkeypatch.setattr(
            observability.urllib.request, 'urlopen',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(observability.urllib.error.URLError('offline')))
        monkeypatch.setattr(
            observability.subprocess, 'run',
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                [], 0, 'export failed permanently: status code: 404', ''))
        assert observability._collector_diagnostics(home, {
            'project': 'owned', 'ports': {'collector_metrics_port': 1},
        }) == {
            'state': 'unknown', 'storage_failure': 'unknown',
            'permanent_failure': 'observed',
        }


def test_scout_cleanup_preserves_native_binding_queue_and_archive_bytes():
    with lab() as (home, _):
        brief = home / 'brief.md'
        brief.write_text('Inspect value.txt.\n')
        command(home, 'task', 'create', 'scout', '--project', 'sample', '--brief', str(brief), '--kind', 'scout')
        binding = observability.worker_binding(home, 'scout', INCARNATION)
        session = Path(binding['session_dir'])
        (session / 'native.jsonl').write_bytes(b'{"message":"old"}\n')
        artifact = session / 'native' / 'artifact'
        artifact.mkdir(parents=True)
        (artifact / 'blob').write_bytes(b'old artifact')
        queue = observability.paths(home)['queue']
        queue.mkdir(parents=True)
        (queue / 'item').write_bytes(b'queued')
        archive = observability.paths(home)['trace_archive']
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b'archive')
        report = home / 'report.md'
        report.write_text('verified\n')
        command(home, 'task', 'ready', 'scout', '--report', str(report))
        command(home, 'task', 'cleanup', 'scout')
        assert (session / 'native.jsonl').read_bytes() == b'{"message":"old"}\n'
        assert (artifact / 'blob').read_bytes() == b'old artifact'
        assert (queue / 'item').read_bytes() == b'queued'
        assert archive.read_bytes() == b'archive'
