"""Central-only Relay producer for continuous human PR review feedback."""
import json
import stat
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import relay, review_feedback, store, supervision, tasks


class ReviewTickError(store.Refusal):
    pass


class SharedReviewError(ReviewTickError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _producer_token(origin, admin_token, project):
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment or
            not isinstance(admin_token, str) or not admin_token):
        raise SharedReviewError('Relay producer credential is unavailable')
    try:
        project = store.validate_id(project)
        request = Request(origin.rstrip('/') + '/v1/credentials',
                          data=json.dumps({'scope': 'producer', 'subjectId': f'agd:review:{project}'},
                                          separators=(',', ':')).encode(), method='POST',
                          headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=15) as response:
            if response.status != 201:
                raise SharedReviewError('Relay producer credential was refused')
            payload = json.loads(response.read())
    except SharedReviewError:
        raise
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SharedReviewError('Relay producer credential is unavailable') from error
    token = payload.get('token') if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise SharedReviewError('Relay producer credential is unavailable')
    return token


def _runtime(home, project, repository):
    service_path = relay._service_file(home, project)
    if service_path.is_symlink() or not service_path.is_file():
        raise SharedReviewError('Relay service record is unavailable')
    try:
        service = json.loads(service_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SharedReviewError('Relay service record is unavailable') from error
    root = service_path.parent
    server = service.get('server') if isinstance(service, dict) else None
    connector = service.get('connector') if isinstance(service, dict) else None
    if (not isinstance(service, dict) or service.get('schema') != 1 or service.get('project') != project or
            service.get('repository') != repository or service.get('root') != str(root) or
            service.get('health') != 'healthy' or not isinstance(server, dict) or
            not isinstance(connector, dict)):
        raise SharedReviewError('Relay service is unavailable')
    server_pid, server_start = server.get('pid'), server.get('start')
    connector_pid, connector_start = connector.get('pid'), connector.get('start')
    if (not isinstance(server_pid, int) or not isinstance(server_start, str) or
            not isinstance(connector_pid, int) or not isinstance(connector_start, str) or
            supervision.gone(server_pid, server_start) or supervision.gone(connector_pid, connector_start)):
        raise SharedReviewError('Relay service is unavailable')
    runtime_path = root / '.relay' / 'runtime.json'
    try:
        mode = runtime_path.stat(follow_symlinks=False).st_mode
        if runtime_path.is_symlink() or not stat.S_ISREG(mode):
            raise OSError('unsafe runtime')
        runtime = json.loads(runtime_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SharedReviewError('Relay runtime is unavailable') from error
    if not isinstance(runtime, dict):
        raise SharedReviewError('Relay runtime is unavailable')
    port, token = runtime.get('port'), runtime.get('token')
    if (runtime.get('pid') != server_pid or not isinstance(port, int) or not 1 <= port <= 65535 or
            not isinstance(token, str) or not token):
        raise SharedReviewError('Relay runtime is unavailable')
    return f'http://127.0.0.1:{port}', token


def _block(home, task_id, reason, shared=False):
    safe = f'human-review-producer: {str(reason)[:400]}'
    folder = tasks.task_file(home, task_id).parent
    try:
        (folder / 'observations').mkdir(mode=0o700, exist_ok=True)
        store.atomic_json(folder / 'observations' / 'human-review-blocked.json', {'reason': safe})
        receipt = tasks.read_pr_receipt(home, task_id)
        if receipt:
            receipt['blocked_reason'] = safe
            tasks.write_pr_receipt(home, task_id, receipt)
        if shared:
            task = tasks.read(home, task_id)
            path = relay._service_file(home, task['project'])
            with store.lock(home):
                service = json.loads(path.read_text())
                if isinstance(service, dict):
                    service.update(health='unhealthy', blocked_reason=safe[:500])
                    store.atomic_json(path, service)
    except (store.Refusal, OSError, json.JSONDecodeError):
        pass


def _feedback_grant(home, task, grant_id):
    with store.lock(home):
        try:
            grant = json.loads((tasks.task_file(home, task['id']).parent/'approvals'/
                                f'{uuid.UUID(grant_id)}.json').read_text())
        except (ValueError, OSError, json.JSONDecodeError) as error:
            raise ReviewTickError('PR review grant is unavailable') from error
        if (grant.get('id') != grant_id or grant.get('task') != task['id'] or
                grant.get('action') != 'pr-publish' or grant.get('remote') is None or
                grant.get('branch') != task.get('branch') or grant.get('base') != task.get('base')):
            raise ReviewTickError('PR review grant is unavailable')
        return grant


def _candidates(home):
    for path in sorted((Path(home) / 'data' / 'tasks').glob('*/task.json')):
        try:
            task = tasks.read(home, path.parent.name)
        except (store.Refusal, OSError, json.JSONDecodeError):
            continue
        grant_id = task.get('pr_grant')
        if task.get('phase') != 'running' or task.get('executor') != 'worker' or not isinstance(grant_id, str):
            continue
        try:
            grant = _feedback_grant(home, task, grant_id)
            receipt = tasks.read_pr_receipt(home, task['id'])
            if receipt is None:
                continue
            pull = receipt.get('pr')
            if (receipt.get('grant_id') != grant['id'] or not isinstance(receipt.get('repository'), str) or
                    not isinstance(pull, dict) or not isinstance(pull.get('number'), int) or pull['number'] < 1 or
                    not isinstance(receipt.get('local_head'), str) or not isinstance(receipt.get('remote_head'), str)):
                raise ReviewTickError('PR review receipt is unavailable')
            yield task, receipt, None
        except (store.Refusal, OSError, json.JSONDecodeError):
            yield task, None, ReviewTickError('PR review receipt is unavailable')


def _fence(github, receipt):
    try:
        current = github('pull_request', None)
    except Exception as error:
        raise ReviewTickError('GitHub review read is unavailable') from error
    if (not isinstance(current, dict) or current.get('state') != 'OPEN' or
            not isinstance(current.get('headSha'), str) or
            receipt['local_head'] != receipt['remote_head'] or
            current['headSha'] != receipt['local_head']):
        raise ReviewTickError('pull request head is superseded')


def tick(home, now=None):
    """Poll each eligible scoped PR once; a single PR failure never stops the watcher."""
    del now
    checked = emitted = blocked = 0
    shared_failure = None
    for task, receipt, candidate_error in list(_candidates(home)):
        checked += 1
        if shared_failure:
            blocked += 1
            _block(home, task['id'], shared_failure, shared=True)
            continue
        if candidate_error:
            blocked += 1
            _block(home, task['id'], candidate_error)
            continue
        try:
            repository, number = receipt['repository'], receipt['pr']['number']
            origin, admin_token = _runtime(home, task['project'], repository)
            github = review_feedback.central_github_reader(repository, number)
            _fence(github, receipt)
            producer = review_feedback.LoopbackRelay(origin, _producer_token(origin, admin_token, task['project']))
            result = review_feedback.watch_human_reviews(home, task['project'], repository, number, github, producer)
            emitted += result.get('emitted', 0)
        except SharedReviewError as error:
            shared_failure = error
            blocked += 1
            _block(home, task['id'], error, shared=True)
        except ReviewTickError as error:
            blocked += 1
            _block(home, task['id'], error)
        except Exception:
            blocked += 1
            _block(home, task['id'], ReviewTickError('human review feedback is unavailable'))
    return {'checked': checked, 'emitted': emitted, 'blocked': blocked}
