import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from . import store, tasks, workers, tmux


def emit(home, task, incarnation, kind, key, details):
    identity = f'{task}:{incarnation}:{kind}:{key}'
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    path = home/'state'/'events'/f'{event_id}.json'
    handled = home/'state'/'events-handled'/f'{event_id}.json'
    if not path.exists() and not handled.exists():
        store.atomic_json(path, dict(schema=1, id=event_id, task=task,
                                     incarnation=incarnation, kind=kind, key=key,
                                     observed_at=time.time(), details=details))
    return event_id

def gone(pid, start):
    try:
        return process_start(pid) != start
    except (FileNotFoundError, ProcessLookupError):
        return True



def episode(home, task, kind, failing):
    path = home/'state'/'tasks'/task['id']/f'{kind}.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    incarnation = task.get('incarnation', 'central')
    if previous.get('incarnation') != incarnation:
        previous = {}
    generation = previous.get('generation', 0) + (1 if failing and not previous.get('failing') else 0)
    if previous.get('failing') != failing or previous.get('incarnation') != incarnation:
        store.atomic_json(path, dict(incarnation=incarnation, failing=failing, generation=generation))
    return f'{kind}:{generation}' if failing else None


def pending(home):
    return [json.loads(path.read_text()) for path in sorted((home/'state'/'events').glob('*.json'))]


def acknowledge(home, ids):
    with store.lock(home):
        for value in ids:
            uid = str(uuid.UUID(value))
            event = home/'state'/'events'/f'{uid}.json'
            receipt = home/'state'/'events-handled'/f'{uid}.json'
            if not event.is_file() and not receipt.is_file():
                raise store.Refusal('unknown event ID')
            if event.exists():
                receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(event, receipt)
    return {'acknowledged': ids}


def decisions(home, task_id):
    folder = tasks.task_file(home, task_id).parent/'decisions'
    return [json.loads(path.read_text()) for path in sorted(folder.glob('*.json'))
            if json.loads(path.read_text()).get('status') == 'open']

def resolve(home, task_id, key, answer):
    if not key or '/' in key or '\\' in key or len(key) > 64:
        raise ValueError('invalid decision key')
    text = Path(answer).read_text()
    if not text.strip():
        raise store.Refusal('decision answer is empty')
    with store.lock(home):
        task = tasks.read(home, task_id)
        folder = tasks.task_file(home, task_id).parent
        path = folder/'decisions'/f'{key}.json'
        if not path.is_file():
            raise store.Refusal('unknown decision key')
        decision = json.loads(path.read_text())
        if decision['status'] != 'open' or decision['incarnation'] != task.get('incarnation', 'central'):
            raise store.Refusal('decision is resolved or belongs to a previous worker')
        answer_path = folder/'decisions'/f'{key}.answer.json'
        store.atomic_json(answer_path, {'body': text})
        if task.get('executor') == 'worker':
            message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{task_id}:{task["incarnation"]}:decision:{key}'))
            store.atomic_json(folder/'inbox'/f'{message_id}.json',
                              {'id': message_id, 'body': text, 'incarnation': task['incarnation']})
            decision['message_id'] = message_id
        decision.update(status='resolved', answer=str(answer_path.relative_to(folder)))
        store.atomic_json(path, decision)
    return decision



