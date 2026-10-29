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


def event(home, task_id, incarnation, kind, key, message):
    task = tasks.read(home, task_id)
    if incarnation != task.get('incarnation') and not (incarnation == 'central' and task['executor'] == 'central'):
        raise store.Refusal('worker incarnation no longer owns task')
    if os.environ.get('AGD_TASK_ID') and (os.environ['AGD_TASK_ID'] != task_id or os.environ.get('AGD_INCARNATION') != incarnation):
        raise store.Refusal('event belongs to a different task process')
    if kind not in ('busy', 'idle', 'blocked', 'decision', 'resolved', 'failed', 'done'):
        raise ValueError('unsupported event kind')
    if not key or '/' in key or len(key) > 64:
        raise ValueError('invalid event key')
    text = Path(message).read_text()
    if not text.strip():
        raise store.Refusal('event message is empty')
    observation = dict(id=str(uuid.uuid4()), task=task_id, incarnation=incarnation,
                       kind=kind, key=key, message=text, observed_at=time.time())
    store.atomic_json(tasks.task_file(home, task_id).parent/'observations'/f'{observation["id"]}.json', observation)
    return observation


def launch(home, task_id, harness='omp', selected_backend=None, unattended=False, approval_id=None,
           profile=None, skill_mode='profile', skills=(), pr_grant=None):
    task = tasks.read(home, task_id)
    if task['phase'] != 'prepared' or task['executor'] != 'central':
        raise store.Refusal('task already launched or not prepared')
    projects_data = projects.read(home, task['project'])
    if harness == 'cursor' and not projects_data['trusted']:
        raise store.Refusal('Cursor project trust requires an explicit user grant')
    work = tasks.verify_workspace(home, task)
    if unattended:
        tasks.authorization(home, task, approval_id or '', 'unattended', task_id)
    if selected_backend is None:
        selected_backend = launcher.active_backend(home, dict(os.environ)) or launcher.resolve_backend(home, None)
    if selected_backend not in ('tmux', 'herdr') or harness not in ('omp', 'cursor'):
        raise ValueError('unsupported backend or harness')
    if pr_grant:
        grant = tasks.authorize_pr_publish(home, task, pr_grant)
        from . import pr, relay
        try:
            repository = pr.github_repository(grant['remote_url'])
            relay.start_project_service(home, task['project'], repository)
        except (store.Refusal, store.DependencyError) as error:
            store.atomic_json(tasks.task_file(home, task_id).parent/'relay-blocked.json',
                              {'reason': str(error)[:400]})
            raise
        if profile is not None or skill_mode != 'profile' or skills:
            raise store.Refusal('PR grant launch controls worker skills')
        profile, skill_mode, skills = None, 'curated', ('agd-scoped-pr-relay',)
    preflight = dict(task, harness=harness, backend=selected_backend, pr_grant=pr_grant)
    harnesses.launch_argv(preflight, tasks.task_file(home, task_id).parent/'brief.md', unattended)
    selection = ({'schema': 1, 'mode': 'curated', 'profile': None,
                  'skills': ['agd-scoped-pr-relay']} if pr_grant else
                 worker_skills.resolve(home, profile=profile, skill_mode=skill_mode, skills=skills))
    module = backend(preflight)
    if selected_backend == 'tmux' and not shutil.which('tmux'):
        raise store.DependencyError('tmux is not installed')
    if selection['skills']:
        harnesses.worker_skill_roots(harness)
    if selected_backend == 'herdr':
        module.available()
    with store.lock(home):
        current = tasks.read(home, task_id)
        if current['phase'] != 'prepared' or current['executor'] != 'central':
            raise store.Refusal('task launch raced another owner; no endpoint created')
        if pr_grant:
            tasks._authorize_pr_publish_locked(home, current, pr_grant)
            selection = {'schema': 1, 'mode': 'curated', 'profile': None,
                         'skills': ['agd-scoped-pr-relay']}
            current['pr_grant'] = pr_grant
        manifest = None
        if selection['skills']:
            manifest = worker_skills.prepare(
                home, dict(current, harness=harness), selection,
                {'agd-scoped-pr-relay': worker_skills.scoped_pr_source()} if pr_grant else None)
        if selected_backend == 'herdr':
            module.prepare(home)
        task = current
        task.update(harness=harness, backend=selected_backend, worker_skill_selection=selection)
        if manifest is not None:
            task['worker_skills'] = manifest
        incarnation = str(uuid.uuid4())
        task.update(executor='worker', incarnation=incarnation, phase='running', worker_alive=None,
                    unattended=unattended)
        if harness == 'omp':
            task['observability'] = observability.worker_binding_locked(home, task_id, incarnation)
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


def send(home, task_id, file):
    task = tasks.read(home, task_id)
    if task.get('executor') != 'worker':
        raise store.Refusal('task has no worker')
    text = Path(file).read_text()
    if not text.strip():
        raise store.Refusal('empty message')
    message_id = str(uuid.uuid4())
    path = tasks.task_file(home, task_id).parent/'inbox'/f'{message_id}.json'
    store.atomic_json(path, {'id': message_id, 'body': text, 'incarnation': task['incarnation']})
    return {'id': message_id, 'delivery': 'queued', 'inbox': str(path)}


def inbox(home, task_id):
    task = tasks.read(home, task_id)
    if os.environ.get('AGD_TASK_ID') and (os.environ['AGD_TASK_ID'] != task_id or
                                          os.environ.get('AGD_INCARNATION') != task.get('incarnation')):
        raise store.Refusal('worker cannot read a different task inbox')
    folder = tasks.task_file(home, task_id).parent
    return [json.loads(p.read_text()) for p in sorted((folder/'inbox').glob('*.json'))
            if not (folder/'inbox-ack'/p.name).exists()]


def acknowledge(home, task_id, message_id):
    task = tasks.read(home, task_id)
    uuid.UUID(message_id)
    if os.environ.get('AGD_INCARNATION') != task.get('incarnation') or os.environ.get('AGD_TASK_ID') != task_id:
        raise store.Refusal('worker message acknowledgement needs current incarnation')
    path = tasks.task_file(home, task_id).parent/'inbox'/f'{message_id}.json'
    if not path.exists():
        raise store.Refusal('message missing')
    receipt = tasks.task_file(home, task_id).parent/'inbox-ack'/path.name
    store.atomic_json(receipt, {'id': message_id, 'incarnation': task['incarnation']})
    return {'acknowledged': message_id}


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
    if task.get('executor') != 'worker' or task['phase'] != 'running':
        raise store.Refusal('only unfinished worker tasks can relaunch')
    if task.get('worker_alive') is not False or not task.get('endpoint'):
        raise store.Refusal('prior worker process absence unconfirmed')
    if inspect(home, task_id)['endpoint']['presence'] != 'missing':
        raise store.Refusal('prior owned endpoint not proved absent')
    work = tasks.verify_workspace(home, task)
    if task.get('worker_skills'):
        worker_skills.ensure(home, task)
        worker_skills.verify(home, task)
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
