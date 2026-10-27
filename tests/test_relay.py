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
    home = tmp_path / 'home'
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


def test_project_service_requires_token_before_runtime_or_state(tmp_path, monkeypatch):
    called = False
    def runtime(*_args, **_kwargs):
        nonlocal called
        called = True
        return tmp_path/'runtime'
    monkeypatch.setattr(relay, 'ensure_runtime', runtime)
    from agd_lib import store
    with pytest.raises(store.Refusal, match='AGD_RELAY_GITHUB_READ_TOKEN'):
        relay.ensure_project_service(tmp_path/'home', 'sample', 'owner/repo')
    assert not called and not (tmp_path/'home').exists()



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


def test_start_project_service_starts_one_owned_connector(tmp_path, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    home = tmp_path/'home'
    root = home/'state'/'relay'/'projects'/'sample'
    (root/'.relay'/'connectors').mkdir(parents=True)
    (root/'.relay'/'connectors'/'github.json').write_text('{}')
    runtime = tmp_path/'runtime'
    (runtime/'bin').mkdir(parents=True)
    record = {'schema': 1, 'project': 'sample', 'repository': 'owner/repo',
              'runtime': str(runtime), 'root': str(root), 'health': 'starting'}
    (root/'service.json').write_text(json.dumps(record))
    monkeypatch.setattr(relay, 'ensure_project_service', lambda *_args, **_kwargs: record)
    monkeypatch.setattr('agd_lib.supervision.process_start', lambda pid: f'start-{pid}')
    monkeypatch.setattr('agd_lib.supervision.gone', lambda *_args: False)

    class Process:
        next_pid = 10
        def __init__(self, argv, cwd, **_kwargs):
            self.argv, self.cwd, self.pid = argv, cwd, Process.next_pid
            Process.next_pid += 1
            if argv[1] == 'start':
                (cwd/'.relay'/'runtime.json').write_text(json.dumps({'pid': self.pid, 'port': 4444}))
        def poll(self):
            return None

    launched = []
    def popen(*args, **kwargs):
        process = Process(*args, **kwargs)
        launched.append(process)
        return process

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: relay.start_project_service(home, 'sample', 'owner/repo', popen=popen), range(2)))

    assert first['health'] == second['health'] == 'healthy'
    assert len(launched) == 2
    assert launched[1].argv[1:4] == ['connect', 'github', '--config']
    assert second['connector']['pid'] == launched[1].pid


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
    with pytest.raises(store.Refusal, match='did not publish'):
        relay.start_project_service(home, 'sample', 'owner/repo', popen=lambda *_args, **_kwargs: Process(), timeout=.01)
    state = json.loads((root/'service.json').read_text())
    assert state['health'] == 'unhealthy'
    assert state['server'] == {'pid': 99, 'start': 'identity', 'status': 'starting'}


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
        assert relay.reply(home, task, grant, 'worker-1', 'lease-1', 'start', None, run=run)['status'] == 'started'
        assert relay.reply(home, task, grant, 'worker-1', 'lease-1', 'complete',
                           {'result': {'summary': 'received'}, 'effects': []}, run=run)['status'] == 'settled'
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
