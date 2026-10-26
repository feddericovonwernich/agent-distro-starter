import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import github, store


def pr_payload(**overrides):
    payload = {
        'state': 'OPEN', 'mergedAt': None, 'headRefName': 'agd/task',
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
        github.finalize(payload, policy, expected_sha='abc')


def test_finalize_accepts_unprotected_current_head():
    result = github.finalize(pr_payload(), {'checks': (), 'approvals': 0}, expected_sha='abc')
    assert result['head_sha'] == 'abc'
