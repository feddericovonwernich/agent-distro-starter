import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

from agd_lib import pr, tasks
from support import lab
from test_projects import command, run


class FakeGitHub:
    def __init__(self, prs=()):
        self.created = []
        self.prs = {pr['number']: pr for pr in prs}

    def find_open_pr(self, repository, head, base):
        return [pr for pr in self.prs.values() if pr.get('state') == 'OPEN']

    def get_pr(self, repository, number):
        return self.prs[number]

    def create_pr(self, repository, head, base, title, body):
        self.created.append((repository, head, base))
        pr = {'number': 7, 'url': 'https://example.test/owner/repo/pull/7',
              'repository': repository, 'headRefName': head, 'headRefOid': 'abc',
              'baseRefName': base, 'state': 'OPEN'}
        self.prs[7] = pr
        return pr


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
        provider = FakeGitHub()

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
        tasks.write_pr_receipt(home, task['id'], {'schema': 1, 'grant_id': grant['id'], 'remote': 'origin',
            'repository': grant['remote_url'], 'branch': task['branch'], 'base': task['base'],
            'local_head': head, 'remote_head': head, 'pr': {'number': 9}})
        provider = FakeGitHub([{'number': 9, 'repository': grant['remote_url'], 'headRefName': task['branch'],
            'headRefOid': head, 'baseRefName': task['base'], 'state': 'CLOSED'}])

        with pytest.raises(tasks.store.Refusal, match='recorded PR'):
            pr.reconcile(home, task['id'], 'worker-inc', grant['id'], github_client=provider)

        assert not provider.created
