"""Confirmed, identity-bound retirement; no provisioning or global cleanup."""
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import sysconfig
import time
from pathlib import Path
from . import store, uninstall_state as state

MODULES = (
    'github', 'harnesses', 'herdr', 'installer', 'langfuse_cli', 'launcher', 'observability', 'omp_runtime',
    'pr', 'projects', 'relay', 'relay_reviews', 'review_feedback', 'reviewer', 'reviewer_episodes', 'reviewer_runner',
    'reviewer_timer', 'store', 'supervision', 'tasks', 'tmux', 'uninstall', 'uninstall_state', 'worker_skills', 'workers',
)
FILES = ('bin/agd', *(f'bin/agd_lib/{name}.py' for name in MODULES),
         'bundled/observability/release-lock.json')
DATA_CLASSES = ['Distro source and private configuration', 'Project clones and task worktrees',
                'Task reports, inboxes and local sessions', 'Distro-local OMP plugins, auth and caches',
                'Owned scheduled reviewer units and local telemetry data']

# This control script is generated, not copied from operator/plugin state.
BOOTSTRAP = '''import hashlib, json, os, runpy, stat, sys
from pathlib import Path
try:
    directory = Path(__file__).parent
    mode = directory.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("unsafe recovery directory")
    path = directory / "operation.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise ValueError("unsafe recovery journal")
        operation = json.load(stream)
    for relative, digest in operation["executor"]["files"].items():
        name = Path(relative)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("unsafe recovery file")
        path = directory / name
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("redirected recovery file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            mode = os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid():
                raise ValueError("unsafe recovery file")
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("recovery checksum changed")
    binary = directory / "executor/bin/agd"
    sys.path.insert(0, str(binary.parent))
    sys.argv = [str(binary), *sys.argv[1:]]
    runpy.run_path(str(binary), run_name="__main__")
except (OSError, ValueError, KeyError, TypeError) as error:
    print(json.dumps({"outcome": "blocked", "reason": str(error)}, indent=2))
    sys.exit(3)
'''


def resolve_home(value, *, cwd=None):
    path = Path(value if value is not None else cwd or Path.cwd()).expanduser()
    if not path.is_absolute():
        path = Path(cwd or Path.cwd()) / path
    if path.is_symlink():
        raise store.Refusal('uninstall home must not be a symlink')
    return path.resolve()


def _deletion_roots(preview):
    roots = [Path(preview['target']['canonical_home'])]
    for row in preview['resources']:
        if row['kind'] in {'checkout', 'treehouse-slot'}:
            roots.append(Path(row['identity']['path']))
    return roots


def _source_manifest(root, roots):
    python = Path(getattr(sys, '_base_executable', None) or sys.executable).resolve()
    libraries = {Path(sysconfig.get_path(name)).resolve() for name in ('stdlib', 'platstdlib')}
    if any(path.is_relative_to(removal) for path in (python, *libraries) for removal in roots):
        raise store.DependencyError('outside Python interpreter and standard library required')
    files = {}
    for name in FILES:
        path = root / name
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise store.Refusal('uninstall source is redirected')
        mode = path.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_nlink != 1:
            raise store.Refusal('uninstall source file ownership is unproven')
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'root': str(root), 'python': str(python), 'files': files}


def _ancestors():
    from . import supervision
    result, pid = set(), os.getppid()
    while pid > 1 and pid not in result:
        result.add(pid)
        pid = supervision.process_identity(pid)['ppid']
    return result


def _resource_pids(value):
    if isinstance(value, dict):
        if type(value.get('pid')) is int and isinstance(value.get('start'), str):
            yield value['pid']
        for item in value.values():
            yield from _resource_pids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _resource_pids(item)


