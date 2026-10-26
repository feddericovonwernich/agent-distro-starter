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
        prompt = (f'Read {brief_path} and complete task {task["id"]} only in {task["worktree"]}. '
                  'Use only guarded AGD worker publish-pr, verify-pr, relay-poll, and relay-reply commands '
                  'for the recorded PR grant. Do not merge, force-push, tag, change the base branch, or clean up. '
                  'Report blocked conditions through AGD.')
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
