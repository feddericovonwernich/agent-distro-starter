import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from support import command, lab
from test_projects import run
from test_reviewer import append_unfinished, completed_session, message, rewrite_title
from test_langfuse_cli import _package_files

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import langfuse_cli, observability, reviewer, store, supervision, tasks, worker_skills, workers


def inspection():
    # Import lazily so the owner's red run can report each missing behavior.
    from agd_lib import reviewer_inspection
    return reviewer_inspection


def stage(home, *, cutoff=200, limit=None):
    config = reviewer.read_config(home)
    if limit is not None:
        config['limits']['max_input_bytes'] = limit
    manifest = reviewer.select_manifest(home, cutoff=cutoff, config_snapshot=config)
    now = time.time()
    reservation = reviewer.reserve_run(
        home, reviewer.enqueue_intent(home, kind='manual'), config_snapshot=config,
        instruction_snapshot=reviewer.INSTRUCTION_FILE.read_bytes(), started_at=now,
        deadline_epoch_ms=int((now + 1800) * 1000))
    assert manifest['instruction_sha256'] == reservation['instruction_snapshot_ref']
    assert manifest['config_snapshot_sha256'] == reservation['config_snapshot_ref']
    # Task 4 will produce this binding. Reserve the real receipt and snapshots now,
    # then stage the schema-2 dispatch transition without using legacy packet APIs.
    receipt = reviewer.paths(home)['receipts'] / (reservation['review_run_id'] + '.json')
    reservation.update(manifest_id=manifest['manifest_id'], report_schema=2,
                       model='fixture/model', omp_version='fixture', dispatch_state='begun')
    store.atomic_json(receipt, reservation)
    return reservation['review_run_id'], manifest


def project_files(project):
    (project / 'AGENTS.md').write_text('Observe validation before reporting.\n')
    (project / 'check.sh').write_text('#!/bin/sh\nprintf checked\\n\n')
    (project / 'secret.env').write_text('API_KEY=do-not-expose\n')
    (project / 'link').symlink_to('value.txt')
    run(['git', 'add', 'AGENTS.md', 'check.sh', 'secret.env', 'link'], cwd=project)
    run(['git', 'commit', '-m', 'target fixtures'], cwd=project)


def capture(home, run_id, text):
    root = reviewer.paths(home)['runtime'] / run_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / ('artifact-' + hashlib.sha256(text.encode()).hexdigest() + '.txt')
    path.write_text(text)
    return reviewer.capture_evidence(home, run_id, path, 'observed behavior')['source_id']


def target(home, run_id, **kwargs):
    return inspection().inspect_target(home, run_id, surface='tooling', **kwargs)


def record(home, run_id, result):
    return inspection().read_target(home, run_id, result['target_ref'])


def bounded(value):
    assert len(json.dumps(value, indent=2, ensure_ascii=False).encode()) <= 16384


def telemetry_fixture(home, monkeypatch, episode, mode='normal'):
    """Install synthetic, asset-verified CLI bytes; queries stay in a local child."""
    package = home / 'synthetic-cli'
    package.mkdir(mode=0o700)
    files = _package_files()
    files['bin/langfuse'] = '''#!/usr/bin/env python3
import base64, datetime, json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ['--version']:
    print('1.2.4')
elif args == ['api', 'schema', '--json']:
    print('{}')
else:
    Path(os.environ['INSPECTION_CHILD_PID']).write_text(str(os.getpid()))
    mode = os.environ['INSPECTION_MODE']
    if mode == 'barrier':
        while not Path(os.environ['INSPECTION_RELEASE']).exists():
            time.sleep(0.02)
        mode = 'normal'
    if mode == 'timeout':
        time.sleep(60)
    elif mode == 'overflow':
        os.write(1, b'x' * 300000)
        time.sleep(60)
    elif mode == 'failure':
        sys.stderr.write('token=synthetic-credential\\n')
        sys.exit(2)
    elif mode == 'partial':
        sys.stdout.write('{"status":200,"body":{"data":[')
    else:
        if args[:5] != ['--api-version', 'auto', 'api', 'observations', 'list']:
            sys.exit(11)
        options = {}
        items = iter(args[5:])
        for flag in items:
            if flag == '--json':
                options['json'] = True
                continue
            key, separator, supplied = flag.partition('=')
            if key not in ('--fields', '--filter', '--cursor', '--limit', '--session-id',
                           '--from-start-time', '--to-start-time', '--trace-id', '--expand-metadata'):
                sys.exit(12)
            try:
                options[key[2:]] = supplied if separator else next(items)
            except StopIteration:
                sys.exit(13)
        if not options.get('json'):
            sys.exit(14)
        config = json.loads(Path(os.environ['INSPECTION_DATA']).read_text())
        rows = config['rows']
        criteria = json.loads(options['filter']) if 'filter' in options else []
        if not isinstance(criteria, list):
            sys.exit(15)
        for row in criteria:
            if (not isinstance(row, dict) or row.get('column') not in ('id', 'sessionId', 'startTime', 'metadata')
                    or row.get('operator') not in ('=', '>=', '>', '<', '<=')):
                sys.exit(16)
        with Path(os.environ['INSPECTION_QUERY_LOG']).open('a') as log:
            log.write(json.dumps({'fields': options.get('fields', ''), 'filter': criteria,
                                  'cursor': options.get('cursor')}) + '\\n')
        def at(value):
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        if criteria:
            for condition in criteria:
                column, op, value = condition['column'], condition['operator'], condition['value']
                def accepts(row):
                    found = row.get('metadata', {}).get(condition.get('key')) if column == 'metadata' else row.get(column)
                    if found is None:
                        return False
                    left, right = (at(found), at(value)) if column == 'startTime' else (found, value)
                    return {'=': left == right, '>=': left >= right, '>': left > right,
                            '<=': left <= right, '<': left < right}[op]
                rows = [row for row in rows if accepts(row)]
        else:
            if options.get('session-id'):
                rows = [row for row in rows if row.get('sessionId') == options['session-id']]
            if options.get('from-start-time'):
                rows = [row for row in rows if at(row['startTime']) >= at(options['from-start-time'])]
            if options.get('to-start-time'):
                rows = [row for row in rows if at(row['startTime']) < at(options['to-start-time'])]
        fields = options.get('fields', '').split(',')
        if 'io' in fields:
            ids = [item['value'] for item in criteria if item['column'] == 'id'
                   and item['type'] == 'string' and item['operator'] == '=']
            # Real service supports unscoped IO, but the fixture refuses it to
            # detect a query that could expose another interaction.
            if len(ids) != 1 or len([row for row in rows if row['id'] == ids[0]]) > 1:
                sys.exit(17)
        else:
            rows = [{k: v for k, v in row.items() if k not in ('input', 'output')} for row in rows]
        limit = min(int(options.get('limit', 50)), config.get('page_size', 20))
        cursor = options.get('cursor')
        offset = 0
        if cursor:
            try:
                epoch, number = base64.urlsafe_b64decode(cursor.encode()).decode().split(':')
                if epoch != config['epoch']:
                    sys.exit(18)
                offset = int(number)
            except Exception:
                sys.exit(18)
        selected = rows[offset:offset + limit]
        end = offset + len(selected)
        next_cursor = base64.urlsafe_b64encode((config['epoch'] + ':' + str(end)).encode()).decode() if end < len(rows) else None
        print(json.dumps({'status': 200, 'headers': {}, 'body': {
            'data': selected, 'meta': {'cursor': next_cursor}}}))
'''
    for name, body in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755 if name.startswith('bin/') else 0o644)
    (package / 'bin').chmod(0o700)
    node = home / 'synthetic-node'
    node.write_text('#!/bin/sh\nif [ \"$1\" = \"-p\" ]; then echo 22.19.0; else exec /usr/bin/python3 \"$@\"; fi\n')
    node.chmod(0o755)
    paths = langfuse_cli._paths(home)
    paths['bin'].mkdir(parents=True)
    entry = package / 'bin/langfuse'
    paths['launcher'].write_text(langfuse_cli._launcher(node, entry))
    paths['launcher'].chmod(0o755)
    langfuse_cli._write_receipt(paths['receipt'], {
        'schema': 1, 'source': 'external', 'owned': False, 'version': '1.2.4',
        'package': str(package), 'entry': str(entry), 'node': str(node),
        'launcher': str(paths['launcher']), 'assets': langfuse_cli._asset_map(package),
    })
    monkeypatch.setattr(observability, 'langfuse_query_context', lambda _, **_kwargs: {
        'base_url': 'http://127.0.0.1:9', 'project_id': 'synthetic-project',
        'public_key': 'pk_fixture', 'secret_key': 'sk_fixture'})
    pid_file = home / 'synthetic-child.pid'
    monkeypatch.setenv('INSPECTION_CHILD_PID', str(pid_file))
    monkeypatch.setenv('INSPECTION_MODE', mode)
    data = home / 'synthetic-observations.json'
    log = home / 'synthetic-query.jsonl'
    scope = {'agd.home_id': episode['home_id'], 'agd.role': episode['role']}
    if episode.get('task_id'):
        scope.update({'agd.task_id': episode['task_id'], 'agd.incarnation': episode['incarnation']})
    data.write_text(json.dumps({'epoch': 'first', 'page_size': 20, 'rows': [
        observation(episode, 'selected-observation', 11, 12, scope,
                    input='SCOPED_INPUT', output='SCOPED_OUTPUT'),
        observation(episode, 'foreign-observation', 31, 32, scope,
                    input='FOREIGN_INPUT', output='FOREIGN_OUTPUT'),
    ]}))
    monkeypatch.setenv('INSPECTION_DATA', str(data))
    monkeypatch.setenv('INSPECTION_QUERY_LOG', str(log))
    return pid_file


