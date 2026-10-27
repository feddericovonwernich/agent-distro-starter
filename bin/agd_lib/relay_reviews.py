"""Central-only Relay producer for continuous human PR review feedback."""
import json
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import relay, review_feedback, store, supervision, tasks


class ReviewTickError(store.Refusal):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _producer_token(origin, admin_token, project):
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment or
            not isinstance(admin_token, str) or not admin_token):
        raise ReviewTickError('Relay producer credential is unavailable')
    try:
        project = store.validate_id(project)
        request = Request(origin.rstrip('/') + '/v1/credentials',
                          data=json.dumps({'scope': 'producer', 'subjectId': f'agd:review:{project}'},
                                          separators=(',', ':')).encode(), method='POST',
                          headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=15) as response:
            if response.status != 201:
                raise ReviewTickError('Relay producer credential was refused')
            payload = json.loads(response.read())
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
        raise ReviewTickError('Relay producer credential is unavailable') from error
    token = payload.get('token') if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ReviewTickError('Relay producer credential is unavailable')
    return token


def _runtime(home, project, repository):
    service_path = relay._service_file(home, project)
    if service_path.is_symlink() or not service_path.is_file():
        raise ReviewTickError('Relay service record is unavailable')
    try:
        service = json.loads(service_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewTickError('Relay service record is unavailable') from error
    root = service_path.parent
    server = service.get('server') if isinstance(service, dict) else None
    connector = service.get('connector') if isinstance(service, dict) else None
    if (not isinstance(service, dict) or service.get('schema') != 1 or service.get('project') != project or
            service.get('repository') != repository or service.get('root') != str(root) or
            service.get('health') not in ('healthy', 'unhealthy') or not isinstance(server, dict) or
            not isinstance(server.get('start'), str) or not isinstance(connector.get('pid'), int) or
            not isinstance(connector.get('start'), str) or
            supervision.gone(server['pid'], server['start']) or
            supervision.gone(connector['pid'], connector['start'])):
        raise ReviewTickError('Relay service is unavailable')
    runtime_path = root / '.relay' / 'runtime.json'
    try:
        mode = runtime_path.stat(follow_symlinks=False).st_mode
        if runtime_path.is_symlink() or not stat.S_ISREG(mode):
            raise OSError('unsafe runtime')
        runtime = json.loads(runtime_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewTickError('Relay runtime is unavailable') from error
    if not isinstance(runtime, dict):
        raise ReviewTickError('Relay runtime is unavailable')
    port, token = runtime.get('port'), runtime.get('token')
    if (runtime.get('pid') != server['pid'] or not isinstance(port, int) or not 1 <= port <= 65535 or
            not isinstance(token, str) or not token):
        raise ReviewTickError('Relay runtime is unavailable')
    return f'http://127.0.0.1:{port}', token


def _block(home, task_id, reason):
    safe = f'human-review-producer: {str(reason)[:400]}'
    try:
        receipt = tasks.read_pr_receipt(home, task_id)
        if receipt:
            receipt['blocked_reason'] = safe
            tasks.write_pr_receipt(home, task_id, receipt)
        task = tasks.read(home, task_id)
        path = relay._service_file(home, task['project'])
        with store.lock(home):
            service = json.loads(path.read_text())
            service.update(health='unhealthy', blocked_reason=safe[:500])
            store.atomic_json(path, service)
    except (store.Refusal, OSError, json.JSONDecodeError):
        pass


def _candidates(home):
    folder = Path(home) / 'data' / 'tasks'
    for path in sorted(folder.glob('*/task.json')):
        try:
            task = tasks.read(home, path.parent.name)
            grant_id = task.get('pr_grant')
            if task.get('phase') != 'running' or task.get('executor') != 'worker' or not isinstance(grant_id, str):
                continue
            grant = tasks.authorize_pr_publish(home, task, grant_id)
            receipt = tasks.read_pr_receipt(home, task['id'])
            pull = receipt.get('pr') if receipt else None
            if (not receipt or receipt.get('grant_id') != grant['id'] or not isinstance(receipt.get('repository'), str) or
                    not isinstance(pull, dict) or not isinstance(pull.get('number'), int) or pull['number'] < 1):
                continue
            yield task, receipt
        except store.Refusal:
            continue


def tick(home, now=None):
    """Poll each eligible scoped PR once; a single PR failure never stops the watcher."""
    del now
    checked = emitted = blocked = 0
    for task, receipt in _candidates(home):
        checked += 1
        try:
            repository, number = receipt['repository'], receipt['pr']['number']
            origin, admin_token = _runtime(home, task['project'], repository)
            github = review_feedback.central_github_reader(repository, number)
            producer = review_feedback.LoopbackRelay(origin, _producer_token(origin, admin_token, task['project']))
            result = review_feedback.watch_human_reviews(home, task['project'], repository, number, github, producer)
            emitted += result.get('emitted', 0)
        except (ReviewTickError, review_feedback.ReviewFeedbackError, store.Refusal, store.DependencyError) as error:
            blocked += 1
            _block(home, task['id'], error)
    return {'checked': checked, 'emitted': emitted, 'blocked': blocked}
