import json
import sys
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import review_feedback


class GitHub:
    def __init__(self, head='a' * 40):
        self.head = head
        self.decision = None
        self.pages = {kind: [[]] for kind in ('reviews', 'review_comments', 'review_threads')}

    def __call__(self, kind, cursor=None):
        if kind == 'pull_request':
            return {'headSha': self.head, 'reviewDecision': self.decision, 'state': 'OPEN'}
        pages = self.pages[kind]
        index = int(cursor or 0)
        return {'items': pages[index], 'next': str(index + 1) if index + 1 < len(pages) else None}


class Relay:
    def __init__(self, status=201):
        self.status = status
        self.requests = []
        self.keys = set()

    def __call__(self, request):
        self.requests.append(request)
        if self.status != 201:
            return self.status
        if request['idempotencyKey'] in self.keys:
            return 200
        self.keys.add(request['idempotencyKey'])
        return 201


def artifact(identifier, kind, head, state=None, body=None):
    return {'id': identifier, 'headSha': head, 'state': state, 'body': body}


def _lock_probe(home, result):
    from agd_lib import store
    with store.lock(Path(home)):
        result.put(True)


def test_central_reader_requires_dedicated_token_and_never_uses_ambient_auth(monkeypatch):
    with pytest.raises(review_feedback.ReviewFeedbackError, match='AGD_RELAY_GITHUB_READ_TOKEN'):
        review_feedback.central_github_reader('owner/repo', 7, {})
    seen = {}
    def run(argv, **kwargs):
        seen.update(kwargs['env'])
        return SimpleNamespace(returncode=0, stdout='{"data": {"repository": {"pullRequest": {"state": "OPEN", "headRefOid": "a", "reviewDecision": null}}}}', stderr='')
    monkeypatch.setattr(review_feedback.subprocess, 'run', run)
    reader = review_feedback.central_github_reader('owner/repo', 7, {'AGD_RELAY_GITHUB_READ_TOKEN': 'read-only'})
    assert reader('pull_request')['state'] == 'OPEN'
    assert seen == {'PATH': __import__('os').environ.get('PATH', ''), 'GH_TOKEN': 'read-only'}


def test_current_head_changes_emit_distinct_relay_events_with_exact_correlation(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['review_comments'] = [[artifact('comment-1', 'review_comment', github.head, body='fix this')]]
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'CHANGES_REQUESTED')]]
    github.pages['review_threads'] = [[artifact('thread-1', 'review_thread', github.head, 'OUTDATED')]]

    first = review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert first['emitted'] == 3
    assert {r['correlationId'] for r in relay.requests} == {'github:owner/repo:pull-request:7'}
    assert all(r['idempotencyKey'].startswith(f'owner/repo/7/{github.head}/') for r in relay.requests)
    assert len(relay.keys) == 3

    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'DISMISSED')]]
    second = review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert second['emitted'] == 1
    assert relay.requests[-1]['payload']['artifacts'][0]['state'] == 'DISMISSED'
    assert len(relay.keys) == 4
    assert relay.requests[-1]['idempotencyKey'] != relay.requests[1]['idempotencyKey']


def test_mixed_case_repository_uses_worker_correlation(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    review_feedback.watch_human_reviews(tmp_path, 'sample', 'Owner/Repo', 7, github, relay)
    assert relay.requests[0]['correlationId'] == 'github:owner/repo:pull-request:7'


def test_restart_deduplicates_and_new_head_replays_current_evidence(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 0

    github.head = 'b' * 40
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 1
    assert relay.requests[-1]['idempotencyKey'].startswith(f'owner/repo/7/{github.head}/review/review-1/')


def test_head_changing_during_read_fails_closed(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    calls = 0
    def changing_head(kind, cursor=None):
        nonlocal calls
        response = github(kind, cursor)
        if kind == 'pull_request':
            calls += 1
            if calls == 2:
                github.head = 'b' * 40
        return github(kind, cursor)
    with pytest.raises(review_feedback.ReviewFeedbackError, match='head changed'):
        review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, changing_head, relay)


def test_producer_failure_marks_service_unhealthy_and_does_not_advance_cursor(tmp_path):
    github, relay = GitHub(), Relay(status=500)
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    with pytest.raises(review_feedback.ReviewFeedbackError, match='Relay producer'):
        review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    state = review_feedback.read_service(tmp_path, 'sample', 'owner/repo', 7)
    assert state['healthy'] is False
    assert state['artifacts'] == {}


def test_invalid_json_or_pagination_fails_closed(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head)], [artifact('review-2', 'review', github.head)]]
    def repeated_cursor(kind, cursor=None):
        if kind == 'pull_request':
            return json.dumps({'headSha': github.head, 'reviewDecision': None, 'state': 'OPEN'})
        if kind == 'reviews':
            return json.dumps({'items': github.pages['reviews'][0], 'next': '0'})
        return json.dumps({'items': [], 'next': None})
    with pytest.raises(review_feedback.ReviewFeedbackError, match='pagination'):
        review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, repeated_cursor, relay)

    with pytest.raises(review_feedback.ReviewFeedbackError, match='JSON'):
        review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7,
                                            lambda *_: '{bad', relay)