def observation(episode, ident, start, end, metadata, **io):
    from datetime import datetime, timezone
    iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'id': ident, 'traceId': 'trace-' + ident, 'sessionId': episode['session_id'],
            'projectId': 'synthetic-project', 'startTime': iso(start), 'endTime': iso(end),
            'metadata': metadata, **io}


def query_log(home):
    path = home / 'synthetic-query.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assert_child_gone(pid_file):
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_project_targets_use_frozen_git_version_and_local_authority():
    with lab() as (home, project):
        project_files(project)
        completed_session(home, role='worker', task_id='selected', texts=('done',))
        run_id, manifest = stage(home)
        (project / 'AGENTS.md').write_text('WORKTREE_CHANGED\n')
        for path, expected in [('AGENTS.md', 'Observe validation'), ('check.sh', 'checked')]:
            result = target(home, run_id, scope='project', project_id='sample', path=path)
            saved = record(home, run_id, result)
            assert saved['identity']['scope'] == 'project'
            assert saved['identity']['context']['project_id'] == 'sample'
            assert saved['identity']['authority'] == {'status': 'verified-project', 'locator': None}
            assert saved['guard']['state'] == 'present'
            assert saved['guard']['revision'] == manifest['episodes'][0]['project_binding']['task_revision']
            assert saved['guard']['source_sha256'] == hashlib.sha256(
                (b'Observe validation before reporting.\n' if path == 'AGENTS.md'
                 else b'#!/bin/sh\nprintf checked\\n\n')).hexdigest()
            assert expected in json.dumps(result)
            assert 'WORKTREE_CHANGED' not in json.dumps(result)
            bounded(result)


def test_unselected_or_changed_project_cannot_authorize_file():
    with lab() as (home, project):
        command(home, 'project', 'add', 'other', '--source', str(home.parent / 'origin.git'))
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='project', project_id='other', path='value.txt')
        registry = home / 'data/projects/sample.json'
        value = json.loads(registry.read_text()); value['source'] = 'different-origin'
        registry.write_text(json.dumps(value))
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='project', project_id='sample', path='value.txt')


@pytest.mark.parametrize('path', ['../value.txt', '/etc/passwd', 'link', 'secret.env', '.git/config'])
def test_project_paths_reject_escape_symlink_and_private_stores(path):
    with lab() as (home, project):
        project_files(project)
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='project', project_id='sample', path=path)


def test_create_proves_absence_and_remove_is_inert():
    with lab() as (home, project):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        created = target(home, run_id, scope='project', project_id='sample', path='new.txt', operation='create')
        assert record(home, run_id, created)['guard']['state'] == 'absent'
        assert not (project / 'new.txt').exists()
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='project', project_id='sample', path='value.txt', operation='create')
        removed = target(home, run_id, scope='project', project_id='sample', path='value.txt', operation='remove')
        assert record(home, run_id, removed)['guard']['state'] == 'present'
        assert (project / 'value.txt').read_text() == 'base\n'


def test_distro_hook_capture_uses_observed_version_not_project_revision():
    with lab() as (home, _):
        hook = home / 'config' / 'hooks' / 'review-hook.sh'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('printf hook-observation\n')
        completed_session(home)
        run_id, _ = stage(home)
        captured = target(home, run_id, scope='distro', path='config/hooks/review-hook.sh')
        saved = record(home, run_id, captured)
        assert saved['identity']['authority'] == {'status': 'verified-distro', 'locator': None}
        assert saved['identity']['context']['project_id'] is None
        assert saved['guard']['state'] == 'present'
        assert saved['guard']['source_sha256'] == hashlib.sha256(hook.read_bytes()).hexdigest()
        assert saved['guard']['revision'] is None
        assert saved['guard']['observed_at'] is not None


def test_file_target_rejects_supplementary_forgery_and_proves_revision_absence():
    with lab() as (home, project):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        forged = capture(home, run_id, 'value.txt belongs to another project and has version abc')
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='project', project_id='other', path='value.txt',
                   basis_source_ids=(forged,))
        (project / 'new.txt').write_text('untracked local bytes')
        result = target(home, run_id, scope='project', project_id='sample',
                        path='new.txt', operation='create')
        assert record(home, run_id, result)['guard']['state'] == 'absent'
        assert (project / 'new.txt').read_text() == 'untracked local bytes'


def test_native_scope_rewrite_and_title_append_stability():
    with lab() as (home, _):
        source, native, _ = completed_session(home, texts=('first-secret', 'second-secret'), active='unfinished-secret')
        run_id, manifest = stage(home)
        episodes = manifest['episodes']
        first = next(e for e in episodes if e['interaction_id'] ==
                     str(uuid.uuid5(uuid.NAMESPACE_URL, source['session_id'] + '-binding-0')))
        eid = first['episode_id']; first_id = source['session_id'] + '-final-0'
        second_id = source['session_id'] + '-final-1'
        read = lambda rid: inspection().inspect(home, run_id, eid, source='native', record_id=rid, part='content')
        initial = read(first_id)
        assert 'first-secret' in json.dumps(initial)
        assert 'second-secret' not in json.dumps(initial) and 'unfinished-secret' not in json.dumps(initial)
        with pytest.raises(store.Refusal):
            read(second_id)
        rewrite_title(native, 'renamed')
        append_unfinished(native, 'later-tail')
        assert 'first-secret' in json.dumps(read(first_id))
        native.write_text(native.read_text().replace('first-secret', 'rewritten'))
        with pytest.raises(store.Refusal):
            read(first_id)


def test_native_utf8_pages_are_bounded_and_cursor_scoped():
    with lab() as (home, _):
        text = 'Aé🙂' * 12000
        source, _, _ = completed_session(home, texts=(text,))
        run_id, manifest = stage(home)
        eid = manifest['episodes'][0]['episode_id']
        rid = source['session_id'] + '-final-0'
        first = inspection().inspect(home, run_id, eid, source='native', record_id=rid, part='content')
        bounded(first)
        cursor = first['next_cursor']
        assert cursor
        pages = [first]
        seen = set()
        while pages[-1]['next_cursor']:
            token = pages[-1]['next_cursor']
            assert token not in seen
            seen.add(token)
            page = inspection().inspect(home, run_id, eid, source='native',
                                        record_id=rid, part='content', cursor=token)
            bounded(page)
            pages.append(page)
        # Consumer-facing pages must expose text and half-open UTF-8 byte ranges.
        raw = text.encode()
        offset = 0
        parts = []
        for page in pages:
            chunk = page['text'].encode()
            span = page['range']
            assert span['start'] == offset and span['end'] == offset + len(chunk)
            assert chunk == raw[span['start']:span['end']]
            assert '\ufffd' not in page['text']
            parts.append(chunk)
            offset = span['end']
        assert b''.join(parts) == raw
        other_run, other_manifest = stage(home)
        with pytest.raises(store.Refusal):
            inspection().inspect(home, other_run, other_manifest['episodes'][0]['episode_id'], source='native', record_id=rid, part='content', cursor=cursor)
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, eid, source='native', cursor=cursor)


def test_missing_component_evidence_and_forged_local_hint_refused():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='shared-tool')
        basis = capture(home, run_id, 'shared-tool is installed locally')
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='shared-tool', basis_source_ids=(basis,), authority_hint='verified-distro')


def test_unknown_authority_is_not_promoted_by_distro_scope():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'observed shared-tool failed')
        saved = record(home, run_id, target(home, run_id, scope='distro', component='shared-tool', basis_source_ids=(basis,)))
        assert saved['identity']['scope'] == 'distro'
        assert saved['identity']['authority'] == {'status': 'unknown', 'locator': None}
        assert saved['guard']['state'] == 'described'


def test_model_hint_cannot_mint_verified_authority():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'shared-tool belongs to the distro')
        for hint in ('verified-project', 'verified-distro'):
            with pytest.raises(store.Refusal):
                target(home, run_id, scope='distro', component='shared-tool', basis_source_ids=(basis,), authority_hint=hint)


