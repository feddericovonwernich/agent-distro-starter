from . import store, tasks


def publish(home, task_id, incarnation, grant_id, title, body, run=store.run, github_client=None):
    if github_client is None:
        from . import github as github_client
    with store.lock(home):
        task = tasks.read(home, task_id)
        if task.get('incarnation') != incarnation:
            raise store.Refusal('obsolete runner')
        grant = tasks._authorize_pr_publish_locked(home, task, grant_id)
        work = tasks.verify_workspace(home, task)
        head = store.git(work, 'rev-parse', 'HEAD')
        remote_head = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
        if remote_head:
            remote_sha = remote_head.split()[0]
            run(['git', 'fetch', grant['remote'], task['branch']], cwd=work)
            if run(['git', 'merge-base', '--is-ancestor', remote_sha, head], cwd=work, check=False).returncode:
                raise store.Refusal('remote branch cannot fast-forward')
        run(['git', 'push', grant['remote'], f'HEAD:refs/heads/{task["branch"]}'], cwd=work)
        remote_head = store.git(work, 'ls-remote', '--heads', grant['remote'], f'refs/heads/{task["branch"]}')
        if not remote_head or remote_head.split()[0] != head:
            raise store.Refusal('remote branch head was not confirmed')
        repository = grant['remote_url']
        matches = github_client.find_open_pr(repository, task['branch'], task['base'])
        if len(matches) > 1:
            raise store.Refusal('multiple matching open pull requests')
        pull_request = matches[0] if matches else github_client.create_pr(repository, task['branch'], task['base'], title, body)
        receipt = {'schema': 1, 'grant_id': grant['id'], 'remote': grant['remote'],
                   'repository': repository, 'branch': task['branch'], 'base': task['base'],
                   'local_head': head, 'remote_head': head, 'pr': pull_request}
        tasks._write_pr_receipt_locked(home, task_id, receipt)
        return receipt
