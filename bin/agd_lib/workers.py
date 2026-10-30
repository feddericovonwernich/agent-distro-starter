import json
import os
import shutil
import time
import uuid
from pathlib import Path
from . import harnesses, launcher, observability, projects, store, tasks, tmux, worker_skills


def backend(task):
    if task['backend'] == 'tmux':
        return tmux
    if task['backend'] == 'herdr':
        try:
            from . import herdr
        except ImportError as error:
            raise store.DependencyError('Herdr adapter unavailable; do not launch on this backend') from error
        return herdr
    raise ValueError('unsupported backend')


def _observation(home, task_id, incarnation, kind, key, text,
                 observation_id=None, request_id=None):
    if kind not in ('busy', 'idle', 'blocked', 'decision', 'resolved', 'failed', 'done'):
        raise ValueError('unsupported event kind')
    if not key or '/' in key or len(key) > 64:
        raise ValueError('invalid event key')
    if not text.strip():
        raise store.Refusal('event message is empty')
    observation = dict(
        id=observation_id or str(uuid.uuid4()),
        task=task_id,
        incarnation=incarnation,
        kind=kind,
        key=key,
        message=text,
    )
    if request_id is not None:
        observation['request_id'] = request_id
    path = tasks.task_file(home, task_id).parent/'observations'/f'{observation["id"]}.json'
    if path.exists():
        existing = json.loads(path.read_text())
        if any(existing.get(name) != value for name, value in observation.items()):
            raise store.Refusal('worker observation conflicts with durable state')
        return existing
    observation['observed_at'] = time.time()
    store.atomic_json(path, observation)
    return observation


def event(home, task_id, incarnation, kind, key, message):
    with store.lock(home):
        task = tasks.read(home, task_id)
        if incarnation != task.get('incarnation') and not (incarnation == 'central' and task['executor'] == 'central'):
            raise store.Refusal('worker incarnation no longer owns task')
        if os.environ.get('AGD_TASK_ID') and (os.environ['AGD_TASK_ID'] != task_id or os.environ.get('AGD_INCARNATION') != incarnation):
            raise store.Refusal('event belongs to a different task process')
        return _observation(
            home, task_id, incarnation, kind, key, Path(message).read_text())


def context(home: Path, task_id: str, incarnation: str) -> dict:
    with store.lock(home):
        task = tasks.read(home, task_id)
        if (task.get('executor') != 'worker' or task.get('harness') != 'omp'
                or task.get('incarnation') != incarnation
                or os.environ.get('AGD_TASK_ID') != task_id
                or os.environ.get('AGD_INCARNATION') != incarnation):
            raise store.Refusal('worker context requires its current OMP task environment')
        worker_skills.require_protocol(home, task, task.get('pr_grant'))
        manifest = worker_skills._manifest(home, task)
        if manifest['state'] != 'complete':
            raise store.Refusal('worker protocol snapshot is not complete')
        protocol = next(row for row in manifest['skills'] if row['id'] == 'agd-worker')
        for location in ('snapshot', 'destination'):
            worker_skills._check(protocol, Path(protocol[location]))
        core = next(row for row in protocol['files'] if row['path'] == 'SKILL.md')
        folder = tasks.task_file(home, task_id).parent
        latest = {}
        for path in (folder/'observations').glob('*.json'):
            observation = json.loads(path.read_text())
            if (observation['incarnation'] != incarnation
                    or observation['key'] == 'agd-contract-incomplete'
                    or observation['kind'] not in ('blocked', 'decision', 'resolved')):
                continue
            key = observation['key']
            previous = latest.get(key)
            if previous is None or (observation['observed_at'], observation['id']) > (
                    previous['observed_at'], previous['id']):
                latest[key] = observation
        active = set()
        for key, observation in latest.items():
            kind = observation['kind']
            if kind == 'decision':
                receipt = folder/'decisions'/f'{key}.json'
                decision = json.loads(receipt.read_text()) if receipt.is_file() else {}
                if (decision.get('incarnation') == incarnation
                        and decision.get('status') == 'resolved'
                        and decision.get('resolved_at', 0) >= observation['observed_at']):
                    continue
            active.add(kind)
        if task['phase'] in ('landed', 'cleaned'):
            outcome = 'closed'
        elif task['phase'] == 'ready':
            outcome = 'ready'
        else:
            outcome = 'decision' if 'decision' in active else 'blocked' if 'blocked' in active else 'unfinished'
        return {
            'schema': 1, 'task_id': task_id, 'incarnation': incarnation,
            'phase': task['phase'], 'kind': task['kind'], 'worktree': task['worktree'],
            'brief': str(folder/'brief.md'), 'report': str(folder/'report-draft.md'),
            'agd_argv': [str(Path(__file__).resolve().parents[1]/'agd'), '--home', str(home)],
            'pr_grant': task.get('pr_grant'),
            'task_skills': manifest['selection']['skills'],
            'system_skills': manifest['selection']['system_skills'],
            'protocol': {'path': str(Path(protocol['destination'])/'SKILL.md'),
                         'sha256': core['sha256']},
            'outcome': outcome,
        }


