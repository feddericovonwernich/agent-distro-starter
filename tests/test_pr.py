import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

from agd_lib import pr, tasks
from support import lab
from test_projects import command, run


class FakeGitHub:
    def __init__(self, prs=(), created_head=None):
        self.created = []
        self.created_head = created_head
        self.prs = {pr['number']: pr for pr in prs}

    def find_open_pr(self, repository, head, base):
        return [pr for pr in self.prs.values() if pr.get('state') == 'OPEN']

    def get_pr(self, repository, number):
        return self.prs[number]

    def create_pr(self, repository, head, base, title, body):
        self.created.append((repository, head, base))
        pr = {'id': 'P_7', 'number': 7, 'url': 'https://example.test/owner/repo/pull/7',
              'repository': repository, 'headRefName': head, 'headRefOid': self.created_head,
              'baseRefName': base, 'state': 'OPEN'}
        self.prs[7] = pr
        return pr


def test_verify_and_ready_requires_current_final_github_evidence(monkeypatch):
    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'verify-ready', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home/'instruction.txt'
        instruction.write_text('Authorize verify-ready to publish origin:agd/verify-ready.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/verify-ready', instruction)
        task.update(phase='running', executor='worker', incarnation='worker-inc')
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', 'worker-inc')
        head = run(['git', 'rev-parse', 'HEAD'], cwd=task['worktree']).stdout.strip()
        provider = FakeGitHub(created_head=head)
        pr.publish(home, task['id'], 'worker-inc', grant['id'], 'Title', 'Body', github_client=provider)
        provider.prs[7].update(mergedAt=None, mergeStateStatus='CLEAN', checks={}, reviewDecision=None)
        provider.effective_policy = lambda repository, base, run=None: {'checks': (), 'approvals': 0}
        report = home/'report.md'; report.write_text('Verified.\n')

        ready = pr.verify_and_ready(home, task['id'], 'worker-inc', grant['id'], report,
                                    github_client=provider)

        assert ready['phase'] == 'ready'
        receipt = tasks.read_pr_receipt(home, task['id'])
        assert receipt['verification']['head'] == head



def test_verify_and_ready_rejects_remote_change_before_ready(monkeypatch):
    with lab() as (home, _):
        brief = home/'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'remote-race', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home/'instruction.txt'
        instruction.write_text('Authorize remote-race to publish origin:agd/remote-race.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/remote-race', instruction)
        task.update(phase='running', executor='worker', incarnation='worker-inc')
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', 'worker-inc')
        head = run(['git', 'rev-parse', 'HEAD'], cwd=task['worktree']).stdout.strip()
        provider = FakeGitHub(created_head=head)
        pr.publish(home, task['id'], 'worker-inc', grant['id'], 'Title', 'Body', github_client=provider)
        provider.prs[7].update(mergedAt=None, mergeStateStatus='CLEAN', checks={}, reviewDecision=None)

        def change_remote(repository, base, run=None):
            other = home/'other'
            run_process = globals()['run']
            remote = run_process(['git', 'remote', 'get-url', 'origin'], cwd=task['worktree']).stdout.strip()
            run_process(['git', 'clone', remote, other])
            run_process(['git', 'checkout', task['branch']], cwd=other)
            run_process(['git', 'config', 'user.name', 'Distro Test'], cwd=other)
            run_process(['git', 'config', 'user.email', 'test@example.invalid'], cwd=other)
            run_process(['git', 'commit', '--allow-empty', '-m', 'race'], cwd=other)
            run_process(['git', 'push', 'origin', task['branch']], cwd=other)
            return {'checks': (), 'approvals': 0}

        provider.effective_policy = change_remote
        report = home/'report.md'; report.write_text('Verified.\n')
        with pytest.raises(tasks.store.Refusal, match='changed before ready'):
            pr.verify_and_ready(home, task['id'], 'worker-inc', grant['id'], report,
                                github_client=provider)
        assert tasks.read(home, task['id'])['phase'] == 'running'

def test_publish_validates_grant_and_writes_receipt_under_one_lock(monkeypatch):
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'publish-lock', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home / 'instruction.txt'
        instruction.write_text('Authorize publish-lock to publish origin:agd/publish-lock.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/publish-lock', instruction)
        task.update(phase='running', executor='worker', incarnation='worker-inc')
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', 'worker-inc')
        provider = FakeGitHub(created_head=run(['git', 'rev-parse', 'HEAD'], cwd=task['worktree']).stdout.strip())

        receipt = pr.publish(home, task['id'], 'worker-inc', grant['id'], 'Title', 'Body',
                             github_client=provider)

        assert receipt['grant_id'] == grant['id']
        assert tasks.read_pr_receipt(home, task['id'])['pr']['number'] == 7
        assert provider.created


