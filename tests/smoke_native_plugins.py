#!/usr/bin/env python3
"""Opt-in native plugin, task snapshot, backend, and cleanup smoke."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
from agd_lib import omp_runtime, tasks, worker_skills  # noqa: E402

SKILLS = {
    'ponytail', 'ponytail-audit', 'ponytail-debt', 'ponytail-gain',
    'ponytail-help', 'ponytail-review', 'systematic-debugging',
    'test-driven-development', 'verification-before-completion',
}


def run(argv, *, cwd=None, env=None):
    result = subprocess.run([str(value) for value in argv], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f'{argv!r}: {result.stderr or result.stdout}')
    return result.stdout


def agd(home, *arguments):
    return json.loads(run([ROOT/'bin/agd', '--home', home, *arguments]))


def digest_tree(root):
    if not root.is_dir() or root.is_symlink():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git(argv, cwd=None):
    return run(['git', *argv], cwd=cwd)


def wait_for_output(module, endpoint, needle):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            output = module.capture(endpoint)
        except Exception:
            output = ''
        if needle in output:
            return output
        time.sleep(.1)
    raise RuntimeError(f'backend output never contained {needle!r}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', required=True, choices=('tmux', 'herdr'))
    args = parser.parse_args()
    omp = shutil.which('omp')
    treehouse = shutil.which('treehouse')
    if not omp or not treehouse:
        raise SystemExit('omp and treehouse are required on PATH')
    if args.backend == 'tmux' and not shutil.which('tmux'):
        raise SystemExit('tmux is required for --backend tmux')
    if args.backend == 'herdr' and not shutil.which('herdr'):
        raise SystemExit('herdr is required for --backend herdr')
    if args.backend == 'herdr' and os.environ.get('HERDR_ENV') != '1':
        raise SystemExit(
            '--backend herdr must run inside an owned Herdr-managed pane')

    account_omp = Path.home()/'.omp'
    account_before = digest_tree(account_omp)
    backend = __import__('agd_lib.' + args.backend, fromlist=['create'])
    endpoint = None
    evidence = {}
    directory = Path(tempfile.mkdtemp(prefix='agd-native-plugin-smoke-'))
    try:
        root = Path(directory)
        home, seed, origin = root/'home', root/'seed', root/'origin.git'
        hostile = {
            **os.environ, 'OMP_PROFILE': 'foreign', 'PI_PROFILE': 'foreign',
            'PI_CONFIG_DIR': 'foreign', 'PI_CODING_AGENT_DIR': str(root/'foreign-agent'),
            'XDG_DATA_HOME': str(root/'foreign-data'),
        }
        os.environ.update({key: hostile[key] for key in (
            'OMP_PROFILE', 'PI_PROFILE', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR',
            'XDG_DATA_HOME')})
        agd(home, 'init', '--backend', args.backend)
        omp_runtime.initialize(home, omp, inherited=hostile)
        plugins = omp_runtime.install_plugins(home, omp)

        git(['init', '-b', 'main', seed])
        git(['config', 'user.name', 'Native Smoke'], cwd=seed)
        git(['config', 'user.email', 'smoke@example.invalid'], cwd=seed)
        (seed/'value.txt').write_text('base\n')
        git(['add', 'value.txt'], cwd=seed)
        git(['commit', '-m', 'initial'], cwd=seed)
        git(['clone', '--bare', seed, origin])
        agd(home, 'project', 'add', 'sample', '--source', str(origin))
        brief = root/'brief.md'; brief.write_text('Inspect value.txt.\n')
        task = agd(home, 'task', 'create', 'native-smoke', '--project', 'sample',
                   '--brief', str(brief), '--kind', 'scout')
        worktree = Path(task['worktree'])
        copied = {path.name for path in (worktree/'.omp/skills').iterdir()}
        if copied != SKILLS or any((worktree/'.omp/skills'/('agd-' + name)).exists()
                                   for name in SKILLS):
            raise RuntimeError(f'unexpected task skills: {sorted(copied)}')

        managed = omp_runtime.environment(home, hostile)
        child_env = {key: managed[key] for key in (
            'HOME', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR', 'OMP_PROFILE',
            'PI_PROFILE', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')}
        child_env.update(PATH=os.environ['PATH'], TERM=os.environ.get('TERM', 'xterm'))
        command = ['/usr/bin/env', '-i', *(f'{key}={value}' for key, value in child_env.items()),
                   omp, 'read', 'skill://systematic-debugging/root-cause-tracing.md']
        endpoint = backend.create(
            home, 'native-smoke', 'read-skill', worktree,
            ['/bin/sh', '-c', '"$@"; status=$?; sleep 30; exit $status',
             'agd-native-smoke', *command])
        try:
            wait_for_output(backend, endpoint, 'Root Cause Tracing')
        finally:
            try:
                backend.close(endpoint)
            finally:
                endpoint = None
                if args.backend == 'herdr':
                    workspace_path = home/'state/herdr-workspace.json'
                    if workspace_path.is_file():
                        workspace = json.loads(workspace_path.read_text())
                        backend.call(
                            workspace, 'workspace', 'close',
                            workspace['workspace'])
        supporting = run([omp, 'read',
                          'skill://test-driven-development/writing-good-tests.md'],
                         cwd=worktree, env=child_env)
        if 'test' not in supporting.casefold():
            raise RuntimeError('native supporting skill resource was not readable')

        source = Path(next(row['source'] for row in worker_skills.catalog(home)
                           if row['id'] == 'systematic-debugging'))
        shutil.rmtree(source)
        destination = worktree/'.omp/skills/systematic-debugging'
        shutil.rmtree(destination)
        worker_skills.ensure(home, tasks.read(home, 'native-smoke'))
        if not (destination/'SKILL.md').is_file():
            raise RuntimeError('completed snapshot did not restore the missing copy')
        rejected = subprocess.run(
            [str(ROOT/'bin/agd'), '--home', str(home), 'task', 'create', 'source-gone',
             '--project', 'sample', '--brief', str(brief), '--kind', 'scout'],
            text=True, capture_output=True, timeout=120)
        if rejected.returncode != 3 or tasks.task_file(home, 'source-gone').exists():
            raise RuntimeError('new task accepted a removed native plugin source')

        report = root/'report.md'; report.write_text('Native skills inspected.\n')
        agd(home, 'task', 'ready', 'native-smoke', '--report', str(report))
        cleaned = agd(home, 'task', 'cleanup', 'native-smoke')
        if cleaned['phase'] != 'cleaned':
            raise RuntimeError('task cleanup did not return the lease')
        evidence = {
            'backend': args.backend,
            'plugins': {key: value['version'] for key, value in plugins.items()},
            'skills': sorted(copied),
            'snapshot_restored_after_source_removal': True,
            'task_phase': cleaned['phase'],
        }
    finally:
        cleanup_failure = None
        if endpoint is not None:
            try:
                backend.close(endpoint)
            except Exception as error:
                cleanup_failure = error
        if args.backend == 'herdr':
            workspace_path = directory/'home/state/herdr-workspace.json'
            if workspace_path.is_file():
                try:
                    workspace = json.loads(workspace_path.read_text())
                    backend.call(
                        workspace, 'workspace', 'close',
                        workspace['workspace'])
                except Exception:
                    pass
        task_path = directory/'home/data/tasks/native-smoke/task.json'
        if task_path.is_file():
            try:
                worktree = Path(json.loads(task_path.read_text())['worktree'])
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                worktree = None
            if worktree is not None and worktree.exists():
                destroyed = subprocess.run(
                    [treehouse, 'destroy', str(worktree), '--include-leased',
                     '--include-unlanded', '--include-in-use', '--yes'],
                    text=True, capture_output=True, timeout=120)
                if destroyed.returncode and cleanup_failure is None:
                    cleanup_failure = RuntimeError(
                        destroyed.stderr or destroyed.stdout)
        shutil.rmtree(directory, ignore_errors=True)
        if cleanup_failure is not None and sys.exc_info()[0] is None:
            raise cleanup_failure
    if digest_tree(account_omp) != account_before:
        raise RuntimeError('account-global OMP state changed')
    time.sleep(8)
    evidence['account_global_omp_unchanged'] = True
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
