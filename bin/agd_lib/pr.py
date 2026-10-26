import os
import re
import stat
from pathlib import Path


from . import store, tasks


def github_repository(remote_url):
    match = re.fullmatch(r'(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?', remote_url)
    if not match:
        raise store.Refusal('PR publication remote must be a canonical GitHub repository')
    return f'{match.group(1)}/{match.group(2)}'


def _check_current_pr(provider, repository, branch, base, head, number):
    candidate = provider.get_pr(repository, number)
    if (candidate.get('repository') != repository or candidate.get('state') != 'OPEN' or
            candidate.get('headRefName') != branch or candidate.get('baseRefName') != base or
            candidate.get('headRefOid') != head):
        raise store.Refusal('PR identity or current head does not match publication')
    return candidate


def reconcile(home, task_id, incarnation, grant_id, github_client=None):
    default_github = github_client is None
    if default_github:
        from . import github as github_client
    with store.lock(home):
        task = tasks.read(home, task_id)
        if (task.get('phase') != 'running' or task.get('executor') != 'worker' or
                not isinstance(task.get('incarnation'), str) or not task['incarnation'] or
                task['incarnation'] != incarnation or os.environ.get('AGD_TASK_ID') != task_id or
                os.environ.get('AGD_INCARNATION') != incarnation):
            raise store.Refusal('publication requires the current worker')
        grant = tasks._authorize_pr_publish_locked(home, task, grant_id)
        repository = github_repository(grant['remote_url']) if default_github else grant['remote_url']
        work = tasks.verify_workspace(home, task)
        head = store.git(work, 'rev-parse', 'HEAD')
        remote = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
        receipt = tasks.read_pr_receipt(home, task_id)
        if not remote:
            if receipt is None:
                return None
            raise store.Refusal('recorded PR remote head cannot be reconciled')
        if remote.split()[0] != head:
            raise store.Refusal('recorded PR remote head cannot be reconciled')
        remote_head = remote.split()[0]
        if receipt is not None:
            if (receipt.get('grant_id') != grant['id'] or receipt.get('remote') != grant['remote'] or
                    receipt.get('repository') != repository or receipt.get('branch') != task['branch'] or
                    receipt.get('base') != task['base'] or receipt.get('remote_head') != remote_head):
                raise store.Refusal('recorded PR does not match publication')
            try:
                current = _check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                            receipt['pr']['number'])
            except store.Refusal as error:
                raise store.Refusal('recorded PR cannot be reconciled') from error
            receipt.pop('verification', None)
            receipt.update(local_head=head, remote_head=remote_head, pr=current)
            tasks._write_pr_receipt_locked(home, task_id, receipt)
            return receipt
        candidates = github_client.find_open_pr(repository, task['branch'], task['base'])
        if len(candidates) > 1:
            raise store.Refusal('multiple matching open pull requests')
        if not candidates:
            return None
        current = _check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                    candidates[0]['number'])
        receipt = {'schema': 1, 'grant_id': grant['id'], 'remote': grant['remote'],
                   'repository': repository, 'branch': task['branch'], 'base': task['base'],
                   'local_head': head, 'remote_head': remote_head, 'pr': current}
        tasks._write_pr_receipt_locked(home, task_id, receipt)
        return receipt


def publish(home, task_id, incarnation, grant_id, title, body, run=store.run, github_client=None):
    default_github = github_client is None
    if default_github:
        from . import github as github_client
    with store.lock(home):
        task = tasks.read(home, task_id)
        if (task.get('phase') != 'running' or task.get('executor') != 'worker' or
                not isinstance(task.get('incarnation'), str) or not task['incarnation'] or
                task['incarnation'] != incarnation or os.environ.get('AGD_TASK_ID') != task_id or
                os.environ.get('AGD_INCARNATION') != incarnation):
            raise store.Refusal('publication requires the current worker')
        grant = tasks._authorize_pr_publish_locked(home, task, grant_id)
        repository = github_repository(grant['remote_url']) if default_github else grant['remote_url']
        work = tasks.verify_workspace(home, task)
        head = store.git(work, 'rev-parse', 'HEAD')
        remote_head = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
        if remote_head:
            remote_sha = remote_head.split()[0]
            run(['git', 'fetch', grant['remote'], task['branch']], cwd=work)
            if run(['git', 'merge-base', '--is-ancestor', remote_sha, head], cwd=work, check=False).returncode:
                raise store.Refusal('remote branch cannot fast-forward')
        run(['git', 'push', '--no-force', grant['remote'], f'HEAD:refs/heads/{task["branch"]}'], cwd=work)
        remote_head = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
        if not remote_head or remote_head.split()[0] != head:
            raise store.Refusal('remote branch head was not confirmed')
        observed_remote_head = remote_head.split()[0]
        receipt = tasks.read_pr_receipt(home, task_id)
        if receipt is not None:
            if (receipt.get('grant_id') != grant['id'] or receipt.get('remote') != grant['remote'] or
                    receipt.get('repository') != repository or receipt.get('branch') != task['branch'] or
                    receipt.get('base') != task['base']):
                raise store.Refusal('recorded PR does not match publication')
            try:
                pull_request = _check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                                  receipt['pr']['number'])
            except store.Refusal as error:
                raise store.Refusal('recorded PR cannot be reconciled') from error
            receipt.pop('verification', None)
            receipt.update(local_head=head, remote_head=observed_remote_head, pr=pull_request)
            tasks._write_pr_receipt_locked(home, task_id, receipt)
            return receipt
        matches = github_client.find_open_pr(repository, task['branch'], task['base'])
        if len(matches) > 1:
            raise store.Refusal('multiple matching open pull requests')
        pull_request = (_check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                           matches[0]['number']) if matches else
                        _check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                          github_client.create_pr(repository, task['branch'], task['base'], title, body)['number']))
        receipt = {'schema': 1, 'grant_id': grant['id'], 'remote': grant['remote'],
                   'repository': repository, 'branch': task['branch'], 'base': task['base'],
                   'local_head': head, 'remote_head': observed_remote_head, 'pr': pull_request}
        tasks._write_pr_receipt_locked(home, task_id, receipt)
        return receipt


