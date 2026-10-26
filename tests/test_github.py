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
