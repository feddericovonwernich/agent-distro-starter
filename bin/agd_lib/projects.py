import uuid
import json
import shutil
from pathlib import Path
from . import store


def record(home, project_id):
    return home/'data'/'projects'/f'{store.validate_id(project_id)}.json'


def read(home, project_id):
    path = record(home, project_id)
    if not path.is_file():
        raise store.Refusal(f'unknown project: {project_id}')
    data = json.loads(path.read_text())
    clone = Path(data['path'])
    if clone.resolve() != (home/'projects'/project_id).resolve() or clone.is_symlink():
        raise store.Refusal('project clone identity changed')
    if store.git(clone, 'rev-parse', '--path-format=absolute', '--git-common-dir') != data['common_dir']:
        raise store.Refusal('project Git identity changed')
    return data


def register(home, project_id, path, base='main', source=None):
    store.validate_id(project_id)
    from .uninstall_state import admission
    with admission(home):
        path = Path(path)
        if path.is_symlink() or path.resolve() != home/'projects'/project_id:
            raise store.Refusal('project must be an actual clone inside projects/')
        with store.lock(home):
            if record(home, project_id).exists():
                raise store.Refusal('project already registered')
            if store.git(path, 'rev-parse', '--is-bare-repository') != 'false':
                raise store.Refusal('project must be a non-bare clone')
            if store.git(path, 'rev-parse', '--show-toplevel') != str(path):
                raise store.Refusal('project path is not the checkout root')
            if store.git(path, 'rev-parse', '--verify', base, check=False) == '':
                raise store.Refusal('base branch does not exist')
            data = dict(schema=1, id=project_id, path=str(path),
                        common_dir=store.git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir'),
                        source=source or store.git(path, 'remote', 'get-url', 'origin', check=False),
                        base=base, trusted=False)
            store.atomic_json(record(home, project_id), data)
            return data


def add(home, project_id, source, base='main'):
    store.validate_id(project_id)
    from .uninstall_state import admission
    with admission(home):
        target = home/'projects'/project_id
        if target.exists() or target.is_symlink() or record(home, project_id).exists():
            raise store.Refusal('clone or record already exists')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        store.run(['git', 'clone', '--', source, str(target)], timeout=120)
        return register(home, project_id, target, base=base, source=source)


def refresh(home, project_id):
    data = read(home, project_id)
    from .uninstall_state import admission
    with admission(home):
        clone = Path(data['path'])
        with store.lock(home):
            if not store.clean(clone) or store.git(clone, 'branch', '--show-current') != data['base']:
                raise store.Refusal('clone must be clean on its base branch')
            store.git(clone, 'fetch', 'origin', data['base'])
            store.git(clone, 'merge', '--ff-only', f'origin/{data["base"]}')
        return read(home, project_id)

def approve(home, project_id, action, instruction):
    if action != 'delete-clone':
        raise ValueError('unsupported project approval')
    text = Path(instruction).read_text().strip()
    if not text:
        raise store.Refusal('explicit clone deletion instruction required')
    with store.lock(home):
        data = read(home, project_id)
        clone = Path(data['path'])
        evidence = dict(id=str(uuid.uuid4()), action=action, project=project_id,
                        path=data['path'], common_dir=data['common_dir'],
                        head=store.git(clone, 'rev-parse', 'HEAD'), instruction=text)
        store.atomic_json(home/'data'/'projects'/f'{project_id}.approval.json', evidence)
    return evidence



def remove(home, project_id, delete=False, approval=None):
    data = read(home, project_id)
    with store.lock(home):
        for task_file in (home/'data'/'tasks').glob('*/task.json'):
            task = json.loads(task_file.read_text())
            if task.get('project') == project_id and task.get('phase') != 'cleaned':
                raise store.Refusal('project has retained tasks')
        if delete:
            evidence = home/'data'/'projects'/f'{project_id}.approval.json'
            if not approval or not evidence.exists():
                raise store.Refusal('clone deletion needs scoped approval')
            grant = json.loads(evidence.read_text())
            clone = Path(data['path'])
            if (grant.get('id') != approval or grant.get('action') != 'delete-clone' or
                grant.get('project') != project_id or grant.get('path') != data['path'] or
                grant.get('common_dir') != data['common_dir'] or
                grant.get('head') != store.git(clone, 'rev-parse', 'HEAD')):
                raise store.Refusal('clone deletion approval identity changed')
            if not store.clean(clone) or store.git(clone, 'branch', '--show-current') != data['base']:
                raise store.Refusal('clone has unlanded or dirty work')
            if store.git(clone, 'ls-files', '--others', '--ignored', '--exclude-standard'):
                raise store.Refusal('clone contains ignored local files; preserve them before deletion')
            if store.git(clone, 'branch', '--no-merged', data['base']) or store.git(clone, 'rev-list', f'origin/{data["base"]}..{data["base"]}'):
                raise store.Refusal('clone has unlanded local commits')
            refs = store.git(clone, 'for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/tags').splitlines()
            if refs != [f'refs/heads/{data["base"]}']:
                raise store.Refusal('clone has other local refs')
            for commit in store.git(clone, 'reflog', '--all', '--format=%H').splitlines():
                if store.run(['git', 'merge-base', '--is-ancestor', commit, f'origin/{data["base"]}'],
                             cwd=clone, check=False).returncode:
                    raise store.Refusal('clone reflog retains unpublished commits')
            shutil.rmtree(clone)
        record(home, project_id).unlink()
        if delete:
            evidence.unlink()
    return dict(removed=project_id, clone_deleted=delete)


