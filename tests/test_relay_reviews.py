import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import relay_reviews, tasks
from support import command, lab


def _task_with_pr(home, task_id='reviewed'):
    brief = home / f'{task_id}.md'; brief.write_text('Change value.\n')
    command(home, 'task', 'create', task_id, '--project', 'sample', '--brief', str(brief), '--kind', 'ship')
    instruction = home / f'{task_id}-grant.md'; instruction.write_text(f'Task {task_id} targets origin:agd/{task_id}.\n')
    grant = tasks.grant_pr_publish(home, task_id, f'origin:agd/{task_id}', instruction)
    task = tasks.read(home, task_id)
    task.update(phase='running', executor='worker', incarnation='worker-1', pr_grant=grant['id']); tasks.save(home, task)
    head = __import__('agd_lib.store', fromlist=['git']).git(Path(task['worktree']), 'rev-parse', 'HEAD')
    tasks.write_pr_receipt(home, task_id, {'schema': 1, 'grant_id': grant['id'], 'remote': 'origin',
        'repository': 'owner/repo', 'branch': task['branch'], 'base': task['base'], 'local_head': head,
        'remote_head': head, 'pr': {'number': 7, 'url': 'https://example.test/pr/7', 'id': 'P7'}})
    root = relay_reviews.relay._service_root(home, 'sample'); (root / '.relay').mkdir(exist_ok=True)
    (root / 'service.json').write_text(json.dumps({'schema': 1, 'project': 'sample', 'repository': 'owner/repo',
        'root': str(root), 'runtime': '/runtime', 'health': 'healthy', 'server': {'pid': 42, 'start': 'server'},
        'connector': {'pid': 43, 'start': 'connector'}}))
    (root / '.relay' / 'runtime.json').write_text(json.dumps({'pid': 42, 'port': 4312, 'token': 'admin-secret'}))
    return task

def test_unpublished_or_dirty_pr_worker_is_not_blocked(monkeypatch):
    with lab() as (home, _project):
        task = _task_with_pr(home)
        tasks.pr_receipt_file(home, task['id']).unlink()
        Path(task['worktree'], 'repair.txt').write_text('pending repair\n')
        assert relay_reviews.tick(home) == {'checked': 0, 'emitted': 0, 'blocked': 0}
        assert not (tasks.task_file(home, task['id']).parent/'observations'/'human-review-blocked.json').exists()


def _github(head):
    return lambda *_: lambda kind, _cursor=None: {'state': 'OPEN', 'headSha': head}


def test_tick_wakes_parked_worker_with_exact_pr_correlation(monkeypatch):
    with lab() as (home, _project):
        task = _task_with_pr(home); head = tasks.read_pr_receipt(home, task['id'])['local_head']; seen = []
        monkeypatch.setattr(relay_reviews.supervision, 'gone', lambda *_: False)
        monkeypatch.setattr(relay_reviews.review_feedback, 'central_github_reader', _github(head))
        monkeypatch.setattr(relay_reviews, '_producer_token', lambda *_: 'producer-secret')
        monkeypatch.setattr(relay_reviews.review_feedback, 'LoopbackRelay', lambda origin, token: (origin, token))
        monkeypatch.setattr(relay_reviews.review_feedback, 'watch_human_reviews',
                            lambda home, project, repository, number, github, producer: seen.append((repository, number, producer)) or {'emitted': 1})
        assert relay_reviews.tick(home) == {'checked': 1, 'emitted': 1, 'blocked': 0}
        assert seen == [('owner/repo', 7, ('http://127.0.0.1:4312', 'producer-secret'))]


def test_task_failure_isolated_from_other_pr_and_shared_service(monkeypatch):
    with lab() as (home, _project):
        task = _task_with_pr(home, 'broken'); _task_with_pr(home, 'healthy')
        monkeypatch.setattr(relay_reviews.supervision, 'gone', lambda *_: False)
        monkeypatch.setattr(relay_reviews.review_feedback, 'central_github_reader', _github(tasks.read_pr_receipt(home, task['id'])['local_head']))
        monkeypatch.setattr(relay_reviews, '_producer_token', lambda *_: 'producer')
        monkeypatch.setattr(relay_reviews.review_feedback, 'LoopbackRelay', lambda *_: object())
        calls = []
        def watch(*_args):
            calls.append(True)
            if len(calls) == 1: raise relay_reviews.review_feedback.ReviewFeedbackError('producer refused')
            return {'emitted': 1}
        monkeypatch.setattr(relay_reviews.review_feedback, 'watch_human_reviews', watch)
        assert relay_reviews.tick(home) == {'checked': 2, 'emitted': 1, 'blocked': 1}
        assert tasks.read_pr_receipt(home, 'broken')['blocked_reason'] == 'human-review-producer: human review feedback is unavailable'
        assert 'blocked_reason' not in tasks.read_pr_receipt(home, 'healthy')
        assert json.loads(relay_reviews.relay._service_file(home, 'sample').read_text())['health'] == 'healthy'