def launch(home, task_id, selected_backend=None, unattended=False, approval_id=None,
           pr_grant=None):
    task = tasks.read(home, task_id)
    if task['phase'] != 'prepared' or task['executor'] != 'central':
        raise store.Refusal('task already launched or not prepared')
    harness = task.get('harness', 'omp')
    selection = task.get('worker_skill_selection', {
        'schema': 2, 'mode': 'none', 'profile': None,
        'skills': [], 'sources': {},
    })
    if (not isinstance(selection, dict) or selection.get('schema') != 2
            or not isinstance(selection.get('skills'), list)):
        raise store.Refusal('task worker skill selection is invalid')
    worker_skills.require_protocol(home, task, pr_grant)
    projects_data = projects.read(home, task['project'])
    if harness == 'cursor' and not projects_data['trusted']:
        raise store.Refusal('Cursor project trust requires an explicit user grant')
    work = tasks.verify_workspace(home, task)
    if task.get('worker_skills'):
        worker_skills.ensure(home, task)
    if unattended:
        tasks.authorization(home, task, approval_id or '', 'unattended', task_id)
    if selected_backend is None:
        selected_backend = launcher.active_backend(home, dict(os.environ)) or launcher.resolve_backend(home, None)
    if selected_backend not in ('tmux', 'herdr') or harness not in ('omp', 'cursor'):
        raise ValueError('unsupported backend or harness')
    if pr_grant:
        if selection['skills']:
            raise store.Refusal('PR grant requires a task created with no ordinary worker skills')
        grant = tasks.authorize_pr_publish(home, task, pr_grant)
        from . import pr, relay
        try:
            repository = pr.github_repository(grant['remote_url'])
            relay.start_project_service(home, task['project'], repository)
        except (store.Refusal, store.DependencyError) as error:
            store.atomic_json(tasks.task_file(home, task_id).parent/'relay-blocked.json',
                              {'reason': str(error)[:400]})
            raise
    preflight = dict(task, backend=selected_backend, pr_grant=pr_grant)
    harnesses.launch_argv(
        preflight, tasks.task_file(home, task_id).parent/'brief.md', unattended)
    module = backend(preflight)
    if selected_backend == 'tmux' and not shutil.which('tmux'):
        raise store.DependencyError('tmux is not installed')
    if selected_backend == 'herdr':
        module.available()
    with store.lock(home):
        current = tasks.read(home, task_id)
        if current['phase'] != 'prepared' or current['executor'] != 'central':
            raise store.Refusal('task launch raced another owner; no endpoint created')
        if pr_grant:
            manifest = worker_skills.add_scoped_pr(home, current, pr_grant)
            current['worker_skills'] = manifest
            current['worker_skill_selection'] = manifest['selection']
            current['pr_grant'] = pr_grant
        if selected_backend == 'herdr':
            module.prepare(home)
        task = current
        task['backend'] = selected_backend
        incarnation = str(uuid.uuid4())
        task.update(executor='worker', incarnation=incarnation, phase='running',
                    worker_alive=None, unattended=unattended)
        if harness == 'omp':
            task['observability'] = observability.worker_binding_locked(
                home, task_id, incarnation)
        tasks.save(home, task)
    endpoint = module.create(home, task_id, incarnation, work,
                             ['env', f'AGD_TASK_ID={task_id}', f'AGD_INCARNATION={incarnation}',
                              str(Path(__file__).resolve().parents[1]/'agd'), '--home', str(home),
                              'worker', 'run', task_id, '--incarnation', incarnation])
    with store.lock(home):
        task['endpoint'] = endpoint
        tasks.save(home, task)
    return task


