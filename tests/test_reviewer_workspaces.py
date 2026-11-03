import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from support import lab
from test_projects import run
from test_reviewer import produced_run

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))


@pytest.fixture
def workspace_lab(monkeypatch):
    """Use an owned registered clone, real review run, and scoped Treehouse pool."""
    treehouse = shutil.which('treehouse')
    if not treehouse:
        pytest.fail('Treehouse is required for reviewer workspace acceptance')
    treehouse_dir = str(Path(treehouse).resolve().parent)
    with lab() as (home, project):
        initial = run(['git', 'rev-parse', 'HEAD'], cwd=project).stdout.strip()
        (project / 'later.txt').write_text('second existing commit\n')
        run(['git', 'add', 'later.txt'], cwd=project)
        run(['git', 'commit', '-m', 'second fixture commit'], cwd=project)
        stable_head = run(['git', 'rev-parse', 'HEAD'], cwd=project).stdout.strip()
        stable_branch = run(['git', 'branch', '--show-current'], cwd=project).stdout.strip()
        stable_bytes = (project / 'value.txt').read_bytes(), (project / 'later.txt').read_bytes()
        private_home = home.parent / 'probe-user-home'
        private_config = home.parent / 'probe-user-config'
        private_home.mkdir(mode=0o700)
        private_config.mkdir(mode=0o700)
        monkeypatch.setenv('HOME', str(private_home))
        monkeypatch.setenv('XDG_CONFIG_HOME', str(private_config))
        review_run, selected = produced_run(home)
        try:
            yield home, project, initial, review_run['review_run_id'], selected
        finally:
            from agd_lib import reviewer
            receipt = reviewer._receipt(home, review_run['review_run_id'])
            if receipt.get('dispatch_state') not in {'final', 'closed'}:
                reviewer.close_run(home, review_run['review_run_id'], 'failed', error_code='fixture')
            assert ((project / 'value.txt').read_bytes(), (project / 'later.txt').read_bytes()) == stable_bytes
            assert run(['git', 'rev-parse', 'HEAD'], cwd=project).stdout.strip() == stable_head
            assert run(['git', 'branch', '--show-current'], cwd=project).stdout.strip() == stable_branch
            assert run(['git', 'status', '--porcelain=v1'], cwd=project).stdout == ''


def _workspace_module():
    from agd_lib import reviewer_workspaces
    return reviewer_workspaces


def _status(home, project):
    from agd_lib import store
    return json.loads(store.run([
        'treehouse', '--root', str(home / 'data' / 'treehouse'), 'status', '--json'],
        cwd=project).stdout)


def _row(home, project, lease_id):
    rows = [row for row in _status(home, project) if row.get('lease_id') == lease_id]
    assert len(rows) == 1
    return rows[0]


def _cleanup_return_or_safe_refusal(workspaces, home, project, run_id, commit, lease_id, path, **kwargs):
    from agd_lib import store
    unknown = [item for item in workspaces._processes_in(path)
               if item.get('inspection') == 'unconfirmed']
    try:
        result = workspaces.cleanup(home, run_id, 'sample', commit, **kwargs)
    except store.Refusal:
        if not unknown:
            raise
        assert _row(home, project, lease_id)['lease_id'] == lease_id
        return None
    if result['outcome'] != 'returned':
        assert unknown and result['outcome'] == 'retained'
        assert _row(home, project, lease_id)['lease_id'] == lease_id
        return None
    assert not any(row.get('lease_id') == lease_id for row in _status(home, project))
    return result


def _close_review(home, run_id):
    from agd_lib import reviewer
    reviewer.close_run(home, run_id, 'failed', error_code='fixture')


def _operator_instruction(home, run_id, project='sample', commit=''):
    path = home / f'operator-{run_id}.txt'
    path.write_text(f'cleanup exact run={run_id} project={project} commit={commit}\n')
    return path


