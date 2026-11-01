import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))

from agd_lib import relay, store


def make_relay_runtime(root, version='0.1.0', executable=True):
    package = root / 'node_modules' / 'open-relay'
    (package / 'dist' / 'src').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'name': 'open-relay', 'version': version,
        'repository': {'url': 'https://github.com/feddericovonwernich/open-relay.git'},
    }))
    if executable:
        (package / 'dist' / 'src' / 'cli.js').write_text('console.log("relay")\n')
        binary = root / 'bin' / 'relay'
        binary.parent.mkdir(parents=True)
        binary.symlink_to(package / 'dist' / 'src' / 'cli.js')
    return root


def fake_npm_success(home):
    def run(argv, cwd=None, timeout=30, check=True):
        stage = Path(argv[argv.index('--prefix') + 1])
        make_relay_runtime(stage)
        return subprocess.CompletedProcess(argv, 0, '', '')
    return run


def runtime_has_open_relay_010(runtime):
    package = runtime / 'node_modules' / 'open-relay'
    return (json.loads((package / 'package.json').read_text()) == {
        'name': 'open-relay', 'version': '0.1.0',
        'repository': {'url': 'https://github.com/feddericovonwernich/open-relay.git'},
    } and (package / 'dist' / 'src' / 'cli.js').is_file())


def test_stale_system_candidate_results_in_usable_private_runtime(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    stale = make_relay_runtime(tmp_path / 'stale', version='0.0.9', executable=True)
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    runtime = relay.ensure_runtime(
        home, which=lambda name: str(stale / 'bin' / name), run=fake_npm_success(home))
    assert runtime == home / 'relay'
    assert runtime_has_open_relay_010(runtime)


def test_partial_private_runtime_is_preserved_and_refused(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'relay').mkdir(parents=True)
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    with pytest.raises(store.Refusal, match='partial'):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))
    assert (home / 'relay').is_dir()


def test_private_runtime_marker_must_match_pinned_archive(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    runtime = make_relay_runtime(home / 'relay')
    (runtime / 'relay-install.json').write_text(json.dumps({
        'archive_sha256': 'not-the-pinned-archive', 'package_version': '0.1.0',
        'node_version': '22.19.0', 'metadata_digest': 'wrong',
    }))
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    with pytest.raises(store.Refusal, match='provenance'):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))


def test_symlinked_private_runtime_is_refused(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / 'relay').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(relay, '_node_version', lambda run: '22.19.0')
    with pytest.raises(store.Refusal, match='symlink'):
        relay.ensure_runtime(home, which=lambda name: None, run=fake_npm_success(home))

def test_lock_tolerates_concurrent_state_directory_creation(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    state = home / 'state'
    original_mkdir = Path.mkdir

    def mkdir_raced(path, *args, **kwargs):
        if path == state:
            original_mkdir(path, *args, **kwargs)
            raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', mkdir_raced)
    with relay._lock(home) as locked:
        assert locked == state
    assert (state / 'relay-install.lock').is_file()


def test_project_service_is_private_and_idempotent(tmp_path, monkeypatch):
    from support import command, uninstall_scope
    home = tmp_path / 'home'
    command(home, 'init', '--backend', 'tmux')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    monkeypatch.setattr(relay, 'ensure_runtime', lambda *_args, **_kwargs: runtime)
    monkeypatch.setenv('AGD_RELAY_GITHUB_READ_TOKEN', 'test-read-token')

    def init(argv, cwd, **_kwargs):
        (cwd/'.relay'/'connectors').mkdir(parents=True)
        (cwd/'.relay'/'connectors'/'github.json').write_text('{"connector":"github","tokenEnv":"GITHUB_TOKEN","repositories":[{"name":"owner/repo","mode":"configured-tools"}],"triggers":[{}, {}, {}],"aggregate":{}}')
    first = relay.ensure_project_service(home, 'sample', 'owner/repo', run=init)
    second = relay.ensure_project_service(home, 'sample', 'owner/repo', run=init)

    assert first == second
    assert first['runtime'] == str(runtime)
    assert first['repository'] == 'owner/repo'
    assert first['root'] == str(home/'state'/'relay'/'projects'/'sample')
    connector = __import__('json').loads((home/'state'/'relay'/'projects'/'sample'/'.relay'/'connectors'/'github.json').read_text())
    assert connector == {'connector': 'github', 'tokenEnv': 'AGD_RELAY_GITHUB_READ_TOKEN',
                         'repositories': [{'name': 'owner/repo', 'mode': 'generic-check-runs'}], 'triggers': []}
    assert not (home/'relay').exists()
    with uninstall_scope(home, []):
        with pytest.raises(store.Refusal):
            relay.ensure_project_service(home, 'late', 'owner/repo', run=init)
        assert not (Path(first['root']).parent / 'late').exists()





def test_project_service_preserves_unrecorded_relay_state(tmp_path, monkeypatch):
    home = tmp_path/'home'
    root = home/'state'/'relay'/'projects'/'sample'/'.relay'/'connectors'
    root.mkdir(parents=True)
    (root/'github.json').write_text('{"connector":"github","tokenEnv":"GITHUB_TOKEN","repositories":[{"name":"owner/repo","mode":"configured-tools"}],"triggers":[{}, {}, {}],"aggregate":{}}')
    monkeypatch.setenv('AGD_RELAY_GITHUB_READ_TOKEN', 'test-read-token')
    monkeypatch.setattr(relay, 'ensure_runtime', lambda *_args, **_kwargs: tmp_path/'runtime')
    with pytest.raises(store.Refusal, match='without a service record'):
        relay.ensure_project_service(home, 'sample', 'owner/repo')

def test_project_service_rejects_symlinked_state_root(tmp_path, monkeypatch):
    import os
    home = tmp_path/'home'
    outside = tmp_path/'outside'; outside.mkdir()
    parent = home/'state'/'relay'/'projects'; parent.mkdir(parents=True)
    os.symlink(outside, parent/'sample')
    monkeypatch.setattr(relay, 'ensure_runtime', lambda *_args, **_kwargs: tmp_path/'system-relay')
    from agd_lib import store
    try:
        relay.ensure_project_service(home, 'sample', 'owner/repo')
    except store.Refusal:
        return
    raise AssertionError('symlinked project state was accepted')





def test_start_project_service_marks_exact_timed_out_server_unhealthy(tmp_path, monkeypatch):
    import json
    home = tmp_path/'home'
    root = home/'state'/'relay'/'projects'/'sample'
    (root/'.relay'/'connectors').mkdir(parents=True)
    runtime = tmp_path/'runtime'; (runtime/'bin').mkdir(parents=True)
    record = {'schema': 1, 'project': 'sample', 'repository': 'owner/repo',
              'runtime': str(runtime), 'root': str(root), 'health': 'starting'}
    (root/'service.json').write_text(json.dumps(record))
    monkeypatch.setattr(relay, 'ensure_project_service', lambda *_args, **_kwargs: record)
    monkeypatch.setattr('agd_lib.supervision.process_start', lambda pid: 'identity')

    class Process:
        pid = 99
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout): pass
    with pytest.raises(store.Refusal):
        relay.start_project_service(home, 'sample', 'owner/repo', popen=lambda *_args, **_kwargs: Process(), timeout=.01)
    state = json.loads((root/'service.json').read_text())
    assert state['health'] == 'unhealthy'