def run_worker(home, task_id, incarnation):
    task = tasks.read(home, task_id)
    if task.get('incarnation') != incarnation:
        raise store.Refusal('obsolete runner')
    if os.environ.get('AGD_TASK_ID') != task_id or os.environ.get('AGD_INCARNATION') != incarnation:
        raise store.Refusal('runner requires its scoped task environment')
    deadline = time.monotonic() + 10
    while not task.get('endpoint') and time.monotonic() < deadline:
        time.sleep(0.05)
        task = tasks.read(home, task_id)
    if not task.get('endpoint'):
        raise store.Refusal('endpoint identity was not published; retaining task')
    tasks.verify_workspace(home, task)
    if task.get('worker_skills'):
        worker_skills.verify(home, task)
    if task['harness'] == 'omp':
        child_env, generation, warning = observability.managed_environment(home, 'worker', task, incarnation)
        session_dir = Path(task['observability']['session_dir'])
    else:
        child_env = dict(os.environ, AGD_HOME=str(home), AGD_TASK_ID=task_id, AGD_INCARNATION=incarnation)
        session_dir = None
        warning = None
    child_env.pop('AGD_OWNER', None)
    child_env.pop('AGD_PR_GRANT', None)
    if task['harness'] == 'omp':
        child_env['AGD_CLI'] = str(Path(__file__).resolve().parents[1]/'agd')
        if task.get('pr_grant'):
            child_env['AGD_PR_GRANT'] = task['pr_grant']
    child_env.pop('AGD_WORKER_RESUME_READY', None)
    if task['harness'] == 'omp' and task['phase'] == 'ready':
        child_env['AGD_WORKER_RESUME_READY'] = '1'
    import subprocess
    from .supervision import process_start
    args = harnesses.launch_argv(task, tasks.task_file(home, task_id).parent/'brief.md', task['unattended'],
                                session_dir=session_dir)
    receipt = home/'state'/'tasks'/task_id/'runner.json'
    runner = dict(incarnation=incarnation, pid=os.getpid(), start=process_start(os.getpid()),
                  phase='spawning')
    if warning:
        runner['observability_warning'] = warning
    if task['harness'] == 'omp':
        runner['observability_generation'] = generation
    store.atomic_json(receipt, runner)
    with store.lock(home):
        current = tasks.read(home, task_id)
        if current.get('incarnation') != incarnation:
            raise store.Refusal('worker incarnation changed before process spawn')
        current['worker_alive'] = True
        tasks.save(home, current)
    child = None
    try:
        child = subprocess.Popen(args, cwd=task['worktree'], env=child_env, stdin=None)
        runner.update(phase='spawned', child_pid=child.pid, child_start=process_start(child.pid))
        if task['harness'] == 'omp':
            observability.record_process_generation(home, 'worker', generation, task_id, incarnation,
                                                    child.pid, runner['child_start'])
        store.atomic_json(receipt, runner)
        status = child.wait()
    except Exception as error:
        if child is not None:
            raise store.Refusal('worker child started but receipt/exit uncertain; retain lease') from error
        status = -1
        reason = str(error)
    else:
        reason = f'worker process exited {status}'
    if tasks.read(home, task_id).get('incarnation') == incarnation:
        note = tasks.task_file(home, task_id).parent/'runner-exit.txt'
        with store.lock(home):
            current = tasks.read(home, task_id)
            if current.get('incarnation') == incarnation:
                current['worker_alive'] = False
                tasks.save(home, current)
        note.write_text(reason)
        event(home, task_id, incarnation, 'failed' if status else 'idle', 'runner-exit', note)
    return {'exit': status}