def reconcile(home):
    found = []
    with store.lock(home):
        for path in sorted((home/'data'/'tasks').glob('*/task.json')):
            task = tasks.read(home, path.parent.name)
            if (task.get('executor') == 'worker' and task['phase'] == 'running'
                and not task.get('endpoint') and task.get('backend') in ('tmux', 'herdr')):
                receipt = home/'state'/'tasks'/task['id']/'endpoint.json'
                if receipt.exists():
                    recorded = json.loads(receipt.read_text())
                    valid = (recorded.get('prefix') == tmux.prefix(home) if task['backend'] == 'tmux'
                             else recorded.get('home') == str(home))
                    if (recorded.get('task') == task['id'] and recorded.get('incarnation') == task.get('incarnation')
                        and valid and recorded.get('pane')):
                        try:
                            state = workers.backend(task).inspect(recorded)
                            presence = state['presence']
                            if task['backend'] == 'herdr' and presence == 'present':
                                command = recorded.get('command')
                                processes = state['process']['foreground_processes']
                                if not command or not any(row.get('argv', [])[-len(command):] == command for row in processes):
                                    presence = 'unknown'
                        except (store.Refusal, store.DependencyError, KeyError, TypeError):
                            presence = 'unknown'
                        if presence == 'present':
                            task['endpoint'] = recorded
                            tasks.save(home, task)
                        elif presence == 'missing':
                            task.update(executor='central', phase='prepared', worker_alive=None,
                                        failed_incarnation=task['incarnation'])
                            tasks.save(home, task)
                            found.append(emit(home, task['id'], task['incarnation'], 'blocked',
                                              'launch-absent', 'start pane vanished before publication; same lease can be launched again'))
            for observation_path in sorted((path.parent/'observations').glob('*.json')):
                observation = json.loads(observation_path.read_text())
                if observation['incarnation'] != task.get('incarnation', 'central'):
                    continue
                if observation['kind'] == 'decision':
                    decision_path = path.parent/'decisions'/f'{observation["key"]}.json'
                    if not decision_path.exists():
                        store.atomic_json(decision_path, dict(key=observation['key'], status='open',
                                                             question=observation['message'],
                                                             incarnation=observation['incarnation']))
                if observation['kind'] in ('decision', 'blocked', 'failed', 'done', 'resolved'):
                    found.append(emit(home, task['id'], observation['incarnation'], observation['kind'],
                                      observation['id'], observation['message']))
                if observation['kind'] == 'idle' and observation['key'] == 'runner-exit' and task['phase'] not in ('ready', 'landed', 'cleaned'):
                    found.append(emit(home, task['id'], observation['incarnation'], 'blocked',
                                      observation['id'], 'worker exited before readiness; inspect retained work'))
            if task.get('executor') == 'worker' and task.get('worker_alive') is not False and task['phase'] != 'cleaned':
                state = workers.inspect(home, task['id'])['endpoint'] if task.get('endpoint') else {'presence': 'unknown'}
                receipt = home/'state'/'tasks'/task['id']/'runner.json'
                if state['presence'] == 'missing' and receipt.exists():
                    runner = json.loads(receipt.read_text())
                    if (runner.get('incarnation') == task['incarnation'] and runner.get('phase') == 'spawned'
                        and gone(runner['pid'], runner['start'])
                        and gone(runner['child_pid'], runner['child_start'])):
                        task['worker_alive'] = False
                        tasks.save(home, task)
                        if task['phase'] not in ('ready', 'landed'):
                            found.append(emit(home, task['id'], task['incarnation'], 'blocked',
                                              'runner-absent', 'worker and child exited before readiness; retained task workspace'))
                key = episode(home, task, 'endpoint-identity',
                              task.get('worker_alive') is not False and state['presence'] != 'present')
                if key:
                    found.append(emit(home, task['id'], task['incarnation'], 'blocked',
                                      key, f'worker endpoint {state["presence"]}; process absence unconfirmed'))
            else:
                episode(home, task, 'endpoint-identity', False)
            if task['phase'] == 'ready':
                try:
                    work = tasks.verify_workspace(home, task)
                    if not store.clean(work) or store.git(work, 'rev-parse', 'HEAD') != task['ready_head']:
                        raise store.Refusal('ready workspace no longer matches committed head')
                except (store.Refusal, store.DependencyError) as error:
                    key = episode(home, task, 'ready-check', True)
                    found.append(emit(home, task['id'], task.get('incarnation', 'central'), 'blocked',
                                      key, str(error)))
                else:
                    episode(home, task, 'ready-check', False)
                    found.append(emit(home, task['id'], task.get('incarnation', 'central'), 'ready',
                                      task['ready_head'], task.get('report', 'report.md')))
            else:
                episode(home, task, 'ready-check', False)
    return {'events': sorted(set(found)), 'pending': pending(home)}


def status(home):
    owner_path = home/'state'/'owner.json'
    owner = json.loads(owner_path.read_text()) if owner_path.exists() else None
    alive = False
    if owner:
        try:
            alive = process_start(owner['watcher_pid']) == owner['watcher_start']
        except (FileNotFoundError, ProcessLookupError):
            pass
    delegated = any(t.get('executor') == 'worker' and t.get('phase') != 'cleaned'
                    and (t.get('phase') == 'running' or t.get('worker_alive') is not False) for t in
                    (tasks.read(home, p.parent.name) for p in (home/'data'/'tasks').glob('*/task.json')))
    open_decisions = [{'task': path.parent.name, 'key': item['key']}
                      for path in sorted((home/'data'/'tasks').glob('*/task.json'))
                      for item in decisions(home, path.parent.name)]
    return {'watcherHealthy': alive and time.time() - owner['heartbeat'] < 12 if owner else False,
            'delegatedWork': delegated, 'pending': pending(home),
            'openDecisions': open_decisions[:100], 'openDecisionCount': len(open_decisions)}


def process_start(pid):
    return Path(f'/proc/{int(pid)}/stat').read_text().split(') ', 1)[1].split()[19]


def watch(home, owner_pid, owner_start, generation):
    lock_path = home/'state'/'session.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise store.Refusal('another central watcher owns this home') from error
        if process_start(owner_pid) != owner_start:
            raise store.Refusal('central owner identity is no longer alive')
        token = os.environ.get('AGD_OWNER')
        if not token:
            raise store.Refusal('central owner token required')
        previous_path = home/'state'/'owner.json'
        if previous_path.exists():
            previous = json.loads(previous_path.read_text())
            try:
                still_alive = process_start(previous['owner_pid']) == previous['owner_start']
            except (FileNotFoundError, ProcessLookupError):
                still_alive = False
            if still_alive and (previous['owner_pid'] != owner_pid or previous.get('token') != token):
                raise store.Refusal('prior central remains alive; foreign watcher cannot take over')
        owner = dict(owner_pid=owner_pid, owner_start=owner_start, generation=generation,
                     token=token, watcher_pid=os.getpid(), watcher_start=process_start(os.getpid()), heartbeat=time.time())
        store.atomic_json(home/'state'/'owner.json', owner)
        print(json.dumps({'type': 'ready', 'generation': generation}), flush=True)
        seen = set()
        while True:
            try:
                owner_alive = process_start(owner_pid) == owner_start
            except (FileNotFoundError, ProcessLookupError):
                owner_alive = False
            if not owner_alive:
                break
            owner['heartbeat'] = time.time()
            store.atomic_json(home/'state'/'owner.json', owner)
            current = {event['id'] for event in reconcile(home)['pending']}
            fresh = current - seen
            if fresh:
                print(json.dumps({'type': 'events', 'ids': sorted(fresh)}), flush=True)
            seen = current
            time.sleep(2)