def _verification_report(home, task_id, report_path):
    expected = tasks.task_file(home, task_id).parent/'report-draft.md'
    path = Path(report_path)
    if path != expected:
        raise store.Refusal('PR verification report must be the task report-draft.md')
    try:
        identity = os.lstat(path)
    except OSError as error:
        raise store.Refusal('PR verification report is unavailable') from error
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode) or not 0 < identity.st_size <= 20000:
        raise store.Refusal('PR verification report is unsafe or out of bounds')
    text = path.read_text()
    if not text.strip():
        raise store.Refusal('empty report')
    return text


def _block_verification_locked(home, task_id, receipt, reason):
    if receipt is not None:
        receipt.pop('verification', None)
        receipt['blocked_reason'] = f'PR verification blocked: {reason[:400]}'
        tasks._write_pr_receipt_locked(home, task_id, receipt)
    store.atomic_json(tasks.task_file(home, task_id).parent/'verification-blocked.json',
                      {'reason': reason[:400]})


def verify_and_ready(home, task_id, incarnation, grant_id, report_path, github_client=None, run=store.run):
    from . import github
    if github_client is None:
        github_client = github
    with store.lock(home):
        task = tasks.read(home, task_id)
        if (task.get('phase') != 'running' or task.get('executor') != 'worker' or
                task.get('incarnation') != incarnation or os.environ.get('AGD_TASK_ID') != task_id or
                os.environ.get('AGD_INCARNATION') != incarnation):
            raise store.Refusal('PR verification requires the current worker')
        try:
            text = _verification_report(home, task_id, report_path)
            grant = tasks._authorize_pr_publish_locked(home, task, grant_id)
            repository = github_repository(grant['remote_url']) if github_client is github else grant['remote_url']
            receipt = tasks.read_pr_receipt(home, task_id)
            if (receipt is None or receipt.get('grant_id') != grant['id'] or
                    receipt.get('remote') != grant['remote'] or receipt.get('repository') != repository or
                    receipt.get('branch') != task['branch'] or receipt.get('base') != task['base']):
                raise store.Refusal('PR verification requires the recorded publication receipt')
            work = tasks.verify_workspace(home, task)
            head = store.git(work, 'rev-parse', 'HEAD')
            remote = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
            if not remote or remote.split()[0] != head:
                raise store.Refusal('PR verification remote head does not match local head')
            current = _check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                        receipt['pr']['number'])
            policy = github_client.effective_policy(repository, task['base'], run=run)
            evidence = github.finalize(current, policy, head, repository, task['branch'], task['base'])
            remote = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
            current = _check_current_pr(github_client, repository, task['branch'], task['base'], head,
                                        receipt['pr']['number'])
            if not remote or remote.split()[0] != head:
                raise store.Refusal('PR verification changed before ready')
            github.finalize(current, policy, head, repository, task['branch'], task['base'])
            receipt.update(local_head=head, remote_head=head, pr=current,
                           verification={'grant_id': grant['id'], 'remote': grant['remote'],
                                         'repository': repository, 'branch': task['branch'], 'base': task['base'],
                                         'head': head, 'remote_head': head, 'pr_head': current['headRefOid'],
                                         'pr_number': current['number'], 'pr_id': current['id'],
                                         'final': evidence})
            tasks._write_pr_receipt_locked(home, task_id, receipt, allow_final=True)
            return tasks._ready_locked(home, task_id, text)
        except store.Refusal as error:
            _block_verification_locked(home, task_id, locals().get('receipt'), str(error))
            raise