def inspect(home, task_id):
    task = tasks.read(home, task_id)
    if not task.get('endpoint'):
        return {'presence': 'unknown', 'task': task}
    try:
        state = backend(task).inspect(task['endpoint'])
    except (store.Refusal, store.DependencyError) as error:
        state = {'presence': 'unknown', 'reason': str(error)}
    return {'task': task, 'endpoint': state}


def _next_inbox_sequence(folder):
    path = folder/'inbox-sequence.json'
    if path.exists():
        state = json.loads(path.read_text())
        value = state.get('value') if isinstance(state, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise store.Refusal('invalid worker inbox sequence')
    else:
        value = 0
    value += 1
    store.atomic_json(path, {'schema': 1, 'value': value})
    return value


def enqueue_locked(home, task, text, message_id=None):
    if not text.strip():
        raise store.Refusal('empty message')
    message_id = str(uuid.UUID(message_id)) if message_id else str(uuid.uuid4())
    folder = tasks.task_file(home, task['id']).parent
    path = folder/'inbox'/f'{message_id}.json'
    store.atomic_json(path, {
        'schema': 1,
        'id': message_id,
        'body': text,
        'incarnation': task['incarnation'],
        'sequence': _next_inbox_sequence(folder),
        'queued_at': time.time(),
    })
    return path


def send(home, task_id, file):
    text = Path(file).read_text()
    if not text.strip():
        raise store.Refusal('empty message')
    inspected = inspect(home, task_id)
    inspected_task = inspected.get('task', {})
    endpoint = inspected.get('endpoint', inspected)
    with store.lock(home):
        task = tasks.read(home, task_id)
        if (task.get('phase') not in ('running', 'ready')
                or task.get('executor') != 'worker'
                or task.get('harness') != 'omp'
                or task.get('worker_alive') is not True
                or endpoint.get('presence') != 'present'
                or inspected_task.get('incarnation') != task.get('incarnation')
                or inspected_task.get('endpoint') != task.get('endpoint')):
            raise store.Refusal('worker is not presently available')
        path = enqueue_locked(home, task, text)
    return {'id': path.stem, 'delivery': 'queued', 'inbox': str(path)}


def _inbox_record(path):
    try:
        record = json.loads(path.read_text())
        uuid.UUID(record['id'])
        valid = (
            record.get('schema') == 1
            and record['id'] == path.stem
            and isinstance(record.get('body'), str)
            and bool(record['body'].strip())
            and isinstance(record.get('incarnation'), str)
            and isinstance(record.get('sequence'), int)
            and not isinstance(record['sequence'], bool)
            and record['sequence'] > 0
            and isinstance(record.get('queued_at'), (int, float))
            and not isinstance(record['queued_at'], bool)
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        valid = False
    if not valid:
        raise store.Refusal('invalid worker inbox record')
    return record


def _reply_record(path):
    try:
        reply = json.loads(path.read_text())
        uuid.UUID(reply['id'])
        uuid.UUID(reply['observation_id'])
        valid = (
            reply.get('schema') == 1
            and reply['id'] == path.stem
            and isinstance(reply.get('enqueueing_incarnation'), str)
            and isinstance(reply.get('completed_incarnation'), str)
            and reply.get('status') in ('done', 'failed')
            and isinstance(reply.get('body'), str)
            and bool(reply['body'].strip())
            and isinstance(reply.get('completed_at'), (int, float))
            and not isinstance(reply['completed_at'], bool)
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        valid = False
    if not valid:
        raise store.Refusal('invalid worker reply record')
    return reply


def _repair_completion_locked(home, task_id, request, reply):
    observation_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f'{task_id}:{request["id"]}:reply'))
    if (reply['id'] != request['id']
            or reply['enqueueing_incarnation'] != request['incarnation']
            or reply['observation_id'] != observation_id):
        raise store.Refusal('worker reply conflicts with request')
    _observation(
        home,
        task_id,
        reply['completed_incarnation'],
        'done' if reply['status'] == 'done' else 'blocked',
        request['id'],
        reply['body'],
        observation_id,
        request['id'],
    )
    folder = tasks.task_file(home, task_id).parent
    receipt = folder/'inbox-ack'/f'{request["id"]}.json'
    expected = {
        'schema': 1,
        'id': request['id'],
        'incarnation': reply['completed_incarnation'],
        'observation_id': observation_id,
    }
    if receipt.exists():
        if json.loads(receipt.read_text()) != expected:
            raise store.Refusal('worker acknowledgement conflicts with reply')
    else:
        store.atomic_json(receipt, expected)
    return reply


def inbox(home, task_id):
    with store.lock(home):
        task = tasks.read(home, task_id)
        scoped = os.environ.get('AGD_TASK_ID') is not None
        if scoped and (os.environ['AGD_TASK_ID'] != task_id or
                       os.environ.get('AGD_INCARNATION') != task.get('incarnation')):
            raise store.Refusal('worker cannot read a different task inbox')
        folder = tasks.task_file(home, task_id).parent
        if scoped:
            for reply_path in (folder/'replies').glob('*.json'):
                reply = _reply_record(reply_path)
                request_path = folder/'inbox'/f'{reply["id"]}.json'
                if not request_path.is_file():
                    raise store.Refusal('worker reply request is missing')
                _repair_completion_locked(
                    home, task_id, _inbox_record(request_path), reply)
        records = [_inbox_record(path) for path in (folder/'inbox').glob('*.json')
                   if not (folder/'inbox-ack'/path.name).exists()]
    return sorted(records, key=lambda record: (record['sequence'], record['id']))


def complete(home, task_id, request_id, incarnation, status, response):
    try:
        request_id = str(uuid.UUID(request_id))
    except (TypeError, ValueError) as error:
        raise store.Refusal('invalid worker request ID') from error
    if status not in ('done', 'failed'):
        raise store.Refusal('invalid worker completion status')
    if not isinstance(response, str) or not response.strip():
        raise store.Refusal('worker response is empty')
    with store.lock(home):
        task = tasks.read(home, task_id)
        if (os.environ.get('AGD_TASK_ID') != task_id
                or os.environ.get('AGD_INCARNATION') != incarnation
                or task.get('incarnation') != incarnation):
            raise store.Refusal('worker completion needs current incarnation')
        folder = tasks.task_file(home, task_id).parent
        request_path = folder/'inbox'/f'{request_id}.json'
        if not request_path.is_file():
            raise store.Refusal('worker request is missing')
        request = _inbox_record(request_path)
        observation_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f'{task_id}:{request_id}:reply'))
        reply_path = folder/'replies'/f'{request_id}.json'
        if reply_path.exists():
            reply = _reply_record(reply_path)
            expected = {
                'id': request_id,
                'enqueueing_incarnation': request['incarnation'],
                'completed_incarnation': incarnation,
                'status': status,
                'body': response,
                'observation_id': observation_id,
            }
            if any(reply.get(name) != value for name, value in expected.items()):
                raise store.Refusal('worker completion conflicts with durable reply')
        else:
            reply = {
                'schema': 1,
                'id': request_id,
                'enqueueing_incarnation': request['incarnation'],
                'completed_incarnation': incarnation,
                'status': status,
                'body': response,
                'completed_at': time.time(),
                'observation_id': observation_id,
            }
            store.atomic_json(reply_path, reply)
        return _repair_completion_locked(home, task_id, request, reply)


