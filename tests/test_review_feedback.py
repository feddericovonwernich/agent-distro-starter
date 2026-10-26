import json
import sys
from pathlib import Path

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
            return {'headSha': self.head, 'reviewDecision': self.decision}
        pages = self.pages[kind]
        index = int(cursor or 0)
        return {'items': pages[index], 'next': str(index + 1) if index + 1 < len(pages) else None}


class Relay:
    def __init__(self, status=201):
        self.status = status
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.status


def artifact(identifier, kind, head, state=None, body=None):
    return {'id': identifier, 'headSha': head, 'state': state, 'body': body}


def test_current_head_changes_emit_once_with_exact_correlation(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['review_comments'] = [[artifact('comment-1', 'review_comment', github.head, body='fix this')]]
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'CHANGES_REQUESTED')]]
    github.pages['review_threads'] = [[artifact('thread-1', 'review_thread', github.head, 'OUTDATED')]]

    first = review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert first['emitted'] == 3
    assert {r['correlationId'] for r in relay.requests} == {'github:owner/repo:pull-request:7'}
    assert {r['idempotencyKey'] for r in relay.requests} == {
        f'owner/repo/7/{github.head}/comment-1', f'owner/repo/7/{github.head}/review-1',
        f'owner/repo/7/{github.head}/thread-1'}

    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'DISMISSED')]]
    second = review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert second['emitted'] == 1
    assert relay.requests[-1]['payload']['artifacts'][0]['state'] == 'DISMISSED'


def test_restart_deduplicates_and_new_head_replays_current_evidence(tmp_path):
    github, relay = GitHub(), Relay()
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)
    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 0

    github.head = 'b' * 40
    github.pages['reviews'] = [[artifact('review-1', 'review', github.head, 'APPROVED')]]
    assert review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, github, relay)['emitted'] == 1
    assert relay.requests[-1]['idempotencyKey'] == f'owner/repo/7/{github.head}/review-1'


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
            return json.dumps({'headSha': github.head, 'reviewDecision': None})
        if kind == 'reviews':
            return json.dumps({'items': github.pages['reviews'][0], 'next': '0'})
        return json.dumps({'items': [], 'next': None})
    with pytest.raises(review_feedback.ReviewFeedbackError, match='pagination'):
        review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7, repeated_cursor, relay)

    with pytest.raises(review_feedback.ReviewFeedbackError, match='JSON'):
        review_feedback.watch_human_reviews(tmp_path, 'sample', 'owner/repo', 7,
                                            lambda *_: '{bad', relay)
