import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import github, store


def pr_payload(**overrides):
    payload = {
        'repository': 'owner/repo', 'state': 'OPEN', 'mergedAt': None, 'headRefName': 'agd/task',
        'headRefOid': 'abc', 'baseRefName': 'main', 'mergeStateStatus': 'CLEAN',
        'reviewDecision': None,
        'checks': {'ci': {'status': 'COMPLETED', 'conclusion': 'SUCCESS'}},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(('payload', 'policy', 'message'), [
    (pr_payload(state='CLOSED'), {'checks': (), 'approvals': 0}, 'open'),
    (pr_payload(headRefOid='other'), {'checks': (), 'approvals': 0}, 'head'),
    (pr_payload(checks={'ci': {'status': 'IN_PROGRESS', 'conclusion': None}}), {'checks': ('ci',), 'approvals': 0}, 'checks'),
    (pr_payload(mergeStateStatus='BEHIND'), {'checks': (), 'approvals': 0}, 'CLEAN'),
    (pr_payload(reviewDecision=None), {'checks': (), 'approvals': 1}, 'APPROVED'),
])
def test_finalize_rejects_nonfinal_or_missing_required_evidence(payload, policy, message):
    with pytest.raises(store.Refusal, match=message):
        github.finalize(payload, policy, expected_sha='abc', repository='owner/repo',
                        branch='agd/task', base='main')


def test_finalize_accepts_unprotected_current_head():
    result = github.finalize(pr_payload(), {'checks': (), 'approvals': 0}, expected_sha='abc',
                             repository='owner/repo', branch='agd/task', base='main')
    assert result['head_sha'] == 'abc'


@pytest.mark.parametrize(('checks', 'allowed'), [
    ({'ci': {'status': 'COMPLETED', 'conclusion': 'NEUTRAL'}}, True),
    ({'ci': {'state': 'SUCCESS'}}, True),
    ({'ci': {'status': 'COMPLETED', 'conclusion': 'SKIPPED'}}, True),
    ({'ci': {'state': 'PENDING'}}, False),
])
def test_finalize_normalizes_check_run_and_status_contexts(checks, allowed):
    call = lambda: github.finalize(pr_payload(checks=checks), {'checks': ('ci',), 'approvals': 0},
                                   expected_sha='abc', repository='owner/repo', branch='agd/task', base='main')
    if allowed:
        assert call()['head_sha'] == 'abc'
    else:
        with pytest.raises(store.Refusal, match='checks'):
            call()


def test_finalize_rejects_mismatched_pr_identity():
    with pytest.raises(store.Refusal, match='identity'):
        github.finalize(pr_payload(baseRefName='release'), {'checks': (), 'approvals': 0},
                        expected_sha='abc', repository='owner/repo', branch='agd/task', base='main')


def test_graphql_uses_typed_variables_for_pr_number():
    def gh(argv, **_):
        if '-F' not in argv:
            raise store.Refusal('number must be typed')
        return type('Result', (), {'stdout': '{"data":{"repository":{"pullRequest":null}}}'})()

    data = github.graphql('query', {'owner': 'owner', 'number': 7}, run=gh)
    assert data == {'repository': {'pullRequest': None}}


def test_get_pr_queries_repository_number_and_normalizes_contexts():
    def gh(argv, **_):
        return type('Result', (), {'stdout': '''{"data":{"repository":{"pullRequest":{
            "id":"P_1","number":7,"url":"https://example.test/owner/repo/pull/7",
            "state":"OPEN","mergedAt":null,"headRefName":"agd/task","headRefOid":"abc",
            "baseRefName":"main","mergeStateStatus":"CLEAN","reviewDecision":null,
            "commits":{"nodes":[{"commit":{"statusCheckRollup":{"contexts":{
                "pageInfo":{"hasNextPage":false},"nodes":[
                {"name":"ci","status":"COMPLETED","conclusion":"SUCCESS"},
                {"context":"legacy","state":"SUCCESS"}]}}}}]}}}}}'''})()

    pr = github.get_pr('owner/repo', 7, run=gh)

    assert pr['repository'] == 'owner/repo'
    assert pr['number'] == 7
    assert pr['checks']['ci']['conclusion'] == 'SUCCESS'
    assert pr['checks']['legacy']['state'] == 'SUCCESS'


def test_get_pr_refuses_truncated_check_contexts():
    def gh(argv, **_):
        return type('Result', (), {'stdout': '''{"data":{"repository":{"pullRequest":{
          "id":"P_1","number":7,"url":"https://example.test/pr/7","state":"OPEN","mergedAt":null,
          "headRefName":"agd/task","headRefOid":"abc","baseRefName":"main","mergeStateStatus":"CLEAN",
          "reviewDecision":null,"commits":{"nodes":[{"commit":{"statusCheckRollup":{"contexts":{
          "pageInfo":{"hasNextPage":true},"nodes":[]}}}}]}}}}}'''})()

    with pytest.raises(store.Refusal, match='check evidence'):
        github.get_pr('owner/repo', 7, run=gh)


def test_find_open_pr_returns_only_exact_head_and_base():
    def gh(argv, **_):
        def node(number, head):
            return {'id': f'P_{number}', 'number': number,
                    'url': f'https://example.test/owner/repo/pull/{number}', 'state': 'OPEN',
                    'mergedAt': None, 'headRefName': head, 'headRefOid': 'abc', 'baseRefName': 'main',
                    'headRepository': {'nameWithOwner': 'owner/repo'},
                    'mergeStateStatus': 'CLEAN', 'reviewDecision': None,
                    'commits': {'nodes': [{'commit': {'statusCheckRollup': {
                        'contexts': {'pageInfo': {'hasNextPage': False}, 'nodes': []}}}}]}}
        return type('Result', (), {'stdout': json.dumps(
            {'data': {'repository': {'pullRequests': {'nodes': [node(7, 'agd/task'), node(8, 'other')]}}}})})()

    prs = github.find_open_pr('owner/repo', 'agd/task', 'main', run=gh)

    assert [pr['number'] for pr in prs] == [7]


@pytest.mark.parametrize(('connection', 'message'), [
    ({'pageInfo': {'hasNextPage': True}, 'nodes': []}, 'incomplete'),
    ({'pageInfo': {'hasNextPage': False}, 'nodes': [{
        'id': 'P_7', 'number': 7, 'url': 'https://example.test/owner/repo/pull/7',
        'state': 'OPEN', 'headRefName': 'agd/task', 'headRefOid': 'abc', 'baseRefName': 'main',
        'headRepository': {'nameWithOwner': 'fork/repo'},
        'commits': {'nodes': [{'commit': {'statusCheckRollup': {
            'contexts': {'pageInfo': {'hasNextPage': False}, 'nodes': []}}}}]}}]}, 'repository'),
])
def test_find_open_pr_refuses_incomplete_or_cross_repository_connection(connection, message):
    def gh(argv, **_):
        return type('Result', (), {'stdout': json.dumps(
            {'data': {'repository': {'pullRequests': connection}}})})()

    with pytest.raises(store.Refusal, match=message):
        github.find_open_pr('owner/repo', 'agd/task', 'main', run=gh)


def test_normalize_policy_combines_classic_requirements_and_empty_rules():
    policy = github.normalize_policy(
        {'required_status_checks': {'contexts': ['ci']},
         'required_pull_request_reviews': {'required_approving_review_count': 2}},
        {'rules': []})

    assert policy == {'checks': ('ci',), 'approvals': 2}


def test_normalize_policy_refuses_unrecognized_effective_rules():
    with pytest.raises(store.Refusal, match='policy'):
        github.normalize_policy(None, {'rules': [{'type': 'unknown'}]})


def test_normalize_policy_unions_known_paginated_rules():
    policy = github.normalize_policy(
        {'required_status_checks': {'contexts': ['classic']},
         'required_pull_request_reviews': {'required_approving_review_count': 1}},
        [[{'type': 'required_status_checks', 'parameters': {'required_status_checks': [
                {'context': 'ci'}, {'context': 'lint', 'integration_id': 42}]}},
          {'type': 'pull_request', 'parameters': {'required_approving_review_count': 2}}],
         [{'type': 'pull_request', 'parameters': {'required_approving_review_count': 3}}]])
    assert policy == {'checks': ('ci', 'classic', 'lint'), 'approvals': 3}
