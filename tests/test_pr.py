import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))

from agd_lib import pr, tasks
from support import lab
from test_projects import command, run


class FakeGitHub:
    def find_open_pr(self, repository, head, base):
        return []

    def create_pr(self, repository, head, base, title, body):
        return {'number': 7, 'url': 'https://example.test/owner/repo/pull/7',
                'repository': repository, 'headRefName': head, 'baseRefName': base}


def test_publish_validates_grant_and_writes_receipt_under_one_lock():
    with lab() as (home, _):
        brief = home / 'brief.md'; brief.write_text('Change value.txt.\n')
        task = command(home, 'task', 'create', 'publish-lock', '--project', 'sample', '--brief', str(brief))
        run(['git', 'commit', '--allow-empty', '-m', 'candidate'], cwd=task['worktree'])
        instruction = home / 'instruction.txt'
        instruction.write_text('Authorize publish-lock to publish origin:agd/publish-lock.\n')
        grant = tasks.grant_pr_publish(home, task['id'], 'origin:agd/publish-lock', instruction)

        receipt = pr.publish(home, task['id'], None, grant['id'], 'Title', 'Body', github_client=FakeGitHub())

        assert receipt['grant_id'] == grant['id']
        assert tasks.read_pr_receipt(home, task['id'])['pr']['number'] == 7