def test_external_target_preserves_private_basis_without_local_file():
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected', texts=('private project incident',))
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'private project incident: upstream vendor/tool contract fails')
        result = target(home, run_id, scope='distro', component='tool-contract', basis_source_ids=(basis,),
                        authority_hint='external', authority_locator='vendor/tool')
        saved = record(home, run_id, result)
        assert saved['identity']['authority'] == {'status': 'external', 'locator': 'vendor/tool'}
        assert saved['identity']['kind'] == 'component'
        assert basis in saved['basis_source_ids']
        assert saved['guard']['state'] == 'described' and saved['guard']['source_sha256'] is None
        assert not (home / 'tool-contract').exists()


def test_external_and_unknown_same_component_remain_distinct():
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'vendor/agd describes agd hook behavior')
        external = record(home, run_id, target(home, run_id, scope='distro', component='agd',
                          basis_source_ids=(basis,), authority_hint='external', authority_locator='vendor/agd'))
        unknown = record(home, run_id, target(home, run_id, scope='distro', component='agd', basis_source_ids=(basis,)))
        assert external['identity'] != unknown['identity']
        assert external['target_ref'] != unknown['target_ref']


def test_owned_file_does_not_prove_same_component_ownership():
    with lab() as (home, _):
        local = home / 'config' / 'hooks' / 'tool'
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text('local hook contract\n')
        completed_session(home)
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'vendor/tool owns the external tool contract')
        external = record(home, run_id, target(home, run_id, scope='distro', component='tool',
                          basis_source_ids=(basis,), authority_hint='external', authority_locator='vendor/tool'))
        owned_result = target(home, run_id, scope='distro', path='config/hooks/tool')
        owned = record(home, run_id, owned_result)
        # A file receipt proves file ownership, not by itself which logical
        # component is implemented by that file.
        component = record(home, run_id, target(
            home, run_id, scope='distro', component='tool',
            basis_source_ids=tuple(owned_result['source_ids'])))
        assert external['identity']['authority']['status'] == 'external'
        assert owned['identity']['authority']['status'] == 'verified-distro'
        assert component['identity']['authority'] == {'status': 'unknown', 'locator': None}
        assert external['identity'] != component['identity']
        assert external['target_ref'] != component['target_ref']


def test_closed_run_cannot_capture_targets_or_skills():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        path = reviewer.paths(home)['receipts'] / (run_id + '.json')
        receipt = json.loads(path.read_text())
        receipt['dispatch_state'] = 'final'
        store.atomic_json(path, receipt)
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='tool')
        with pytest.raises(store.Refusal):
            inspection().list_skills(home, run_id)


@pytest.mark.parametrize('locator', ['token=abc', 'https://user:password@example.org/tool', 'bad\nline', 'x' * 257, ''])
def test_authority_locator_rejects_credentials_and_control_characters(locator):
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'upstream vendor/tool')
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='tool', basis_source_ids=(basis,),
                   authority_hint='external', authority_locator=locator)


def test_target_refs_cannot_cross_runs():
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        first, _ = stage(home)
        saved = target(home, first, scope='project', project_id='sample', path='value.txt')
        second, _ = stage(home)
        with pytest.raises(store.Refusal):
            inspection().read_target(home, second, saved['target_ref'])
        assert record(home, first, saved)['target_ref'] == saved['target_ref']


def test_skills_are_lazy_and_show_produces_target():
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'vendor/tool failed')
        described = target(home, run_id, scope='distro', component='tool', basis_source_ids=(basis,))
        assert record(home, run_id, described)['guard']['state'] == 'described'
        catalog = inspection().list_skills(home, run_id)
        bounded(catalog)
        skill_id = worker_skills.catalog(home)[0]['id']
        shown = inspection().show_skill(home, run_id, skill_id)
        assert record(home, run_id, shown)['identity']['kind'] == 'file'
        bounded(shown)


def test_cli_refuses_unselected_episode_with_bounded_output():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        cli = Path(__file__).resolve().parents[1] / 'bin/agd'
        result = subprocess.run([sys.executable, str(cli), '--home', str(home), 'reviewer', 'inspect', run_id,
                                 '--episode', '0' * 64, '--source', 'native'],
                                capture_output=True, timeout=15, env=dict(os.environ, PATH='/usr/bin:/bin'))
        assert result.returncode != 0
        assert len(result.stdout) <= 16384 and len(result.stderr) <= 16384


def test_langfuse_metadata_then_scoped_detail_rejects_wrong_interaction(monkeypatch):
    with lab() as (home, _):
        completed_session(home, texts=('native-first', 'native-second'))
        run_id, manifest = stage(home)
        episode = min(manifest['episodes'], key=lambda e: e['closed_at'])
        telemetry_fixture(home, monkeypatch, episode)
        eid = episode['episode_id']
        metadata = inspection().inspect(home, run_id, eid, source='langfuse')
        bounded(metadata)
        rendered = json.dumps(metadata)
        assert 'SCOPED_INPUT' not in rendered and 'SCOPED_OUTPUT' not in rendered
        assert 'FOREIGN_INPUT' not in rendered and 'FOREIGN_OUTPUT' not in rendered
        assert 'selected-observation' in rendered
        assert 'foreign-observation' not in rendered
        detail = inspection().inspect(home, run_id, eid, source='langfuse',
                                      record_id='selected-observation', part='output')
        bounded(detail)
        assert 'SCOPED_OUTPUT' in json.dumps(detail)
        assert 'FOREIGN_OUTPUT' not in json.dumps(detail)
        queries = query_log(home)
        io_queries = [q for q in queries if 'io' in q['fields'].split(',')]
        assert io_queries
        for query in io_queries:
            filters = query['filter']
            assert any(f.get('column') == 'id' and f.get('operator') == '='
                       and f.get('value') == 'selected-observation' for f in filters)
            assert any(f.get('column') == 'sessionId' and f.get('value') == episode['session_id']
                       for f in filters)
            assert {f.get('operator') for f in filters if f.get('column') == 'startTime'} >= {'>=', '<'}
        io_count = len(io_queries)
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, eid, source='langfuse',
                                 record_id='foreign-observation', part='output')
        assert len([q for q in query_log(home) if 'io' in q['fields'].split(',')]) == io_count


@pytest.mark.parametrize('mode', ['overflow', 'timeout', 'failure', 'partial'])
def test_langfuse_failures_fallback_without_leaking_or_leaving_child(mode, monkeypatch):
    with lab() as (home, _):
        completed_session(home, texts=('NATIVE_FALLBACK',))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        pid_file = telemetry_fixture(home, monkeypatch, episode, mode)
        if mode == 'timeout':
            path = reviewer.paths(home)['receipts'] / (run_id + '.json')
            receipt = json.loads(path.read_text())
            receipt['deadline_epoch_ms'] = int((time.time() + 2) * 1000)
            store.atomic_json(path, receipt)
        try:
            result = inspection().inspect(home, run_id, episode['episode_id'], source='auto')
            bounded(result)
            rendered = json.dumps(result)
            assert 'synthetic-credential' not in rendered
            assert 'FOREIGN_OUTPUT' not in rendered
        except store.Refusal as error:
            assert 'synthetic-credential' not in str(error)
        assert_child_gone(pid_file)


def test_unique_input_budget_exact_boundary_and_duplicate_capture():
    with lab() as (home, _):
        completed_session(home)
        limit = 24000
        run_id, manifest = stage(home, limit=limit)
        manifest_bytes = len(json.dumps(manifest, indent=2, ensure_ascii=False).encode())
        catalog_path = reviewer.paths(home)['catalogs'] / (manifest['catalog_id'] + '.json')
        seed_bytes = manifest_bytes + len(catalog_path.read_bytes())
        remaining = limit - seed_bytes
        assert remaining > 10
        body = 'x' * remaining
        first = capture(home, run_id, body)
        again = capture(home, run_id, body)
        assert first == again
        assert len(reviewer.read_evidence(home, run_id)['sources']) == 1
        with pytest.raises(store.Refusal):
            capture(home, run_id, 'y')


def test_intent_and_linked_feedback_refuse_changed_artifacts():
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='feedback')
        task_dir = tasks.task_file(home, task['id']).parent
        brief = task_dir / 'brief.md'
        brief.write_text('FROZEN_TASK_INTENT\n')
        question = task_dir / 'question.txt'
        question.write_text('Which?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
        supervision.reconcile(home)
        answer = task_dir / 'answer.txt'
        answer.write_text('LINKED_FEEDBACK\n')
        supervision.resolve(home, task['id'], 'choice', answer)
        run_id, manifest = stage(home, cutoff=time.time() + 100)
        episode = next(e for e in manifest['episodes'] if e.get('task_id') == task['id'])
        locators = reviewer.read_selection_locators(home, manifest['manifest_id'], episode['episode_id'])['rows']
        intent = next(row for row in locators if row['kind'] == 'intent')
        feedback = next(row for row in locators if row['kind'] == 'feedback')
        for locator, sentinel in ((intent, 'FROZEN_TASK_INTENT'), (feedback, 'LINKED_FEEDBACK')):
            result = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                          record_id=locator['sha256'], part='content')
            assert sentinel in json.dumps(result)
            assert locator['sha256'] == hashlib.sha256((home / locator['path']).read_bytes()).hexdigest()
        brief.write_text('REPLACED_INTENT\n')
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                 record_id=intent['sha256'], part='content')
        (home / feedback['link_path']).write_text('{}')
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                 record_id=feedback['sha256'], part='content')