def test_connector_spawn_failure_marks_owned_service_unhealthy(tmp_path, monkeypatch):
    home = tmp_path/'home'
    root = home/'state'/'relay'/'projects'/'sample'
    (root/'.relay'/'connectors').mkdir(parents=True)
    (root/'.relay'/'connectors'/'github.json').write_text('{}')
    runtime = tmp_path/'runtime'; (runtime/'bin').mkdir(parents=True)
    record = {'schema': 1, 'project': 'sample', 'repository': 'owner/repo',
              'runtime': str(runtime), 'root': str(root), 'health': 'starting'}
    (root/'service.json').write_text(json.dumps(record))
    monkeypatch.setattr(relay, 'ensure_project_service', lambda *_args, **_kwargs: record)
    monkeypatch.setattr('agd_lib.supervision.process_start', lambda pid: 'identity')

    class Server:
        pid = 99
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout): pass
    def popen(argv, cwd, **_kwargs):
        if argv[1] == 'connect':
            raise OSError('connector unavailable')
        (cwd/'.relay'/'runtime.json').write_text(json.dumps({'pid': 99, 'port': 4444}))
        return Server()

    with pytest.raises(store.DependencyError, match='connector could not start'):
        relay.start_project_service(home, 'sample', 'owner/repo', popen=popen)
    assert json.loads((root/'service.json').read_text())['health'] == 'unhealthy'

