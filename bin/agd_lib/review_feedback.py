"""Read-only GitHub human-review producer for the central Relay service."""
import hashlib
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from . import store


class ReviewFeedbackError(store.Refusal):
    pass


_KINDS = ('reviews', 'review_comments', 'review_threads')
_KIND_NAMES = dict(reviews='review', review_comments='review_comment', review_threads='review_thread')
_REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class GitHubReader:
    """Authenticated read-only ``gh api`` adapter using central gh credentials."""
    def __init__(self, repository, pull_request, run=store.run):
        if not _REPOSITORY.fullmatch(repository) or not isinstance(pull_request, int) or pull_request < 1:
            raise ValueError('invalid GitHub PR identity')
        self.owner, self.name = repository.split('/', 1)
        self.repository, self.pull_request, self.run = repository, pull_request, run

    def _gh(self, *args):
        result = self.run(['gh', 'api', *args], check=True)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReviewFeedbackError('GitHub returned invalid JSON') from error

    def _graphql(self, query, cursor=None):
        args = ['graphql', '-f', f'query={query}', '-F', f'owner={self.owner}', '-F', f'name={self.name}',
                '-F', f'number={self.pull_request}']
        if cursor is not None:
            args += ['-F', f'cursor={cursor}']
        return self._gh(*args)

    def __call__(self, kind, cursor=None):
        if kind == 'pull_request':
            data = self._graphql('query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){state headRefOid reviewDecision}}}')
            pull = data.get('data', {}).get('repository', {}).get('pullRequest')
            if not isinstance(pull, dict):
                raise ReviewFeedbackError('GitHub pull request is unavailable')
            return {'state': pull.get('state'), 'headSha': pull.get('headRefOid'), 'reviewDecision': pull.get('reviewDecision')}
        if kind == 'review_comments':
            page = int(cursor or '1')
            rows = self._gh(f'repos/{self.repository}/pulls/{self.pull_request}/comments?per_page=100&page={page}')
            if not isinstance(rows, list):
                raise ReviewFeedbackError('GitHub review comments are unavailable')
            return {'items': [{'id': str(row.get('id', '')), 'headSha': row.get('commit_id'),
                               'state': None, 'body': row.get('body')} for row in rows],
                    'next': str(page + 1) if len(rows) == 100 else None}
        field = 'reviews' if kind == 'reviews' else 'reviewThreads'
        selection = 'nodes{id state body commit{oid}}' if kind == 'reviews' else 'nodes{id isResolved isOutdated comments(first:100){nodes{body commit{oid}} pageInfo{hasNextPage}}}'
        data = self._graphql('query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){' + field + '(first:100,after:$cursor){' + selection + ' pageInfo{hasNextPage endCursor}}}}}', cursor)
        connection = data.get('data', {}).get('repository', {}).get('pullRequest', {}).get(field)
        if not isinstance(connection, dict) or not isinstance(connection.get('nodes'), list):
            raise ReviewFeedbackError(f'GitHub {kind} is unavailable')
        if kind == 'reviews':
            items = [{'id': row.get('id'), 'headSha': (row.get('commit') or {}).get('oid'),
                      'state': row.get('state'), 'body': row.get('body')} for row in connection['nodes']]
        else:
            items = []
            for row in connection['nodes']:
                comments_connection = row.get('comments') or {}
                comments = comments_connection.get('nodes')
                if comments_connection.get('pageInfo', {}).get('hasNextPage'):
                    raise ReviewFeedbackError('GitHub review thread pagination is unavailable')
                if not isinstance(comments, list) or not comments:
                    raise ReviewFeedbackError('GitHub review thread is unavailable')
                latest = comments[-1]
                items.append({'id': row.get('id'), 'headSha': (latest.get('commit') or {}).get('oid'),
                              'state': 'RESOLVED' if row.get('isResolved') else ('OUTDATED' if row.get('isOutdated') else 'OPEN'),
                              'body': latest.get('body')})
        page = connection.get('pageInfo')
        if not isinstance(page, dict):
            raise ReviewFeedbackError(f'GitHub {kind} pagination is unavailable')
        return {'items': items, 'next': page.get('endCursor') if page.get('hasNextPage') else None}


class LoopbackRelay:
    """Authenticated producer POST boundary. Token stays in central process memory."""
    def __init__(self, endpoint, producer_token):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or not producer_token):
            raise ValueError('Relay endpoint must be a bare loopback origin with a producer token')
        self.endpoint, self.producer_token = endpoint.rstrip('/') + '/v1/events', producer_token

    def __call__(self, payload):
        request = Request(self.endpoint, data=json.dumps(payload, separators=(',', ':')).encode(), method='POST',
                          headers={'Authorization': f'Bearer {self.producer_token}', 'Content-Type': 'application/json'})
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=15) as response:
                return response.status
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise ReviewFeedbackError('Relay producer redirected request')
            return error.code
        except (URLError, OSError) as error:
            raise ReviewFeedbackError('Relay producer is unavailable') from error