def _assert_identity(home, project, commit, result):
    path = Path(result['path'])
    assert path.is_dir() and path.resolve() != project.resolve()
    assert result['lease_id']
    assert result['lease_holder']
    from agd_lib import projects
    assert result['base_ref_oid'] == commit
    assert subprocess.run(
        ['git', 'rev-parse', '--verify', f"refs/heads/{result['base_ref']}^{{commit}}"],
        cwd=project, check=True, capture_output=True, text=True).stdout.strip() == commit
    assert result['branch'] == subprocess.run(
        ['git', 'branch', '--show-current'], cwd=path, check=True,
        capture_output=True, text=True).stdout.strip()
    assert subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=path, check=True,
        capture_output=True, text=True).stdout.strip() == commit
    common_dir = subprocess.run(
        ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=path,
        check=True, capture_output=True, text=True).stdout.strip()
    expected_common = subprocess.run(
        ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=project,
        check=True, capture_output=True, text=True).stdout.strip()
    assert common_dir == expected_common == result['common_dir']
    row = _row(home, project, result['lease_id'])
    assert row['lease_holder'] == result['lease_holder']
    assert Path(row['path']).resolve() == path.resolve()
    assert row.get('branch') == result['branch']


def test_probe_uses_exact_registered_clone_and_commit(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    result = workspaces.create(home, run_id, 'sample', commit)
    _assert_identity(home, project, commit, result)
    home_id = json.loads((home / 'data' / 'home.json').read_text())['home_id']
    assert all(part in result['lease_holder']
               for part in (home_id, run_id, 'sample', commit, result['attempt']))
    assert str(project.resolve()) != result['path']
    assert result['project'] == 'sample'
    assert result['review_run_id'] == run_id
    assert result['exposure'] == 'exposed'


def test_probe_existing_commit_does_not_fetch_or_run_user_setup(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    user_config = Path(os.environ['XDG_CONFIG_HOME']) / 'treehouse'
    user_config.mkdir(parents=True, exist_ok=True)
    marker = Path(os.environ['HOME']) / 'post-create-ran'
    user_config_file = user_config / 'config.toml'
    user_config_file.write_text(
        f"[hooks]\npost_create = [\"printf hook-ran > '{marker}'\"]\n")
    before_config = user_config_file.read_bytes()
    origin = home.parent / 'origin.git'
    offline_origin = home.parent / 'origin.offline'
    origin.rename(offline_origin)
    try:
        result = workspaces.create(home, run_id, 'sample', commit)
    finally:
        offline_origin.rename(origin)
    assert not marker.exists()
    assert user_config_file.read_bytes() == before_config
    _assert_identity(home, project, commit, result)


def test_allocation_reconciles_exact_holder_after_interruption(workspace_lab, monkeypatch):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    original = workspaces.store.atomic_json
    interrupted = []

    def interrupt(path, value):
        if value.get('lease_id') and not interrupted:
            interrupted.append(True)
            raise RuntimeError('fixture interruption after Treehouse allocation')
        return original(path, value)

    monkeypatch.setattr(workspaces.store, 'atomic_json', interrupt)
    with pytest.raises(RuntimeError, match='fixture interruption'):
        workspaces.create(home, run_id, 'sample', commit)
    monkeypatch.setattr(workspaces.store, 'atomic_json', original)
    result = workspaces.create(home, run_id, 'sample', commit)
    _assert_identity(home, project, commit, result)
    assert len([row for row in _status(home, project)
                if row.get('lease_holder') == result['lease_holder']]) == 1


def test_same_run_project_commit_does_not_allocate_twice(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    first = workspaces.create(home, run_id, 'sample', commit)
    second = workspaces.create(home, run_id, 'sample', commit)
    assert {key: second[key] for key in ('lease_id', 'lease_holder', 'path', 'branch', 'commit')} == {
        key: first[key] for key in ('lease_id', 'lease_holder', 'path', 'branch', 'commit')}
    assert len([row for row in _status(home, project)
                if row.get('lease_holder') == first['lease_holder']]) == 1


def test_foreign_or_ambiguous_lease_is_refused_by_receipt_consumer(workspace_lab):
    home, project, commit, run_id, selected = workspace_lab
    from agd_lib import store
    workspaces = _workspace_module()
    original = workspaces.create(home, run_id, 'sample', commit)
    root = home / 'data' / 'treehouse'
    base = json.loads((home / 'data' / 'projects' / 'sample.json').read_text())['base']
    include = home / 'empty-include'
    include.write_text('')

    def allocate(holder):
        return json.loads(store.run([
            'treehouse', '--root', str(root), 'get', '--lease', '--json', '--no-fetch',
            '--lease-holder', holder, '--base', base, '--include-file', str(include)],
            cwd=project).stdout)

    foreign = allocate('foreign-owner')
    receipt_path = workspaces._receipt_path(home, run_id, 'sample', commit)
    record = workspaces._record(receipt_path)
    record['lease_id'] = foreign['lease_id']
    workspaces._write(receipt_path, record)
    with pytest.raises(store.Refusal, match='lease identity changed'):
        workspaces.list_workspaces(home, run_id)
    assert _row(home, project, original['lease_id'])['lease_holder'] == original['lease_holder']
    assert _row(home, project, foreign['lease_id'])['lease_holder'] == 'foreign-owner'

    from test_reviewer import _retry_run_with_packet
    ambiguous_run = _retry_run_with_packet(home, selected)
    ambiguous_id = ambiguous_run['review_run_id']
    probe = workspaces.create(home, ambiguous_id, 'sample', commit)
    ambiguous = [allocate('ambiguous-owner'), allocate('ambiguous-owner')]
    receipt_path = workspaces._receipt_path(home, ambiguous_id, 'sample', commit)
    record = workspaces._record(receipt_path)
    record['lease_holder'] = 'ambiguous-owner'
    workspaces._write(receipt_path, record)
    with pytest.raises(store.Refusal, match='ambiguous probe lease holder'):
        workspaces.list_workspaces(home, ambiguous_id)
    assert all(_row(home, project, item['lease_id'])['lease_holder'] == 'ambiguous-owner'
               for item in ambiguous)
    assert _row(home, project, probe['lease_id'])['lease_holder'] == probe['lease_holder']


def test_returned_receipt_is_durable_for_public_list(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    result = workspaces.create(home, run_id, 'sample', commit)
    record = workspaces._records(home, run_id)[0]
    workspaces._return(home, project, record)
    listed = workspaces.list_workspaces(home, run_id)
    assert len(listed) == 1
    item = listed[0]
    assert {item[key] for key in ('review_run_id', 'project', 'commit',
                                  'lease_id', 'lease_holder')} == {
        run_id, 'sample', commit, result['lease_id'], result['lease_holder']}
    assert item['state'] == 'returned'
    assert item['exposure'] == 'exposed'
    assert 'path' not in item
    assert not any(row.get('lease_id') == result['lease_id'] for row in _status(home, project))

def test_dirty_or_committed_probe_is_retained(workspace_lab):
    home, project, commit, run_id, selected = workspace_lab
    workspaces = _workspace_module()
    dirty = workspaces.create(home, run_id, 'sample', commit)
    dirty_path = Path(dirty['path'])
    (dirty_path / 'local-note.txt').write_text('retain this work\n')
    _close_review(home, run_id)
    instruction = _operator_instruction(home, run_id, commit=commit)
    retained = workspaces.cleanup(home, run_id, 'sample', commit, instruction=instruction)
    assert retained['outcome'] in ('retained', 'refused')
    assert dirty_path.exists() and (dirty_path / 'local-note.txt').exists()
    assert _row(home, project, dirty['lease_id'])['lease_holder'] == dirty['lease_holder']

    from test_reviewer import _retry_run_with_packet
    committed_run = _retry_run_with_packet(home, selected)
    committed_id = committed_run['review_run_id']
    committed = workspaces.create(home, committed_id, 'sample', commit)
    committed_path = Path(committed['path'])
    (committed_path / 'committed.txt').write_text('retain this commit\n')
    run(['git', 'add', 'committed.txt'], cwd=committed_path)
    run(['git', 'commit', '-m', 'probe fixture commit'], cwd=committed_path)
    _close_review(home, committed_id)
    instruction = _operator_instruction(home, committed_id, commit=commit)
    retained = workspaces.cleanup(home, committed_id, 'sample', commit, instruction=instruction)
    assert retained['outcome'] in ('retained', 'refused')
    assert committed_path.exists()
    assert _row(home, project, committed['lease_id'])['lease_holder'] == committed['lease_holder']


def test_exposed_probe_is_retained_without_containment_proof(workspace_lab):
    home, project, commit, run_id, selected = workspace_lab
    workspaces = _workspace_module()
    result = workspaces.create(home, run_id, 'sample', commit)
    retained = workspaces.cleanup(home, run_id, 'sample', commit)
    assert retained['outcome'] in ('retained', 'refused')
    assert retained.get('retained') or retained.get('reason')
    assert Path(result['path']).exists()
    assert _row(home, project, result['lease_id'])['lease_holder'] == result['lease_holder']
    _close_review(home, run_id)

    from test_reviewer import _retry_run_with_packet
    clean_run = _retry_run_with_packet(home, selected)
    clean_id = clean_run['review_run_id']
    clean = workspaces.create(home, clean_id, 'sample', commit)
    _close_review(home, clean_id)
    instruction = _operator_instruction(home, clean_id, commit=commit)
    returned = _cleanup_return_or_safe_refusal(
        workspaces, home, project, clean_id, commit, clean['lease_id'], Path(clean['path']),
        instruction=instruction)
    if returned is None:
        assert any(item.get('path') == clean['path'] for item in workspaces.list_workspaces(home, clean_id))
        return
    assert all('path' not in item for item in workspaces.list_workspaces(home, clean_id))


def test_unexposed_clean_allocation_can_be_returned(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    script = r'''
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agd_lib import reviewer_workspaces, store
original = store.atomic_json
def interrupt(path, value):
    if value.get('lease_id') and value.get('exposure') != 'exposed':
        original(path, value)
        os._exit(87)
    return original(path, value)
store.atomic_json = interrupt
reviewer_workspaces.create(Path(sys.argv[2]), sys.argv[3], 'sample', sys.argv[4])
'''
    child = subprocess.run(
        [sys.executable, '-B', '-c', script, str(ROOT / 'bin'), str(home), run_id, commit],
        env=os.environ.copy(), capture_output=True, text=True)
    assert child.returncode == 87, child.stderr
    listed = workspaces.list_workspaces(home, run_id)
    assert listed and all(not item.get('path') for item in listed)
    lease_id = listed[0]['lease_id']
    path = Path(_row(home, project, lease_id)['path'])
    _close_review(home, run_id)
    returned = _cleanup_return_or_safe_refusal(
        workspaces, home, project, run_id, commit, lease_id, path)
    if returned is None:
        return


def test_detached_or_uncertain_probe_user_blocks_return(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    result = workspaces.create(home, run_id, 'sample', commit)
    path = Path(result['path'])
    process = subprocess.Popen(['sleep', '30'], cwd=path, start_new_session=True)
    _close_review(home, run_id)
    instruction = _operator_instruction(home, run_id, commit=commit)
    from agd_lib import store
    try:
        retained = workspaces.cleanup(home, run_id, 'sample', commit, instruction=instruction)
        assert retained['outcome'] in ('retained', 'refused')
        assert path.exists()
        assert _row(home, project, result['lease_id'])['lease_holder'] == result['lease_holder']
    except store.Refusal:
        if not any(item.get('inspection') == 'unconfirmed' for item in workspaces._processes_in(path)):
            raise
        retained = {'outcome': 'refused'}
    finally:
        process.terminate()
        process.wait(timeout=10)
    returned = _cleanup_return_or_safe_refusal(
        workspaces, home, project, run_id, commit, result['lease_id'], path,
        instruction=instruction)
    if returned is None:
        return


def test_directory_fd_holder_blocks_return_when_cwd_is_elsewhere(workspace_lab):
    home, project, commit, run_id, _ = workspace_lab
    workspaces = _workspace_module()
    result = workspaces.create(home, run_id, 'sample', commit)
    path = Path(result['path'])
    child = subprocess.Popen(
        [sys.executable, '-c',
         'import os, sys, time; fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY); '
         'os.chdir("/"); print("ready", flush=True); time.sleep(30)', str(path)],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    _close_review(home, run_id)
    instruction = _operator_instruction(home, run_id, commit=commit)
    from agd_lib import store
    try:
        assert child.stdout.readline().strip() == 'ready'
        users = workspaces._processes_in(path)
        assert any(item.get('pid') == child.pid and item.get('inspection') != 'unconfirmed'
                   for item in users)
        try:
            retained = workspaces.cleanup(home, run_id, 'sample', commit, instruction=instruction)
        except store.Refusal:
            assert any(item.get('inspection') == 'unconfirmed' for item in users)
            retained = {'outcome': 'refused'}
        assert retained['outcome'] in ('retained', 'refused')
        assert _row(home, project, result['lease_id'])['lease_holder'] == result['lease_holder']
    finally:
        child.terminate()
        child.wait(timeout=10)
    users = workspaces._processes_in(path)
    assert not any(item.get('pid') == child.pid for item in users)
    returned = _cleanup_return_or_safe_refusal(
        workspaces, home, project, run_id, commit, result['lease_id'], path,
        instruction=instruction)
    if returned is None:
        return
    assert all('path' not in item for item in workspaces.list_workspaces(home, run_id))
