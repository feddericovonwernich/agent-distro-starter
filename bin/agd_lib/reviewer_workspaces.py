"""Run-owned Treehouse probe workspaces.

Probes are durable leases, not delivery tasks.  The receipt is the authority for
reconciliation; Treehouse status remains the authority for the current lease.
"""
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from . import projects, reviewer, store, supervision


def _root(home):
    return store.home(home)


def _paths(home, run_id):
    try:
        run_id = str(uuid.UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('review run ID') from error
    directory = _root(home) / 'state' / 'reviewer' / 'workspaces' / run_id
    return run_id, directory


def _receipt_path(home, run_id, project, commit):
    _, directory = _paths(home, run_id)
    name = hashlib.sha256(f'{project}\0{commit}'.encode()).hexdigest()
    return directory / f'{name}.json'


def _review_run(home, run_id):
    receipt = reviewer._receipt(home, run_id)
    if receipt.get('dispatch_state') == 'final':
        raise store.Refusal('review run is closed')
    return receipt


def _home_id(home):
    try:
        value = json.loads((_root(home) / 'data' / 'home.json').read_text())
        return value['home_id']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise store.Refusal('home identity is unavailable') from error


def _git(path, *args):
    return store.git(path, *args)


def _git_safe(home, path, *args, check=True):
    return store.run(['git', *args], cwd=path, check=check, env=_safe_env(home)).stdout.strip()


def _safe_env(home):
    root = _root(home)
    isolated = root / 'data' / 'reviewer' / 'treehouse-environment'
    config = isolated / 'config'
    cache = isolated / 'cache'
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env.update(HOME=str(isolated), XDG_CONFIG_HOME=str(config), XDG_CACHE_HOME=str(cache),
               TREEHOUSE_ROOT=str(root / 'data' / 'treehouse'), GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_COUNT='1',
               GIT_CONFIG_KEY_0='core.hooksPath', GIT_CONFIG_VALUE_0='/dev/null')
    return env


def _treehouse(home, project, args):
    return store.run(['treehouse', '--root', str(_root(home) / 'data' / 'treehouse'), *args],
                     cwd=project, env=_safe_env(home), timeout=120)


def _status(home, project):
    try:
        return json.loads(_treehouse(home, project, ['status', '--json']).stdout)
    except (ValueError, TypeError, KeyError) as error:
        raise store.Refusal('Treehouse status is malformed') from error


def _record(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise store.Refusal('reviewer workspace receipt is malformed') from error
    if not isinstance(value, dict):
        raise store.Refusal('reviewer workspace receipt is malformed')
    return value


def _write(path, value):
    reviewer._private(path.parent)
    store.atomic_json(path, value)


def _validate_allocation(home, project, record, row):
    path = Path(row.get('path', '')).resolve()
    pool = (_root(home) / 'data' / 'treehouse').resolve()
    common = _git(project, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if (not path.is_dir() or path == project.resolve() or not path.is_relative_to(pool)
            or _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') != common
            or _git(path, 'rev-parse', 'HEAD') != record['commit']
            or _git(path, 'branch', '--show-current') != record['branch']):
        raise store.Refusal('Treehouse returned the wrong probe identity')
    if not row.get('lease_id'):
        raise store.Refusal('Treehouse returned an unleased probe')
    if (record.get('lease_id') is not None
            and row.get('lease_id') != record.get('lease_id')):
        raise store.Refusal('Treehouse lease identity changed')
    return path



def _ensure_base_ref(home, clone, record, receipt):
    name = record.get('base_ref')
    expected = record.get('base_ref_oid')
    if not isinstance(name, str) or not isinstance(expected, str):
        raise store.Refusal('probe base reference is missing')
    ref = name if name.startswith('refs/') else f'refs/heads/{name}'
    current = _git_safe(home, clone, 'rev-parse', '--verify', f'{ref}^{{commit}}', check=False)
    if current:
        if current != expected:
            raise store.Refusal('probe base reference changed')
        return
    if record.get('state') != 'intent':
        raise store.Refusal('probe base reference disappeared')
    created = _git_safe(home, clone, 'update-ref', ref, expected,
                        '0' * 40, check=False)
    if created is not None:
        current = _git_safe(home, clone, 'rev-parse', '--verify', f'{ref}^{{commit}}', check=False)
        if current != expected:
            raise store.Refusal('probe base reference was not created')
    _write(receipt, record)


def _delete_base_ref(home, clone, record):
    name, expected = record.get('base_ref'), record.get('base_ref_oid')
    if not isinstance(name, str) or not isinstance(expected, str):
        raise store.Refusal('probe base reference is missing')
    ref = name if name.startswith('refs/') else f'refs/heads/{name}'
    current = _git_safe(home, clone, 'rev-parse', '--verify', f'{ref}^{{commit}}', check=False)
    if not current:
        return
    if current != expected:
        raise store.Refusal('probe base reference changed')
    _git_safe(home, clone, 'update-ref', '-d', ref, expected)
def _matching_status(home, project, record):
    rows = [row for row in _status(home, project)
            if row.get('lease_holder') == record['lease_holder']]
    if len(rows) > 1:
        raise store.Refusal('ambiguous probe lease holder')
    return rows[0] if rows else None

def _reconcile_record(home, record):
    if record.get('state') == 'returned':
        return record
    project = projects.read(home, record['project'])
    clone = Path(project['path']).resolve()
    receipt = _receipt_path(home, record['review_run_id'], record['project'], record['commit'])
    _ensure_base_ref(home, clone, record, receipt)
    row = _matching_status(home, clone, record)
    if row is None:
        if record.get('lease_id'):
            raise store.Refusal('probe lease is absent')
        return record
    path = _validate_allocation(home, clone, record, row)
    if record.get('lease_id') not in (None, row.get('lease_id')):
        raise store.Refusal('probe lease identity changed')
    record.update(path=str(path), lease_id=row['lease_id'], state='allocated')
    receipt = _receipt_path(home, record['review_run_id'], record['project'], record['commit'])
    _write(receipt, record)
    return record


def _result(record, *, disclose=True):
    value = {key: record[key] for key in (
        'schema', 'review_run_id', 'project', 'commit', 'common_dir', 'base',
        'base_ref', 'base_ref_oid', 'branch',
        'attempt', 'lease_holder', 'lease_id', 'exposure', 'state') if key in record}
    if disclose and record.get('path'):
        value['path'] = record['path']
    return value


def create(home: Path, review_run_id: str, project_id: str, commit: str) -> dict:
    home = _root(home)
    run_id, directory = _paths(home, review_run_id)
    _review_run(home, run_id)
    project = projects.read(home, project_id)
    clone = Path(project['path']).resolve()
    if not isinstance(commit, str) or _git_safe(home, clone, 'rev-parse', '--verify', commit, check=False) == '':
        raise store.Refusal('commit is not present in the registered clone')
    commit = _git(clone, 'rev-parse', '--verify', commit)
    receipt_path = _receipt_path(home, run_id, project_id, commit)
    common = _git(clone, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if receipt_path.exists():
        record = _record(receipt_path)
        if any(record.get(key) != value for key, value in (
                ('review_run_id', run_id), ('project', project_id), ('commit', commit),
                ('common_dir', common))):
            raise store.Refusal('probe tuple identity changed')
    else:
        attempt = str(uuid.uuid4())
        holder = f'{_home_id(home)}:{run_id}:{project_id}:{commit}:{attempt}'
        branch = f'agd/reviewer/{run_id.replace("-", "")}/{project_id}/{commit[:12]}/{attempt[:12]}'
        base_ref = f'agd/reviewer-base/{run_id.replace("-", "")}/{attempt.replace("-", "")}'
        record = {'schema': 1, 'review_run_id': run_id, 'project': project_id,
                  'commit': commit, 'common_dir': common, 'base': project['base'],
                  'base_ref': base_ref, 'base_ref_oid': commit,
                  'attempt': attempt, 'lease_holder': holder, 'branch': branch,
                  'exposure': 'never-exposed', 'state': 'intent'}
        with store.lock(home):
            if not receipt_path.exists():
                _write(receipt_path, record)
            else:
                record = _record(receipt_path)
    if record.get('state') == 'returned':
        raise store.Refusal('probe allocation was already returned')
    _ensure_base_ref(home, clone, record, receipt_path)
    row = _matching_status(home, clone, record)
    if row is None:
        include = directory / 'include-empty'
        include.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        include.write_text('')
        try:
            allocation = json.loads(_treehouse(home, clone, [
                'get', '--lease', '--json', '--no-fetch', '--lease-holder', record['lease_holder'],
                '--base', record['base_ref'], '--branch', record['branch'],
                '--worktree-path', '{pool}/{slot}/{repo}', '--include-file', str(include)]).stdout)
        finally:
            include.unlink(missing_ok=True)
        if allocation.get('lease_holder') != record['lease_holder']:
            raise store.Refusal('Treehouse returned a foreign lease holder')
        row = allocation
    path = _validate_allocation(home, clone, record, row)
    record.update(path=str(path), lease_id=row['lease_id'], state='allocated')
    _write(receipt_path, record)
    record['exposure'] = 'exposed'
    _write(receipt_path, record)
    return _result(record)


def _records(home, run_id):
    _, directory = _paths(home, run_id)
    if not directory.exists():
        return []
    return [_record(path) for path in sorted(directory.glob('*.json')) if path.name != 'include-empty']


def list_workspaces(home: Path, review_run_id: str) -> list[dict]:
    result = []
    for record in _records(home, review_run_id):
        record = _reconcile_record(home, record)
        disclose = record.get('exposure') == 'exposed' and record.get('state') != 'returned'
        result.append(_result(record, disclose=disclose))
    return result


def _processes_in(path):
    path = Path(path).resolve()
    found = []
    proc = Path('/proc')

    def inside(value):
        try:
            value = Path(value).resolve()
        except (OSError, RuntimeError):
            return False
        return value == path or value.is_relative_to(path)

    def unknown(pid, reason, identity=None):
        value = dict(identity or {}, pid=pid, inspection='unconfirmed', reason=reason)
        found.append(value)

    try:
        candidates = list(proc.iterdir())
    except OSError as error:
        raise store.Refusal('process directory is not inspectable') from error
    for candidate in candidates:
        if not candidate.name.isdigit() or int(candidate.name) == os.getpid():
            continue
        pid = int(candidate.name)
        try:
            identity = supervision._process_stat(pid)
        except FileNotFoundError:
            continue
        except (PermissionError, ProcessLookupError, OSError):
            identity = {}
            try:
                identity['uid'] = candidate.stat().st_uid
            except OSError:
                pass
            unknown(pid, 'process identity is not inspectable', identity)
            continue
        if identity.get('uid') != os.getuid():
            continue
        try:
            matches = [Path(os.readlink(candidate / 'cwd'))]
            for fd in (candidate / 'fd').iterdir():
                try:
                    matches.append(Path(os.readlink(fd)))
                except FileNotFoundError:
                    continue
            try:
                for line in (candidate / 'maps').read_text(errors='replace').splitlines():
                    fields = line.split(maxsplit=5)
                    if len(fields) == 6:
                        mapped = fields[5].removesuffix(' (deleted)')
                        if mapped.startswith('/'):
                            matches.append(Path(mapped))
            except FileNotFoundError:
                pass
        except FileNotFoundError:
            continue
        except (PermissionError, ProcessLookupError, OSError):
            unknown(pid, 'process paths are not inspectable', identity)
            continue
        if any(inside(value) for value in matches):
            found.append(identity)
    return found


def _instruction(instruction, run_id, project, commit):
    if instruction is None:
        return False
    path = Path(instruction)
    try:
        mode = path.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid():
            return False
        text = path.read_text()
    except (OSError, UnicodeError):
        return False
    return all(value in text for value in (run_id, project, commit))


def _clean_unchanged(home, project, record, path):
    if _processes_in(path):
        return False
    if _git(path, 'rev-parse', 'HEAD') != record['commit'] or not store.clean(path):
        return False
    row = _matching_status(home, project, record)
    if row is None:
        raise store.Refusal('probe lease is absent')
    if _validate_allocation(home, project, record, row) != path:
        raise store.Refusal('probe lease identity changed')
    return True


def _return(home, project, record):
    _ensure_base_ref(home, project, record,
                     _receipt_path(home, record['review_run_id'], record['project'], record['commit']))
    row = _matching_status(home, project, record)
    if row is None:
        raise store.Refusal('probe lease is absent')
    path = _validate_allocation(home, project, record, row)
    _treehouse(home, project, ['return', str(path), '--if-lease-id', record['lease_id'],
                                '--if-lease-holder', record['lease_holder']])
    current = [item for item in _status(home, project)
               if Path(item.get('path', '')).resolve() == path]
    if any(item.get('lease_id') == record['lease_id']
           or item.get('lease_holder') == record['lease_holder'] for item in current):
        raise store.Refusal('returned probe lease remains present')
    _delete_base_ref(home, project, record)
    record['state'] = 'returned'


def cleanup(home: Path, review_run_id: str, project_id: str, commit: str, *, instruction: Path | None = None) -> dict:
    run_id, _ = _paths(home, review_run_id)
    records = [record for record in _records(home, run_id)
               if record.get('project') == project_id and record.get('commit') == commit]
    if len(records) != 1:
        raise store.Refusal('unknown probe allocation')
    record = records[0]
    project = projects.read(home, project_id)
    path = Path(record.get('path', '')).resolve() if record.get('path') else None
    if path is None:
        raise store.Refusal('probe path is not allocated')
    never = record.get('exposure') == 'never-exposed'
    authorized = never or _instruction(instruction, run_id, project_id, commit)
    if not authorized:
        return {'outcome': 'retained', 'reason': 'exposed probe requires exact operator instruction', 'path': str(path)}
    run = reviewer._receipt(home, run_id)
    if run.get('dispatch_state') != 'final':
        raise store.Refusal('review run is not closed')
    if not _clean_unchanged(home, Path(project['path']), record, path):
        result = {'outcome': 'retained', 'reason': 'probe is dirty, committed, or has active users'}
        if record.get('exposure') == 'exposed':
            result['path'] = str(path)
        return result
    _return(home, Path(project['path']), record)
    result = {'outcome': 'returned', 'lease_id': record['lease_id']}
    if record.get('exposure') == 'exposed':
        result['path'] = str(path)
    return result


def uninstall_inventory(home, *, expected=None):
    expected = {row['key']: row for row in expected or [] if row.get('kind') == 'reviewer-probe'}
    rows = []
    seen = set()
    root = _root(home) / 'state' / 'reviewer' / 'workspaces'
    for path in sorted(root.glob('*/*.json')) if root.exists() else []:
        record = _record(path)
        key = f"reviewer-probe:{record.get('review_run_id')}:{record.get('project')}:{record.get('commit')}"
        seen.add(key)
        probe_path = Path(record['path']).resolve() if record.get('path') else None
        presence = 'present' if (
            record.get('state') == 'allocated'
            and probe_path is not None
            and probe_path.exists()
        ) else 'absent'
        blockers = []
        users = []
        if presence == 'present':
            try:
                users = _processes_in(probe_path)
            except store.Refusal as error:
                blockers.append({'code': 'probe-process-inspection', 'reason': str(error)})
            for user in users:
                blockers.append({'code': 'probe-process-inspection',
                                 'reason': 'probe path has active or unconfirmed process users'})
        identity = {key: record.get(key) for key in (
            'review_run_id', 'project', 'commit', 'path', 'lease_id', 'lease_holder',
            'common_dir', 'base_ref', 'base_ref_oid', 'branch')}
        identity['processes'] = users
        loss = []
        if presence == 'present':
            loss.append({'kind': 'reviewer-probe', 'path': str(probe_path),
                         'sha256': projects._loss_file(probe_path)})
        rows.append({'key': key, 'kind': 'reviewer-probe', 'identity': identity,
                     'presence': presence, 'ownership': 'owned', 'blockers': blockers,
                     'work_loss': loss})
    for key, row in expected.items():
        if key not in seen:
            rows.append({**row, 'presence': 'absent', 'ownership': 'owned', 'blockers': []})
    return rows


def uninstall_quiesce(home, resources, operation):
    from . import uninstall_state
    for resource in resources:
        if resource.get('kind') != 'reviewer-probe':
            continue
        approved = uninstall_state.require_scope(home, operation, resource['key'])
        if approved.get('presence') != 'present':
            continue
        identity = approved.get('identity', {})
        project = projects.read(home, identity.get('project'))
        record = next((row for row in _records(home, identity['review_run_id'])
                       if row.get('project') == identity.get('project')
                       and row.get('commit') == identity.get('commit')), None)
        if record is None:
            raise store.Refusal('reviewer probe receipt is missing')
        path = Path(identity.get('path', '')).resolve()
        if not _clean_unchanged(home, Path(project['path']), record, path) and _processes_in(path):
            raise store.Refusal('reviewer probe process identity is unconfirmed')


def uninstall_remove(home, resources, operation):
    from . import uninstall_state
    for resource in resources:
        if resource.get('kind') != 'reviewer-probe':
            continue
        approved = uninstall_state.require_scope(home, operation, resource['key'])
        if approved.get('presence') != 'present':
            continue
        identity = approved.get('identity', {})
        project = projects.read(home, identity.get('project'))
        record = next((row for row in _records(home, identity['review_run_id'])
                       if row.get('project') == identity.get('project')
                       and row.get('commit') == identity.get('commit')), None)
        if record is None:
            raise store.Refusal('reviewer probe receipt is missing')
        path = Path(identity.get('path', '')).resolve()
        _ensure_base_ref(home, Path(project['path']), record,
                         _receipt_path(home, record['review_run_id'], record['project'], record['commit']))
        if _processes_in(path):
            raise store.Refusal('reviewer probe process identity is unconfirmed')
        row = _matching_status(home, Path(project['path']), record)
        if row is None or _validate_allocation(home, Path(project['path']), record, row) != path:
            raise store.Refusal('reviewer probe lease identity changed')
        command = ['return', str(path), '--if-lease-id', record['lease_id'],
                   '--if-lease-holder', record['lease_holder']]
        if approved.get('work_loss'):
            command.append('--force')
        _treehouse(home, Path(project['path']), command)
        destroy = ['destroy', str(path), '--yes']
        if approved.get('work_loss'):
            destroy.append('--include-unlanded')
        _treehouse(home, Path(project['path']), destroy)
        _delete_base_ref(home, Path(project['path']), record)
        if path.exists():
            raise store.Refusal('reviewer probe removal is unconfirmed')