def trust(home, project_id, instruction):
    with store.lock(home):
        data = read(home, project_id)
        if not Path(instruction).read_text().strip():
            raise store.Refusal('explicit trust instruction required')
        data['trusted'] = True
        data['trust_instruction'] = Path(instruction).read_text()
        store.atomic_json(record(home, project_id), data)
    return data


def _removal_identity(path):
    import os
    import re
    import stat
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or path == Path.home().resolve()
            or path in {Path(value) for value in ('/', '/tmp', '/var', '/run', '/home', '/dev', '/proc', '/sys')}):
        raise store.Refusal('unsafe checkout removal root')
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid():
        raise store.Refusal('checkout root ownership is unproven')
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = Path(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), line.split()[4]))
        if mount == path or mount.is_relative_to(path):
            raise store.Refusal('checkout removal root contains a mount')
    return {'path': str(path), 'device': mode.st_dev, 'inode': mode.st_ino, 'uid': mode.st_uid}


def _checkout_git(path, *argv, check=True):
    import os
    return store.run(['git', *argv], cwd=path, check=check,
                     env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))


def _registrations(common):
    raw = _checkout_git(common, 'worktree', 'list', '--porcelain', '-z').stdout
    rows = []
    for record in raw.split('\0\0'):
        fields = record.strip('\0').split('\0')
        if not fields or not fields[0]:
            continue
        value = dict(field.split(' ', 1) if ' ' in field else (field, True) for field in fields)
        if not isinstance(value.get('worktree'), str):
            raise store.Refusal('Git worktree registration is malformed')
        rows.append({'path': value['worktree'], 'head': value.get('HEAD'),
                     'branch': value.get('branch'), 'locked': value.get('locked', False)})
    return rows


def _loss_file(path):
    import hashlib
    import os
    import stat
    if not os.path.lexists(path):
        return 'absent'
    mode = path.lstat()
    if stat.S_ISLNK(mode.st_mode):
        return 'symlink:' + hashlib.sha256(os.readlink(path).encode()).hexdigest()
    if stat.S_ISREG(mode.st_mode):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if stat.S_ISDIR(mode.st_mode):
        from .uninstall_state import fingerprint
        return fingerprint([(str(item.relative_to(path)), _loss_file(item)) for item in sorted(path.iterdir())])
    raise store.Refusal('checkout contains an unsupported local file type')