def _collect(home, previous=None):
    from . import launcher, observability, projects, relay, reviewer_runner, reviewer_timer, tasks, worker_skills, workers
    target = previous['target'] if previous else state.target_identity(home)
    if home.exists():
        physical = projects._removal_identity(home)
        if any(physical[key] != target[key] for key in ('device', 'inode', 'uid')):
            raise store.Refusal('uninstall home physical identity changed')
    resources, blockers, warnings = [], [], []
    definitions = (
        (reviewer_timer, {'systemd-reviewer'}),
        (launcher, {'central', 'native-omp', 'backend-server', 'backend-workspace'}),
        (observability, {'docker'}),
        (workers, {'worker', 'worker-endpoint'}),
        (worker_skills, {'worker-skills'}),
        (reviewer_runner, {'reviewer-run', 'reviewer-lease', 'reviewer-backend'}),
        (relay, {'relay'}),
        (tasks, {'treehouse-slot'}), (projects, {'checkout'}),
    )
    marker = (home / 'data/home.json').is_file()
    for module, kinds in definitions:
        saved = [row for row in previous['resources'] if row['kind'] in kinds] if previous else None
        if previous and not marker and not saved:
            continue
        try:
            arguments = {'expected': saved}
            if module is reviewer_runner:
                arguments['service'] = next((row for row in resources if row['kind'] == 'systemd-reviewer'
                                             and row['ownership'] == 'owned'), None)
            resources.extend(module.uninstall_inventory(home, **arguments))
        except (store.Refusal, store.DependencyError, OSError, ValueError, KeyError, TypeError) as error:
            dependency = (isinstance(error, store.DependencyError)
                          or isinstance(error, FileNotFoundError)
                          or isinstance(error.__cause__, FileNotFoundError))
            blockers.append({'code': 'inspection-dependency' if dependency else 'inspection-uncertain',
                             'source': module.__name__.rsplit('.', 1)[-1],
                             'reason': str(error) if isinstance(error, (store.Refusal, store.DependencyError))
                             else 'resource evidence is malformed or unavailable'})
    loose = []
    if home.is_dir() and not os.path.lexists(home / '.git'):
        managed = {'bin', 'lib', 'bundled', '.omp', 'config', 'data', 'state', 'projects'}
        for path in sorted(home.iterdir()):
            if path.name not in managed:
                loose.append({'kind': 'unverified-home-file', 'path': path.name, 'sha256': projects._loss_file(path)})
    home_row = {'key': 'home:' + target['home_id'], 'kind': 'home', 'identity': target,
                'presence': 'present' if home.exists() else 'absent', 'ownership': 'owned', 'blockers': [], 'work_loss': loose}
    resources.append(home_row)
    ancestors = _ancestors()
    if any(ancestors.intersection(_resource_pids(row['identity'])) for row in resources if row['kind'] != 'home'):
        blockers.append({'code': 'managed-ancestor', 'reason': 'run uninstall from an independent normal terminal'})
    for row in resources:
        blockers.extend(dict(item, resource=row['key']) for item in row.get('blockers', []))
        if row['ownership'] != 'owned' or row['presence'] == 'unknown':
            blockers.append({'code': 'ownership-uncertain', 'resource': row['key'], 'reason': 'positive ownership/absence required'})
    resources.sort(key=lambda row: row['key'])
    loss = sorted((item for row in resources for item in row.get('work_loss', [])),
                  key=lambda item: json.dumps(item, sort_keys=True))
    value = {'schema': 1, 'target': target, 'resources': resources, 'work_loss': loss,
             'retained': ['Shared OMP, Bun, Python, Herdr, tmux, Treehouse and Relay installations',
                          'Global account configuration, Docker images, other distros and remote repositories'],
             'data_classes': DATA_CLASSES, 'blockers': blockers, 'warnings': warnings,
             'outcome': 'blocked' if blockers else 'ready'}
    value['digest'] = state._inventory_digest(value)
    value['discard_digest'] = state.fingerprint(loss)
    return value


def inventory(home):
    previous = state.read_operation(home)
    active = previous and previous['phase'] not in {'completed', 'cancelled'}
    value = _collect(home, previous['inventory'] if active else None)
    if any(state.operation_dir(home).is_relative_to(root) for root in _deletion_roots(value)):
        raise store.Refusal('recovery directory would be inside an owned removal root')
    value['source'] = previous['inventory']['source'] if active else _source_manifest(
        Path(__file__).resolve().parents[2], _deletion_roots(value))
    return value


def _ask_discard(preview, ask):
    if not preview['work_loss']:
        return None
    token = 'DISCARD ' + preview['discard_digest'][:16]
    return preview['discard_digest'] if ask(f'Local work will be lost. Type {token}: ') == token else False