def test_replaced_native_source_is_not_substituted():
    with lab() as (home, _):
        source, native, _ = completed_session(home, texts=('bound original',))
        run_id, manifest = stage(home)
        eid = manifest['episodes'][0]['episode_id']
        rid = source['session_id'] + '-final-0'
        native.unlink()
        native.symlink_to(home / 'foreign.jsonl')
        (home / 'foreign.jsonl').write_text('foreign sentinel')
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, eid, source='native', record_id=rid, part='content')


def test_tool_target_needs_no_skill_lookup(monkeypatch):
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'upstream vendor/tool contract failed')
        def unavailable(_home):
            raise AssertionError('unrelated skill catalog access')
        monkeypatch.setattr(worker_skills, 'catalog', unavailable)
        result = target(home, run_id, scope='distro', component='tool',
                        basis_source_ids=(basis,), authority_hint='external', authority_locator='vendor/tool')
        assert record(home, run_id, result)['identity']['authority']['status'] == 'external'


def test_target_record_and_exposure_journal_tampering_is_refused():
    with lab() as (home, _):
        source, _, _ = completed_session(home, role='worker', task_id='selected',
                                         texts=('journal integrity sentinel',))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        rid = source['session_id'] + '-final-0'
        inspection().inspect(home, run_id, episode['episode_id'], source='native',
                             record_id=rid, part='content')
        exposed_hash = hashlib.sha256(b'journal integrity sentinel').hexdigest()
        journal_files = [p for p in (reviewer.paths(home)['evidence'] / 'inspection' / run_id).glob('*.json')
                         if exposed_hash in p.read_text() and episode['episode_id'] in p.read_text()]
        assert journal_files
        journal = journal_files[0]
        journal.write_text(journal.read_text().replace(exposed_hash, '0' * 64))
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                 record_id=rid, part='content')
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        result = target(home, run_id, scope='project', project_id='sample', path='value.txt')
        ref = result['target_ref']
        path = reviewer.paths(home)['inspections'] / run_id / 'targets' / (ref + '.json')
        value = json.loads(path.read_text())
        assert reviewer._digest(value) == ref
        value['guard']['source_sha256'] = '0' * 64
        path.write_text(json.dumps(value))
        with pytest.raises(store.Refusal):
            inspection().read_target(home, run_id, ref)


def test_skill_show_distinguishes_current_baseline_and_assigned_snapshot():
    with lab() as (home, _):
        skill = next(row for row in worker_skills.catalog(home) if row['id'] == 'systematic-debugging')
        source = Path(skill['source']) / 'SKILL.md'
        old = '---\nname: systematic-debugging\ndescription: Fixture old\n---\nHISTORIC SKILL\n'
        current = '---\nname: systematic-debugging\ndescription: Fixture new\n---\nCURRENT SKILL\n'
        source.write_text(old)
        _, _, task = completed_session(home, role='worker', task_id='skill-history')
        assigned = next(row for row in task['worker_skills']['skills'] if row['id'] == skill['id'])
        snapshot = Path(assigned['snapshot']) / 'SKILL.md'
        assert snapshot.read_text() == old
        source.write_text(current)
        run_id, _ = stage(home)
        shown = inspection().show_skill(home, run_id, skill['id'])
        text = json.dumps(shown)
        assert hashlib.sha256(current.encode()).hexdigest() in text
        assert hashlib.sha256(old.encode()).hexdigest() in text
        assert 'CURRENT SKILL' in text
        snapshot.write_text(old.replace('HISTORIC SKILL', 'REPLACED HISTORIC'))
        try:
            after = inspection().show_skill(home, run_id, skill['id'])
        except store.Refusal:
            return
        serialized = json.dumps(after)
        assert 'REPLACED HISTORIC' not in serialized
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() not in serialized
        assert after.get('limitations')


def test_nonsecret_setting_observation_does_not_capture_secret_store():
    with lab() as (home, _):
        completed_session(home)
        settings = home / 'config' / 'private-settings.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'model': 'model-a', 'api_key': 'PRIVATE_TOKEN_987'}))
        secret_hash = hashlib.sha256(settings.read_bytes()).hexdigest()
        run_id, _ = stage(home)
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', path='config/private-settings.json')
        basis = capture(home, run_id, 'observed model setting: model-a; source: sanitized local query')
        result = target(home, run_id, scope='distro', component='model-routing',
                        basis_source_ids=(basis,))
        saved = record(home, run_id, result)
        assert saved['guard']['state'] == 'described'
        assert saved['guard']['source_sha256'] is None
        assert basis in saved['basis_source_ids']
        rendered = json.dumps([result, saved, reviewer.read_evidence(home, run_id)])
        assert 'PRIVATE_TOKEN_987' not in rendered
        assert secret_hash not in rendered