def checkout_inventory(path):
    path = Path(path)
    identity = _removal_identity(path)
    if _checkout_git(path, 'rev-parse', '--show-toplevel').stdout.strip() != str(path):
        raise store.Refusal('checkout path is not a Git root')
    common = Path(_checkout_git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    git_dir = Path(_checkout_git(path, 'rev-parse', '--absolute-git-dir').stdout.strip())
    common_identity = _removal_identity(common)
    linked = git_dir != common
    if not linked and not common.is_relative_to(path):
        raise store.Refusal('main checkout has an external Git common directory')
    registrations = _registrations(common)
    exact = [item for item in registrations if item['path'] == str(path)]
    if len(exact) != 1 or exact[0]['locked']:
        raise store.Refusal('checkout registration is missing or locked')
    head = _checkout_git(path, 'rev-parse', 'HEAD').stdout.strip()
    refs = _checkout_git(path, 'for-each-ref', '--format=%(refname) %(objectname)').stdout.splitlines()
    identity.update(common=common_identity, git_dir=str(git_dir), linked=linked, head=head,
                    registrations=registrations, refs=refs)
    loss = []
    changed = set(_checkout_git(path, 'diff', '--name-only', '-z', 'HEAD').stdout.split('\0')) - {''}
    for kind, files in (
            ('tracked-file', changed),
            ('untracked-file', set(_checkout_git(path, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0')) - {''}),
            ('ignored-file', set(_checkout_git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').stdout.split('\0')) - {''})):
        for name in sorted(files):
            loss.append({'kind': kind, 'checkout': str(path), 'path': name, 'sha256': _loss_file(path / name)})
    # ponytail: publication uses cached remote refs; fetch explicitly before uninstall for newer evidence.
    if linked:
        unique = _checkout_git(path, 'rev-list', 'HEAD', '--reflog', '--not', '--all').stdout.splitlines()
    else:
        unique = _checkout_git(path, 'rev-list', '--all', '--reflog', '--not', '--remotes').stdout.splitlines()
        remote_refs = {line.split(' ', 1)[0].split('/', 3)[-1]: line.split(' ', 1)[1]
                       for line in refs if line.startswith('refs/remotes/') and ' ' in line}
        for line in refs:
            name, commit = line.split(' ', 1)
            if not name.startswith('refs/remotes/') and remote_refs.get(name.removeprefix('refs/heads/')) != commit:
                loss.append({'kind': 'local-only-ref', 'checkout': str(path), 'ref': name, 'commit': commit})
    if unique:
        loss.append({'kind': 'unpublished-commits', 'checkout': str(path), 'commits': sorted(set(unique))})
    return {'key': 'checkout:' + str(path), 'kind': 'checkout', 'identity': identity,
            'presence': 'present', 'ownership': 'owned', 'blockers': [], 'work_loss': loss}


def uninstall_inventory(home, *, expected=None):
    import os
    from . import uninstall_state
    home = store.home(home)
    roots, task_paths = set(), set()
    for task_file in (home / 'data/tasks').glob('*/task.json'):
        task = uninstall_state.read_evidence(task_file)
        if task.get('worktree'):
            task_paths.add(task['worktree'])
    for directory, children, files in os.walk(home, followlinks=False):
        if '.git' in children or '.git' in files:
            roots.add(Path(directory))
            if '.git' in children:
                children.remove('.git')
    for receipt in (home / 'data/projects').glob('*.json'):
        if receipt.name.endswith('.approval.json'):
            continue
        value = uninstall_state.read_evidence(receipt)
        path = Path(value['path'])
        if path != home / 'projects' / value['id']:
            raise store.Refusal('registered project path escaped its owned root')
        roots.add(path)
    saved = {row['identity']['path']: row for row in expected or [] if row['kind'] == 'checkout'}
    roots.update(Path(path) for path in saved)
    rows = []
    for path in sorted(roots):
        if str(path) in task_paths:
            continue
        if not os.path.lexists(path):
            if str(path) in saved:
                row = dict(saved[str(path)], presence='absent')
                identity = row['identity']
                if identity['linked'] and Path(identity['common']['path']).is_dir():
                    if any(item['path'] == str(path) for item in _registrations(Path(identity['common']['path']))):
                        row.update(presence='present', blockers=[{'code': 'git-registration-remains',
                                                                 'reason': 'removed checkout still has a registration'}])
                rows.append(row)
            continue
        row = checkout_inventory(path)
        if not row['identity']['linked']:
            foreign = [item for item in row['identity']['registrations']
                       if item['path'] != str(path) and item['path'] not in task_paths
                       and Path(item['path']) not in roots]
            if foreign:
                row['blockers'] = [{'code': 'external-worktrees', 'reason': 'backing clone has unowned external registrations',
                                    'paths': [item['path'] for item in foreign]}]
        rows.append(row)
    return rows


def remove_checkout(home, resource, operation):
    import os
    from . import uninstall_state
    approved = uninstall_state.require_scope(home, operation, resource['key'])
    identity = approved['identity']
    path, common = Path(identity['path']), Path(identity['common']['path'])
    if not os.path.lexists(path):
        if identity['linked'] and common.is_dir():
            remaining = [item for item in _registrations(common) if item['path'] == str(path)]
            if remaining:
                uninstall_state.checkpoint(home, operation, phase='removing', irreversible=True)
                _checkout_git(common, 'worktree', 'remove', '--force', str(path))
        if common.is_dir() and any(item['path'] == str(path) for item in _registrations(common)):
            raise store.Refusal('checkout registration remains')
    else:
        fresh = checkout_inventory(path)
        current = fresh['identity']
        for key in ('path', 'device', 'inode', 'uid', 'common', 'git_dir', 'linked', 'head'):
            if current[key] != identity[key]:
                raise store.Refusal('checkout identity changed after approval')
        if any(item not in identity['refs'] for item in current['refs']):
            raise store.Refusal('checkout refs changed after approval')
        if any(item not in approved.get('work_loss', []) for item in fresh['work_loss']):
            raise store.Refusal('checkout work changed after approval')
        if not identity['linked'] and any(item['path'] != str(path) for item in current['registrations']):
            raise store.Refusal('backing clone still has linked worktrees')
        uninstall_state.checkpoint(home, operation, phase='removing', irreversible=True)
        if identity['linked']:
            argv = ['worktree', 'remove']
            if approved.get('work_loss'):
                argv.append('--force')
            _checkout_git(common, *argv, str(path))
        else:
            shutil.rmtree(path)
        if os.path.lexists(path):
            raise store.Refusal('checkout removal is unconfirmed')
        if identity['linked'] and any(item['path'] == str(path) for item in _registrations(common)):
            raise store.Refusal('linked checkout registration remains')
    uninstall_state.checkpoint(home, operation, phase='removing', completed=approved['key'])