def _announce(preview, report):
    report('Agent Distro Uninstall')
    report('Home: ' + preview['target']['canonical_home'])
    report('Erases: ' + '; '.join(preview['data_classes']))
    report(f"Owned resources: {len(preview['resources'])}; local-work findings: {len(preview['work_loss'])}")
    for row in preview['resources']:
        if row['kind'] == 'docker':
            report('Docker: ' + ', '.join(item['kind'] + ':' + item['name'] for item in row['identity']['objects']))
    for item in preview['work_loss']:
        report('Local work: ' + json.dumps(item, sort_keys=True))


def stage_executor(home, operation):
    directory = state.operation_dir(home)
    destination = directory / 'executor'
    source = operation['inventory']['source']
    if operation.get('executor'):
        state._validate_executor(home, operation)
        return directory / 'bootstrap.py'
    destination.mkdir(mode=0o700, exist_ok=True)
    if destination.is_symlink() or destination.stat().st_uid != os.getuid():
        raise store.Refusal('staged executor directory is unsafe')
    files = {}
    for name, digest in source['files'].items():
        if name not in FILES:
            raise store.Refusal('unapproved uninstall source file')
        original = Path(source['root']) / name
        content = original.read_bytes()
        if original.is_symlink() or hashlib.sha256(content).hexdigest() != digest:
            raise store.Refusal('uninstall source changed during staging')
        path = destination / name
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.path.lexists(path):
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise store.Refusal('interrupted staged file conflicts with source evidence')
        else:
            with path.open('xb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            path.chmod(0o600)
        files['executor/' + name] = digest
    bootstrap = directory / 'bootstrap.py'
    if bootstrap.is_symlink():
        raise store.Refusal('recovery bootstrap is redirected')
    content = BOOTSTRAP.encode()
    if bootstrap.exists() and bootstrap.read_bytes() != content:
        raise store.Refusal('recovery bootstrap conflicts with captured executor')
    if not bootstrap.exists():
        with bootstrap.open('xb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        bootstrap.chmod(0o600)
    files['bootstrap.py'] = hashlib.sha256(content).hexdigest()
    directories = {directory, destination}
    for name in files:
        directories.update(parent for parent in (directory / name).parents if parent.is_relative_to(directory))
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    operation['executor'] = {'source_root': source['root'], 'python': source['python'], 'files': files}
    state.checkpoint(home, operation, phase='preparing')
    return bootstrap


def _recovery(home, operation):
    return [operation['executor']['python'], '-I', '-S', '-B',
            str(state.operation_dir(home) / 'bootstrap.py'), '--home', str(home), 'uninstall']


def handoff(home, operation):
    os.chdir(home.parent)
    argv = _recovery(home, operation)
    os.execv(argv[0], argv)


def _refresh(home, operation, ask, report):
    fresh = _collect(home, operation['inventory'])
    fresh['source'] = operation['inventory']['source']
    if fresh['blockers']:
        raise store.Refusal('post-quiesce inventory has unresolved resource evidence')
    old = operation['inventory']
    old_scope = {row['key']: (row['kind'], row['identity']) for row in old['resources']}
    new_scope = any(old_scope.get(row['key']) != (row['kind'], row['identity']) for row in fresh['resources'])
    new_work = any(item not in old['work_loss'] for item in fresh['work_loss'])
    if new_scope or new_work:
        _announce(fresh, report)
        if new_scope and ask('Resources changed. Confirm home: ') != str(home):
            return False
        grant = _ask_discard(fresh, ask)
        if grant is False:
            return False
    else:
        grant = fresh['discard_digest'] if fresh['work_loss'] else None
    state.reapprove(home, operation, fresh, discard_digest=grant)
    return True


def _verify(home, operation):
    if os.path.lexists(home):
        raise store.Refusal('distro home survived or was recreated; it was not deleted again')
    fresh = _collect(home, operation['inventory'])
    if fresh['blockers'] or any(row['presence'] != 'absent' for row in fresh['resources']):
        raise store.Refusal('owned resource absence is unconfirmed after home removal')


def _run(home, operation, ask, report):
    from . import launcher, observability, projects, relay, reviewer_runner, reviewer_timer, tasks, worker_skills, workers
    if operation['phase'] != 'verifying':
        state.checkpoint(home, operation, phase='quiescing')
        report('1 / 3  Stop owned processes and future activation')
        rows = operation['inventory']['resources']
        reviewer_timer.uninstall_quiesce(home, rows, operation)
        launcher.uninstall_quiesce(home, rows, operation)
        workers.uninstall_quiesce(home, rows, operation)
        reviewer_runner.uninstall_quiesce(home, rows, operation)
        relay.uninstall_quiesce(home, rows, operation)
        launcher.uninstall_remove(home, rows, operation)
        if not _refresh(home, operation, ask, report):
            if operation['irreversible_started']:
                raise store.Refusal('changed work was not approved; retain recovery evidence')
            state.checkpoint(home, operation, phase='cancelled')
            return {'outcome': 'cancelled', 'home': str(home)}
        runtime = {'central', 'native-omp', 'worker', 'worker-endpoint', 'reviewer-run',
                   'reviewer-lease', 'relay', 'backend-server', 'backend-workspace'}
        if any(row['kind'] in runtime and row['presence'] != 'absent' for row in operation['inventory']['resources']):
            raise store.Refusal('new or surviving runtime requires a fresh quiesce; data retained')
        report('2 / 3  Remove approved units, data and Git registrations')
        rows = operation['inventory']['resources']
        observability.uninstall_quiesce(home, rows, operation)
        reviewer_timer.uninstall_remove(home, rows, operation)
        observability.uninstall_remove(home, rows, operation)
        worker_skills.uninstall_remove(home, rows, operation)
        tasks.uninstall_remove(home, rows, operation)
        checkouts = [row for row in rows if row['kind'] == 'checkout']
        for row in sorted(checkouts, key=lambda row: (not row['identity']['linked'], row['identity']['path'] == str(home))):
            if row['identity']['path'] != str(home):
                projects.remove_checkout(home, row, operation)
        remaining = _collect(home, operation['inventory'])
        if remaining['blockers'] or any(row['presence'] != 'absent' for row in remaining['resources']
                                        if row['kind'] != 'home' and not
                                        (row['kind'] == 'checkout' and row['identity']['path'] == str(home))):
            raise store.Refusal('owned resource absence is unconfirmed before home removal')
        if not _refresh(home, operation, ask, report):
            raise store.Refusal('final changed work was not approved; retain recovery evidence')
        if home.exists():
            physical = projects._removal_identity(home)
            if any(physical[key] != operation['target'][key] for key in ('device', 'inode', 'uid')):
                raise store.Refusal('home identity changed before removal')
            # Persist verification authority before the first home unlink; recovery never needs target code.
            state.checkpoint(home, operation, phase='verifying', irreversible=True)
            source = next((row for row in operation['inventory']['resources']
                           if row['kind'] == 'checkout' and row['identity']['path'] == str(home)), None)
            if source:
                projects.remove_checkout(home, source, operation)
                state.checkpoint(home, operation, phase='verifying')
            else:
                shutil.rmtree(home)
        else:
            state.checkpoint(home, operation, phase='verifying', irreversible=True)
    elif home.exists():
        # Its marker may be gone, but the partial directory must still be the original inode.
        physical = projects._removal_identity(home)
        if any(physical[key] != operation['target'][key] for key in ('device', 'inode', 'uid')):
            raise store.Refusal('home identity changed before recovery removal')
        shutil.rmtree(home)
    state.checkpoint(home, operation, phase='verifying', completed='home:' + operation['target']['home_id'])
    report('3 / 3  Verify absence for at least eight seconds')
    deadline = time.monotonic() + 8
    while True:
        _verify(home, operation)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))
    state.checkpoint(home, operation, phase='completed')
    result = {'outcome': 'uninstalled', 'home': str(home), 'journal': str(state.operation_dir(home)),
              'retained': operation['inventory']['retained']}
    store.atomic_json(state.operation_dir(home) / 'result.json', result)
    try:
        _clear_terminal_executor(home, operation)
    except (OSError, store.Refusal):
        result['warnings'] = ['Verified uninstall completed; executor cleanup needs inspection at the retained journal']
        store.atomic_json(state.operation_dir(home) / 'result.json', result)
    report('Distro removed. If your parent shell started there, cd ' + shlex.quote(str(home.parent)))
    return result