def turn_state(home, task):
    folder = tasks.task_file(home, task['id']).parent/'observations'
    turns = (json.loads(path.read_text()) for path in folder.glob('*.json'))
    latest = max((item for item in turns if item['incarnation'] == task['incarnation']
                  and item['key'] == 'turn' and item['kind'] in ('busy', 'idle')),
                 key=lambda item: item['observed_at'], default=None)
    return latest['kind'] if latest else 'unknown'


def interrupt(home, task_id):
    task = tasks.read(home, task_id)
    if task.get('harness') != 'omp' or turn_state(home, task) != 'busy':
        raise store.Refusal('interrupt needs a confirmed busy omp turn')
    if inspect(home, task_id)['endpoint']['presence'] != 'present':
        raise store.Refusal('worker endpoint not present')
    backend(task).send_key(task['endpoint'], 'escape')
    return {'delivery': 'unconfirmed', 'worker_alive': task.get('worker_alive')}


def stop(home, task_id):
    task = tasks.read(home, task_id)
    if task.get('worker_alive') is False and task.get('endpoint') and inspect(home, task_id)['endpoint']['presence'] == 'missing':
        return task
    if task.get('harness') != 'omp' or turn_state(home, task) != 'idle':
        raise store.Refusal('stop needs a confirmed idle omp composer')
    if inspect(home, task_id)['endpoint']['presence'] != 'present':
        raise store.Refusal('worker endpoint not present')
    backend(task).send_text(task['endpoint'], '/quit')
    backend(task).send_key(task['endpoint'], 'enter')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = tasks.read(home, task_id)
        if inspect(home, task_id)['endpoint']['presence'] == 'missing':
            exits = (json.loads(path.read_text()) for path in
                     (tasks.task_file(home, task_id).parent/'observations').glob('*.json'))
            if any(row['incarnation'] == task['incarnation'] and row['key'] == 'runner-exit' for row in exits):
                with store.lock(home):
                    current = tasks.read(home, task_id)
                    if current['incarnation'] == task['incarnation']:
                        current['worker_alive'] = False
                        tasks.save(home, current)
            if current.get('worker_alive') is False:
                return current
        time.sleep(0.2)
    raise store.Refusal('quit submitted but process absence unconfirmed; retain workspace')