def test_dead_connector_blocks_every_affected_pr_without_http(monkeypatch):
    with lab() as (home, _project):
        _task_with_pr(home, 'first'); _task_with_pr(home, 'second')
        monkeypatch.setattr(relay_reviews.supervision, 'gone', lambda pid, _start: pid == 43)
        called = []
        monkeypatch.setattr(relay_reviews.review_feedback, 'central_github_reader', lambda *_: called.append(True))
        assert relay_reviews.tick(home) == {'checked': 2, 'emitted': 0, 'blocked': 2}
        assert called == []
        assert tasks.read_pr_receipt(home, 'first')['blocked_reason'] == 'human-review-producer: Relay service is unavailable'
        assert tasks.read_pr_receipt(home, 'second')['blocked_reason'] == 'human-review-producer: Relay service is unavailable'


def test_malformed_service_and_stale_head_block_without_emitting(monkeypatch):
    with lab() as (home, _project):
        _task_with_pr(home); monkeypatch.setattr(relay_reviews.supervision, 'gone', lambda *_: False)
        root = relay_reviews.relay._service_root(home, 'sample'); service = json.loads((root / 'service.json').read_text())
        service['connector'] = []; (root / 'service.json').write_text(json.dumps(service))
        assert relay_reviews.tick(home) == {'checked': 1, 'emitted': 0, 'blocked': 1}
        assert tasks.read_pr_receipt(home, 'reviewed')['blocked_reason'] == 'human-review-producer: Relay service is unavailable'
        task = _task_with_pr(home, 'stale'); service['connector'] = {'pid': 43, 'start': 'connector'}; service['health'] = 'healthy'; (root / 'service.json').write_text(json.dumps(service))
        monkeypatch.setattr(relay_reviews.review_feedback, 'central_github_reader', lambda *_: lambda *_: {'state': 'OPEN', 'headSha': 'stale'})
        monkeypatch.setattr(relay_reviews, '_producer_token', lambda *_: 'producer')
        monkeypatch.setattr(relay_reviews.review_feedback, 'LoopbackRelay', lambda *_: object())
        called = []; monkeypatch.setattr(relay_reviews.review_feedback, 'watch_human_reviews', lambda *_: called.append(True))
        relay_reviews.tick(home)
        assert called == []
        assert tasks.read_pr_receipt(home, task['id'])['blocked_reason'] == 'human-review-producer: pull request head is superseded'


def test_producer_token_request_is_loopback_no_proxy_no_redirect_and_secret_free():
    class Response:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"token":"producer-secret"}'
    captured = []
    class Opener:
        def open(self, request, timeout): captured.append((request, timeout)); return Response()
    with patch('agd_lib.relay_reviews.build_opener', side_effect=lambda *handlers: captured.extend(handlers) or Opener()):
        assert relay_reviews._producer_token('http://127.0.0.1:4312', 'admin-secret', 'sample') == 'producer-secret'
    assert captured[-1][0].full_url == 'http://127.0.0.1:4312/v1/credentials'
    assert captured[-1][0].get_header('Authorization') == 'Bearer admin-secret'
    assert any(getattr(handler, 'proxies', None) == {} for handler in captured[:-1])
    with pytest.raises(relay_reviews.ReviewTickError) as error: relay_reviews._producer_token('http://127.0.0.1:4312', '', 'sample')
    assert 'secret' not in str(error.value)


def test_malformed_scoped_receipt_is_a_durable_task_block(monkeypatch):
    with lab() as (home, _project):
        task = _task_with_pr(home)
        monkeypatch.setattr(relay_reviews.supervision, 'gone', lambda *_: False)
        tasks.pr_receipt_file(home, task['id']).write_text('{}')

        assert relay_reviews.tick(home) == {'checked': 1, 'emitted': 0, 'blocked': 1}
        observation = tasks.task_file(home, task['id']).parent / 'observations' / 'human-review-blocked.json'
        assert json.loads(observation.read_text())['reason'] == 'human-review-producer: PR review receipt is unavailable'