def test_cli_routes_external_authority_and_rejects_invalid_target_arguments():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        basis = capture(home, run_id, 'vendor/tool provides the upstream contract')
        cli = Path(__file__).resolve().parents[1] / 'bin/agd'
        base = [sys.executable, str(cli), '--home', str(home), 'reviewer', 'target', 'inspect', run_id]
        env = dict(os.environ, AGD_HOME=str(home), TREEHOUSE_ROOT=str(home.parent / 'pool'))
        valid = ['--scope', 'distro', '--component', 'tool', '--surface', 'tooling',
                 '--basis-source', basis, '--authority-hint', 'external',
                 '--authority-locator', 'vendor/tool']
        completed = subprocess.run(base + valid, env=env, capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
        value = json.loads(completed.stdout)
        bounded(value)
        saved = record(home, run_id, value)
        assert saved['identity']['scope'] == 'distro'
        assert saved['identity']['authority'] == {'status': 'external', 'locator': 'vendor/tool'}
        for invalid in (
            ['--scope', 'distro', '--path', 'value.txt', '--component', 'tool', '--surface', 'tooling'],
            ['--scope', 'project', '--path', 'value.txt', '--surface', 'tooling'],
            ['--scope', 'distro', '--component', 'tool', '--surface', 'tooling', '--operation', 'execute'],
            ['--scope', 'distro', '--component', 'tool', '--surface', 'x' * 81],
            ['--scope', 'distro', '--component', 'tool', '--surface', 'tooling',
             '--basis-source', basis, '--authority-hint', 'verified-distro'],
        ):
            refused = subprocess.run(base + invalid, env=env, capture_output=True, timeout=15)
            assert refused.returncode != 0
            assert len(refused.stdout) <= 16384 and len(refused.stderr) <= 16384


def test_langfuse_cursor_paging_and_fresh_run_reenumeration(monkeypatch):
    with lab() as (home, _):
        completed_session(home, texts=('first interaction', 'later interaction'))
        run_id, manifest = stage(home)
        episode = min(manifest['episodes'], key=lambda e: e['closed_at'])
        telemetry_fixture(home, monkeypatch, episode)
        data_path = home / 'synthetic-observations.json'
        data = json.loads(data_path.read_text())
        scope = {'agd.home_id': episode['home_id'], 'agd.role': episode['role']}
        data['page_size'] = 2
        data['rows'] = [observation(episode, f'observation-{n}', 10.1 + n / 20,
                                    10.15 + n / 20, scope, output=f'output-{n}')
                        for n in range(6)]
        data_path.write_text(json.dumps(data))
        seen, cursors, cursor = [], [], None
        while True:
            result = inspection().inspect(home, run_id, episode['episode_id'], source='langfuse', cursor=cursor)
            bounded(result)
            seen.extend(row['id'] for row in result['rows'])
            cursor = result['next_cursor']
            if cursor is None:
                break
            assert cursor not in cursors
            cursors.append(cursor)
        assert seen == [f'observation-{n}' for n in range(6)]
        provider_cursors = {row['cursor'] for row in query_log(home) if row['cursor']}
        assert provider_cursors and not provider_cursors.intersection(cursors)
        # A provider cursor cannot be carried across runs, even if the same
        # episode/version is selected. Newly arrived metadata is discoverable.
        data['epoch'] = 'later'
        data['rows'].insert(0, observation(episode, 'new-arrival', 10.05, 10.06, scope))
        data_path.write_text(json.dumps(data))
        other_run, other_manifest = stage(home)
        matching = next(e for e in other_manifest['episodes'] if e['episode_id'] == episode['episode_id'])
        before = len(query_log(home))
        first = inspection().inspect(home, other_run, matching['episode_id'], source='langfuse')
        assert 'new-arrival' in {row['id'] for row in first['rows']}
        assert query_log(home)[before]['cursor'] is None
        with pytest.raises(store.Refusal):
            inspection().inspect(home, other_run, matching['episode_id'], source='langfuse', cursor=cursors[0])


def test_langfuse_native_start_window_and_conflicting_metadata(monkeypatch):
    with lab() as (home, _):
        completed_session(home, texts=('first interaction', 'later interaction'))
        run_id, manifest = stage(home)
        episode = min(manifest['episodes'], key=lambda e: e['closed_at'])
        telemetry_fixture(home, monkeypatch, episode)
        scope = {'agd.home_id': episode['home_id'], 'agd.role': episode['role']}
        foreign = {**scope, 'agd.home_id': str(uuid.uuid4())}
        data_path = home / 'synthetic-observations.json'
        data = json.loads(data_path.read_text())
        data['rows'] = [
            observation(episode, 'before-prompt', 9.5, 9.9, scope, output='EARLY_PRIVATE'),
            observation(episode, 'correct-window', 10.5, 11.5, scope, output='RIGHT_OUTPUT'),
            observation(episode, 'foreign-home', 10.6, 11.6, foreign, output='FOREIGN_PRIVATE'),
        ]
        data_path.write_text(json.dumps(data))
        result = inspection().inspect(home, run_id, episode['episode_id'], source='langfuse')
        rendered = json.dumps(result)
        assert 'correct-window' in rendered
        assert all(value not in rendered for value in ('before-prompt', 'foreign-home'))
        for record_id in ('before-prompt', 'foreign-home'):
            with pytest.raises(store.Refusal):
                inspection().inspect(home, run_id, episode['episode_id'], source='langfuse',
                                     record_id=record_id, part='output')
        for query in query_log(home):
            assert 'io' not in query['fields'].split(',')


def test_langfuse_worker_scope_rejects_conflicting_task_and_incarnation(monkeypatch):
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        run_id, manifest = stage(home)
        episode = next(e for e in manifest['episodes'] if e.get('task_id') == 'selected')
        telemetry_fixture(home, monkeypatch, episode)
        scope = {'agd.home_id': episode['home_id'], 'agd.role': 'worker',
                 'agd.task_id': episode['task_id'], 'agd.incarnation': episode['incarnation']}
        data_path = home / 'synthetic-observations.json'
        data = json.loads(data_path.read_text())
        data['rows'] = [
            observation(episode, 'right-worker', 10.5, 11.5, scope),
            observation(episode, 'wrong-task', 10.6, 11.6, {**scope, 'agd.task_id': 'different'}),
            observation(episode, 'wrong-incarnation', 10.7, 11.7,
                        {**scope, 'agd.incarnation': str(uuid.uuid4())}),
            observation(episode, 'wrong-role', 10.8, 11.8, {**scope, 'agd.role': 'central'}),
        ]
        data_path.write_text(json.dumps(data))
        result = inspection().inspect(home, run_id, episode['episode_id'], source='langfuse')
        names = {row['id'] for row in result['rows']}
        assert 'right-worker' in names
        assert not names.intersection({'wrong-task', 'wrong-incarnation', 'wrong-role'})


def test_langfuse_overlapping_span_is_not_exact_interaction_evidence(monkeypatch):
    with lab() as (home, _):
        completed_session(home, texts=('first interaction', 'later interaction'))
        run_id, manifest = stage(home)
        episode = min(manifest['episodes'], key=lambda e: e['closed_at'])
        telemetry_fixture(home, monkeypatch, episode)
        data_path = home / 'synthetic-observations.json'
        data = json.loads(data_path.read_text())
        scope = {'agd.home_id': episode['home_id'], 'agd.role': episode['role']}
        data['rows'] = [observation(episode, 'overlap', 11.0, 31.0, scope, output='AMBIGUOUS')]
        data_path.write_text(json.dumps(data))
        result = inspection().inspect(home, run_id, episode['episode_id'], source='auto')
        assert result['source'] == 'native'
        assert result.get('limitations')
        assert 'AMBIGUOUS' not in json.dumps(result)


def _collect_text_pages(fetch, expected):
    pages, cursor, previous = [], None, 0
    while True:
        page = fetch(cursor)
        bounded(page)
        chunk = page['text'].encode()
        assert page['range'] == {'start': previous, 'end': previous + len(chunk)}
        assert chunk == expected[previous:previous + len(chunk)]
        assert '\ufffd' not in page['text']
        pages.append(chunk)
        previous += len(chunk)
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert b''.join(pages) == expected


@pytest.mark.parametrize('kind', ['message', 'intent', 'target'])
def test_large_native_artifact_and_target_utf8_ranges_and_version(kind):
    with lab() as (home, _):
        text = 'Aé🙂' * 38000 + 'end-marker'
        assert len(text.encode()) > 262144
        if kind == 'message':
            source, native, _ = completed_session(home, texts=(text,))
            run_id, manifest = stage(home, limit=2000000)
            episode = manifest['episodes'][0]
            rid = source['session_id'] + '-final-0'
            fetch = lambda cursor: inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                                         record_id=rid, part='content', cursor=cursor)
            mutate = lambda: native.write_text(native.read_text().replace('end-marker', 'end-changed'))
        elif kind == 'intent':
            _, _, task = completed_session(home, role='worker', task_id='large-intent')
            artifact = tasks.task_file(home, task['id']).parent / 'brief.md'
            artifact.write_text(text)
            run_id, manifest = stage(home, limit=2000000)
            episode = next(e for e in manifest['episodes'] if e.get('task_id') == task['id'])
            locators = reviewer.read_selection_locators(home, manifest['manifest_id'], episode['episode_id'])['rows']
            rid = next(item['sha256'] for item in locators if item['kind'] == 'intent')
            fetch = lambda cursor: inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                                         record_id=rid, part='content', cursor=cursor)
            mutate = lambda: artifact.write_text(text.replace('end-marker', 'end-changed'))
        else:
            file = home / 'config' / 'hooks' / 'large-hook.txt'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
            completed_session(home)
            run_id, _ = stage(home, limit=2000000)
            fetch = lambda cursor: target(home, run_id, scope='distro', path='config/hooks/large-hook.txt',
                                          cursor=cursor)
            mutate = lambda: file.write_text(text.replace('end-marker', 'end-changed'))
        first = fetch(None)
        assert first['next_cursor']
        if kind == 'target':
            assert record(home, run_id, first)['guard']['source_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        _collect_text_pages(fetch, text.encode())
        mutate()
        with pytest.raises(store.Refusal):
            fetch(first['next_cursor'])


def set_deadline(home, run_id, seconds):
    path = reviewer.paths(home)['receipts'] / (run_id + '.json')
    receipt = json.loads(path.read_text())
    receipt['deadline_epoch_ms'] = int((time.time() + seconds) * 1000)
    store.atomic_json(path, receipt)


def test_langfuse_cli_validation_obeys_remaining_deadline_and_reaps_child(monkeypatch):
    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        telemetry_fixture(home, monkeypatch, episode)
        pid_file = home / 'slow-validation.pid'
        monkeypatch.setenv('SLOW_VALIDATION_PID', str(pid_file))
        node = home / 'synthetic-node'
        node.write_text('#!/usr/bin/python3\nimport os, time\n'
                        'open(os.environ[\"SLOW_VALIDATION_PID\"], \"w\").write(str(os.getpid()))\n'
                        'time.sleep(6)\n')
        node.chmod(0o755)
        set_deadline(home, run_id, 1.5)
        started = time.monotonic()
        try:
            result = inspection().inspect(home, run_id, episode['episode_id'], source='auto')
            assert result['source'] == 'native' and result.get('limitations')
        except store.Refusal:
            pass
        assert time.monotonic() - started < 4
        assert_child_gone(pid_file)


def test_project_identity_validation_obeys_remaining_deadline_and_reaps_child(monkeypatch):
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        bin_dir = home / 'slow-bin'
        bin_dir.mkdir(mode=0o700)
        git = bin_dir / 'git'
        git.write_text('#!/usr/bin/python3\nimport os, sys, time\n'
                       'open(os.environ[\"SLOW_GIT_PID\"], \"w\").write(str(os.getpid()))\n'
                       'time.sleep(6)\nsys.exit(3)\n')
        git.chmod(0o755)
        pid_file = home / 'slow-git.pid'
        monkeypatch.setenv('SLOW_GIT_PID', str(pid_file))
        monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'))
        set_deadline(home, run_id, 1.5)
        started = time.monotonic()
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='project', project_id='sample', path='value.txt')
        assert time.monotonic() - started < 4
        assert_child_gone(pid_file)