def test_poll_and_reply_accept_pinned_delivery_shape(tmp_path, monkeypatch):
    from support import command, lab
    from agd_lib import tasks
    with lab() as (home, _project):
        brief = home/'brief.md'; brief.write_text('Change value.\n')
        command(home, 'task', 'create', 'relaypoll', '--project', 'sample', '--brief', str(brief), '--kind', 'ship')
        instruction = home/'grant.md'; instruction.write_text('Task relaypoll targets origin:agd/relaypoll.\n')
        grant = tasks.grant_pr_publish(home, 'relaypoll', 'origin:agd/relaypoll', instruction)
        task = tasks.read(home, 'relaypoll')
        task.update(phase='running', executor='worker', incarnation='worker-1')
        tasks.save(home, task)
        head = __import__('agd_lib.store', fromlist=['git']).git(Path(task['worktree']), 'rev-parse', 'HEAD')
        tasks.write_pr_receipt(home, 'relaypoll', {'schema': 1, 'grant_id': grant['id'], 'remote': 'origin',
                              'repository': 'owner/repo', 'branch': task['branch'], 'base': task['base'],
                              'local_head': head, 'remote_head': head, 'pr': {'number': 7, 'url': 'https://example.test/pr/7', 'id': 'P7'}})
        root, runtime = home/'state'/'relay'/'projects'/'sample', tmp_path/'runtime'
        (runtime/'bin').mkdir(parents=True); root.mkdir(parents=True)
        (root/'service.json').write_text(json.dumps({'runtime': str(runtime), 'root': str(root)}))
        monkeypatch.setenv('AGD_TASK_ID', 'relaypoll'); monkeypatch.setenv('AGD_INCARNATION', 'worker-1')
        delivery = {'event': {'id': 'event-1', 'type': 'agd.pr.review', 'version': 1,
                    'correlationId': 'github:owner/repo:pull-request:7',
                    'payload': {'repository': 'owner/repo', 'pullRequest': 7, 'headSha': head}},
                    'outputSchema': {'type': 'object', 'required': ['summary'], 'additionalProperties': False,
                                     'properties': {'summary': {'type': 'string', 'minLength': 1}}},
                    'attempt': 1, 'workerId': 'worker-1', 'leaseId': 'lease-1',
                    'leaseExpiresAt': 'x', 'hardDeadlineAt': 'y'}
        def run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps(delivery if 'poll' in argv else {'ok': True}), '')
        assert relay.poll(home, task, grant, 'worker-1', run=run)['lease_id'] == 'lease-1'
        assert relay.reply_file(home, task, grant, 'worker-1', 'lease-1', 'start', None, run=run)['status'] == 'started'
        reply = tasks.task_file(home, 'relaypoll').parent/'relay-reply.json'
        assert reply == home/'data'/'tasks'/'relaypoll'/'relay-reply.json'
        reply.write_text('{"result":{"summary":"received"},"effects":[]}')
        assert relay.reply_file(home, task, grant, 'worker-1', 'lease-1', 'complete', reply, run=run)['status'] == 'settled'
        stale = {'event': {'id': 'event-2', 'type': 'pr.ci.settled', 'version': 1,
                 'correlationId': 'github:owner/repo:pull-request:7',
                 'payload': {'repository': {'fullName': 'owner/repo'},
                             'pullRequest': {'number': 7, 'headSha': 'stale', 'baseRef': task['base']}}},
                 'outputSchema': delivery['outputSchema'], 'attempt': 1, 'workerId': 'worker-1',
                 'leaseId': 'lease-2', 'leaseExpiresAt': 'x', 'hardDeadlineAt': 'y'}
        calls = []
        def stale_run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(stale if 'poll' in argv else {'ok': True}), '')
        with pytest.raises(store.Refusal, match='delivery identity'):
            relay.poll(home, task, grant, 'worker-1', run=stale_run)
        assert any(argv[2:5] == ['reply', 'lease-2', 'cancelled'] for argv in calls)


def test_poll_rejects_prepared_or_stale_worker_without_subprocess(tmp_path, monkeypatch):
    from support import command, lab
    from agd_lib import tasks
    with lab() as (home, _project):
        brief = home/'brief.md'; brief.write_text('Change value.\n')
        command(home, 'task', 'create', 'notrunning', '--project', 'sample', '--brief', str(brief), '--kind', 'ship')
        task = tasks.read(home, 'notrunning')
        monkeypatch.setenv('AGD_TASK_ID', 'notrunning'); monkeypatch.setenv('AGD_INCARNATION', 'stale')
        called = False
        def run(*_args, **_kwargs):
            nonlocal called
            called = True
        with pytest.raises(store.Refusal, match='current worker'):
            relay.poll(home, task, {'id': 'grant'}, 'stale', run=run)
        assert not called


def test_uninstall_relay_rejects_copy_and_stops_only_original(tmp_path):
    import json
    import shutil
    import subprocess
    import sys
    from agd_lib import supervision
    from support import command, uninstall_scope
    home = tmp_path / 'home'
    command(home, 'init', '--backend', 'tmux')
    root = home / 'state/relay/projects/sample'
    root.mkdir(parents=True)
    process = subprocess.Popen(
        [sys.executable, '-c', 'import sys\nprint("ready", flush=True)\nsys.stdin.read()'],
        cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        assert process.stdout.readline() == 'ready\n'
        identity = supervision.process_identity(process.pid)
        identity.pop('state')
        path = root / 'service.json'
        store.atomic_json(path, {
            'schema': 1, 'project': 'sample', 'repository': 'fixture/repo',
            'runtime': str(tmp_path / 'shared-runtime'), 'root': str(root),
            'health': 'healthy', 'target': supervision.runtime_home_identity(home),
            'server': identity})
        copy = tmp_path / 'copy'
        shutil.copytree(home, copy)
        copied = relay.uninstall_inventory(copy)
        assert any(row['ownership'] != 'owned' and row['blockers'] for row in copied)
        assert process.poll() is None
        resources = relay.uninstall_inventory(home)
        original = path.read_bytes()
        with uninstall_scope(home, resources) as operation:
            relay.uninstall_quiesce(home, resources, operation)
            assert process.wait(timeout=3) != 0
            assert path.read_bytes() == original
            assert all(row['presence'] == 'absent'
                       for row in relay.uninstall_inventory(home, expected=resources))
            assert resources[0]['key'] in operation['completed']
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=3)
