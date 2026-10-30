import shutil
from pathlib import Path
from . import store

def worker_skill_roots(harness):
    if harness == 'omp':
        return ('.omp/skills',)
    if harness == 'cursor':
        raise store.DependencyError('Cursor native skill discovery is unverified; cannot provision skills')
    raise ValueError('unsupported harness')


def worker_skill_args(task):
    skills = (task.get('worker_skills') or {}).get('skills', ())
    if not skills:
        return ['--no-skills']
    return ['--skills', ','.join(sorted(record['id'] for record in skills))]


def launch_argv(task, brief_path, unattended=False, session_dir=None):
    code_root = Path(__file__).resolve().parents[2]
    if task['harness'] == 'omp':
        binary = shutil.which('omp')
        if not binary:
            raise store.DependencyError('omp is not installed')
        args = [binary, '--no-extensions', *worker_skill_args(task),
                '--cwd', task['worktree']]
        if session_dir is not None:
            args.extend(['--session-dir', str(session_dir)])
        args.extend(['-e', str(code_root/'lib'/'omp-worker.ts')])
        if task.get('phase') != 'ready':
            args.append('Read the assigned task brief and complete the assignment in its workspace.')
        if unattended:
            args.insert(1, '--auto-approve')
        return args
    if task['harness'] == 'cursor':
        prompt = (f'Read {brief_path} and complete task {task["id"]} only in {task["worktree"]}. '
                  f'Write a short report to {brief_path.parent / "report-draft.md"}, then run '
                  f'{code_root / "bin" / "agd"} --home {brief_path.parents[3]} task ready {task["id"]} '
                  f'--report {brief_path.parent / "report-draft.md"}. '
                  'Never publish, merge, or clean up.')
        binary = shutil.which('cursor-agent') or shutil.which('agent')
        if not binary:
            raise store.DependencyError('Cursor Agent CLI is not installed')
        args = [binary, '--workspace', task['worktree'], '--trust', prompt]
        if unattended:
            args.insert(1, '--yolo')
        return args
    raise ValueError('unsupported harness')