def _path(home, project_id, pull_request):
    store.validate_id(project_id)
    if not isinstance(pull_request, int) or pull_request < 1:
        raise ValueError('pull request must be positive')
    home = Path(home)
    if not home.is_absolute() or home.is_symlink():
        raise ValueError('home must be an absolute non-symlink directory')
    path = home / 'data' / 'relay' / 'review-feedback' / project_id / f'{pull_request}.json'
    current = home
    for part in path.relative_to(home).parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise ReviewFeedbackError('review feedback state path is unsafe')
    return path


def _default(repository, pull_request):
    return {'version': 1, 'repository': repository, 'pull_request': pull_request, 'healthy': True,
            'blocked_reason': None, 'head_sha': None, 'artifacts': {}, 'idempotency_keys': [],
            'cursor': {}, 'head_digest': None, 'review_decision': None, 'observed': False}


def _valid_digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value))


def _validate_state(state, repository, pull_request):
    if not isinstance(state, dict) or state.get('version') != 1 or state.get('repository') != repository or state.get('pull_request') != pull_request or not isinstance(state.get('healthy'), bool) or not isinstance(state.get('observed'), bool) or not isinstance(state.get('blocked_reason'), (str, type(None))) or not isinstance(state.get('head_sha'), (str, type(None))) or not isinstance(state.get('head_digest'), (str, type(None))) or not isinstance(state.get('review_decision'), (str, type(None))) or not isinstance(state.get('artifacts'), dict) or not isinstance(state.get('idempotency_keys'), list) or not isinstance(state.get('cursor'), dict):
        raise ReviewFeedbackError('review feedback state is malformed')
    if (state['head_sha'] is not None and not _string(state['head_sha'], 'stored head', 128)) or (state['head_digest'] is not None and not _valid_digest(state['head_digest'])) or any(not isinstance(key, str) or not _valid_digest(value) for key, value in state['artifacts'].items()) or len(state['idempotency_keys']) > 1000 or any(not isinstance(key, str) or not key for key in state['idempotency_keys']):
        raise ReviewFeedbackError('review feedback state is malformed')
    if state['observed']:
        cursor = state['cursor']
        sources = cursor.get('sources') if isinstance(cursor, dict) else None
        if cursor.get('version') != 1 or cursor.get('head_sha') != state['head_sha'] or not isinstance(sources, dict) or set(sources) != set(_KINDS):
            raise ReviewFeedbackError('review feedback state is malformed')
        for source in sources.values():
            if not isinstance(source, dict) or not _valid_digest(source.get('completed_snapshot_digest')) or not isinstance(source.get('high_water_id'), str):
                raise ReviewFeedbackError('review feedback state is malformed')


def _save(home, project_id, pull_request, state):
    _validate_state(state, state['repository'], pull_request)
    store.atomic_json(_path(home, project_id, pull_request), state)


def read_service(home, project_id, repository, pull_request):
    path = _path(home, project_id, pull_request)
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError('repository must be owner/repository')
    if not path.exists():
        return _default(repository, pull_request)
    if path.is_symlink() or not path.is_file():
        raise ReviewFeedbackError('review feedback state path is unsafe')
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewFeedbackError('review feedback state is unavailable') from error
    _validate_state(state, repository, pull_request)
    return state


def _json(value, what):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ReviewFeedbackError(f'GitHub {what} returned invalid JSON') from error
    if not isinstance(value, dict):
        raise ReviewFeedbackError(f'GitHub {what} returned invalid data')
    return value


