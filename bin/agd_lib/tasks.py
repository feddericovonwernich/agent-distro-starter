import hashlib
import stat
import re
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit
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

def treehouse_args(task):
    return ['treehouse', '--root', task['treehouse_root']] if task.get('treehouse_root') else ['treehouse']



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
        if os.environ.get('TREEHOUSE_ROOT'):
            root = Path(os.environ['TREEHOUSE_ROOT'])
            task['treehouse_root'] = str(root if root.is_absolute() else (clone/root).resolve())
        directory = task_file(home, task_id).parent
        directory.mkdir(parents=True, mode=0o700)
        (directory/'brief.md').write_text(brief_text)
        save(home, task)
    empty = task_file(home, task_id).parent/'include-empty'
    empty.write_text('')
    try:
        allocation = json.loads(store.run(
            [*treehouse_args(task), 'get', '--lease', '--json', '--lease-holder', holder,
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
    status = json.loads(store.run([*treehouse_args(task), 'status', '--json'], cwd=project['path']).stdout)
    matches = [item for item in status if item.get('lease_holder') == task['lease_holder']]
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


def verify_workspace(home, task):
    project = projects.read(home, task['project'])
    work = Path(task['worktree'])
    if work.is_symlink() or not work.is_dir() or work.resolve() == Path(project['path']):
        raise store.Refusal('task workspace is absent or points at stable clone')
    if store.git(work, 'rev-parse', '--path-format=absolute', '--git-common-dir') != project['common_dir']:
        raise store.Refusal('task Git clone identity changed')
    if store.git(work, 'branch', '--show-current') != task['branch']:
        raise store.Refusal('task branch identity changed')
    status = json.loads(store.run([*treehouse_args(task), 'status', '--json'], cwd=project['path']).stdout)
    matches = [row for row in status if Path(row['path']).resolve() == work.resolve()]
    if len(matches) != 1 or matches[0].get('lease_id') != task['lease_id'] or matches[0].get('lease_holder') != task['lease_holder']:
        raise store.Refusal('task lease identity changed')
    return work


def verify_worker_skills(home, task):
    if task.get('worker_skills'):
        from . import worker_skills
        worker_skills.verify(home, task)
        paths = [str(Path(record['destination']).relative_to(task['worktree']))
                 for record in task['worker_skills']['skills']]
        history = store.run(['git', 'rev-list', '--max-count=1', f'{task["start"]}..HEAD', '--', *paths],
                            cwd=task['worktree']).stdout
        if history.strip():
            raise store.Refusal('worker skill artifact appears in task history')


def ready(home, task_id, report):
    text = Path(report).read_text()
    if not text.strip():
        raise store.Refusal('empty report')
    with store.lock(home):
        task = read(home, task_id)
        if task['phase'] not in ('prepared', 'running'):
            raise store.Refusal('task not prepared or running')
        if os.environ.get('AGD_TASK_ID') and (os.environ['AGD_TASK_ID'] != task_id or
                                              os.environ.get('AGD_INCARNATION') != task.get('incarnation')):
            raise store.Refusal('worker incarnation cannot report this task')
        work = verify_workspace(home, task)
        verify_worker_skills(home, task)
        if not store.clean(work):
            raise store.Refusal('task workspace has uncommitted or untracked changes')
        head = store.git(work, 'rev-parse', 'HEAD')
        if task['kind'] == 'ship' and (head == task['start'] or store.run(['git', 'merge-base', '--is-ancestor', task['start'], head], cwd=work, check=False).returncode):
            raise store.Refusal('ship task needs committed changes from its base')
        if task['kind'] == 'scout' and head != task['start']:
            raise store.Refusal('scout contains unexpected commits')
        (task_file(home, task_id).parent/'report.md').write_text(text)
        task.update(phase='ready', ready_head=head, report='report.md')
        save(home, task)
    return task


def pr_receipt_file(home, task_id):
    return task_file(home, task_id).parent/'pr.json'


def validate_pr_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get('schema') != 1 or not isinstance(receipt.get('grant_id'), str):
        raise store.Refusal('PR receipt is invalid')
    if receipt.get('blocked_reason'):
        return receipt
    required = ('remote', 'repository', 'branch', 'base', 'local_head', 'remote_head', 'pr')
    if any(not isinstance(receipt.get(key), str) or not receipt[key] for key in required[:-1]):
        raise store.Refusal('PR receipt is invalid')
    pull_request = receipt['pr']
    if not isinstance(pull_request, dict) or not isinstance(pull_request.get('number'), int) or pull_request['number'] < 1:
        raise store.Refusal('PR receipt is invalid')
    return receipt




def read_pr_receipt(home, task_id):
    path = pr_receipt_file(home, task_id)
    if not path.exists():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise store.Refusal('PR receipt is unsafe')
    try:
        return validate_pr_receipt(json.loads(path.read_text()))
    except json.JSONDecodeError as error:
        raise store.Refusal('PR receipt is invalid') from error


def _write_pr_receipt_locked(home, task_id, receipt):
    validate_pr_receipt(receipt)
    read(home, task_id)
    path = pr_receipt_file(home, task_id)
    if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)):
        raise store.Refusal('PR receipt is unsafe')
    store.atomic_json(path, receipt)


def write_pr_receipt(home, task_id, receipt):
    with store.lock(home):
        _write_pr_receipt_locked(home, task_id, receipt)


def grant_pr_publish(home, task_id, target, instruction):
    with store.lock(home):
        task = read(home, task_id)
        if task['kind'] != 'ship':
            raise store.Refusal('PR publication requires a ship task')
        try:
            remote, branch = target.split(':', 1)
        except ValueError as error:
            raise store.Refusal('PR publication target must be REMOTE:BRANCH') from error
        if not remote or branch != task['branch']:
            raise store.Refusal('PR publication target must match the assigned task branch')
        text = Path(instruction).read_text()
        if not text.strip():
            raise store.Refusal('explicit instruction required')
        if not re.search(rf'(?<![A-Za-z0-9-]){re.escape(target)}(?![A-Za-z0-9-])', text):
            raise store.Refusal('instruction must identify exact task and target')
        task_without_target = re.sub(rf'(?<![A-Za-z0-9-]){re.escape(target)}(?![A-Za-z0-9-])', '', text)
        if not re.search(rf'(?<![A-Za-z0-9-]){re.escape(task_id)}(?![A-Za-z0-9-])', task_without_target):
            raise store.Refusal('instruction must independently name exact task')
        work = verify_workspace(home, task)
        if not store.clean(work):
            raise store.Refusal('task workspace has uncommitted or untracked changes')
        remote_url = store.git(work, 'remote', 'get-url', remote, check=False)
        if not remote_url:
            raise store.Refusal('PR publication remote is unavailable')
        grant = dict(id=str(uuid.uuid4()), task=task_id, action='pr-publish', target=target,
                     instruction=text, grant_head=store.git(work, 'rev-parse', 'HEAD'),
                     branch=task['branch'], base=task['base'], remote=remote, remote_url=remote_url)
        store.atomic_json(task_file(home, task_id).parent/'approvals'/f'{grant["id"]}.json', grant)
        return grant


def _authorize_pr_publish_locked(home, task, grant_id):
    task = read(home, task['id'])
    try:
        grant_id = str(uuid.UUID(grant_id))
        grant = json.loads((task_file(home, task['id']).parent/'approvals'/f'{grant_id}.json').read_text())
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        raise store.Refusal('PR publication grant missing or invalid') from error
    if (grant.get('task') != task['id'] or grant.get('action') != 'pr-publish' or
            grant.get('branch') != task['branch'] or grant.get('base') != task['base'] or
            grant.get('target') != f'{grant.get("remote")}:{task["branch"]}'):
        raise store.Refusal('PR publication grant does not match task target')
    work = verify_workspace(home, task)
    if not store.clean(work):
        raise store.Refusal('task workspace has uncommitted or untracked changes')
    if store.git(work, 'remote', 'get-url', grant['remote'], check=False) != grant.get('remote_url'):
        raise store.Refusal('PR publication remote changed')
    if store.run(['git', 'merge-base', '--is-ancestor', grant['grant_head'], 'HEAD'],
                 cwd=work, check=False).returncode:
        raise store.Refusal('PR publication head is not a descendant of grant head')
    return grant


def authorize_pr_publish(home, task, grant_id):
    with store.lock(home):
        return _authorize_pr_publish_locked(home, task, grant_id)


def approve(home, task_id, action, target, instruction):
    task = read(home, task_id)
    if action not in ('local-merge', 'publish', 'discard', 'unattended'):
        raise ValueError('unsupported approval')
    text = Path(instruction).read_text().strip()
    if not text:
        raise store.Refusal('explicit instruction required')
    if action == 'discard' and target != task_id:
        raise store.Refusal('discard approval must target exact task')
    work = verify_workspace(home, task)
    evidence = dict(id=str(uuid.uuid4()), task=task_id, action=action, target=target,
                    head=store.git(work, 'rev-parse', 'HEAD'), instruction=text)
    if action == 'discard':
        evidence['inventory'] = inventory(work)
    store.atomic_json(task_file(home, task_id).parent/'approvals'/f'{evidence["id"]}.json', evidence)
    return evidence


def authorization(home, task, approval_id, action, target):
    try:
        approval_id = str(uuid.UUID(approval_id))
        approval = json.loads((task_file(home, task['id']).parent/'approvals'/f'{approval_id}.json').read_text())
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        raise store.Refusal('approval missing or invalid') from error
    if approval['task'] != task['id'] or approval['action'] != action or approval['target'] != target:
        raise store.Refusal('approval does not match operation and target')
    work = verify_workspace(home, task)
    if approval['head'] != store.git(work, 'rev-parse', 'HEAD'):
        raise store.Refusal('approval bound to a different head')
    if action == 'discard' and approval.get('inventory') != inventory(work):
        raise store.Refusal('dirty path inventory changed since discard approval')
    return approval


def inventory(work):
    digest = hashlib.sha256()
    names = store.run(['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=work).stdout
    for name in sorted(set(names.split('\0')) - {''}):
        path = work/name
        digest.update(name.encode() + b'\0')
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def require_stopped_worker(home, task):
    if task.get('executor') != 'worker':
        return
    from . import workers
    if task.get('worker_alive') is not False or not task.get('endpoint') or workers.inspect(home, task['id'])['endpoint']['presence'] != 'missing':
        raise store.Refusal('worker and owned endpoint not proved stopped')


def land(home, task_id, approval_id):
    task = read(home, task_id)
    project = projects.read(home, task['project'])
    target = task['project'] + ':' + task['base']
    with store.lock(home):
        task = read(home, task_id)
        require_stopped_worker(home, task)
        if task['phase'] not in ('ready', 'landed'):
            raise store.Refusal('task is not ready to land')
        authorization(home, task, approval_id, 'local-merge', target)
        work = verify_workspace(home, task)
        verify_worker_skills(home, task)
        clone = Path(project['path'])
        if not store.clean(work) or not store.clean(clone) or store.git(clone, 'branch', '--show-current') != task['base']:
            raise store.Refusal('task or stable clone dirty/wrong branch')
        head = store.git(work, 'rev-parse', 'HEAD')
        if head != task['ready_head']:
            raise store.Refusal('ready commit moved')
        destination = store.git(clone, 'rev-parse', 'HEAD')
        if store.run(['git', 'merge-base', '--is-ancestor', destination, head], cwd=clone, check=False).returncode:
            raise store.Refusal('landing would not fast-forward')
        if destination != head:
            store.git(clone, 'merge', '--ff-only', head)
        if store.git(clone, 'rev-parse', 'HEAD') != head:
            raise store.Refusal('landing unconfirmed')
        task.update(phase='landed', landed_head=head, landed_target=target)
        save(home, task)
    return task


def cleanup(home, task_id, discard=False, approval_id=None):
    task = read(home, task_id)
    project = projects.read(home, task['project'])
    with store.lock(home):
        task = read(home, task_id)
        if task['phase'] == 'cleaned':
            return task
        if task.get('cleanup_phase') in ('removing', 'returning'):
            if task.get('cleanup_discard') != discard or task.get('cleanup_approval') != approval_id:
                raise store.Refusal('cleanup retry must use its recorded authorization')
        if task.get('cleanup_phase') == 'returning':
            status = json.loads(store.run([*treehouse_args(task), 'status', '--json'],
                                          cwd=project['path']).stdout)
            if not any(row.get('lease_id') == task['lease_id'] and row.get('lease_holder') == task['lease_holder']
                       for row in status):
                if task.get('worker_skills'):
                    from . import worker_skills
                    if not worker_skills.removed(home, task):
                        raise store.Refusal('worker skill removal is incomplete')
                task['phase'] = 'cleaned'
                save(home, task)
                return task
        work = verify_workspace(home, task)
        if task.get('cleanup_phase') not in ('removing', 'returning'):
            verify_worker_skills(home, task)
        require_stopped_worker(home, task)
        if discard:
            if not approval_id:
                raise store.Refusal('discard requires approval')
            authorization(home, task, approval_id, 'discard', task['id'])
        else:
            if not store.clean(work):
                raise store.Refusal('uncommitted or untracked task work')
            if task['kind'] == 'scout':
                if task['phase'] != 'ready' or store.git(work, 'rev-parse', 'HEAD') != task['start']:
                    raise store.Refusal('scout report missing or work modified')
            else:
                if task['phase'] != 'landed' or store.git(work, 'rev-parse', 'HEAD') != task['ready_head']:
                    raise store.Refusal('task work has not landed')
                if store.run(['git', 'merge-base', '--is-ancestor', task['ready_head'], task['base']], cwd=project['path'], check=False).returncode:
                    raise store.Refusal('landing ancestry unconfirmed')
        task.update(cleanup_phase='returning', cleanup_discard=discard, cleanup_approval=approval_id)
        save(home, task)
        if task.get('worker_skills'):
            from . import worker_skills
            worker_skills.remove(home, task)
        argv = [*treehouse_args(task), 'return', task['worktree'], '--if-lease-id', task['lease_id'],
                '--if-lease-holder', task['lease_holder']]
        if discard:
            argv.append('--force')
        store.run(argv, cwd=project['path'], timeout=120)
        task['phase'] = 'cleaned'
        save(home, task)
    return task


def publication_record(home, task_id, approval_id, remote, branch, url, evidence_file):
    task = read(home, task_id)
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise store.Refusal('review URL must be canonical HTTP(S) with no embedded credentials')
    if task['phase'] != 'ready':
        raise store.Refusal('only ready work can be published')
    with store.lock(home):
        approval = authorization(home, task, approval_id, 'publish', f'{remote}:{branch}')
        work = verify_workspace(home, task)
        verify_worker_skills(home, task)
        if branch != task['branch'] or not store.clean(work) or store.git(work, 'rev-parse', 'HEAD') != task['ready_head']:
            raise store.Refusal('published branch differs from ready head')
        remote_head = store.git(work, 'ls-remote', '--heads', remote, f'refs/heads/{branch}')
        if not remote_head or remote_head.split()[0] != task['ready_head']:
            raise store.Refusal('published remote head not proven')
        evidence = Path(evidence_file).read_text()
        if url not in evidence:
            raise store.Refusal('review URL is not present in forge output evidence')
        (task_file(home, task_id).parent/'publication.txt').write_text(evidence)
        task['publication'] = dict(url=url, remote=remote, branch=branch, head=task['ready_head'], approval=approval['id'])
        save(home, task)
    return task