def relaunch(home, task_id):
    task = tasks.read(home, task_id)
    worker_skills.require_protocol(home, task, task.get('pr_grant'))
    if task.get('executor') != 'worker' or task['phase'] not in ('running', 'ready'):
        raise store.Refusal('only running or ready worker tasks can relaunch')
    if task['phase'] == 'ready' and task.get('harness') != 'omp':
        raise store.Refusal('ready worker relaunch requires OMP')
    if task.get('worker_alive') is not False or not task.get('endpoint'):
        raise store.Refusal('prior worker process absence unconfirmed')
    if inspect(home, task_id)['endpoint']['presence'] != 'missing':
        raise store.Refusal('prior owned endpoint not proved absent')
    work = tasks.verify_workspace(home, task)
    if task.get('worker_skills'):
        worker_skills.ensure(home, task)
        worker_skills.verify(home, task)
    if (task['phase'] == 'ready' and
            (not store.clean(work) or store.git(work, 'rev-parse', 'HEAD') != task['ready_head'])):
        raise store.Refusal('ready workspace no longer matches committed head')
    harnesses.launch_argv(task, tasks.task_file(home, task_id).parent/'brief.md', task['unattended'])
    if task['backend'] == 'herdr':
        backend(task).prepare(home)
    incarnation = str(uuid.uuid4())
    task['prior_endpoint'] = task.pop('endpoint')
    task.update(incarnation=incarnation, worker_alive=None, condition='normal')
    with store.lock(home):
        current = tasks.read(home, task_id)
        if current.get('incarnation') != task['prior_endpoint']['incarnation'] or current.get('worker_alive') is not False:
            raise store.Refusal('prior worker changed during relaunch; no endpoint created')
        if current.get('pr_grant'):
            tasks._authorize_pr_publish_locked(home, current, current['pr_grant'])
        if task['harness'] == 'omp':
            task['observability'] = observability.worker_binding_locked(home, task_id, incarnation)
        tasks.save(home, task)
    endpoint = backend(task).create(home, task_id, incarnation, work,
                                    ['env', f'AGD_TASK_ID={task_id}', f'AGD_INCARNATION={incarnation}',
                                     str(Path(__file__).resolve().parents[1]/'agd'), '--home', str(home),
                                     'worker', 'run', task_id, '--incarnation', incarnation])
    with store.lock(home):
        task['endpoint'] = endpoint
        tasks.save(home, task)
    return task
