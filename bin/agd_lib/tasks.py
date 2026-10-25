import json
import os
import uuid
from pathlib import Path
from . import projects, store


def task_file(home, task_id):
    return home/'data'/'tasks'/store.validate_id(task_id)/'task.json'


def read(home, task_id):
    path = task_file(home, task_id)
    if not path.exists():
        raise store.Refusal(f'unknown task: {task_id}')
    return json.loads(path.read_text())


def save(home, task):
    store.atomic_json(task_file(home, task['id']), task)


def create(home, task_id, project_id, brief, base=None, kind='ship'):
    store.validate_id(task_id)
    project = projects.read(home, project_id)
    if kind not in ('ship', 'scout'):
        raise ValueError('kind must be ship or scout')
    brief_text = Path(brief).read_text()
    if not brief_text.strip():
        raise ValueError('brief must not be empty')
    base = base or project['base']
    clone = Path(project['path'])
    start = store.git(clone, 'rev-parse', '--verify', base)
    home_id = json.loads((home/'data'/'home.json').read_text())['home_id']
    attempt = str(uuid.uuid4())
    holder = f'{home_id}:{task_id}:{attempt}'
    with store.lock(home):
        if task_file(home, task_id).exists():
            raise store.Refusal('task already exists; reconcile its lease instead')
        task = dict(schema=1, id=task_id, project=project_id, kind=kind, phase='preparing',
                    condition='normal', base=base, start=start, branch='agd/'+task_id,
                    lease_holder=holder, attempt=attempt, executor='central')
        directory = task_file(home, task_id).parent
        directory.mkdir(parents=True, mode=0o700)
        (directory/'brief.md').write_text(brief_text)
        save(home, task)
    empty = task_file(home, task_id).parent/'include-empty'
    empty.write_text('')
    try:
        allocation = json.loads(store.run(
            ['treehouse', 'get', '--lease', '--json', '--lease-holder', holder,
             '--base', base, '--branch', task['branch'], '--include-file', str(empty)],
            cwd=clone, timeout=120).stdout)
        path = Path(allocation['path']).resolve()
        if path == clone or store.git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') != project['common_dir']:
            raise store.Refusal('Treehouse returned a worktree outside registered clone')
        if store.git(path, 'branch', '--show-current') != task['branch']:
            raise store.Refusal('Treehouse returned the wrong task branch')
        if store.git(path, 'rev-parse', 'HEAD') != start:
            raise store.Refusal('Treehouse acquired from an unexpected base commit')
        with store.lock(home):
            task.update(phase='prepared', worktree=str(path), lease_id=allocation['lease_id'])
            save(home, task)
        return task
    finally:
        empty.unlink(missing_ok=True)


def reconcile_allocation(home, task_id):
    task = read(home, task_id)
    if task['phase'] != 'preparing':
        return task
    project = projects.read(home, task['project'])
    status = json.loads(store.run(['treehouse', 'status', '--json'], cwd=project['path']).stdout)
    # The documented status format is checked against the installed binary in the integration test.
    matches = [item for item in status['worktrees'] if item.get('lease_holder') == task['lease_holder']]
    if len(matches) != 1:
        raise store.Refusal(f'lease allocation uncertain: {len(matches)} matching slots; inspect Treehouse')
    match = matches[0]
    path = Path(match['path']).resolve()
    if store.git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') != project['common_dir'] or store.git(path, 'branch', '--show-current') != task['branch']:
        raise store.Refusal('recovered lease identity mismatch')
    with store.lock(home):
        task.update(phase='prepared', worktree=str(path), lease_id=match['lease_id'])
        save(home, task)
    return task
