#!/usr/bin/env python3
"""Opt-in native plugin, task snapshot, backend, and cleanup smoke."""
import argparse
import hashlib
import json
import os
import pty
import shutil
import subprocess
import signal
import sys
import tempfile
import time
import uuid
from urllib.request import urlopen
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
from agd_lib import installer, launcher, omp_runtime, reviewer_runner, store, supervision, tasks, worker_skills, workers  # noqa: E402

SKILLS = {
    'agd-worker',
    'ponytail', 'ponytail-audit', 'ponytail-debt', 'ponytail-gain',
    'ponytail-help', 'ponytail-review', 'systematic-debugging',
    'test-driven-development', 'verification-before-completion',
}


def run(argv, *, cwd=None, env=None):
    result = subprocess.run([str(value) for value in argv], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f'{argv!r}: {result.stderr or result.stdout}')
    return result.stdout


def agd(home, *arguments):
    return json.loads(run([ROOT/'bin/agd', '--home', home, *arguments]))


def digest_tree(root):
    if not root.is_dir() or root.is_symlink():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git(argv, cwd=None):
    return run(['git', *argv], cwd=cwd)


def wait_for_output(module, endpoint, needle):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            output = module.capture(endpoint)
        except Exception:
            output = ''
        if needle in output:
            return output
        time.sleep(.1)
    raise RuntimeError(f'backend output never contained {needle!r}')


def wait_for_worker_idle(home, task_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = tasks.read(home, task_id)
        observations = [
            json.loads(path.read_text())
            for path in (tasks.task_file(home, task_id).parent/'observations').glob('*.json')
        ]
        if any(row['incarnation'] == task.get('incarnation')
               and row['kind'] == 'idle' and row['key'] == 'turn'
               for row in observations):
            return task
        if task.get('worker_alive') is False:
            raise RuntimeError(f'{task_id} exited before becoming idle')
        time.sleep(.2)
    raise RuntimeError(f'{task_id} did not become idle')


def send_and_wait_for_reply(home, task_id, root, body, timeout=180):
    message = root/f'{task_id}-{uuid.uuid4()}.md'
    message.write_text(body)
    request = agd(home, 'worker', 'send', task_id, '--file', str(message))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        supervision.reconcile(home)
        event = next(
            (row for row in supervision.pending(home)
             if row.get('key') == request['id']
             and row.get('kind') in ('done', 'blocked')),
            None,
        )
        if event:
            supervision.acknowledge(home, [event['id']])
            if event['kind'] != 'done':
                raise RuntimeError(f'worker follow-up failed: {event["details"]}')
            return request, event['details']
        time.sleep(.2)
    raise RuntimeError(f'worker follow-up {request["id"]} produced no event')


def fetch_json(url):
    with urlopen(url) as response:
        return json.load(response)


def stream_url(url):
    with urlopen(url) as response:
        yield from iter(lambda: response.read(1024 * 1024), b'')


def prove_plugin_lifecycles(root, plugins, env):
    ponytail = Path(plugins['ponytail@ponytail']['path'])/'pi-extension/index.js'
    superpowers = Path(plugins['superpowers@superpowers-dev']['path'])/'.pi/extensions/superpowers.ts'
    script = root/'plugin-lifecycles.mjs'
    script.write_text('''
import { pathToFileURL } from "node:url";
const load = async path => (await import(pathToFileURL(path).href)).default;
function api() {
  const handlers = {}, commands = {};
  return { handlers, commands, value: {
    on: (name, handler) => { handlers[name] = handler; },
    registerCommand: (name, command) => { commands[name] = command; },
    appendEntry: () => {}, sendUserMessage: () => {},
  }};
}
const ponytail = api();
(await load(process.argv[2]))(ponytail.value);
const sections = {};
const context = { isIdle: () => true, sessionManager: { getBranch: () => [] },
  ui: { setStatus: () => {}, notify: () => {},
    theme: { fg: (_color, text) => text } } };
await ponytail.handlers.session_start({}, context);
await ponytail.handlers.before_agent_start({ systemPromptOptions: { sections } }, context);
if (!ponytail.commands.ponytail || !sections.ponytail?.includes("PONYTAIL MODE ACTIVE"))
  throw new Error("Ponytail command/instruction lifecycle did not run");
const superpowers = api();
(await load(process.argv[3]))(superpowers.value);
await superpowers.handlers.session_start();
const injected = await superpowers.handlers.context({ messages: [] });
if (!JSON.stringify(injected).includes("superpowers:using-superpowers bootstrap for pi"))
  throw new Error("Superpowers bootstrap lifecycle did not run");
console.log(JSON.stringify({ ponytail: true, superpowers: true }));
''')
    return json.loads(run(['bun', script, ponytail, superpowers], env=env))


def start_central(home, backend_name, env):
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(ROOT/'bin/agd'), '--home', str(home), 'start', '--backend', backend_name],
        stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    path = home/'state/central-launcher.json'
    deadline = time.monotonic() + 30
    receipt = None
    try:
        while time.monotonic() < deadline:
            if path.is_file():
                candidate = json.loads(path.read_text())
                if candidate.get('phase') == 'ready':
                    receipt = candidate
                    break
            if process.poll() is not None:
                break
            time.sleep(.1)
        if receipt is None:
            try:
                output = os.read(master, 65536).decode(errors='replace')
            except OSError:
                output = ''
            raise RuntimeError(f'agd start did not produce a ready central: {output}')
        if launcher.active_backend(home, env) != backend_name:
            raise RuntimeError('ready central backend did not verify')
        return receipt
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        os.close(master)