def _clear_terminal_executor(home, previous):
    executor = previous.get('executor')
    if not executor:
        return
    directory = state.operation_dir(home)
    approved = {Path(name): digest for name, digest in executor['files'].items()}
    parents = {parent for name in approved for parent in name.parents if parent != Path('.')}
    paths = []
    for name in ('executor', 'bootstrap.py'):
        root = directory / name
        if os.path.lexists(root):
            paths.append(root)
            if root.is_dir() and not root.is_symlink():
                paths.extend(root.rglob('*'))
    for path in paths:
        relative, mode = path.relative_to(directory), path.lstat()
        if mode.st_uid != os.getuid() or stat.S_ISLNK(mode.st_mode):
            raise store.Refusal('terminal executor cleanup ownership is unproven')
        if stat.S_ISDIR(mode.st_mode):
            if relative not in parents:
                raise store.Refusal('terminal executor contains an unbound directory')
        elif (relative not in approved or not stat.S_ISREG(mode.st_mode)
              or hashlib.sha256(path.read_bytes()).hexdigest() != approved[relative]):
            raise store.Refusal('terminal executor contains an unbound or changed file')
    if (directory / 'executor').exists():
        shutil.rmtree(directory / 'executor')
    if (directory / 'bootstrap.py').exists():
        (directory / 'bootstrap.py').unlink()


