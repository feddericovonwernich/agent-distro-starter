#!/usr/bin/env python3
"""Opt-in disposable native uninstall. Never accepts an existing home target."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'bin'), str(ROOT / 'tests')]
from agd_lib import launcher, observability, omp_runtime, reviewer, reviewer_timer, store, supervision, tasks, uninstall
from test_uninstall import terminal


def run(argv, *, cwd=None, check=True, env=None):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd, check=False,
                            capture_output=True, text=True, timeout=60, env=env)
    if check and result.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} exited {result.returncode}: {result.stderr.strip()}')
    return result


def checkouts(base):
    source = base / 'source'
    source.mkdir()
    for name in ('bin', 'lib', 'bundled'):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in run(['git', 'ls-files', '-z', '--', '.omp'], cwd=ROOT).stdout.split('\0'):
        if name:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, path)
    (source / '.gitignore').write_text('config/\ndata/\nstate/\nprojects/\n**/__pycache__/\n')
    run(['git', 'init', '-b', 'main', source])
    run(['git', 'add', '.'], cwd=source)
    run(['git', '-c', 'user.name=Uninstall Fixture', '-c', 'user.email=fixture@example.invalid',
         'commit', '-m', 'disposable current source'], cwd=source)
    origin, backing = base / 'source-origin.git', base / 'source-backing'
    run(['git', 'clone', '--bare', source, origin])
    run(['git', 'clone', origin, backing])
    homes = [base / 'distro', base / 'sentinel']
    for home in homes:
        run(['git', 'worktree', 'add', '--detach', home, 'main'], cwd=backing)
        run([sys.executable, home / 'bin/agd', '--home', home, 'init', '--backend', 'herdr'])
    return backing, homes


def reviewer_service(home, directory, script):
    config = {'schedule': {'mode': 'interval', 'timezone': None, 'calendar': None, 'interval_hours': 12},
              'limits': dict(reviewer.DEFAULT_LIMITS)}
    units = reviewer_timer.render_units(home, config, runtime={'python': sys.executable, 'path': os.environ['PATH']},
                                         source_cli=script)
    directory.mkdir(parents=True, exist_ok=True)
    for kind in ('timer', 'service'):
        path = directory / units[kind + '_name']
        if os.path.lexists(path):
            raise RuntimeError('fixture unit name already exists')
        content = units[kind].replace('OnCalendar=*-*-* *:00:00 UTC', 'OnCalendar=2099-01-01 00:00:00 UTC')
        path.write_text(content); path.chmod(0o600)
    record = reviewer_timer._timer_record(home)
    record.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    store.atomic_json(record, {'schema': 1, 'home_id': reviewer_timer._home_id(home), 'uid': os.getuid(),
                               'state': 'enabled', 'schedule': config['schedule'], 'unit_directory': str(directory)})
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', units['timer_name']])
    run(['systemctl', '--user', 'start', '--no-block', units['service_name']])
    return units


def docker_fixture(home):
    rendered = observability.render_stack(home)
    images = observability._load_lock()['images'].values()
    image = next((image for image in images if run(['docker', 'image', 'inspect', image], check=False).returncode == 0), None)
    if image is None:
        raise RuntimeError('no already-cached pinned telemetry image; no image was pulled')
    labels = ['--label', 'agd.home_id=' + rendered['home_id'], '--label', 'agd.home_identity=' + rendered['home_identity'],
              '--label', 'com.docker.compose.project=' + rendered['project']]
    for suffix in ('_postgres', '_clickhouse', '_redis', '_minio'):
        run(['docker', 'volume', 'create', *labels, rendered['project'] + suffix])
    network = rendered['project'] + '_network'
    run(['docker', 'network', 'create', *labels, network])
    container = run(['docker', 'container', 'create', '--pull=never', '--network', network, *labels,
                     '--entrypoint', '/bin/true', image]).stdout.strip()
    return image, container


def central(home, binary):
    omp_runtime.initialize(home, str(binary))
    store.atomic_json(home / 'data/dependencies.json', {'schema': 1, 'omp': {'source': 'existing',
        'path': str(binary), 'version': run([binary, '--version']).stdout.strip(), 'capabilities': ['models-json', 'login']}})
    launcher.prepare_start(home, None, dict(os.environ), launcher._backend_create,
                           launcher._backend_inspect, launcher._backend_attach)
    return launcher._read_receipt(home)[0]


def wait_for(predicate, description, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError(description)
        time.sleep(.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('tmux', 'herdr'), required=True)
    parser.add_argument('--with-docker', action='store_true')
    args = parser.parse_args()
    if not shutil.which(args.backend):
        raise RuntimeError(f'native {args.backend} unavailable')
    real_home = Path.home()
    unit_directory = Path(os.environ.get('XDG_CONFIG_HOME', real_home / '.config')) / 'systemd/user'
    stamp_directory = Path(os.environ.get('XDG_DATA_HOME', real_home / '.local/share')) / 'systemd/timers'
    binary = real_home / '.local/bin/omp'
    if not binary.is_file():
        selected = shutil.which('omp')
        if not selected:
            raise RuntimeError('existing native OMP unavailable')
        binary = Path(selected).resolve()
    base = Path(tempfile.mkdtemp(prefix='au-'))
    original_environment = dict(os.environ)
    environment = {'PATH': str(binary.parent) + ':' + os.environ['PATH'], 'LANG': 'C.UTF-8', 'TERM': 'xterm-256color',
                   'DOCKER_CONFIG': os.environ.get('DOCKER_CONFIG', str(real_home / '.docker'))}
    environment['DBUS_SESSION_BUS_ADDRESS'] = os.environ.get(
        'DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}') + '/bus')
    for name, leaf in (('HOME', 'a'), ('XDG_CONFIG_HOME', 'c'), ('XDG_RUNTIME_DIR', 'r'),
                       ('XDG_DATA_HOME', 'd'), ('XDG_STATE_HOME', 's'), ('XDG_CACHE_HOME', 'k')):
        path = base / leaf; path.mkdir(mode=0o700); environment[name] = str(path)
    os.environ.clear(); os.environ.update(environment)
    homes, owned_units, captured = [], [], []
    try:
        backing, homes = checkouts(base)
        home, sentinel = homes
        for target in homes:
            store.atomic_json(target / 'data/launcher.json', {**json.loads((target / 'data/launcher.json').read_text()),
                                                            'backend': args.backend})
        seed = base / 'seed'; seed.mkdir()
        run(['git', 'init', '-b', 'main', seed]); (seed / 'value.txt').write_text('base\n')
        run(['git', 'add', '.'], cwd=seed)
        run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'initial'], cwd=seed)
        origin = base / 'project-origin.git'; run(['git', 'clone', '--bare', seed, origin])
        run([sys.executable, home / 'bin/agd', '--home', home, 'project', 'add', 'sample', '--source', origin])
        brief = home / 'data/brief.md'; brief.write_text('Native lifetime probe. Do not publish, merge or call any hosted model.\n')
        os.environ['TREEHOUSE_ROOT'] = str(home / 'data/treehouse')
        created = json.loads(run([sys.executable, home / 'bin/agd', '--home', home, 'task', 'create',
                                 'native-worker', '--project', 'sample', '--kind', 'scout', '--brief', brief,
                                 '--skill-mode', 'none']).stdout)
        os.environ.pop('TREEHOUSE_ROOT', None)
        service_script = base / 'reviewer-probe.py'
        service_script.write_text('''import fcntl, os, signal, sys
from pathlib import Path
home = Path(next(a.split("=",1)[1] for a in sys.argv if a.startswith("--home=")))
path = home / "state/reviewer/run.lock"; path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a+") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (home / "state/reviewer/probe-ready").write_text(str(os.getpid()))
    signal.pause()
''')
        for target in homes:
            owned_units.append((target, reviewer_service(target, unit_directory, service_script)))
        wait_for(lambda: all((target / 'state/reviewer/probe-ready').exists() for target in homes), 'reviewer leases not acquired')
        if args.with_docker:
            image, container = docker_fixture(home)
            foreign_volume = 'agd-uninstall-sentinel-' + json.loads((sentinel / 'data/home.json').read_text())['home_id']
            run(['docker', 'volume', 'create', '--label', 'agd.home_id=' + reviewer_timer._home_id(sentinel), foreign_volume])
        receipts = [central(target, binary) for target in homes]
        worker_result, worker_error = home / 'data/worker-launch.json', home / 'data/worker-launch.err'
        owner = json.loads((home / 'state/owner.json').read_text())['token']
        launch = [sys.executable, home / 'bin/agd', '--home', home, 'worker', 'launch', 'native-worker', '--backend', args.backend]
        if args.backend == 'herdr':
            script = base / 'launch-worker.sh'
            script.write_text('#!/bin/sh\nAGD_OWNER=' + shlex.quote(owner)
                              + ' AGD_HERDR_SESSION=' + shlex.quote(receipts[0]['endpoint']['session'])
                              + ' ' + shlex.join(map(str, launch))
                              + ' >' + shlex.quote(str(worker_result)) + ' 2>' + shlex.quote(str(worker_error)) + '\n')
            script.chmod(0o700)
            endpoint = receipts[0]['endpoint']
            launcher._herdr_call(endpoint, 'pane', 'run', endpoint['anchor']['pane_id'], '/bin/sh ' + shlex.quote(str(script)))
            wait_for(lambda: worker_result.exists() and bool(worker_result.stat().st_size),
                     'managed worker launch did not finish: ' + str(worker_error))
            launched = json.loads(worker_result.read_text())
        else:
            os.environ['AGD_OWNER'] = owner
            launched = json.loads(run(launch).stdout)
            os.environ.pop('AGD_OWNER', None)
        runner = home / 'state/tasks/native-worker/runner.json'
        wait_for(lambda: runner.exists() and bool(json.loads(runner.read_text()).get('child')), 'native worker child not published')
        child = json.loads(runner.read_text())['child']
        if not supervision.process_presence(child):
            raise RuntimeError('native worker exited before uninstall proof')
        preview = uninstall.inventory(home)
        captured = preview['resources']
        if preview['blockers']:
            raise RuntimeError('native ownership inventory blocked: ' + json.dumps(preview['blockers']))
        refs = run(['git', 'show-ref'], cwd=backing).stdout
        print('Native resources ready:', ', '.join(sorted({row['kind'] for row in captured})), flush=True)
        code, stdout, stderr = terminal([sys.executable, home / 'bin/agd', '--home', '.', 'uninstall'],
                                        cwd=home, home=home, env=dict(os.environ), timeout=180)
        print(stderr, end='', flush=True)
        if code or json.loads(stdout)['outcome'] != 'uninstalled':
            raise RuntimeError('native uninstall failed: ' + stdout)
        assert not home.exists()
        assert not supervision.process_presence(child)
        assert str(home) not in run(['git', 'worktree', 'list', '--porcelain'], cwd=backing).stdout
        assert run(['git', 'show-ref'], cwd=backing).stdout == refs
        assert supervision.status(sentinel)['watcherHealthy']
        assert (sentinel / 'bin/agd').is_file()
        for target, units in owned_units:
            if target == home:
                for kind in ('timer', 'service'):
                    assert not (unit_directory / units[kind + '_name']).exists()
                assert not (stamp_directory / ('stamp-' + units['timer_name'])).exists()
            else:
                assert (unit_directory / units['service_name']).is_file()
        if args.with_docker:
            expected = [row for row in captured if row['kind'] == 'docker']
            assert all(row['presence'] == 'absent' for row in observability.uninstall_inventory(home, expected=expected))
            run(['docker', 'volume', 'inspect', foreign_volume]); run(['docker', 'image', 'inspect', image])
            run(['docker', 'volume', 'rm', foreign_volume])
        print('First home, worker, reviewer, native backend, slots, source registration and optional Docker absent; sentinel survived.', flush=True)
        code, stdout, stderr = terminal([sys.executable, sentinel / 'bin/agd', '--home', '.', 'uninstall'],
                                        cwd=sentinel, home=sentinel, env=dict(os.environ), timeout=180)
        if code or json.loads(stdout)['outcome'] != 'uninstalled':
            raise RuntimeError('sentinel fixture cleanup failed: ' + stdout + stderr)
        run([sys.executable, '-m', 'pytest', str(ROOT / 'tests/test_uninstall.py') + '::test_crash_during_home_removal_resumes_without_original_cli',
             str(ROOT / 'tests/test_uninstall.py') + '::test_crash_after_home_unlink_repeats_delayed_verification',
             '--basetemp', base / 'crash-proof', '-q'], env=original_environment)
        shutil.rmtree(base)
        print('Native full uninstall and partial/post-unlink recovery passed. No hosted inference or image pull was authorized.')
    except BaseException:
        # Stop only witnessed processes/units. Never widen Treehouse or remove retained data on failure.
        for target in homes:
            if not (target / 'data/home.json').exists():
                continue
            timer = reviewer_timer.unit_name(target)
            units = {'timer_name': timer, 'service_name': timer[:-6] + '.service'}
            marker = f'X-AGD-Reviewer-Owner={reviewer_timer._home_id(target)}:{os.getuid()}' if (target / 'data/home.json').exists() else None
            path = unit_directory / units['service_name']
            if marker and reviewer_timer._owned(path, marker):
                run(['systemctl', '--user', 'disable', '--now', units['timer_name']], check=False)
                run(['systemctl', '--user', 'stop', units['service_name']], check=False)
        for target in homes:
            try:
                from agd_lib import workers
                for row in workers.uninstall_inventory(target):
                    if row['kind'] == 'worker' and row['ownership'] == 'owned':
                        supervision.stop_owned_processes([process for process in
                            (row['identity']['runner'], row['identity']['child']) if process])
                rows = launcher.uninstall_inventory(target)
                for row in rows:
                    if row['ownership'] == 'owned':
                        for process in row['identity'].get('processes', []):
                            supervision.stop_owned_processes([process])
                        server = row['identity'].get('server')
                        if server:
                            supervision.stop_owned_processes([server['process']])
            except (OSError, store.Refusal, store.DependencyError, ValueError, KeyError) as error:
                print('Fixture cleanup blocked:', target, str(error), file=sys.stderr)
        print('Scoped evidence retained:', base, file=sys.stderr, flush=True)
        raise
    finally:
        os.environ.clear(); os.environ.update(original_environment)


if __name__ == '__main__':
    main()