def test_prepared_task_cannot_publish_or_touch_remote():
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'prepared-publish', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home / 'instruction.txt'
        instruction.write_text('Authorize prepared-publish to publish origin:agd/prepared-publish.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/prepared-publish', instruction)
        provider = FakeGitHub()

        with pytest.raises(tasks.store.Refusal, match='current worker'):
            pr.publish(home, task['id'], None, grant['id'], 'Title', 'Body', github_client=provider)

        assert not run(['git', 'ls-remote', '--heads', 'origin', 'refs/heads/agd/prepared-publish'],
                       cwd=task['worktree']).stdout.strip()
        assert not provider.created


def test_reconcile_refuses_closed_recorded_pr_without_creating(monkeypatch):
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'closed-pr', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home / 'instruction.txt'
        instruction.write_text('Authorize closed-pr to publish origin:agd/closed-pr.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/closed-pr', instruction)
        task.update(phase='running', executor='worker', incarnation='worker-inc')
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', 'worker-inc')
        head = run(['git', 'rev-parse', 'HEAD'], cwd=task['worktree']).stdout.strip()
        run(['git', 'push', 'origin', f'HEAD:refs/heads/{task["branch"]}'], cwd=task['worktree'])
        tasks.write_pr_receipt(home, task['id'], {'schema': 1, 'grant_id': grant['id'], 'remote': 'origin',
            'repository': grant['remote_url'], 'branch': task['branch'], 'base': task['base'],
            'local_head': head, 'remote_head': head,
            'pr': {'id': 'P_9', 'number': 9, 'url': 'https://example.test/owner/repo/pull/9'}})
        provider = FakeGitHub([{'id': 'P_9', 'number': 9, 'url': 'https://example.test/owner/repo/pull/9',
            'repository': grant['remote_url'], 'headRefName': task['branch'],
            'headRefOid': head, 'baseRefName': task['base'], 'state': 'CLOSED'}])
        with pytest.raises(tasks.store.Refusal, match='recorded PR'):
            pr.reconcile(home, task['id'], 'worker-inc', grant['id'], github_client=provider)

        assert not provider.created



def test_publish_refuses_closed_recorded_pr_before_creating(monkeypatch):
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'closed-publish', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home / 'instruction.txt'
        instruction.write_text('Authorize closed-publish to publish origin:agd/closed-publish.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/closed-publish', instruction)
        task.update(phase='running', executor='worker', incarnation='worker-inc')
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', 'worker-inc')
        head = run(['git', 'rev-parse', 'HEAD'], cwd=task['worktree']).stdout.strip()
        tasks.write_pr_receipt(home, task['id'], {'schema': 1, 'grant_id': grant['id'], 'remote': 'origin',
            'repository': grant['remote_url'], 'branch': task['branch'], 'base': task['base'],
            'local_head': head, 'remote_head': head,
            'pr': {'id': 'P_9', 'number': 9, 'url': 'https://example.test/owner/repo/pull/9'}})
        provider = FakeGitHub([{'id': 'P_9', 'number': 9, 'url': 'https://example.test/owner/repo/pull/9',
            'repository': grant['remote_url'], 'headRefName': task['branch'], 'headRefOid': head,
            'baseRefName': task['base'], 'state': 'CLOSED'}], created_head=head)

        with pytest.raises(tasks.store.Refusal, match='recorded PR'):
            pr.publish(home, task['id'], 'worker-inc', grant['id'], 'Title', 'Body', github_client=provider)

        assert not provider.created


def test_reconcile_reports_clean_unpublished_state_without_remote_branch(monkeypatch):
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'unpublished-pr', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home / 'instruction.txt'
        instruction.write_text('Authorize unpublished-pr to publish origin:agd/unpublished-pr.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/unpublished-pr', instruction)
        task.update(phase='running', executor='worker', incarnation='worker-inc')
        tasks.save(home, task)
        monkeypatch.setenv('AGD_TASK_ID', task['id'])
        monkeypatch.setenv('AGD_INCARNATION', 'worker-inc')

        assert pr.reconcile(home, task['id'], 'worker-inc', grant['id'], github_client=FakeGitHub()) is None
        assert tasks.read_pr_receipt(home, task['id']) is None
