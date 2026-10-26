import json
import re

from . import store


PR_QUERY = '''
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id number url state mergedAt headRefName headRefOid baseRefName mergeStateStatus reviewDecision
      headRepository { nameWithOwner }
      commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          ... on CheckRun { name status conclusion }
          ... on StatusContext { context state }
        }
      } } } } } }
    }
  }
}
'''


SEARCH_QUERY = '''
query($owner: String!, $name: String!, $head: String!, $base: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, states: OPEN, headRefName: $head, baseRefName: $base) {
      pageInfo { hasNextPage }
      nodes {
        id number url state mergedAt headRefName headRefOid baseRefName mergeStateStatus reviewDecision
        headRepository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            ... on CheckRun { name status conclusion }
            ... on StatusContext { context state }
          }
        } } } } } }
      }
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
    if node.get('headRepository', {}).get('nameWithOwner') != repository:
        raise store.Refusal('GitHub PR head repository does not match task')
    try:
        rollup = node['commits']['nodes'][-1]['commit']['statusCheckRollup']
        if rollup['contexts']['pageInfo']['hasNextPage']:
            raise store.Refusal('GitHub PR check evidence is incomplete')
        contexts = rollup['contexts']['nodes']
    except store.Refusal:
        raise
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


def find_open_pr(repository, head, base, run=store.run):
    owner, name = split_repository(repository)
    data = graphql(SEARCH_QUERY, {'owner': owner, 'name': name, 'head': head, 'base': base}, run)
    search = data['repository'].get('pullRequests', {})
    if search.get('pageInfo', {}).get('hasNextPage'):
        raise store.Refusal('GitHub PR search is incomplete')
    nodes = search.get('nodes')
    if not isinstance(nodes, list):
        raise store.Refusal('GitHub PR search is incomplete')
    found = []
    for node in nodes:
        if not isinstance(node, dict) or node.get('state') != 'OPEN':
            continue
        if node.get('headRefName') != head or node.get('baseRefName') != base:
            continue
        if node.get('headRepository', {}).get('nameWithOwner') != repository:
            raise store.Refusal('GitHub PR head repository does not match task')
        try:
            rollup = node['commits']['nodes'][-1]['commit']['statusCheckRollup']
            if rollup['contexts']['pageInfo']['hasNextPage']:
                raise store.Refusal('GitHub PR check evidence is incomplete')
            contexts = rollup['contexts']['nodes']
        except store.Refusal:
            raise
        except (KeyError, IndexError, TypeError):
            raise store.Refusal('GitHub PR check evidence is incomplete')
        checks = {item.get('name', item.get('context')): item for item in contexts
                  if isinstance(item, dict) and item.get('name', item.get('context'))}
        found.append(dict(node, repository=repository, checks=checks))
    return found


def normalize_policy(classic, rules):
    if classic is not None and not isinstance(classic, dict):
        raise store.Refusal('GitHub policy evidence is unrecognized')
    if isinstance(rules, dict):
        pages = [rules.get('rules')]
    elif isinstance(rules, list):
        pages = rules
    else:
        pages = []
    if not pages or any(not isinstance(page, list) for page in pages):
        raise store.Refusal('GitHub policy evidence is incomplete')
    classic = classic or {}
    checks = set(classic.get('required_status_checks', {}).get('contexts', []))
    approvals = classic.get('required_pull_request_reviews', {}).get('required_approving_review_count', 0)
    if (not all(isinstance(check, str) and check for check in checks) or
            not isinstance(approvals, int) or approvals < 0):
        raise store.Refusal('GitHub policy evidence is unrecognized')
    for rule in (rule for page in pages for rule in page):
        if not isinstance(rule, dict) or not isinstance(rule.get('parameters'), dict):
            raise store.Refusal('GitHub policy evidence is unrecognized')
        parameters = rule['parameters']
        if rule.get('type') == 'required_status_checks':
            required = parameters.get('required_status_checks')
            if not isinstance(required, list):
                raise store.Refusal('GitHub policy evidence is unrecognized')
            for check in required:
                if not isinstance(check, dict) or not isinstance(check.get('context'), str) or not check['context']:
                    raise store.Refusal('GitHub policy evidence is unrecognized')
                checks.add(check['context'])
        elif rule.get('type') == 'pull_request':
            count = parameters.get('required_approving_review_count', 0)
            if not isinstance(count, int) or count < 0:
                raise store.Refusal('GitHub policy evidence is unrecognized')
            approvals = max(approvals, count)
        else:
            raise store.Refusal('GitHub policy evidence is unrecognized')
    return {'checks': tuple(sorted(checks)), 'approvals': approvals}



def _api_json(path, run, optional=False):
    result = run(['gh', 'api', '--method', 'GET', path], check=False)
    if result.returncode:
        if optional and '404' in (result.stderr or result.stdout):
            return None
        raise store.Refusal('GitHub policy request failed')
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise store.Refusal('GitHub policy response is invalid') from error



def _rules_pages(path, run):
    result = run(['gh', 'api', '--method', 'GET', '--paginate', '--slurp', f'{path}?per_page=100'],
                 check=False)
    if result.returncode:
        raise store.Refusal('GitHub policy request failed')
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise store.Refusal('GitHub policy response is invalid') from error
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise store.Refusal('GitHub policy response is incomplete')
    return pages



def effective_policy(repository, base, run=store.run):
    owner, name = split_repository(repository)
    path = f'repos/{owner}/{name}/branches/{base}'
    classic = _api_json(f'{path}/protection', run, optional=True)
    rules = _rules_pages(f'repos/{owner}/{name}/rules/branches/{base}', run)
    return normalize_policy(classic, rules)

def create_pr(repository, head, base, title, body, run=store.run):
    owner, name = split_repository(repository)
    if not all(isinstance(value, str) and value for value in (head, base, title, body)):
        raise store.Refusal('GitHub PR creation fields are invalid')
    result = run(['gh', 'pr', 'create', '--repo', f'{owner}/{name}', '--head', head, '--base', base,
                  '--title', title, '--body', body, '--no-maintainer-edit'])
    match = re.fullmatch(r'\s*https://github\.com/[^/\s]+/[^/\s]+/pull/([1-9][0-9]*)\s*', result.stdout)
    if not match:
        raise store.Refusal('GitHub PR creation response is ambiguous')
    pr = get_pr(repository, int(match.group(1)), run)
    if pr['headRefName'] != head or pr['baseRefName'] != base:
        raise store.Refusal('GitHub created PR identity does not match task')
    return pr


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
