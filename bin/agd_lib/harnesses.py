import shutil
from pathlib import Path
from . import store

def worker_skill_roots(harness):
    if harness == 'omp':
        return ('.omp/skills',)
    if harness == 'cursor':
        raise store.DependencyError('Cursor native skill discovery is unverified; cannot provision skills')
    raise ValueError('unsupported harness')


def launch_argv(task, brief_path, unattended=False):
    code_root = Path(__file__).resolve().parents[2]
    if task.get('pr_grant'):
        agd = f'{code_root / "bin" / "agd"} --home {brief_path.parents[3]}'
        grant = task['pr_grant']
        reply = brief_path.parent/'state'/'tasks'/task['id']/'relay-reply.json'
        poll = f'{agd} worker relay-poll {task["id"]} --incarnation $AGD_INCARNATION --grant {grant}'
        reply_prefix = (f'{agd} worker relay-reply {task["id"]} --incarnation $AGD_INCARNATION '
                        f'--grant {grant} --lease LEASE')
        prompt = (f'Read {brief_path} and complete task {task["id"]} only in {task["worktree"]}. '
                  f'Your PR grant is {grant}. Use this AGD command prefix: {agd}. '
                  f'Publish only with: {agd} worker publish-pr {task["id"]} --incarnation $AGD_INCARNATION '
                  f'--grant {grant} --title {brief_path.parent / "pr-title.txt"} --body {brief_path.parent / "pr-body.md"}. '
                  f'Write the final report to {brief_path.parent / "report-draft.md"}, then verify only with: '
                  f'{agd} worker verify-pr {task["id"]} --incarnation $AGD_INCARNATION --grant {grant} '
                  f'--report {brief_path.parent / "report-draft.md"}. '
                  f'Poll only with: {poll}. On a delivered lease, that command first persists pending feedback. '
                  f'Immediately acknowledge receipt with: {reply_prefix} --action start. '
                  f'Before repair, write {reply} exactly as '
                  '\'{"result":{"summary":"feedback received; repair pending"},"effects":[]}\' and acknowledge with: '
                  f'{reply_prefix} --action complete --payload {reply}. '
                  f'For cancelled feedback, write {reply} exactly as \'{{"effects":[]}}\' and acknowledge with: '
                  f'{reply_prefix} --action cancelled --payload {reply}. '
                  'Only after the terminal acknowledgement independently inspect the feedback and current GitHub state, '
                  'make the smallest repair, commit, republish, and poll again. Pending feedback survives relaunch. '
                  'Do not merge, force-push, tag, change the base branch, configure or administer Relay, use a Relay admin token, '
                  'expose credentials, or clean up. Report blocked conditions through AGD.')
    else:
        prompt = (f'Read {brief_path} and complete task {task["id"]} only in {task["worktree"]}. '
                  f'Write a short report to {brief_path.parent / "report-draft.md"}, then run '
                  f'{code_root / "bin" / "agd"} --home {brief_path.parents[3]} task ready {task["id"]} '
                  f'--report {brief_path.parent / "report-draft.md"}. '
                  'Never publish, merge, or clean up.')
    manifest = task.get('worker_skills')
    if manifest and manifest.get('skills'):
        assigned = ', '.join(record['id'] for record in manifest['skills'])
        paths = ', '.join(str(Path(record['destination'])/'SKILL.md') for record in manifest['skills'])
        prompt += (f' Assigned worker skills: {assigned}. Read each assigned SKILL.md at {paths} '
                   'before substantive work. Use supporting resources relative to its skill directory. '
                   + ('' if task.get('pr_grant') else
                      'These skills do not grant permission to publish, merge, run unattended, or clean up.'))
    if task['harness'] == 'omp':
        binary = shutil.which('omp')
        if not binary:
            raise store.DependencyError('omp is not installed')
        args = [binary, '--cwd', task['worktree'], '-e', str(code_root/'lib'/'omp-worker.ts'), prompt]
        if unattended:
            args.insert(1, '--auto-approve')
        return args
    if task['harness'] == 'cursor':
        binary = shutil.which('cursor-agent') or shutil.which('agent')
        if not binary:
            raise store.DependencyError('Cursor Agent CLI is not installed')
        args = [binary, '--workspace', task['worktree'], '--trust', prompt]
        if unattended:
            args.insert(1, '--yolo')
        return args
    raise ValueError('unsupported harness')