def test_authenticated_gh_and_loopback_boundaries_use_central_credentials_only():
    commands = []
    responses = [
        {'data': {'repository': {'pullRequest': {'state': 'OPEN', 'headRefOid': 'a' * 40, 'reviewDecision': None}}}},
        [{'id': 3, 'commit_id': 'a' * 40, 'body': 'review text'}],
    ]
    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(stdout=json.dumps(responses.pop(0)))
    github = review_feedback.GitHubReader('owner/repo', 7, run=run)
    assert github('pull_request')['state'] == 'OPEN'
    assert github('review_comments')['items'][0]['id'] == '3'
    assert commands[0][:3] == ['gh', 'api', 'graphql']
    assert commands[1][2] == 'repos/owner/repo/pulls/7/comments?per_page=100&page=1'

    seen = []
    class Response:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *_): return False
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            return Response()
    with patch('agd_lib.review_feedback.build_opener', return_value=Opener()):
        assert review_feedback.LoopbackRelay('http://127.0.0.1:4312', 'secret')({'type': 'agd.pr.review'}) == 201
    assert seen[0].full_url == 'http://127.0.0.1:4312/v1/events'
    assert seen[0].get_header('Authorization') == 'Bearer secret'


def test_malformed_or_symlinked_state_fails_closed(tmp_path):
    state = tmp_path / 'data' / 'relay' / 'review-feedback' / 'sample' / '7.json'
    state.parent.mkdir(parents=True)
    state.write_text('{not json')
    with pytest.raises(review_feedback.ReviewFeedbackError, match='state is unavailable'):
        review_feedback.read_service(tmp_path, 'sample', 'owner/repo', 7)
    state.unlink()
    target = tmp_path / 'state.json'
    target.write_text('{}')
    state.symlink_to(target)
    with pytest.raises(review_feedback.ReviewFeedbackError, match='path is unsafe'):
        review_feedback.read_service(tmp_path, 'sample', 'owner/repo', 7)


def test_snapshot_cursor_is_durable_per_source_and_head(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    cursor = review_feedback.read_service(tmp_path, 'sample', 'owner/repo', 7)['cursor']
    assert cursor['version'] == 1
    assert cursor['head_sha'] == github.head
    assert cursor['sources']['reviews']['completed_snapshot_digest']
    assert cursor['sources']['reviews']['high_water_id'] == 'review-1'


def test_loopback_rejects_redirect_without_following_or_exposing_token():
    from urllib.error import HTTPError
    from unittest.mock import Mock
    opener = Mock()
    opener.open.side_effect = HTTPError('http://attacker.invalid/', 302, 'Found', {}, None)
    with patch('agd_lib.review_feedback.build_opener', return_value=opener, create=True) as factory:
        with pytest.raises(review_feedback.ReviewFeedbackError) as error:
            review_feedback.LoopbackRelay('http://127.0.0.1:4312/', 'secret')({'type': 'agd.pr.review'})
    assert factory.called
    assert opener.open.call_count == 1
    assert 'secret' not in str(error.value)


def test_restart_detects_an_edited_old_artifact(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['review_comments'] = [[artifact('comment-1', 'review_comment', github.head, body='first')]]
    review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 0
    github.pages['review_comments'] = [[artifact('comment-1', 'review_comment', github.head, body='edited')]]
    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 1
    assert len(relay.keys) == 2


def test_loopback_disables_environment_proxy(monkeypatch):
    from urllib.request import ProxyHandler
    monkeypatch.setenv('HTTP_PROXY', 'http://attacker.invalid:8080')
    captured = []
    class Response:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *_): return False
    class Opener:
        def open(self, *_args, **_kwargs): return Response()
    with patch('agd_lib.review_feedback.build_opener', side_effect=lambda *handlers: captured.extend(handlers) or Opener()):
        assert review_feedback.LoopbackRelay('http://127.0.0.1:4312', 'secret')({'type': 'agd.pr.review'}) == 201
    assert any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in captured)


def test_relay_post_does_not_hold_home_lock(tmp_path):
    github = GitHub()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]

    def relay(_request):
        result = multiprocessing.Queue()
        process = multiprocessing.Process(target=_lock_probe, args=(str(tmp_path), result))
        process.start()
        process.join(.5)
        acquired = not process.is_alive() and result.get_nowait() is True
        if process.is_alive():
            process.terminate()
            process.join()
        return 201 if acquired else 500

    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 1
