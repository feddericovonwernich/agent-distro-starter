"""Read-only GitHub human-review producer contract for the central service."""
import hashlib
import json
import re
from pathlib import Path

from . import store


class ReviewFeedbackError(store.Refusal):
    pass


_KINDS = ('reviews', 'review_comments', 'review_threads')
_KIND_NAMES = dict(reviews='review', review_comments='review_comment', review_threads='review_thread')
_REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')


def _path(home, project_id, pull_request):
    store.validate_id(project_id)
    if not isinstance(pull_request, int) or pull_request < 1:
        raise ValueError('pull request must be positive')
    return Path(home) / 'data' / 'relay' / 'review-feedback' / project_id / f'{pull_request}.json'


def _default(repository, pull_request):
    return {'repository': repository, 'pull_request': pull_request, 'healthy': True,
            'artifacts': {}, 'idempotency_keys': [], 'cursor': {}, 'head_digest': None,
            'review_decision': None, 'observed': False}


def read_service(home, project_id, repository, pull_request):
    path = _path(home, project_id, pull_request)
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError('repository must be owner/repository')
    if not path.exists():
        return _default(repository, pull_request)
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewFeedbackError('review feedback state is unavailable') from error
    if state.get('repository') != repository or state.get('pull_request') != pull_request:
        raise ReviewFeedbackError('review feedback state identity changed')
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


def _read_pages(github, kind):
    cursor, seen, items = None, set(), []
    while True:
        page = _json(github(kind, cursor), kind)
        rows = page.get('items')
        if not isinstance(rows, list):
            raise ReviewFeedbackError(f'GitHub {kind} is unavailable')
        items.extend(rows)
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
    identifier = _string(row.get('id'), 'review evidence ID', 512)
    state = _string(row.get('state'), 'review evidence state', 256, nullable=True)
    body = _string(row.get('body'), 'review evidence body', 2000, nullable=True)
    return {'id': identifier, 'kind': _KIND_NAMES[kind], 'state': state, 'body': body}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _status(response):
    if isinstance(response, int):
        return response
    if isinstance(response, dict):
        return response.get('status')
    return getattr(response, 'status', None)


def _mark_unhealthy(home, project_id, repository, pull_request, reason):
    with store.lock(Path(home)):
        state = read_service(home, project_id, repository, pull_request)
        state['healthy'] = False
        state['blocked_reason'] = reason[:500]
        store.atomic_json(_path(home, project_id, pull_request), state)


def watch_human_reviews(home, project_id, repository, pull_request, github, relay):
    """Read and emit current-head review evidence.

    ``github(kind, cursor)`` is the sole GitHub boundary.  It returns a mapping
    (or JSON object) for ``pull_request`` and the three paginated evidence kinds.
    ``relay(request)`` is the sole loopback producer boundary and returns 201/200.
    A caller must block the scoped task when this function raises.
    """
    try:
        metadata = _json(github('pull_request', None), 'pull request')
        head = _string(metadata.get('headSha'), 'pull request head', 128)
        decision = metadata.get('reviewDecision')
        if decision is not None:
            decision = _string(decision, 'review decision', 256)
        artifacts, cursor = [], {}
        for kind in _KINDS:
            rows, cursor[kind] = _read_pages(github, kind)
            for row in rows:
                item = _artifact(row, kind, head)
                if item is not None:
                    artifacts.append(item)
        artifacts.sort(key=lambda item: (item['kind'], item['id']))
        if len({(item['kind'], item['id']) for item in artifacts}) != len(artifacts):
            raise ReviewFeedbackError('GitHub review evidence identity is ambiguous')

        with store.lock(Path(home)):
            state = read_service(home, project_id, repository, pull_request)
            same_head = state.get('head_digest') is not None and state.get('head_sha') == head
            prior = state['artifacts'] if same_head else {}
            changed = [item for item in artifacts if prior.get(f"{item['kind']}:{item['id']}") != _digest(item)]
            if state.get('observed') and state.get('review_decision') != decision:
                changed.append({'id': 'review-decision', 'kind': 'review_decision',
                                'state': decision, 'body': None})
            elif not state.get('observed') and decision is not None:
                changed.append({'id': 'review-decision', 'kind': 'review_decision',
                                'state': decision, 'body': None})

            emitted = 0
            for item in changed:
                key = f'{repository}/{pull_request}/{head}/{item["id"]}'
                request = {'type': 'agd.pr.review', 'version': 1,
                           'payload': {'schemaVersion': 1, 'repository': repository,
                                       'pullRequest': pull_request, 'headSha': head,
                                       'reviewDecision': decision, 'artifacts': [item]},
                           'correlationId': f'github:{repository}:pull-request:{pull_request}',
                           'idempotencyKey': key}
                if _status(relay(request)) not in (200, 201):
                    raise ReviewFeedbackError('Relay producer rejected review feedback')
                emitted += 1
            state.update({'healthy': True, 'blocked_reason': None, 'head_sha': head,
                          'head_digest': _digest({'head': head, 'decision': decision, 'artifacts': artifacts}),
                          'review_decision': decision, 'observed': True, 'cursor': cursor,
                          'artifacts': {f"{item['kind']}:{item['id']}": _digest(item) for item in artifacts},
                          'idempotency_keys': (state.get('idempotency_keys', []) +
                                               [f'{repository}/{pull_request}/{head}/{item["id"]}' for item in changed])[-1000:]})
            store.atomic_json(_path(home, project_id, pull_request), state)
            return {'emitted': emitted, 'head_sha': head, 'healthy': True}
    except ReviewFeedbackError as error:
        _mark_unhealthy(home, project_id, repository, pull_request, str(error))
        raise
    except Exception as error:
        _mark_unhealthy(home, project_id, repository, pull_request, 'GitHub or Relay boundary is unavailable')
        raise ReviewFeedbackError('GitHub or Relay boundary is unavailable') from error
