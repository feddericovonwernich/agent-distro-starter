import json

from . import store


PR_QUERY = '''
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id number url state mergedAt headRefName headRefOid baseRefName mergeStateStatus reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
        ... on CheckRun { name status conclusion }
        ... on StatusContext { context state }
      } } } } } }
    }
  }
}
'''


def split_repository(repository):
    try:
        owner, name = repository.split('/', 1)
    except ValueError as error:
        raise store.Refusal('GitHub repository must be OWNER/REPOSITORY') from error
    if not owner or not name or '/' in name:
        raise store.Refusal('GitHub repository must be OWNER/REPOSITORY')
    return owner, name


def get_pr(repository, number, run=store.run):
    owner, name = split_repository(repository)
    if not isinstance(number, int) or number < 1:
        raise store.Refusal('GitHub PR number is invalid')
    node = graphql(PR_QUERY, {'owner': owner, 'name': name, 'number': number}, run)['repository'].get('pullRequest')
    if not isinstance(node, dict):
        raise store.Refusal('GitHub PR is missing')
    try:
        contexts = node['commits']['nodes'][-1]['commit']['statusCheckRollup']['contexts']['nodes']
    except (KeyError, IndexError, TypeError):
        raise store.Refusal('GitHub PR check evidence is incomplete')
    checks = {}
    for context in contexts:
        if not isinstance(context, dict):
            raise store.Refusal('GitHub PR check evidence is incomplete')
        key = context.get('name', context.get('context'))
        if not isinstance(key, str) or not key:
            raise store.Refusal('GitHub PR check evidence is incomplete')
        checks[key] = context
    return dict(node, repository=repository, checks=checks)


def graphql(query, variables, run=store.run):
    argv = ['gh', 'api', 'graphql', '--method', 'POST', '-f', f'query={query}']
    argv.extend(flag for key, value in variables.items() for flag in ('-F', f'{key}={value}'))
    try:
        result = run(argv)
    except store.DependencyError:
        raise
    except store.Refusal as error:
        raise store.Refusal('GitHub GraphQL request failed') from error
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise store.Refusal('GitHub returned invalid JSON') from error
    if payload.get('errors') or not isinstance(payload.get('data'), dict):
        raise store.Refusal('GitHub GraphQL response is incomplete')
    return payload['data']


def finalize(pr, policy, expected_sha, repository, branch, base):
    if pr.get('state') != 'OPEN' or pr.get('mergedAt') is not None:
        raise store.Refusal('PR is not open and unmerged')
    if (pr.get('repository') != repository or pr.get('headRefName') != branch or
            pr.get('baseRefName') != base):
        raise store.Refusal('PR identity does not match task')
    if pr.get('headRefOid') != expected_sha:
        raise store.Refusal('PR head does not match expected head')
    if pr.get('mergeStateStatus') != 'CLEAN':
        raise store.Refusal('PR merge state is not CLEAN')
    if not isinstance(policy, dict) or set(policy) != {'checks', 'approvals'}:
        raise store.Refusal('GitHub policy evidence is incomplete')
    required_checks, approvals = policy['checks'], policy['approvals']
    if not isinstance(required_checks, (list, tuple)) or not isinstance(approvals, int) or approvals < 0:
        raise store.Refusal('GitHub policy evidence is unrecognized')
    checks = pr.get('checks')
    if not isinstance(checks, dict):
        raise store.Refusal('PR check evidence is incomplete')
    for name in required_checks:
        check = checks.get(name)
        green_run = (isinstance(check, dict) and check.get('status') == 'COMPLETED' and
                     check.get('conclusion') in ('SUCCESS', 'NEUTRAL', 'SKIPPED'))
        green_status = isinstance(check, dict) and check.get('state') == 'SUCCESS'
        if not (green_run or green_status):
            raise store.Refusal('PR required checks are not green')
    if approvals and pr.get('reviewDecision') != 'APPROVED':
        raise store.Refusal('PR required reviews are not APPROVED')
    return {'head_sha': expected_sha, 'pr': pr}