def _string(value, name, maximum, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ReviewFeedbackError(f'GitHub {name} is unavailable')
    return value


def _metadata(github):
    metadata = _json(github('pull_request', None), 'pull request')
    if metadata.get('state') != 'OPEN':
        raise ReviewFeedbackError('GitHub pull request is not open')
    head = _string(metadata.get('headSha'), 'pull request head', 128)
    decision = metadata.get('reviewDecision')
    return head, _string(decision, 'review decision', 256) if decision is not None else None


def _read_pages(github, kind):
    cursor, seen, items = None, set(), []
    while True:
        page = _json(github(kind, cursor), kind)
        rows = page.get('items')
        if not isinstance(rows, list):
            raise ReviewFeedbackError(f'GitHub {kind} is unavailable')
        items.extend(rows)
        # ponytail: cap snapshots at 100; add durable chunked pagination when review throughput requires it.
        if len(items) > 100:
            raise ReviewFeedbackError('GitHub review evidence exceeds bounded limit')
        cursor = page.get('next')
        if cursor is None:
            return items, None
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ReviewFeedbackError(f'GitHub {kind} pagination is unavailable')
        seen.add(cursor)


def _artifact(row, kind, head):
    if not isinstance(row, dict):
        raise ReviewFeedbackError('GitHub review evidence is unavailable')
    if row.get('headSha') != head:
        if isinstance(row.get('headSha'), str):
            return None
        raise ReviewFeedbackError('GitHub review evidence lacks current-head identity')
    return {'id': _string(row.get('id'), 'review evidence ID', 512), 'kind': _KIND_NAMES[kind],
            'state': _string(row.get('state'), 'review evidence state', 256, nullable=True),
            'body': _string(row.get('body'), 'review evidence body', 2000, nullable=True)}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _status(response):
    return response if isinstance(response, int) else response.get('status') if isinstance(response, dict) else getattr(response, 'status', None)


def _mark_unhealthy(home, project_id, repository, pull_request, reason):
    with store.lock(Path(home)):
        state = read_service(home, project_id, repository, pull_request)
        state['healthy'], state['blocked_reason'] = False, reason[:500]
        _save(home, project_id, pull_request, state)


def watch_human_reviews(home, project_id, repository, pull_request, github, relay):
    """Emit changed evidence; any exception is an explicit task-blocking failure."""
    try:
        head, decision = _metadata(github)
        artifacts, sources = [], {}
        for kind in _KINDS:
            rows, _ = _read_pages(github, kind)
            sources[kind] = {'completed_snapshot_digest': _digest(rows),
                             'high_water_id': max((str(row.get('id', '')) for row in rows if isinstance(row, dict)), default='')}
            artifacts.extend(item for row in rows if (item := _artifact(row, kind, head)) is not None)
        end_head, end_decision = _metadata(github)
        if (end_head, end_decision) != (head, decision):
            raise ReviewFeedbackError('GitHub pull request head changed during review read')
        artifacts.sort(key=lambda item: (item['kind'], item['id']))
        if len({(item['kind'], item['id']) for item in artifacts}) != len(artifacts):
            raise ReviewFeedbackError('GitHub review evidence identity is ambiguous')
        with store.lock(Path(home)):
            state = read_service(home, project_id, repository, pull_request)
            prior = state['artifacts'] if state.get('head_sha') == head else {}
            changed = [item for item in artifacts if prior.get(f"{item['kind']}:{item['id']}") != _digest(item)]
            if (not state.get('observed') and decision is not None) or (state.get('observed') and state.get('review_decision') != decision):
                changed.append({'id': 'review-decision', 'kind': 'review_decision', 'state': decision, 'body': None})
            emitted, keys = 0, []
            for item in changed:
                artifact_digest = _digest(item)
                key = f'{repository}/{pull_request}/{head}/{item["kind"]}/{item["id"]}/{artifact_digest}'
                request = {'type': 'agd.pr.review', 'version': 1, 'payload': {'schemaVersion': 1, 'repository': repository, 'pullRequest': pull_request, 'headSha': head, 'reviewDecision': decision, 'artifacts': [item]}, 'correlationId': f'github:{repository}:pull-request:{pull_request}', 'idempotencyKey': key}
                if _status(relay(request)) not in (200, 201):
                    raise ReviewFeedbackError('Relay producer rejected review feedback')
                emitted += 1
                keys.append(key)
            state.update({'healthy': True, 'blocked_reason': None, 'head_sha': head, 'head_digest': _digest({'head': head, 'decision': decision, 'artifacts': artifacts}), 'review_decision': decision, 'observed': True, 'cursor': {'version': 1, 'head_sha': head, 'sources': sources}, 'artifacts': {f"{item['kind']}:{item['id']}": _digest(item) for item in artifacts}, 'idempotency_keys': (state.get('idempotency_keys', []) + keys)[-1000:]})
            _save(home, project_id, pull_request, state)
            return {'emitted': emitted, 'head_sha': head, 'healthy': True}
    except ReviewFeedbackError as error:
        _mark_unhealthy(home, project_id, repository, pull_request, str(error))
        raise
    except Exception as error:
        _mark_unhealthy(home, project_id, repository, pull_request, 'GitHub or Relay boundary is unavailable')
        raise ReviewFeedbackError('GitHub or Relay boundary is unavailable') from error