def _fixture_record(directory, value):
    """Place attacker-controlled bytes without changing any native authority record."""
    directory.mkdir(parents=True, exist_ok=True)
    ident = reviewer._digest(value)
    (directory / (ident + '.json')).write_text(json.dumps(value))
    return ident


@pytest.mark.parametrize('actually_present', [True, False])
def test_r1_self_addressed_distro_absence_is_not_issued(actually_present):
    with lab() as (home, _):
        hook = home / 'config/hooks/proved-hook'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('owned original\n')
        completed_session(home)
        run_id, _ = stage(home)
        issued = target(home, run_id, scope='distro', path='config/hooks/proved-hook')
        assert record(home, run_id, issued)['guard']['state'] == 'present'
        target_dir = reviewer.paths(home)['inspections'] / run_id / 'targets'
        forged = json.loads((target_dir / (issued['target_ref'] + '.json')).read_text())
        forged['identity']['locator'] = ('config/hooks/proved-hook' if actually_present
                                         else 'config/hooks/not-issued')
        forged['operation'] = 'create'
        forged['guard'].update(state='absent', source_sha256=None)
        forged['basis_source_ids'] = []
        ref = _fixture_record(target_dir, forged)
        assert (home / forged['identity']['locator']).exists() is actually_present
        with pytest.raises(store.Refusal):
            inspection().read_target(home, run_id, ref)


def test_r1_distro_absence_receipt_cannot_be_imported_from_another_run():
    with lab() as (home, _):
        (home / 'config/hooks').mkdir(parents=True, exist_ok=True)
        completed_session(home)
        first, _ = stage(home)
        issued = target(home, first, scope='distro', path='config/hooks/new-hook', operation='create')
        assert record(home, first, issued)['guard']['state'] == 'absent'
        second, _ = stage(home)
        source = reviewer.paths(home)['inspections'] / first / 'targets' / (issued['target_ref'] + '.json')
        forged = json.loads(source.read_text())
        target_dir = reviewer.paths(home)['inspections'] / second / 'targets'
        assert _fixture_record(target_dir, forged) == issued['target_ref']
        with pytest.raises(store.Refusal):
            inspection().read_target(home, second, issued['target_ref'])


def test_r1_forged_present_target_and_source_cannot_mint_file_proof():
    with lab() as (home, _):
        hook = home / 'config/hooks/actual-hook'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('actual-owned-version\n')
        completed_session(home)
        run_id, _ = stage(home)
        issued = target(home, run_id, scope='distro', path='config/hooks/actual-hook')
        assert record(home, run_id, issued)['guard']['source_sha256'] == hashlib.sha256(hook.read_bytes()).hexdigest()
        source_dir = reviewer.paths(home)['evidence'] / 'inspection' / run_id
        target_dir = reviewer.paths(home)['inspections'] / run_id / 'targets'
        target_value = json.loads((target_dir / (issued['target_ref'] + '.json')).read_text())
        source_value = json.loads((source_dir / (issued['source_ids'][0] + '.json')).read_text())
        false_text = 'unobserved forged bytes\n'
        false_hash = hashlib.sha256(false_text.encode()).hexdigest()
        source_value.update(text=false_text, bytes=len(false_text.encode()), text_sha256=false_hash)
        source_value['provenance'].update(version_sha256=false_hash, start=0, end=len(false_text.encode()),
                                          previous_cursor=None, complete=True)
        forged_source = _fixture_record(source_dir, source_value)
        target_value['guard']['source_sha256'] = false_hash
        target_value['basis_source_ids'] = [forged_source]
        forged_target = _fixture_record(target_dir, target_value)
        assert hook.read_text() == 'actual-owned-version\n'
        with pytest.raises(store.Refusal):
            inspection().read_target(home, run_id, forged_target)
        with pytest.raises(store.Refusal):
            inspection().read_journal(home, run_id)


def test_r1_self_addressed_selected_native_source_is_not_exposure():
    with lab() as (home, _):
        source, _, _ = completed_session(home, texts=('issued final',))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        first = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                     record_id=source['session_id'] + '-final-0', part='content')
        source_dir = reviewer.paths(home)['evidence'] / 'inspection' / run_id
        forged = json.loads((source_dir / (first['source_id'] + '.json')).read_text())
        # The prompt really exists in the selected branch, but no command exposed
        # it. Content-addressing and a correct native hash cannot issue a receipt.
        prompt = 'prompt'
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        forged.update(text=prompt, bytes=len(prompt), text_sha256=prompt_hash)
        forged['provenance'].update(record_id=source['session_id'] + '-prompt-0',
                                    start=0, end=len(prompt), version_sha256=prompt_hash,
                                    previous_cursor=None, complete=True)
        _fixture_record(source_dir, forged)
        with pytest.raises(store.Refusal):
            inspection().read_journal(home, run_id)


def test_r1_evidenced_url_userinfo_is_refused_before_target_receipt():
    with lab() as (home, _):
        completed_session(home)
        run_id, _ = stage(home)
        locator = 'https://alice:pass@example.org/tool'
        basis = capture(home, run_id, 'Observed upstream locator: ' + locator)
        assert locator in reviewer.read_evidence(home, run_id)['sources'][basis]['text']
        before = inspection().read_journal(home, run_id)
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='upstream-tool',
                   basis_source_ids=(basis,), authority_hint='external', authority_locator=locator)
        after = inspection().read_journal(home, run_id)
        assert before == after


def _disclosed_source_ids(value):
    result = set()
    for item in _nested_dicts(value):
        single = item.get('source_id')
        if isinstance(single, str) and len(single) == 64:
            result.add(single)
        multiple = item.get('source_ids', [])
        if isinstance(multiple, list):
            result.update(source for source in multiple if isinstance(source, str) and len(source) == 64)
    return result


def _wide_native_metadata(home):
    source, native, _ = completed_session(home, texts=('bounded final',))
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    binding_index = next(i for i, row in enumerate(rows) if row.get('customType') == 'agd:episode-binding')
    final_index = next(i for i, row in enumerate(rows) if row.get('id') == source['session_id'] + '-final-0')
    parent = rows[binding_index]['id']
    inserted = []
    for number in range(12):
        entry = message('action', when=11.01 + number / 1000, role='assistant')
        entry['id'] = f'wide-{number:02d}-' + 'x' * 1600
        entry['parentId'] = parent
        parent = entry['id']
        inserted.append(entry)
    rows[final_index]['parentId'] = parent
    rows[final_index:final_index] = inserted
    native.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    return source, {row['id'] for row in inserted}


def test_r1_native_metadata_large_ids_page_without_undisclosed_exposure():
    with lab() as (home, _):
        _, expected = _wide_native_metadata(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        before = inspection().read_journal(home, run_id)
        disclosed, seen, cursor = set(), set(), None
        while True:
            try:
                response = inspection().inspect(home, run_id, episode['episode_id'],
                                                source='native', cursor=cursor)
            except store.Refusal:
                # A refused response cannot claim exposure or spend the budget.
                after = inspection().read_journal(home, run_id)
                assert set(after['sources']) == set(before['sources']) | disclosed
                raise
            bounded(response)
            disclosed.update(_disclosed_source_ids(response) &
                             set(inspection().read_journal(home, run_id)['sources']))
            seen.update(row['message_id'] for row in response['rows'] if 'message_id' in row)
            cursor = response['next_cursor']
            if cursor is None:
                break
        assert expected <= seen
        after = inspection().read_journal(home, run_id)
        assert set(after['sources']) == set(before['sources']) | disclosed


def test_r1_skills_show_assignments_and_content_are_bounded_without_orphans():
    with lab() as (home, _):
        skill = next(row for row in worker_skills.catalog(home) if row['id'] == 'systematic-debugging')
        source = Path(skill['source']) / 'SKILL.md'
        source.write_text('---\nname: systematic-debugging\ndescription: Fixture\n---\n' + '\x00' * 2400)
        for number in range(8):
            task_id = f'stress-{number:02d}-' + 'q' * 37
            completed_session(home, role='worker', task_id=task_id)
        run_id, manifest = stage(home)
        selected = {episode['task_id'] for episode in manifest['episodes'] if episode.get('task_id', '').startswith('stress-')}
        assert len(selected) >= 2
        before = inspection().read_journal(home, run_id)
        sources, targets, assigned, cursor = set(), set(), set(), None
        while True:
            try:
                page = inspection().show_skill(home, run_id, 'systematic-debugging', cursor=cursor)
            except store.Refusal:
                after = inspection().read_journal(home, run_id)
                assert set(after['sources']) == set(before['sources']) | sources
                assert set(after['targets']) == set(before['targets']) | targets
                raise
            bounded(page)
            sources.update(_disclosed_source_ids(page) &
                           set(inspection().read_journal(home, run_id)['sources']))
            targets.add(page['target_ref'])
            assigned.update(row['task_id'] for row in page.get('assigned', []))
            cursor = page.get('next_cursor')
            if cursor is None:
                break
        assert selected <= assigned
        after = inspection().read_journal(home, run_id)
        assert set(after['sources']) == set(before['sources']) | sources
        assert set(after['targets']) == set(before['targets']) | targets


def _nested_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_dicts(child)


def test_r1_cli_discovers_and_reads_feedback_beyond_inline_locators():
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='many-feedback')
        directory = tasks.task_file(home, task['id']).parent
        for number in range(6):
            key = f'choice-{number}'
            question = directory / f'question-{number}.txt'
            answer = directory / f'answer-{number}.txt'
            question.write_text(f'Question {number}?\n')
            answer.write_text(f'PRIVATE_LINKED_ANSWER_{number}\n')
            workers.event(home, task['id'], task['incarnation'], 'decision', key, question)
            supervision.reconcile(home)
            supervision.resolve(home, task['id'], key, answer)
        expected_path = directory / 'decisions/choice-5.answer.json'
        expected = hashlib.sha256(expected_path.read_bytes()).hexdigest()
        run_id, manifest = stage(home, cutoff=time.time() + 100)
        episode = next(e for e in manifest['episodes'] if e.get('task_id') == task['id'])
        assert episode['locator_count'] > len(episode['locators'])
        assert expected not in {row.get('sha256') for row in episode['locators']}
        seen, cursor, visited = set(), None, set()
        while True:
            args = ['reviewer', 'inspect', run_id, '--episode', episode['episode_id'], '--source', 'native']
            if cursor:
                args.extend(['--cursor', cursor])
            result = command(home, *args)
            bounded(result)
            seen.update(row['sha256'] for row in _nested_dicts(result)
                        if row.get('kind') == 'feedback' and isinstance(row.get('sha256'), str))
            cursor = result.get('next_cursor')
            if cursor is None:
                break
            assert cursor not in visited
            visited.add(cursor)
        assert expected in seen
        detail = command(home, 'reviewer', 'inspect', run_id, '--episode', episode['episode_id'],
                         '--source', 'native', '--record', expected, '--part', 'content')
        assert 'PRIVATE_LINKED_ANSWER_5' in json.dumps(detail)


