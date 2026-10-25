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
    target = home/'projects'/project_id
    if target.exists() or target.is_symlink() or record(home, project_id).exists():
        raise store.Refusal('clone or record already exists')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.run(['git', 'clone', '--', source, str(target)], timeout=120)
    return register(home, project_id, target, base=base, source=source)


def refresh(home, project_id):
    data = read(home, project_id)
    clone = Path(data['path'])
    with store.lock(home):
        if not store.clean(clone) or store.git(clone, 'branch', '--show-current') != data['base']:
            raise store.Refusal('clone must be clean on its base branch')
        store.git(clone, 'fetch', 'origin', data['base'])
        store.git(clone, 'merge', '--ff-only', f'origin/{data["base"]}')
    return read(home, project_id)


def remove(home, project_id, delete=False, approval=None):
    data = read(home, project_id)
    with store.lock(home):
        for task_file in (home/'data'/'tasks').glob('*/task.json'):
            if json.loads(task_file.read_text()).get('project') == project_id:
                raise store.Refusal('project has retained tasks')
        if delete:
            evidence = home/'data'/'projects'/f'{project_id}.approval.json'
            if not approval or not evidence.exists() or json.loads(evidence.read_text()).get('id') != approval:
                raise store.Refusal('clone deletion needs scoped approval')
            clone = Path(data['path'])
            if not store.clean(clone) or store.git(clone, 'branch', '--show-current') != data['base']:
                raise store.Refusal('clone has unlanded or dirty work')
            if store.git(clone, 'branch', '--no-merged', data['base']) or store.git(clone, 'rev-list', f'origin/{data["base"]}..{data["base"]}'):
                raise store.Refusal('clone has unlanded local commits')
            shutil.rmtree(clone)
        record(home, project_id).unlink()
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