def execute(home, preview, *, discard_digest, ask, report):
    previous = state.read_operation(home)
    if previous and previous['phase'] in {'completed', 'cancelled'}:
        _clear_terminal_executor(home, previous)
    with state.retirement(home, preview, discard_digest=discard_digest) as operation:
        try:
            if not operation.get('executor'):
                stage_executor(home, operation)
            staged = state.operation_dir(home) / 'executor/bin/agd_lib/uninstall.py'
            if Path(__file__).resolve() != staged or not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
                report('Recovery: ' + shlex.join(_recovery(home, operation)))
                handoff(home, operation)
            os.chdir(home.parent)
            return _run(home, operation, ask, report)
        except (store.Refusal, store.DependencyError, OSError, ValueError) as error:
            state.checkpoint(home, operation, phase='blocked' if operation.get('executor') else 'preparing', error=str(error))
            raise


def _decline(home, previous):
    if previous:
        if previous['irreversible_started']:
            return {'outcome': 'blocked', 'home': str(home), 'reason': 'irreversible retirement remains pending'}, 3
        with state.retirement(home, previous['inventory'], discard_digest=previous['discard_digest']) as operation:
            state.checkpoint(home, operation, phase='cancelled')
    return {'outcome': 'cancelled', 'home': str(home)}, 0


def command(home, *, dry_run, ask, report):
    try:
        previous = state.read_operation(home)
        active = previous and previous['phase'] not in {'completed', 'cancelled'}
        if not os.path.lexists(home) and not active:
            return {'outcome': 'already-absent', 'home': str(home)}, 0
        preview = inventory(home)
        if preview['blockers']:
            return preview, 4 if any(item['code'] == 'inspection-dependency' for item in preview['blockers']) else 3
        if dry_run:
            return preview, 0
        same_actor = active and previous['coordinator'] == state._actor()
        if not same_actor:
            _announce(preview, report)
            if ask('Confirm home: ') != str(home):
                return _decline(home, previous if active else None)
            grant = _ask_discard(preview, ask)
            if grant is False:
                return _decline(home, previous if active else None)
        else:
            grant = previous['discard_digest']
            preview = previous['inventory']
        result = execute(home, preview, discard_digest=grant, ask=ask, report=report)
        return result, 0
    except (store.Refusal, store.DependencyError, OSError, ValueError, KeyError, TypeError) as error:
        return {'outcome': 'blocked', 'home': str(home), 'reason': str(error),
                'journal': str(state.operation_dir(home))}, 4 if isinstance(error, store.DependencyError) else 3
