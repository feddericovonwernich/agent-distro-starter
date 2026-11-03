import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'bin'), str(ROOT / 'tests')]
from agd_lib import installer, omp_runtime, reviewer, reviewer_runner, reviewer_timer, store, supervision  # noqa: E402
from support import command, write_native_plugins  # noqa: E402
from test_reviewer import ended_worker, message  # noqa: E402


def test_public_timer_managed_native_run_waits_for_exit_and_cleans_endpoint():
    binaries = {name: shutil.which(name) for name in ('omp', 'herdr', 'treehouse')}
    if not all(binaries.values()):
        pytest.skip('standalone OMP, Herdr, and Treehouse are required')
    binaries = {name: str(Path(path).resolve()) for name, path in binaries.items()}
    root = Path(tempfile.mkdtemp(prefix='agdn-', dir='/tmp'))
    tools = root / 'tools'
    for name, path in binaries.items():
        tools.mkdir(parents=True, exist_ok=True)
        (tools / name).symlink_to(path)
    barrier = root / 'write-result-seen'
    release = root / 'release-write-result'
    fixture = ROOT / 'tests' / 'native_reviewer_fixture.ts'
    wrapper = tools / 'omp'
    wrapper.unlink()
    wrapper.write_text(
        '#!/usr/bin/env python3\nimport os,sys\nreal=' + repr(binaries['omp']) + '\n'
        'args=sys.argv[1:]\n'
        'if "--print" in args: args += ["-e", ' + repr(str(fixture)) + ', "-e", '
        + repr(str(root / 'barrier-extension.ts')) + ']\n'
        'os.execv(real,[real,*args])\n')
    wrapper.chmod(0o700)
    (root / 'barrier-extension.ts').write_text(
        'import { existsSync, writeFileSync } from "node:fs";\n'
        'export default async function(api: any) { api.on("tool_result", async (event: any) => {\n'
        '  if (event.toolCallId !== "write-report") return;\n'
        f'  writeFileSync({str(barrier)!r}, "ready\\n");\n'
        f'  while (!existsSync({str(release)!r})) await new Promise(resolve => setTimeout(resolve, 25));\n'
        '}); }\n')
    environment = {
        'PATH': str(tools) + ':/usr/bin:/bin', 'HOME': str(root / 'operator-home'),
        'USER': str(os.getuid()), 'LANG': 'C.UTF-8', 'TERM': 'dumb', 'NO_COLOR': '1',
        'PI_CONFIG_DIR': '.omp', 'PI_CODING_AGENT_DIR': str(root / 'agent'),
        'OMP_PROFILE': 'default', 'PI_PROFILE': 'default',
        'XDG_DATA_HOME': str(root / 'xdg/data'), 'XDG_STATE_HOME': str(root / 'xdg/state'),
        'XDG_CACHE_HOME': str(root / 'xdg/cache'), 'XDG_CONFIG_HOME': str(root / 'c'),
    }
    for name in ('operator-home', 'agent', 'xdg/data', 'xdg/state', 'xdg/cache', 'c'):
        (root / name).mkdir(parents=True, mode=0o700)
    previous = dict(os.environ)
    control = None
    try:
        os.environ.clear()
        os.environ.update(environment)
        home = root / 'home'
        seed = root / 'seed'
        origin = root / 'origin.git'
        command(home, 'init', '--backend', 'herdr')
        subprocess.run(['/usr/bin/git', 'init', '-b', 'main', str(seed)], check=True,
                       capture_output=True, text=True)
        subprocess.run(['/usr/bin/git', 'config', 'user.name', 'Owned Native Smoke'], cwd=seed,
                       check=True, capture_output=True, text=True)
        subprocess.run(['/usr/bin/git', 'config', 'user.email', 'smoke@example.invalid'], cwd=seed,
                       check=True, capture_output=True, text=True)
        (seed / 'value.txt').write_text('synthetic owned fixture\n')
        subprocess.run(['/usr/bin/git', 'add', 'value.txt'], cwd=seed, check=True,
                       capture_output=True, text=True)
        subprocess.run(['/usr/bin/git', 'commit', '-m', 'synthetic fixture'], cwd=seed,
                       check=True, capture_output=True, text=True)
        subprocess.run(['/usr/bin/git', 'clone', '--bare', str(seed), str(origin)], check=True,
                       capture_output=True, text=True)
        command(home, 'project', 'add', 'sample', '--source', str(origin))
        write_native_plugins(home)
        subprocess.run(['/usr/bin/git', 'init', '--quiet', str(home)], check=True,
                       capture_output=True, text=True)
        subprocess.run(['/usr/bin/git', '-C', str(home), '-c', 'user.name=Owned Native Smoke',
                        '-c', 'user.email=smoke@example.invalid', 'commit', '--allow-empty',
                        '-m', 'synthetic baseline'], check=True, capture_output=True, text=True)
        omp_runtime.initialize(home, str(wrapper), inherited=environment)
        dependencies = {
            'schema': 1,
            'omp': {'source': 'existing', **installer.probe_omp(
                wrapper, installer._omp_runner(home, store.run))},
            'herdr': {'source': 'existing', **installer.probe_herdr(
                binaries['herdr'], 'agd-p-' + uuid.uuid4().hex[:8], store.run)},
            'treehouse': {'source': 'existing', **installer.probe_treehouse(
                binaries['treehouse'], store.run)},
        }
        installer.write_dependencies(home, dependencies)
        agent = Path(omp_runtime.environment(home, environment)['PI_CODING_AGENT_DIR'])
        (agent / 'config.yml').write_text('modelRoles:\n  default: agd-fixture/reviewer\n')
        config = reviewer.read_config(home)
        config['review_model'] = 'agd-fixture/reviewer'
        config['limits']['max_runtime_seconds'] = 60
        config['schedule'] = {'mode': 'interval', 'timezone': None, 'calendar': None,
                              'interval_hours': 1}
        store.atomic_json(reviewer.paths(home)['config'], config)
        ended_worker(home, 'managed-timer-seed', str(uuid.uuid4()),
                     [message('AGD_NATIVE_REVIEWER_SENTINEL: synthetic completed execution evidence')])
        reviewer_timer.enable(home, config, xdg_config_home=root / 'xdg-config',
                              command_runner=lambda _argv: None, now=1)
        reviewer_timer.timer_tick(home, now=3601)
        control_session = 'agd-control-' + uuid.uuid4().hex[:8]
        control = subprocess.Popen([str(tools / 'herdr'), '--session', control_session, 'server'],
                                   cwd=home, env=environment, start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = subprocess.run([str(tools / 'herdr'), '--session', control_session,
                                     'status', 'server'], cwd=home, env=environment,
                                    capture_output=True, text=True)
            if (status.returncode == 0
                    and re.search(r'(?m)^status:\s*running\s*$', status.stdout)
                    and control.poll() is None):
                break
            time.sleep(.05)
        else:
            pytest.fail('unrelated control Herdr server did not start')
        child_env = {key: value for key, value in environment.items()
                     if key not in {'HERDR_ENV', 'AGD_HERDR_SESSION'}}
        active = None

        def cleanup_run(run_id):
            try:
                receipt = reviewer._receipt(home, run_id)
            except store.Refusal:
                return
            identities = [value for value in (receipt.get('coordinator'), receipt.get('child'))
                          if isinstance(value, dict)]
            if identities:
                supervision.stop_owned_processes(identities, timeout=5)
            endpoint = receipt.get('endpoint')
            if isinstance(endpoint, dict) and receipt.get('backend') == 'herdr':
                try:
                    from agd_lib import herdr
                    herdr.reviewer_close(home, endpoint)
                except store.Refusal:
                    pass

        def start_public_run(kind):
            barrier.unlink(missing_ok=True)
            release.unlink(missing_ok=True)
            ended_worker(home, kind, str(uuid.uuid4()),
                         [message('AGD_NATIVE_REVIEWER_SENTINEL: synthetic execution evidence')])
            return subprocess.Popen(
                [sys.executable, str(ROOT / 'bin' / 'agd'), '--home', str(home),
                 'reviewer', 'run'],
                cwd=root, env=child_env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)

        process = subprocess.Popen([sys.executable, str(ROOT / 'bin' / 'agd'), '--home', str(home),
                                    'reviewer', 'run', '--timer'], cwd=root, env=child_env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not barrier.exists():
                if process.poll() is not None:
                    break
                time.sleep(.05)
            assert barrier.exists(), process.stderr.read() if process.poll() is not None else 'barrier missing'
            assert not list(reviewer.paths(home)['reports'].glob('*.json'))
            release.write_text('release\n')
            stdout, stderr = process.communicate(timeout=80)
            assert process.returncode == 0, (stdout, stderr)
            result = json.loads(stdout)
            assert result['outcome'] == 'exited'
            reports = [json.loads(path.read_text()) for path in
                       reviewer.paths(home)['reports'].glob('*.json')]
            assert len(reports) == 1
            assert reports[0]['review_run_id'] == result['review_run_id']
            payload = reports[0]['report']
            packet = reviewer.read_packet(home, result['packet_id'])
            seed_source_id = next(excerpt['source_id'] for excerpt in packet['excerpts']
                                  if 'AGD_NATIVE_REVIEWER_SENTINEL' in excerpt['text'])
            supplementary = reviewer.read_evidence(home, result['review_run_id'])
            assert len(supplementary['sources']) == 1
            supplement_source_id = next(iter(supplementary['sources']))
            finding = payload['findings']
            assert len(finding) == 1
            assert finding[0]['target'] == 'agd-reviewer-execution-trace'
            assert finding[0]['citations'] == [seed_source_id, supplement_source_id]
            control_status = subprocess.run([str(tools / 'herdr'), '--session', control_session,
                                             'status', 'server'], cwd=home, env=environment,
                                            capture_output=True, text=True)
            assert (control_status.returncode == 0
                    and re.search(r'(?m)^status:\s*running\s*$', control_status.stdout)
                    and control.poll() is None)
            assert all(row['presence'] in ('absent', 'missing')
                       for row in reviewer_runner.uninstall_inventory(home))
            reports_before_recovery = len(reports)
            crash = start_public_run('managed-crash-seed')
            crash_run_id = None
            try:
                deadline = time.monotonic() + 40
                bound = None
                while time.monotonic() < deadline:
                    for path in reviewer.paths(home)['receipts'].glob('*.json'):
                        value = json.loads(path.read_text())
                        if value.get('coordinator', {}).get('pid') == crash.pid:
                            if value.get('endpoint') and (reviewer.paths(home)['runtime'] /
                                                          path.stem / 'session-bind.json').exists():
                                bound = value
                                break
                    if bound:
                        break
                    if crash.poll() is not None:
                        break
                    time.sleep(.05)
                assert bound is not None, 'public coordinator never bound native reviewer'
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline and not barrier.exists():
                    time.sleep(.05)
                assert barrier.exists(), 'public crash run did not reach report write'
                run_id = bound['review_run_id']
                runtime = reviewer.paths(home)['runtime'] / run_id
                crash_run_id = run_id
                os.kill(crash.pid, signal.SIGSTOP)
                release.write_text('release\n')
                exit_path = runtime / 'exit.json'
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and not exit_path.exists():
                    time.sleep(.1)
                assert exit_path.exists()
                assert json.loads(exit_path.read_text())['code'] == 0
                crash.kill()
                crash.wait(timeout=10)
                crash.communicate()
                session_files = sorted(
                    (home / 'data/observability/reviewer' / run_id / 'omp').glob('*.jsonl'))
                assert len(session_files) == 1
                session_bytes = session_files[0].read_bytes()
                context_path = runtime / 'native-provider-context.json'
                context_mtime = context_path.stat().st_mtime_ns
                recovered = subprocess.run(
                    [sys.executable, str(ROOT / 'bin' / 'agd'), '--home', str(home),
                     'reviewer', 'run'], cwd=root, env=child_env, capture_output=True,
                    text=True, timeout=80)
                assert recovered.returncode == 0, recovered.stderr
                recovered_result = json.loads(recovered.stdout)
                assert recovered_result['review_run_id'] == run_id
                assert recovered_result['outcome'] == 'exited'
                assert session_files[0].read_bytes() == session_bytes
                assert context_path.stat().st_mtime_ns == context_mtime
                reports_after_recovery = len(list(reviewer.paths(home)['reports'].glob('*.json')))
                assert reports_after_recovery == reports_before_recovery + 1
                replay = subprocess.run(
                    [sys.executable, str(ROOT / 'bin' / 'agd'), '--home', str(home),
                     'reviewer', 'run'], cwd=root, env=child_env, capture_output=True,
                    text=True, timeout=80)
                assert replay.returncode == 0 and json.loads(replay.stdout)['outcome'] == 'no-work'
                assert len(list(reviewer.paths(home)['reports'].glob('*.json'))) == reports_after_recovery
            finally:
                if crash.poll() is None:
                    crash.kill()
                    crash.wait(timeout=10)
                if crash_run_id:
                    cleanup_run(crash_run_id)

            active = start_public_run('managed-uninstall-seed')
            active_run_id = None
            try:
                from support import uninstall_scope
                deadline = time.monotonic() + 40
                active_receipt = None
                while time.monotonic() < deadline:
                    for path in reviewer.paths(home)['receipts'].glob('*.json'):
                        value = json.loads(path.read_text())
                        if (value.get('coordinator', {}).get('pid') == active.pid
                                and value.get('endpoint') and isinstance(value.get('child'), dict)
                                and (reviewer.paths(home)['runtime'] / path.stem /
                                     'session-bind.json').exists()):
                            active_receipt = value
                            break
                    if active_receipt and barrier.exists():
                        break
                    if active.poll() is not None:
                        break
                    time.sleep(.05)
                assert active_receipt is not None and barrier.exists()
                active_run_id = active_receipt['review_run_id']
                reports_before_uninstall = len(list(reviewer.paths(home)['reports'].glob('*.json')))
                resources = reviewer_runner.uninstall_inventory(home)
                active_rows = [row for row in resources
                               if active_run_id in row['key'] and row['presence'] == 'present']
                assert {row['kind'] for row in active_rows} >= {'reviewer-run', 'reviewer-backend'}
                with uninstall_scope(home, resources) as operation:
                    reviewer_runner.uninstall_quiesce(home, resources, operation)
                assert active.poll() is not None
                control_status = subprocess.run([str(tools / 'herdr'), '--session', control_session,
                                                 'status', 'server'], cwd=home, env=environment,
                                                capture_output=True, text=True)
                assert (control_status.returncode == 0
                        and re.search(r'(?m)^status:\s*running\s*$', control_status.stdout)
                        and control.poll() is None)
                after_resources = {row['key']: row for row in reviewer_runner.uninstall_inventory(home)}
                assert all(after_resources[key]['presence'] in ('absent', 'missing')
                           for key in (row['key'] for row in active_rows))
                final_receipt = reviewer._receipt(home, active_run_id)
                assert final_receipt['outcome'] == 'unknown'
                assert len(list(reviewer.paths(home)['reports'].glob('*.json'))) == reports_before_uninstall
            finally:
                if active is not None and active.poll() is None:
                    active.kill()
                    active.wait(timeout=10)
                if active is not None:
                    active.communicate()
                if active_run_id:
                    cleanup_run(active_run_id)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.communicate()
    finally:
        if control is not None and control.poll() is None:
            os.killpg(control.pid, signal.SIGTERM)
            control.wait(timeout=10)
        os.environ.clear()
        os.environ.update(previous)