def test_r1_tool_only_capture_does_not_open_frozen_skill_catalog():
    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        catalog = reviewer.paths(home)['catalogs'] / (manifest['catalog_id'] + '.json')
        unavailable = catalog.with_suffix('.unavailable')
        assert catalog.is_file()
        catalog.rename(unavailable)
        try:
            basis = capture(home, run_id, 'upstream vendor/tool contract observed')
            result = target(home, run_id, scope='distro', component='tool', basis_source_ids=(basis,),
                            authority_hint='external', authority_locator='vendor/tool')
            assert record(home, run_id, result)['identity']['authority'] == {
                'status': 'external', 'locator': 'vendor/tool'}
        finally:
            unavailable.rename(catalog)


def test_r1_deadline_bounds_wait_for_mutation_lock():
    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        episode_id = manifest['episodes'][0]['episode_id']
        bin_path = str(Path(__file__).resolve().parents[1] / 'bin')
        holder_program = (
            'import sys\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {bin_path!r})\nfrom agd_lib import store\n'
            'with store.lock(Path(sys.argv[1])):\n'
            ' print(\"locked\", flush=True)\n sys.stdin.read()\n')
        set_deadline(home, run_id, 1.5)
        holder = subprocess.Popen([sys.executable, '-c', holder_program, str(home)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        worker = None
        timed_out = False
        try:
            assert holder.stdout.readline().strip() == 'locked'
            program = (
                'import sys\nfrom pathlib import Path\n'
                f'sys.path.insert(0, {bin_path!r})\n'
                'from agd_lib import reviewer_inspection, store\n'
                'try:\n'
                ' reviewer_inspection.inspect(Path(sys.argv[1]), sys.argv[2], sys.argv[3], source=\"native\")\n'
                'except store.Refusal:\n sys.exit(3)\n'
                'sys.exit(0)\n')
            worker = subprocess.Popen([sys.executable, '-c', program, str(home), run_id, episode_id],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                worker.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            holder.stdin.close()
            holder.wait(timeout=5)
            holder.stdout.close()
            holder.stderr.close()
            if worker is not None and worker.poll() is None:
                worker.communicate(timeout=5)
        assert not timed_out
        assert worker.returncode == 3


@pytest.mark.parametrize('reader', ['target', 'journal'])
def test_r1_deadline_bounds_target_and_journal_git_revalidation(reader, monkeypatch):
    with lab() as (home, _):
        completed_session(home, role='worker', task_id='selected')
        run_id, _ = stage(home)
        issued = target(home, run_id, scope='project', project_id='sample', path='value.txt')
        assert record(home, run_id, issued)['guard']['state'] == 'present'
        tools = home / 'delayed-git-bin'
        tools.mkdir(mode=0o700)
        git = tools / 'git'
        git.write_text('#!/usr/bin/python3\nimport os, sys, time\n'
                       'open(os.environ[\"R1_GIT_PID\"], \"w\").write(str(os.getpid()))\n'
                       'time.sleep(6)\nsys.exit(3)\n')
        git.chmod(0o755)
        pid_file = home / 'r1-git.pid'
        monkeypatch.setenv('R1_GIT_PID', str(pid_file))
        monkeypatch.setenv('PATH', str(tools) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'))
        set_deadline(home, run_id, 1.2)
        started = time.monotonic()
        with pytest.raises(store.Refusal):
            if reader == 'target':
                inspection().read_target(home, run_id, issued['target_ref'])
            else:
                inspection().read_journal(home, run_id)
        elapsed = time.monotonic() - started
        assert_child_gone(pid_file)
        assert elapsed < 4


def test_r1_slow_query_does_not_block_home_mutation_or_publish_after_close(monkeypatch):
    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        pid_file = telemetry_fixture(home, monkeypatch, episode, mode='barrier')
        release = home / 'release-query'
        monkeypatch.setenv('INSPECTION_RELEASE', str(release))
        before = inspection().read_journal(home, run_id)
        bin_path = str(Path(__file__).resolve().parents[1] / 'bin')
        query_program = (
            'import sys\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {bin_path!r})\n'
            'from agd_lib import observability, reviewer_inspection, store\n'
            'observability.langfuse_query_context = lambda *a, **k: {'
            '\"base_url\":\"http://127.0.0.1:9\", \"project_id\":\"synthetic-project\",'
            '\"public_key\":\"pk_fixture\", \"secret_key\":\"sk_fixture\"}\n'
            'try:\n'
            ' reviewer_inspection.inspect(Path(sys.argv[1]), sys.argv[2], sys.argv[3], source=\"langfuse\")\n'
            'except store.Refusal:\n sys.exit(3)\n'
            'sys.exit(0)\n')
        query = subprocess.Popen([sys.executable, '-c', query_program, str(home), run_id, episode['episode_id']],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        mutator = closer = None
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline and query.poll() is None:
                time.sleep(0.02)
            assert pid_file.exists(), 'synthetic query never started'
            mutate_program = (
                'import sys\nfrom pathlib import Path\n'
                f'sys.path.insert(0, {bin_path!r})\n'
                'from agd_lib import reviewer\n'
                'reviewer.enqueue_intent(Path(sys.argv[1]), kind=\"manual\")\n')
            mutator = subprocess.Popen([sys.executable, '-c', mutate_program, str(home)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                _, stderr = mutator.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pytest.fail('unrelated home mutation blocked by in-flight inspection')
            assert mutator.returncode == 0, stderr
            assert query.poll() is None
            close_program = (
                'import sys\nfrom pathlib import Path\n'
                f'sys.path.insert(0, {bin_path!r})\n'
                'from agd_lib import reviewer\n'
                'reviewer.close_run(Path(sys.argv[1]), sys.argv[2], \"failed\")\n')
            closer = subprocess.Popen([sys.executable, '-c', close_program, str(home), run_id],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                _, stderr = closer.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pytest.fail('closing the run blocked by in-flight inspection')
            assert closer.returncode == 0, stderr
            release.touch()
            _, stderr = query.communicate(timeout=10)
            assert query.returncode == 3, stderr
            assert_child_gone(pid_file)
            after = inspection().read_journal(home, run_id)
            assert after['sources'] == before['sources']
            assert after['targets'] == before['targets']
            assert after['cursors'] == before['cursors']
        finally:
            release.touch()
            for process in (query, mutator, closer):
                if process is not None and process.poll() is None:
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)


def test_r1_skill_catalog_metadata_pagination_has_only_disclosed_sources():
    with lab() as (home, _):
        directory = home / 'config/worker-skills'
        directory.mkdir(parents=True, exist_ok=True)
        names = set()
        for number in range(45):
            skill_id = f'long-{number:02d}-' + 'q' * 39
            names.add(skill_id)
            skill_dir = directory / skill_id
            skill_dir.mkdir()
            (skill_dir / 'SKILL.md').write_text(
                f'---\nname: {skill_id}\ndescription: Local fixture\n---\nInspect the implicated behavior.\n')
        completed_session(home)
        run_id, _ = stage(home)
        baseline = inspection().read_journal(home, run_id)
        sources, seen, cursor = set(), set(), None
        while True:
            try:
                page = inspection().list_skills(home, run_id, cursor=cursor)
            except store.Refusal:
                journal = inspection().read_journal(home, run_id)
                assert set(journal['sources']) == set(baseline['sources']) | sources
                raise
            bounded(page)
            sources.add(page['source_id'])
            seen.update(row['id'] for row in page['rows'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert names <= seen
        journal = inspection().read_journal(home, run_id)
        assert set(journal['sources']) == set(baseline['sources']) | sources


def test_r1_retirement_quiesces_or_revalidates_inflight_capture(monkeypatch):
    import select
    with lab() as (home, _):
        hook = home / 'config/hooks/retirement-hook'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('owned hook\n')
        completed_session(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        pid_file = telemetry_fixture(home, monkeypatch, episode, mode='barrier')
        release = home / 'release-before-retirement'
        monkeypatch.setenv('INSPECTION_RELEASE', str(release))
        root = Path(__file__).resolve().parents[1]
        query_program = (
            'import json, sys\nfrom pathlib import Path\n'
            f"sys.path.insert(0, {str(root / 'bin')!r})\n"
            'from agd_lib import observability, reviewer_inspection, store\n'
            'observability.langfuse_query_context = lambda *a, **k: {'
            '\"base_url\":\"http://127.0.0.1:9\", \"project_id\":\"synthetic-project\",'
            '\"public_key\":\"pk_fixture\", \"secret_key\":\"sk_fixture\"}\n'
            'try:\n'
            ' value = reviewer_inspection.inspect(Path(sys.argv[1]), sys.argv[2], sys.argv[3], source=\"langfuse\")\n'
            'except store.Refusal:\n sys.exit(3)\n'
            'print(json.dumps(value))\n')
        retirement_program = (
            'import sys\nfrom pathlib import Path\n'
            f"sys.path.insert(0, {str(root / 'tests')!r})\n"
            f"sys.path.insert(0, {str(root / 'bin')!r})\n"
            'from support import uninstall_scope\nfrom agd_lib import store\n'
            'print(\"starting\", flush=True)\n'
            'sys.stdin.read(1)\n'
            'try:\n'
            ' with uninstall_scope(Path(sys.argv[1]), []):\n'
            '  print(\"entered\", flush=True)\n  sys.stdin.read()\n'
            'except store.Refusal:\n print(\"busy\", flush=True)\n')
        query = subprocess.Popen([sys.executable, '-c', query_program, str(home), run_id, episode['episode_id']],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        retiring = None
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline and query.poll() is None:
                time.sleep(0.02)
            assert pid_file.exists(), 'synthetic query never started'
            retiring = subprocess.Popen([sys.executable, '-c', retirement_program, str(home)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert retiring.stdout.readline().strip() == 'starting'
            retiring.stdin.write('x')
            retiring.stdin.flush()
            early = ''
            if select.select([retiring.stdout], [], [], 0.5)[0]:
                early = retiring.stdout.readline().strip()
            evidence_dir = reviewer.paths(home)['evidence'] / 'inspection' / run_id
            before = {p.name for p in evidence_dir.glob('*.json')} if evidence_dir.exists() else set()
            release.touch()
            _, query_error = query.communicate(timeout=10)
            assert_child_gone(pid_file)
            if early == 'entered':
                assert query.returncode == 3, query_error
                after = {p.name for p in evidence_dir.glob('*.json')} if evidence_dir.exists() else set()
                assert after == before
            else:
                assert query.returncode in (0, 3), query_error
                if not early:
                    assert select.select([retiring.stdout], [], [], 10)[0]
                    early = retiring.stdout.readline().strip()
                if early == 'busy':
                    retiring.stdin.close()
                    retiring.wait(timeout=10)
                    retiring = subprocess.Popen([sys.executable, '-c', retirement_program, str(home)],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, text=True)
                    assert retiring.stdout.readline().strip() == 'starting'
                    retiring.stdin.write('x')
                    retiring.stdin.flush()
                    assert select.select([retiring.stdout], [], [], 10)[0]
                    early = retiring.stdout.readline().strip()
                assert early == 'entered'
            with pytest.raises(store.Refusal):
                target(home, run_id, scope='distro', path='config/hooks/retirement-hook')
        finally:
            release.touch()
            if query.poll() is None:
                try:
                    query.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    query.kill()
                    query.communicate(timeout=5)
            if retiring is not None:
                if retiring.stdin and not retiring.stdin.closed:
                    retiring.stdin.close()
                if retiring.poll() is None:
                    retiring.wait(timeout=10)


def test_r1_copied_native_source_is_not_issued_in_another_run():
    with lab() as (home, _):
        source, _, _ = completed_session(home, texts=('shared episode output',))
        first, manifest = stage(home)
        episode = manifest['episodes'][0]
        page = inspection().inspect(home, first, episode['episode_id'], source='native',
                                    record_id=source['session_id'] + '-final-0', part='content')
        first_dir = reviewer.paths(home)['evidence'] / 'inspection' / first
        original = json.loads((first_dir / (page['source_id'] + '.json')).read_text())
        second, second_manifest = stage(home)
        assert any(item['episode_id'] == episode['episode_id'] for item in second_manifest['episodes'])
        copied = json.loads(json.dumps(original))
        copied['run_id'] = second
        copied['manifest_id'] = second_manifest['manifest_id']
        second_dir = reviewer.paths(home)['evidence'] / 'inspection' / second
        _fixture_record(second_dir, copied)
        with pytest.raises(store.Refusal):
            inspection().read_journal(home, second)


def _inspection_record_bytes(home, run_id):
    roots = [reviewer.paths(home)['inspections'] / run_id,
             reviewer.paths(home)['evidence'] / 'inspection' / run_id]
    return {str(path.relative_to(home)): path.read_bytes()
            for root in roots if root.exists()
            for path in root.rglob('*.json') if path.is_file()}


def test_r1_oversized_historical_skill_envelope_never_commits_an_undelivered_page():
    with lab() as (home, _):
        skill = next(row for row in worker_skills.catalog(home) if row['id'] == 'systematic-debugging')
        (Path(skill['source']) / 'SKILL.md').write_text(
            '---\nname: systematic-debugging\ndescription: Fixture\n---\n' + '\x00' * 2400)
        completed_session(home, role='worker', task_id='catalog-owner')
        run_id, manifest = stage(home)
        catalog_path = reviewer.paths(home)['catalogs'] / (manifest['catalog_id'] + '.json')
        catalog = json.loads(catalog_path.read_text())
        assignment = next(row for row in catalog['assigned'] if row['task_id'] == 'catalog-owner')
        snapshot = next(row for row in assignment['skills'] if row['id'] == 'systematic-debugging')
        # Adversarial immutable input: distinct historical assignment records
        # whose snapshots are no longer available. This exercises refusal or
        # bounded disclosure, not an assumption that today’s selector emits it.
        for number in range(130):
            task_id = f'history-{number:03d}-' + 'q' * 32
            catalog['assigned'].append({'task_id': task_id, 'skills': [snapshot], 'usage': 'unknown'})
        assert len(json.dumps(catalog['assigned'], indent=2).encode()) > 16384
        catalog_id = reviewer._digest(catalog)
        reviewer.publish_immutable(reviewer.paths(home)['catalogs'] / (catalog_id + '.json'), catalog)
        frozen = {key: value for key, value in manifest.items() if key != 'manifest_id'}
        frozen['catalog_id'] = catalog_id
        frozen['catalog_bytes'] = len(reviewer._json_bytes(catalog))
        manifest_id = reviewer._digest(frozen)
        reviewer.publish_immutable(reviewer.paths(home)['selections'] / (manifest_id + '.json'), frozen)
        receipt_path = reviewer.paths(home)['receipts'] / (run_id + '.json')
        receipt = json.loads(receipt_path.read_text())
        receipt['manifest_id'] = manifest_id
        store.atomic_json(receipt_path, receipt)
        before = _inspection_record_bytes(home, run_id)
        try:
            response = inspection().show_skill(home, run_id, 'systematic-debugging')
        except store.Refusal:
            assert _inspection_record_bytes(home, run_id) == before
            seed = len(json.dumps({**frozen, 'manifest_id': manifest_id}, indent=2,
                                  ensure_ascii=False).encode()) + len(reviewer._json_bytes(catalog))
            remaining = reviewer.read_config(home)['limits']['max_input_bytes'] - seed
            assert remaining > 0
            full = capture(home, run_id, 'z' * remaining)
            assert reviewer.read_evidence(home, run_id)['sources'][full]['bytes'] == remaining
        else:
            bounded(response)
            assert response.get('next_cursor') or response.get('limitations')