def central_output(home, backend, receipt):
    endpoint = receipt['endpoint']
    if receipt['backend'] == 'tmux':
        return store.run(
            [*backend.prefix(home), 'capture-pane', '-p', '-S', '-200',
             '-t', endpoint['pane']]).stdout
    return backend.call(
        endpoint, 'pane', 'read', endpoint['pane'], '--source',
        'recent-unwrapped', '--lines', '200').stdout


def central_send(home, backend, receipt, text):
    endpoint = receipt['endpoint']
    if receipt['backend'] == 'tmux':
        store.run([*backend.prefix(home), 'send-keys', '-l', '-t', endpoint['pane'], text])
        store.run([*backend.prefix(home), 'send-keys', '-t', endpoint['pane'], 'Enter'])
    else:
        backend.call(endpoint, 'pane', 'send-text', endpoint['pane'], text)
        backend.call(endpoint, 'pane', 'send-keys', endpoint['pane'], 'enter')


def stop_central(home, backend, receipt):
    launcher._backend_inspect(home, receipt)
    endpoint = receipt['endpoint']
    owner = json.loads((home/'state/owner.json').read_text())
    central_send(home, backend, receipt, '/quit')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if receipt['backend'] == 'tmux':
            present = not store.run(
                [*backend.prefix(home), 'list-panes', '-t', endpoint['pane']],
                check=False).returncode
        else:
            present = not backend.call(
                endpoint, 'pane', 'get', endpoint['pane'], check=False).returncode
        if not present:
            break
        time.sleep(.1)
    if present:
        if receipt['backend'] == 'tmux':
            store.run([*backend.prefix(home), 'kill-window', '-t', endpoint['window']])
        else:
            backend.call(endpoint, 'pane', 'close', endpoint['pane'])
    if receipt['backend'] == 'tmux':
        store.run([*backend.prefix(home), 'kill-server'])
    else:
        if endpoint['server_argv'] != [
                'herdr', '--session', endpoint['session'], 'server']:
            raise RuntimeError('owned Herdr central server identity changed')
        if not supervision.gone(endpoint['server_pid'], endpoint['server_start']):
            backend.call(endpoint, 'workspace', 'close', endpoint['workspace'])
            os.killpg(endpoint['server_pid'], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        owner_gone = all(
            supervision.gone(owner[key], owner[key.replace('_pid', '_start')])
            for key in ('owner_pid', 'watcher_pid'))
        if receipt['backend'] == 'tmux':
            server_gone = store.run(
                [*backend.prefix(home), 'display-message', '-p', '#{pid}'],
                check=False).returncode != 0
        else:
            server_gone = supervision.gone(
                endpoint['server_pid'], endpoint['server_start'])
        if owner_gone and server_gone:
            return
        time.sleep(.1)
    raise RuntimeError('central, watcher, or backend server remained after teardown')


def herdr_server_pids(session):
    expected = ['herdr', '--session', session, 'server']
    matches = []
    for process in Path('/proc').glob('[0-9]*'):
        try:
            argv = [
                item.decode() for item in
                process.joinpath('cmdline').read_bytes().split(b'\0') if item]
        except (OSError, UnicodeDecodeError):
            continue
        if argv == expected:
            matches.append(int(process.name))
    return matches


def stop_reserved_backend(home, backend, backend_name):
    if backend_name == 'tmux':
        server = store.run(
            [*backend.prefix(home), 'display-message', '-p', '#{pid}'],
            check=False)
        if server.returncode:
            return
        store.run([*backend.prefix(home), 'kill-server'])
        return
    session = json.loads((home/'data/launcher.json').read_text())['herdr_session']
    matches = herdr_server_pids(session)
    if len(matches) > 1:
        raise RuntimeError('reserved Herdr central server identity is ambiguous')
    if not matches:
        return
    pid = matches[0]
    try:
        start = supervision.process_start(pid)
    except (FileNotFoundError, ProcessLookupError):
        return
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if supervision.gone(pid, start):
            return
        time.sleep(.1)
    raise RuntimeError('reserved Herdr central server remained after teardown')


def cleanup_central(home, backend, receipt):
    if receipt.get('phase') == 'ready':
        stop_central(home, backend, receipt)
        return
    endpoint = receipt.get('endpoint')
    if not isinstance(endpoint, dict) or not endpoint:
        stop_reserved_backend(home, backend, receipt.get('backend'))
        return
    if receipt.get('backend') == 'tmux':
        server = store.run(
            [*backend.prefix(home), 'display-message', '-p', '#{pid}'],
            check=False)
        if server.returncode or server.stdout.strip() != endpoint.get('server_pid'):
            raise RuntimeError('partial tmux central server identity changed')
        store.run([*backend.prefix(home), 'kill-server'])
        return
    if endpoint.get('server_argv') != [
            'herdr', '--session', endpoint.get('session'), 'server']:
        raise RuntimeError('partial Herdr central server identity changed')
    if supervision.gone(endpoint['server_pid'], endpoint['server_start']):
        return
    if endpoint.get('workspace'):
        backend.call(
            endpoint, 'workspace', 'close', endpoint['workspace'], check=False)
    os.killpg(endpoint['server_pid'], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if supervision.gone(endpoint['server_pid'], endpoint['server_start']):
            return
        time.sleep(.1)
    raise RuntimeError('partial Herdr central server remained after teardown')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', required=True, choices=('tmux', 'herdr'))
    args = parser.parse_args()
    omp = shutil.which('omp')
    treehouse = shutil.which('treehouse')
    if not omp or not treehouse or not shutil.which('bun'):
        raise SystemExit('omp, treehouse, and bun are required on PATH')
    if args.backend == 'tmux' and not shutil.which('tmux'):
        raise SystemExit('tmux is required for --backend tmux')
    if args.backend == 'herdr' and not shutil.which('herdr'):
        raise SystemExit('herdr is required for --backend herdr')
    if args.backend == 'herdr' and os.environ.get('HERDR_ENV') != '1':
        raise SystemExit(
            '--backend herdr must run inside an owned Herdr-managed pane')

    native_evidence = json.loads(run(
        ['bun', ROOT/'tests/worker-native.test.ts'],
        env={**os.environ, 'AGD_NATIVE_CHECK': '1'}))
    account_omp = Path.home()/'.omp'
    account_before = digest_tree(account_omp)
    backend = __import__('agd_lib.' + args.backend, fromlist=['create'])
    endpoint = None
    worker_endpoint = None
    worker_task_id = None
    central_receipt = None
    central_needs_cleanup = False
    evidence = {}
    directory = Path(tempfile.mkdtemp(prefix='agd-native-plugin-smoke-'))
    try:
        root = Path(directory)
        home, seed, origin = root/'home', root/'seed', root/'origin.git'
        hostile = {
            **os.environ, 'OMP_PROFILE': 'foreign', 'PI_PROFILE': 'foreign',
            'PI_CONFIG_DIR': 'foreign', 'PI_CODING_AGENT_DIR': str(root/'foreign-agent'),
            'XDG_DATA_HOME': str(root/'foreign-data'),
            'XDG_STATE_HOME': str(root/'foreign-state'),
            'XDG_CACHE_HOME': str(root/'foreign-cache'),
        }
        os.environ.update({key: hostile[key] for key in (
            'OMP_PROFILE', 'PI_PROFILE', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR',
            'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')})
        agd(home, 'init', '--backend', args.backend)
        if args.backend == 'tmux':
            existing_server = store.run(
                [*backend.prefix(home), 'display-message', '-p', '#{pid}'],
                check=False)
            if not existing_server.returncode:
                raise RuntimeError('disposable tmux server name already exists')
        else:
            session = json.loads((home/'data/launcher.json').read_text())['herdr_session']
            if herdr_server_pids(session):
                raise RuntimeError('disposable Herdr central session already exists')
        dependencies = installer.setup(
            home, args.backend, lambda _message: False, fetch_json, stream_url,
            store.run)
        omp = dependencies['omp']['path']
        plugins = omp_runtime.installed_plugins(home)
        managed = omp_runtime.environment(home, hostile)
        lifecycle = prove_plugin_lifecycles(root, plugins, managed)
        central_needs_cleanup = True
        central_receipt = start_central(home, args.backend, hostile)
        central_send(home, backend, central_receipt, '/ponytail status')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            output = central_output(home, backend, central_receipt)
            if 'Ponytail:' in output:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('actual central did not expose the Ponytail command lifecycle')

        git(['init', '-b', 'main', seed])
        git(['config', 'user.name', 'Native Smoke'], cwd=seed)
        git(['config', 'user.email', 'smoke@example.invalid'], cwd=seed)
        (seed/'value.txt').write_text('base\n')
        git(['add', 'value.txt'], cwd=seed)
        git(['commit', '-m', 'initial'], cwd=seed)
        git(['clone', '--bare', seed, origin])
        agd(home, 'project', 'add', 'sample', '--source', str(origin))
        brief = root/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = agd(home, 'task', 'create', 'native-smoke', '--project', 'sample',
                   '--brief', str(brief), '--kind', 'scout')
        worktree = Path(task['worktree'])
        copied = {path.name for path in (worktree/'.omp/skills').iterdir()}
        if copied != SKILLS or any((worktree/'.omp/skills'/('agd-' + name)).exists()
                                   for name in SKILLS):
            raise RuntimeError(f'unexpected task skills: {sorted(copied)}')
        none_brief = root/'none-brief.md'
        none_brief.write_text(
            'Inspect value.txt and verify it contains base. Leave the workspace unchanged '
            'and submit a concise evidence report through the AGD worker protocol.\n')
        none_task = agd(
            home, 'task', 'create', 'native-none', '--project', 'sample',
            '--brief', str(none_brief), '--kind', 'scout', '--skill-mode', 'none')
        none_worktree = Path(none_task['worktree'])
        none_skills = none_worktree/'.omp/skills'
        if {path.name for path in none_skills.iterdir()} != {'agd-worker'}:
            raise RuntimeError('none-mode task must contain only the mandatory AGD protocol')

        managed = omp_runtime.environment(home, hostile)
        child_env = {key: managed[key] for key in (
            'HOME', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR', 'OMP_PROFILE',
            'PI_PROFILE', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')}
        child_env.update(PATH=os.environ['PATH'], TERM=os.environ.get('TERM', 'xterm'))
        reviewer_runtime = reviewer_runner._runtime_boundary(home, str(uuid.uuid4()))
        reviewer_plugins = run([omp, 'plugin', 'list', '--json'],
                               cwd=reviewer_runtime, env=child_env)
        if any(plugin_id in reviewer_plugins for plugin_id in plugins):
            raise RuntimeError('reviewer project inherited orchestrator plugins')
        command = ['/usr/bin/env', '-i', *(f'{key}={value}' for key, value in child_env.items()),
                   omp, 'read', 'skill://systematic-debugging/root-cause-tracing.md']
        endpoint = backend.create(
            home, 'native-smoke', 'read-skill', worktree,
            ['/bin/sh', '-c', '"$@"; status=$?; sleep 30; exit $status',
             'agd-native-smoke', *command])
        try:
            wait_for_output(backend, endpoint, 'Root Cause Tracing')
        finally:
            try:
                backend.close(endpoint)
            finally:
                endpoint = None
                pass
        supporting = run([omp, 'read',
                          'skill://test-driven-development/writing-good-tests.md'],
                         cwd=worktree, env=child_env)
        if 'test' not in supporting.casefold():
            raise RuntimeError('native supporting skill resource was not readable')

        source = Path(next(row['source'] for row in worker_skills.catalog(home)
                           if row['id'] == 'systematic-debugging'))
        shutil.rmtree(source)
        destination = worktree/'.omp/skills/systematic-debugging'
        shutil.rmtree(destination)
        worker_skills.ensure(home, tasks.read(home, 'native-smoke'))
        if not (destination/'SKILL.md').is_file():
            raise RuntimeError('completed snapshot did not restore the missing copy')
        rejected = subprocess.run(
            [str(ROOT/'bin/agd'), '--home', str(home), 'task', 'create', 'source-gone',
             '--project', 'sample', '--brief', str(brief), '--kind', 'scout'],
            text=True, capture_output=True, timeout=120)
        if rejected.returncode != 3 or tasks.task_file(home, 'source-gone').exists():
            raise RuntimeError('new task accepted a removed native plugin source')

        launched = agd(home, 'worker', 'launch', 'native-smoke',
                       '--backend', args.backend)
        worker_endpoint = launched['endpoint']
        worker_task_id = 'native-smoke'
        initial_incarnation = launched['incarnation']
        initial_endpoint = launched['endpoint']
        current = wait_for_worker_idle(home, 'native-smoke')
        if current['phase'] != 'ready':
            raise RuntimeError('selected worker did not complete its initial task')
        runner = json.loads((home/'state/tasks/native-smoke/runner.json').read_text())
        if runner.get('phase') != 'spawned':
            raise RuntimeError('actual worker process did not publish a spawn receipt')
        try:
            worker_output = backend.capture(worker_endpoint)
        except Exception:
            worker_output = ''
        if 'Ponytail loaded' in worker_output:
            raise RuntimeError('worker loaded the orchestrator Ponytail extension')

        _, first_reply = send_and_wait_for_reply(
            home, 'native-smoke', root,
            'Read value.txt and report its current value without modifying files.\n')
        if 'base' not in first_reply:
            raise RuntimeError(f'first retained-worker reply omitted the file value: {first_reply}')
        after_skill_reply = tasks.read(home, 'native-smoke')
        if (after_skill_reply['incarnation'] != initial_incarnation
                or after_skill_reply['endpoint'] != initial_endpoint):
            raise RuntimeError('worker identity changed during the first follow-up')

        marker = 'RETAINED-ROUNDTRIP-OK'
        _, second_reply = send_and_wait_for_reply(
            home, 'native-smoke', root,
            f'Reply with exactly {marker} and no other text.\n')
        if marker not in second_reply:
            raise RuntimeError(f'second retained-worker reply was incorrect: {second_reply}')
        after_second_reply = tasks.read(home, 'native-smoke')
        if (after_second_reply['incarnation'] != initial_incarnation
                or after_second_reply['endpoint'] != initial_endpoint):
            raise RuntimeError('worker identity changed during the second follow-up')
        runner = json.loads((home/'state/tasks/native-smoke/runner.json').read_text())
        if (runner.get('incarnation') != initial_incarnation
                or runner.get('phase') != 'spawned'
                or workers.inspect(home, 'native-smoke')['endpoint']['presence'] != 'present'):
            raise RuntimeError('ready crash probe lost its owned worker identity')
        child_fd = os.pidfd_open(runner['child_pid'])
        try:
            if supervision.gone(runner['child_pid'], runner['child_start']):
                raise RuntimeError('ready crash probe child identity changed')
            signal.pidfd_send_signal(child_fd, signal.SIGKILL)
        finally:
            os.close(child_fd)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (supervision.gone(runner['pid'], runner['start'])
                    and supervision.gone(runner['child_pid'], runner['child_start'])
                    and tasks.read(home, 'native-smoke').get('worker_alive') is False
                    and workers.inspect(home, 'native-smoke')['endpoint']['presence'] == 'missing'):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('crashed ready worker absence remains unconfirmed')
        worker_endpoint = None
        report_before = (home/'data/tasks/native-smoke/report.md').read_bytes()
        recovered = agd(home, 'worker', 'relaunch', 'native-smoke')
        worker_endpoint = recovered['endpoint']
        if recovered['incarnation'] == initial_incarnation or recovered['lease_id'] != task['lease_id']:
            raise RuntimeError('ready-worker recovery changed the lease or reused its incarnation')
        _, recovery_reply = send_and_wait_for_reply(
            home, 'native-smoke', root, 'Reply exactly READY-RECOVERY-OK without changing files.\n')
        if 'READY-RECOVERY-OK' not in recovery_reply:
            raise RuntimeError('ready-worker recovery did not answer its queued follow-up')
        if (tasks.read(home, 'native-smoke')['ready_head'] != current['ready_head']
                or (home/'data/tasks/native-smoke/report.md').read_bytes() != report_before):
            raise RuntimeError('ready-worker recovery replayed or changed the completed assignment')
        workers.stop(home, 'native-smoke')
        worker_endpoint = None

        launched_none = agd(home, 'worker', 'launch', 'native-none',
                            '--backend', args.backend)
        worker_endpoint = launched_none['endpoint']
        worker_task_id = 'native-none'
        none_current = wait_for_worker_idle(home, 'native-none')
        if none_current['phase'] != 'ready':
            raise RuntimeError('none-mode worker did not complete its initial task')
        none_report = (home/'data/tasks/native-none/report.md').read_text()
        if 'base' not in none_report:
            raise RuntimeError(f'none-mode worker did not report the inspected value: {none_report}')
        workers.stop(home, 'native-none')
        worker_endpoint = None

        stop_central(home, backend, central_receipt)
        central_receipt = None
        central_needs_cleanup = False

        cleaned = agd(home, 'task', 'cleanup', 'native-smoke')
        none_cleaned = agd(home, 'task', 'cleanup', 'native-none')
        if cleaned['phase'] != 'cleaned' or none_cleaned['phase'] != 'cleaned':
            raise RuntimeError('task cleanup did not return both leases')
        evidence = {
            'backend': args.backend,
            'plugins': {key: value['version'] for key, value in plugins.items()},
            'plugin_lifecycles': lifecycle,
            'skills': sorted(copied),
            'native_protocol': native_evidence,
            'none_worker_system_skills': ['agd-worker'],
            'reviewer_plugins_isolated': True,
            'retained_worker_replies': 2,
            'ready_worker_crash_recovery_reply': True,
            'worker_spawned_and_stopped': True,
            'snapshot_restored_after_source_removal': True,
            'task_phase': cleaned['phase'],
        }
    finally:
        cleanup_failure = None
        if endpoint is not None:
            try:
                backend.close(endpoint)
            except Exception as error:
                cleanup_failure = error
        if worker_endpoint is not None:
            try:
                backend.close(worker_endpoint)
                runner_path = directory/'home/state/tasks'/worker_task_id/'runner.json'
                runner = json.loads(runner_path.read_text()) if runner_path.is_file() else {}
                identities = [
                    (runner.get('pid'), runner.get('start')),
                    (runner.get('child_pid'), runner.get('child_start')),
                ]
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if all(not isinstance(pid, int)
                           or supervision.gone(pid, start)
                           for pid, start in identities):
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('worker process remained after owned endpoint teardown')
            except Exception as error:
                if cleanup_failure is None:
                    cleanup_failure = error
        if central_needs_cleanup:
            try:
                receipt = central_receipt
                receipt_path = directory/'home/state/central-launcher.json'
                if receipt is None and receipt_path.is_file():
                    receipt = json.loads(receipt_path.read_text())
                if receipt is not None:
                    cleanup_central(directory/'home', backend, receipt)
            except Exception as error:
                if cleanup_failure is None:
                    cleanup_failure = error
        if args.backend == 'herdr':
            workspace_path = directory/'home/state/herdr-workspace.json'
            if workspace_path.is_file():
                try:
                    workspace = json.loads(workspace_path.read_text())
                    backend.call(
                        workspace, 'workspace', 'close',
                        workspace['workspace'])
                except Exception:
                    pass
        if cleanup_failure is not None:
            raise RuntimeError(f'owned runtime cleanup unconfirmed; retained {directory}') from cleanup_failure
        for task_path in (directory/'home/data/tasks').glob('*/task.json'):
            try:
                worktree = Path(json.loads(task_path.read_text())['worktree'])
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                worktree = None
            if worktree is not None and worktree.exists():
                destroyed = subprocess.run(
                    [treehouse, 'destroy', str(worktree), '--include-leased',
                     '--include-unlanded', '--include-in-use', '--yes'],
                    text=True, capture_output=True, timeout=120)
                if destroyed.returncode:
                    raise RuntimeError(
                        f'lease cleanup failed; retained {directory}: '
                        f'{destroyed.stderr or destroyed.stdout}')
        shutil.rmtree(directory, ignore_errors=True)
    if digest_tree(account_omp) != account_before:
        raise RuntimeError('account-global OMP state changed')
    time.sleep(8)
    if directory.exists():
        raise RuntimeError('a child process recreated the disposable distro')
    evidence['account_global_omp_unchanged'] = True
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
