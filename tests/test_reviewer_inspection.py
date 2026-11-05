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
from test_reviewer import append_unfinished, build_report, completed_session, message, produced_run, rewrite_title
from test_langfuse_cli import _package_files

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import langfuse_cli, observability, reviewer, store, supervision, tasks, worker_skills, workers


def inspection():
    # Import lazily so the owner's red run can report each missing behavior.
    from agd_lib import reviewer_inspection
    return reviewer_inspection


def stage(home, *, cutoff=200, limit=None, max_candidates=None, detail=()):
    config = reviewer.read_config(home)
    if limit is not None:
        config['limits']['max_input_bytes'] = limit
    if max_candidates is not None:
        config['limits']['max_candidates'] = max_candidates
    manifest = reviewer.select_manifest(home, cutoff=cutoff, config_snapshot=config,
                                        detail_episode_ids=detail)
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
        journal = inspection().read_journal(home, run_id)
        assert issued['target_ref'] in journal['targets']
        assert forged_target not in journal['targets']
        assert forged_source not in journal['sources']
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='forged-component',
                   basis_source_ids=(forged_source,))


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
        forged_id = _fixture_record(source_dir, forged)
        journal = inspection().read_journal(home, run_id)
        assert first['source_id'] in journal['sources']
        assert forged_id not in journal['sources']
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='unexposed-prompt',
                   basis_source_ids=(forged_id,))


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
        forged_id = _fixture_record(second_dir, copied)
        journal = inspection().read_journal(home, second)
        assert forged_id not in journal['sources']
        with pytest.raises(store.Refusal):
            target(home, second, scope='distro', component='copied-native-source',
                   basis_source_ids=(forged_id,))


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


@pytest.mark.parametrize('interruption', ['sources', 'cursors', 'receipt'])
def test_r2_interrupted_issuance_preserves_prior_records_and_budget(interruption, monkeypatch):
    with lab() as (home, _):
        hooks = home / 'config/hooks'
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / 'prior-hook').write_text('previously admitted target\n')
        (hooks / 'pending-hook').write_text('PENDING_PAGE_' + 'q' * 5000)
        source, _, _ = completed_session(home, texts=('previously exposed ' + 'n' * 2500,))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        prior_page = inspection().inspect(
            home, run_id, episode['episode_id'], source='native',
            record_id=source['session_id'] + '-final-0', part='content')
        assert prior_page['next_cursor']
        prior_target = target(home, run_id, scope='distro', path='config/hooks/prior-hook')
        baseline = inspection().read_journal(home, run_id)
        receipt_path = reviewer.paths(home)['receipts'] / (run_id + '.json')
        receipt_before = json.loads(receipt_path.read_text())
        directories = {
            'sources': reviewer.paths(home)['evidence'] / 'inspection' / run_id,
            'cursors': reviewer.paths(home)['inspections'] / run_id / 'cursors',
            'targets': reviewer.paths(home)['inspections'] / run_id / 'targets',
        }
        written = {category: [] for category in directories}
        publish = reviewer.publish_immutable
        atomic_json = store.atomic_json

        def interrupted_publish(path, value):
            publish(path, value)
            path = Path(path)
            for category, directory in directories.items():
                if path.parent == directory:
                    written[category].append(path.stem)
                    if interruption == category:
                        raise OSError('injected interruption after immutable publication')

        def interrupted_receipt(path, value):
            if interruption == 'receipt' and Path(path) == receipt_path:
                raise OSError('injected interruption before receipt update')
            return atomic_json(path, value)

        with monkeypatch.context() as fault:
            fault.setattr(reviewer, 'publish_immutable', interrupted_publish)
            fault.setattr(store, 'atomic_json', interrupted_receipt)
            with pytest.raises(OSError):
                target(home, run_id, scope='distro', path='config/hooks/pending-hook')

        expected = {
            'sources': {'sources'},
            'cursors': {'sources', 'cursors'},
            'receipt': {'sources', 'cursors', 'targets'},
        }[interruption]
        assert {category for category, ids in written.items() if ids} == expected
        assert json.loads(receipt_path.read_text()) == receipt_before
        for category, ids in written.items():
            assert all((directories[category] / (ident + '.json')).is_file() for ident in ids)

        # Files written by the interrupted call were never disclosed or issued.
        journal = inspection().read_journal(home, run_id)
        assert journal == baseline
        assert record(home, run_id, prior_target)['target_ref'] == prior_target['target_ref']
        for category, ids in written.items():
            assert not set(ids).intersection(journal[category])
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='undisclosed-source',
                   basis_source_ids=(written['sources'][0],))
        if written['cursors']:
            with pytest.raises(store.Refusal):
                target(home, run_id, scope='distro', path='config/hooks/pending-hook',
                       cursor=written['cursors'][0])
        if written['targets']:
            with pytest.raises(store.Refusal):
                inspection().read_target(home, run_id, written['targets'][0])

        # A fresh publication must work without deleting or adopting the orphans.
        recovered = target(home, run_id, scope='distro', path='config/hooks/pending-hook')
        assert recovered['text'].startswith('PENDING_PAGE_')
        assert recovered['next_cursor']
        assert record(home, run_id, recovered)['guard']['state'] == 'present'
        journal = inspection().read_journal(home, run_id)
        for category, ids in written.items():
            assert not set(ids).intersection(journal[category])
            assert all((directories[category] / (ident + '.json')).is_file() for ident in ids)
        assert prior_page['source_id'] in journal['sources']
        assert prior_page['next_cursor'] in journal['cursors']
        assert prior_target['target_ref'] in journal['targets']
        assert recovered['target_ref'] in journal['targets']

        # Spend the exact remaining consumer-visible budget. Orphan bytes do not
        # count; the next unique supplementary input must then be refused.
        used = len(json.dumps(manifest, ensure_ascii=False, indent=2).encode()) + manifest['catalog_bytes']
        used += sum(row['bytes'] for row in journal['sources'].values())
        used += sum(len(reviewer._json_bytes(row)) for row in journal['targets'].values())
        remaining = reviewer.read_config(home)['limits']['max_input_bytes'] - used
        assert remaining > 0
        full = capture(home, run_id, 'x' * remaining)
        assert reviewer.read_evidence(home, run_id)['sources'][full]['bytes'] == remaining
        with pytest.raises(store.Refusal):
            capture(home, run_id, 'one extra byte')


@pytest.mark.parametrize('category', ['sources', 'cursors', 'targets'])
def test_r2_missing_issued_record_still_refuses(category):
    with lab() as (home, _):
        hooks = home / 'config/hooks'
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / 'issued-hook').write_text('issued data ' + 'p' * 5000)
        completed_session(home)
        run_id, _ = stage(home)
        issued = target(home, run_id, scope='distro', path='config/hooks/issued-hook')
        assert issued['next_cursor']
        paths = {
            'sources': (reviewer.paths(home)['evidence'] / 'inspection' / run_id, issued['source_ids'][0]),
            'cursors': (reviewer.paths(home)['inspections'] / run_id / 'cursors', issued['next_cursor']),
            'targets': (reviewer.paths(home)['inspections'] / run_id / 'targets', issued['target_ref']),
        }
        directory, ident = paths[category]
        (directory / (ident + '.json')).unlink()
        with pytest.raises(store.Refusal):
            inspection().read_journal(home, run_id)
        with pytest.raises(store.Refusal):
            inspection().read_target(home, run_id, issued['target_ref'])


@pytest.mark.parametrize('collision', ['identical', 'wrong-bytes', 'symlink'])
def test_r2_interrupted_target_identical_retry_or_unsafe_collision(collision, monkeypatch):
    with lab() as (home, _):
        hooks = home / 'config/hooks'
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / 'existing-hook').write_text('previously admitted target\n')
        source, _, _ = completed_session(home, texts=('previously exposed native answer',))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        prior_source = inspection().inspect(
            home, run_id, episode['episode_id'], source='native',
            record_id=source['session_id'] + '-final-0', part='content')
        prior_target = target(home, run_id, scope='distro', path='config/hooks/existing-hook')
        baseline = inspection().read_journal(home, run_id)
        receipt_path = reviewer.paths(home)['receipts'] / (run_id + '.json')
        target_dir = reviewer.paths(home)['inspections'] / run_id / 'targets'
        original_publish = reviewer.publish_immutable
        original_atomic = store.atomic_json
        written = []

        # Freeze only after selection and prior issuance, so a newly observed
        # absence has exactly the same complete record on a subsequent retry.
        fixed_time = time.time()
        monkeypatch.setattr(inspection().time, 'time', lambda: fixed_time)

        def record_publication(path, value):
            original_publish(path, value)
            path = Path(path)
            if path.parent == target_dir:
                written.append(path.stem)

        def interrupt_receipt(path, value):
            if Path(path) == receipt_path:
                raise OSError('injected interruption before receipt update')
            return original_atomic(path, value)

        with monkeypatch.context() as fault:
            fault.setattr(reviewer, 'publish_immutable', record_publication)
            fault.setattr(store, 'atomic_json', interrupt_receipt)
            with pytest.raises(OSError):
                target(home, run_id, scope='distro', path='config/hooks/new-hook', operation='create')

        assert len(written) == 1
        orphan_ref = written[0]
        orphan_path = target_dir / (orphan_ref + '.json')
        original = orphan_path.read_bytes()
        assert reviewer._digest(json.loads(original)) == orphan_ref
        assert not (hooks / 'new-hook').exists()
        assert inspection().read_journal(home, run_id) == baseline
        assert record(home, run_id, prior_target)['target_ref'] == prior_target['target_ref']
        with pytest.raises(store.Refusal):
            inspection().read_target(home, run_id, orphan_ref)

        if collision == 'identical':
            # A new request must independently recheck absence, not trust the
            # unissued file merely because its digest matches.
            (hooks / 'new-hook').write_text('the formerly absent file now exists')
            with pytest.raises(store.Refusal):
                target(home, run_id, scope='distro', path='config/hooks/new-hook', operation='create')
            (hooks / 'new-hook').unlink()
            assert inspection().read_journal(home, run_id) == baseline
            retried = target(home, run_id, scope='distro', path='config/hooks/new-hook', operation='create')
            assert retried['target_ref'] == orphan_ref
            assert retried['guard']['state'] == 'absent'
            assert retried['guard']['observed_at'] == fixed_time
            assert orphan_path.read_bytes() == original
            assert record(home, run_id, retried)['target_ref'] == orphan_ref
            after = inspection().read_journal(home, run_id)
            assert after['sources'] == baseline['sources']
            assert after['cursors'] == baseline['cursors']
            assert set(after['targets']) == set(baseline['targets']) | {orphan_ref}
            assert prior_source['source_id'] in after['sources']
            assert prior_target['target_ref'] in after['targets']
        else:
            if collision == 'wrong-bytes':
                orphan_path.write_bytes(b'not the requested immutable record')
            else:
                backing = home / 'unsafe-collision-backing.json'
                backing.write_bytes(original)
                orphan_path.unlink()
                orphan_path.symlink_to(backing)
            with pytest.raises(store.Refusal):
                target(home, run_id, scope='distro', path='config/hooks/new-hook', operation='create')
            assert inspection().read_journal(home, run_id) == baseline
            assert record(home, run_id, prior_target)['target_ref'] == prior_target['target_ref']
            with pytest.raises(store.Refusal):
                inspection().read_target(home, run_id, orphan_ref)


def report_v2_inputs(home, run_id, manifest, native_source):
    episode = next(item for item in manifest['episodes'] if item['session_id'] == native_source['session_id'])
    eid = episode['episode_id']
    prompt = inspection().inspect(home, run_id, eid, source='native',
                                  record_id=native_source['session_id'] + '-prompt-0', part='content')
    final = inspection().inspect(home, run_id, eid, source='native',
                                 record_id=native_source['session_id'] + '-final-0', part='content')
    row = {'episode_id': eid, 'assessment': 'reflected',
           'intent_sources': [prompt['source_id']], 'action_sources': [final['source_id']],
           'outcome_sources': [final['source_id']], 'limitations': []}
    return row, prompt['source_id'], final['source_id']


def report_v2_finding(row, ref, citation, scope):
    return {'episode_ids': [row['episode_id']], 'target_ref': ref,
            'finding': 'The observed result omitted validation.',
            'proposed_change': 'Check the result before reporting.',
            'expected_benefit': 'Failures become visible.', 'applicability': scope,
            'scope_rationale': 'The check transfers across executions.' if scope == 'distro' else 'This is a project check.',
            'citations': [citation], 'counterexamples': [],
            'alternatives': 'Use manual review.', 'uncertainty': 'No counterexample was available.',
            'validation': 'Exercise the check on a failed result.',
            'validation_limits': ['Causality requires human assessment.'],
            'risks': ['The check may reject valid results.'], 'draft': None}


def report_v2(row, findings=()):
    return {'schema': 2, 'summary': 'Bounded review.', 'episode_reviews': [row],
            'findings': list(findings)}


@pytest.mark.parametrize('kind', ['script', 'agents', 'hook', 'component', 'new-file'])
def test_report_v2_general_destination_accepts_issued_target(kind):
    with lab() as (home, project):
        project_files(project)
        hook = home / 'config' / 'hooks' / 'report-hook.sh'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('printf hook\n')
        native, _, _ = completed_session(home, role='worker', task_id='reviewed', texts=('PRIVATE_NATIVE_SENTINEL',))
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        if kind in ('script', 'agents', 'new-file'):
            path = {'script': 'check.sh', 'agents': 'AGENTS.md', 'new-file': 'new-check.sh'}[kind]
            result = target(home, run_id, scope='project', project_id='sample', path=path,
                            operation='create' if kind == 'new-file' else 'modify')
            scope = 'project'
        elif kind == 'hook':
            result = target(home, run_id, scope='distro', path='config/hooks/report-hook.sh')
            scope = 'distro'
        else:
            result = target(home, run_id, scope='distro', component='model-routing',
                            basis_source_ids=(citation,), authority_hint='unknown')
            scope = 'distro'
        finding = report_v2_finding(row, result['target_ref'], citation, scope)
        submitted = reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row, [finding]))
        stored = json.loads(reviewer.report_path(home, submitted['report_id']).read_text())
        assert stored['report'] == report_v2(row, [finding])
        assert len(submitted['proposal_ids']) == 1
        assert 'PRIVATE_NATIVE_SENTINEL' not in json.dumps(stored)
        reviewer.finalize_run(home, run_id, 'exited')


@pytest.mark.parametrize('invalid', ['root-key', 'row-key', 'finding-key', 'missing-role',
                                     'metadata-only', 'manifest-citation', 'unknown-target',
                                     'unreflected', 'unknown-episode', 'bad-draft'])
def test_report_v2_reflection_rejects_unadmitted_or_invalid_input(invalid):
    with lab() as (home, _):
        native, _, _ = completed_session(home, texts=('observed',))
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        metadata = inspection().inspect(home, run_id, row['episode_id'], source='native')['source_id']
        dest = target(home, run_id, scope='distro', component='model-routing',
                      basis_source_ids=(citation,), authority_hint='unknown')
        finding = report_v2_finding(row, dest['target_ref'], citation, 'distro')
        report = report_v2(row, [finding])
        if invalid == 'root-key':
            report['extra'] = True
        elif invalid == 'row-key':
            row['extra'] = True
        elif invalid == 'finding-key':
            finding['target_source_version'] = 'untrusted'
        elif invalid == 'missing-role':
            row['intent_sources'] = []
        elif invalid == 'metadata-only':
            row.update(intent_sources=[metadata], action_sources=[metadata], outcome_sources=[metadata])
        elif invalid == 'manifest-citation':
            finding['citations'] = [manifest['manifest_id']]
        elif invalid == 'unknown-target':
            finding['target_ref'] = '0' * 64
        elif invalid == 'unreflected':
            row['assessment'] = 'screened'
        elif invalid == 'unknown-episode':
            finding['episode_ids'] = ['0' * 64]
        else:
            finding['draft'] = {'not': 'text'}
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report)


def test_report_v2_frozen_commit_refuses_late_capture_and_target():
    with lab() as (home, _):
        native, _, _ = completed_session(home)
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))
        with pytest.raises(store.Refusal):
            inspection().inspect(home, run_id, row['episode_id'], source='native')
        with pytest.raises(store.Refusal):
            target(home, run_id, scope='distro', component='tool',
                   basis_source_ids=(citation,), authority_hint='unknown')
        with pytest.raises(store.Refusal):
            capture(home, run_id, 'late capture')


def test_report_v2_target_guard_revision_uses_cross_run_native_evidence():
    with lab() as (home, _):
        hook = home / 'config' / 'hooks' / 'guard.sh'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('first version\n')
        native, _, _ = completed_session(home, texts=('unchanged evidence',))
        first_run, first_manifest = stage(home)
        first_row, _, first_citation = report_v2_inputs(home, first_run, first_manifest, native)
        first_target = target(home, first_run, scope='distro', path='config/hooks/guard.sh')
        first_guard = record(home, first_run, first_target)['guard']['source_sha256']
        second_run, second_manifest = stage(home)
        second_row, _, second_citation = report_v2_inputs(home, second_run, second_manifest, native)
        first = reviewer.submit_report(home, first_run, str(uuid.uuid4()),
                                       report_v2(first_row, [report_v2_finding(first_row, first_target['target_ref'], first_citation, 'distro')]))
        reviewer.finalize_run(home, first_run, 'exited')
        old_report = reviewer.report_path(home, first['report_id']).read_bytes()
        hook.write_text('second version\n')
        assert first_citation != second_citation
        second_target = target(home, second_run, scope='distro', path='config/hooks/guard.sh')
        assert first_guard != record(home, second_run, second_target)['guard']['source_sha256']
        second = reviewer.submit_report(home, second_run, str(uuid.uuid4()),
                                        report_v2(second_row, [report_v2_finding(second_row, second_target['target_ref'], second_citation, 'distro')]))
        assert second['proposal_ids'] == first['proposal_ids']
        proposal = reviewer.read_proposal(home, first['proposal_ids'][0])
        assert len(proposal['revisions']) == 2
        assert reviewer.report_path(home, first['report_id']).read_bytes() == old_report


def test_report_v2_reflection_revalidates_native_after_exposure():
    with lab() as (home, _):
        native, path, _ = completed_session(home, texts=('original answer',))
        run_id, manifest = stage(home)
        row, _, _ = report_v2_inputs(home, run_id, manifest, native)
        path.write_text(path.read_text().replace('original answer', 'rewritten answer'))
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))
        episode = manifest['episodes'][0]
        assert not reviewer._episode_progress(home, episode['episode_id'], episode['basis'])['reflected']


def test_report_v2_destination_rejects_foreign_run_target():
    with lab() as (home, _):
        native, _, _ = completed_session(home)
        first, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, first, manifest, native)
        foreign = target(home, first, scope='distro', component='model-routing',
                         basis_source_ids=(citation,), authority_hint='unknown')
        second, other_manifest = stage(home)
        other_row, _, other_citation = report_v2_inputs(home, second, other_manifest, native)
        finding = report_v2_finding(other_row, foreign['target_ref'], other_citation, 'distro')
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, second, str(uuid.uuid4()), report_v2(other_row, [finding]))


@pytest.mark.parametrize('assessment', ['screened', 'limited', 'unassessed'])
def test_report_v2_coverage_screening_and_hold_do_not_claim_reflection(assessment):
    with lab() as (home, _):
        completed_session(home)
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        metadata = inspection().inspect(home, run_id, episode['episode_id'], source='native')['source_id']
        row = {'episode_id': episode['episode_id'], 'assessment': assessment,
               'intent_sources': [metadata], 'action_sources': [], 'outcome_sources': [],
               'limitations': ['Native outcome unavailable.'] if assessment == 'limited' else []}
        report = {'schema': 2, 'summary': 'Screened.', 'episode_reviews': [] if assessment == 'unassessed' else [row],
                  'findings': []}
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report)
        before = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert not before['screened'] and not before['reflected']
        reviewer.finalize_run(home, run_id, 'exited')
        progress = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert not progress['reflected']
        assert progress['screened'] == (assessment != 'unassessed')
        assert bool(progress['hold']) == (assessment == 'limited')
        subsequent = reviewer.select_manifest(home, cutoff=201, config_snapshot=reviewer.read_config(home))
        selected = {item['episode_id'] for item in subsequent['episodes']}
        if assessment == 'limited':
            assert episode['episode_id'] not in selected
            assert any(item['episode_id'] == episode['episode_id'] for item in subsequent['held'])
            detail = reviewer.select_manifest(home, cutoff=201, config_snapshot=reviewer.read_config(home),
                                              detail_episode_ids=(episode['episode_id'],))
            assert episode['episode_id'] in {item['episode_id'] for item in detail['episodes']}
        else:
            assert episode['episode_id'] in selected


@pytest.mark.parametrize('outcome', ['failed', 'unknown', 'no-work', 'timeout'])
def test_report_v2_coverage_non_success_never_consumes(outcome):
    with lab() as (home, _):
        native, _, _ = completed_session(home)
        run_id, manifest = stage(home)
        row, _, _ = report_v2_inputs(home, run_id, manifest, native)
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))
        reviewer.close_run(home, run_id, 'failed' if outcome == 'timeout' else outcome,
                           error_code='timeout' if outcome == 'timeout' else None)
        episode = manifest['episodes'][0]
        progress = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert not progress['screened'] and not progress['reflected'] and not progress['ranges']


@pytest.mark.parametrize('boundary', ['prepared', 'report', 'receipt', 'progress', 'event'])
def test_report_v2_coverage_recovers_once_at_write_boundaries(monkeypatch, boundary):
    with lab() as (home, _):
        native, _, _ = completed_session(home)
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        dest = target(home, run_id, scope='distro', component='model-routing',
                      basis_source_ids=(citation,), authority_hint='unknown')
        report = report_v2(row, [report_v2_finding(row, dest['target_ref'], citation, 'distro')])
        request = str(uuid.uuid4())
        original_publish, original_atomic = reviewer.publish_immutable, store.atomic_json
        fired = False
        def publish(path, value):
            nonlocal fired
            original_publish(path, value)
            if not fired and boundary == 'prepared' and Path(path).parent == reviewer.paths(home)['prepared']:
                fired = True
                raise RuntimeError(boundary)
            if not fired and boundary == 'report' and Path(path).parent == reviewer.paths(home)['reports']:
                fired = True
                raise RuntimeError(boundary)
        def atomic(path, value):
            nonlocal fired
            original_atomic(path, value)
            path = Path(path)
            match = ((boundary == 'receipt' and path == reviewer.paths(home)['receipts'] / (run_id + '.json')
                      and value.get('dispatch_state') == 'report-committed') or
                     (boundary == 'progress' and reviewer.paths(home)['progress'] in path.parents) or
                     (boundary == 'event' and path.parent == home / 'state' / 'events'))
            if not fired and match:
                fired = True
                raise RuntimeError(boundary)
        monkeypatch.setattr(reviewer, 'publish_immutable', publish)
        monkeypatch.setattr(store, 'atomic_json', atomic)
        with pytest.raises(RuntimeError):
            reviewer.submit_report(home, run_id, request, report)
            reviewer.finalize_run(home, run_id, 'exited')
        assert fired
        monkeypatch.setattr(reviewer, 'publish_immutable', original_publish)
        monkeypatch.setattr(store, 'atomic_json', original_atomic)
        reviewer.recover_submissions(home, run_id)
        result = reviewer.submit_report(home, run_id, request, report)
        reviewer.finalize_run(home, run_id, 'exited')
        episode = manifest['episodes'][0]
        progress = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert progress['reflected']
        assert len(progress['submissions']) == 1
        assert len(result['proposal_ids']) == 1
        assert len(supervision.pending(home)) == 1
        reviewer.finalize_run(home, run_id, 'exited')
        assert reviewer._episode_progress(home, episode['episode_id'], episode['basis']) == progress
        assert len(supervision.pending(home)) == 1


def test_report_v2_destination_authority_change_requires_supersession():
    with lab() as (home, _):
        native, _, _ = completed_session(home, texts=('vendor/tool observed failure',))
        first_run, first_manifest = stage(home)
        first_row, _, first_citation = report_v2_inputs(home, first_run, first_manifest, native)
        unknown = target(home, first_run, scope='distro', component='model-routing',
                         basis_source_ids=(first_citation,), authority_hint='unknown')
        second_run, second_manifest = stage(home)
        second_row, _, second_citation = report_v2_inputs(home, second_run, second_manifest, native)
        external = target(home, second_run, scope='distro', component='model-routing',
                          basis_source_ids=(second_citation,), authority_hint='external',
                          authority_locator='vendor/tool')
        first = reviewer.submit_report(home, first_run, str(uuid.uuid4()),
                                       report_v2(first_row, [report_v2_finding(first_row, unknown['target_ref'], first_citation, 'distro')]))
        reviewer.finalize_run(home, first_run, 'exited')
        second = reviewer.submit_report(home, second_run, str(uuid.uuid4()),
                                        report_v2(second_row, [report_v2_finding(second_row, external['target_ref'], second_citation, 'distro')]))
        reviewer.finalize_run(home, second_run, 'exited')
        assert first['proposal_ids'][0] != second['proposal_ids'][0]
        first_proposal = reviewer.read_proposal(home, first['proposal_ids'][0])
        second_proposal = reviewer.read_proposal(home, second['proposal_ids'][0])
        assert 'unknown' in json.dumps(first_proposal)
        assert 'external' in json.dumps(second_proposal)
        assert 'vendor/tool' in json.dumps(second_proposal)
        shown_report = command(home, 'reviewer', 'reports', 'show', second['report_id'])
        shown_proposal = command(home, 'reviewer', 'proposals', 'show', second['proposal_ids'][0])
        event = next(item for item in supervision.pending(home) if item['key'] == second_run)
        for displayed in (shown_report, shown_proposal, event['details']):
            rendered = json.dumps(displayed).casefold()
            assert 'distro' in rendered and 'external' in rendered and 'vendor/tool' in rendered
            assert 'vendor/tool observed failure' not in rendered
            assert 'upstream' in rendered or 'owner' in rendered or 'routing' in rendered
            assert '"locally_editable": true' not in rendered
        unknown_report = command(home, 'reviewer', 'reports', 'show', first['report_id'])
        unknown_proposal = command(home, 'reviewer', 'proposals', 'show', first['proposal_ids'][0])
        unknown_event = next(item for item in supervision.pending(home) if item['key'] == first_run)
        for displayed in (unknown_report, unknown_proposal, unknown_event['details']):
            rendered = json.dumps(displayed).casefold()
            assert 'distro' in rendered and 'unknown' in rendered
            assert 'owner' in rendered or 'routing' in rendered or 'confirmation' in rendered
            assert '"locally_editable": true' not in rendered
        assert reviewer.read_triage(home, first['proposal_ids'][0])['current'] != 'superseded'
        result = reviewer.dispose(home, first['proposal_ids'][0], 'superseded', 'Confirmed upstream ownership.',
                                  second['proposal_ids'][0], {'uid': os.getuid(), 'identity': str(os.getuid())})
        assert result['current'] == 'superseded'
        assert result['history'][-1]['replacement'] == second['proposal_ids'][0]


def test_report_v2_coverage_unions_successful_native_ranges_not_manual():
    with lab() as (home, _):
        text = 'bounded native content ' * 300
        native, _, _ = completed_session(home, texts=(text,))
        first_run, first_manifest = stage(home)
        second_run, second_manifest = stage(home)
        first_episode = first_manifest['episodes'][0]
        second_episode = second_manifest['episodes'][0]
        rid = native['session_id'] + '-final-0'
        first_page = inspection().inspect(home, first_run, first_episode['episode_id'],
                                          source='native', record_id=rid, part='content')
        assert first_page['next_cursor']
        manual = capture(home, first_run, text)
        prompt_first = inspection().inspect(home, first_run, first_episode['episode_id'], source='native',
                                            record_id=native['session_id'] + '-prompt-0', part='content')
        row = {'episode_id': first_episode['episode_id'], 'assessment': 'reflected',
               'intent_sources': [prompt_first['source_id']], 'action_sources': [first_page['source_id']],
               'outcome_sources': [first_page['source_id']], 'limitations': ['Outcome was partially exposed.']}
        dest = target(home, first_run, scope='distro', component='model-routing',
                      basis_source_ids=(manual,), authority_hint='unknown')
        finding = report_v2_finding(row, dest['target_ref'], manual, 'distro')
        reviewer.submit_report(home, first_run, str(uuid.uuid4()), report_v2(row, [finding]))
        reviewer.finalize_run(home, first_run, 'exited')
        scope = reviewer._source_scope(home, {'role': 'central', 'session_id': native['session_id']})
        material = reviewer._material_id(scope, {'message_id': rid, 'text': text})
        assert material not in reviewer._reviewed_material(home, scope)
        page = inspection().inspect(home, second_run, second_episode['episode_id'],
                                    source='native', record_id=rid, part='content')
        ids = [page['source_id']]
        while page['next_cursor']:
            page = inspection().inspect(home, second_run, second_episode['episode_id'], source='native',
                                        record_id=rid, part='content', cursor=page['next_cursor'])
            ids.append(page['source_id'])
        prompt = inspection().inspect(home, second_run, second_episode['episode_id'], source='native',
                                      record_id=native['session_id'] + '-prompt-0', part='content')
        second_row = {'episode_id': second_episode['episode_id'], 'assessment': 'reflected',
                      'intent_sources': [prompt['source_id']], 'action_sources': ids,
                      'outcome_sources': ids, 'limitations': []}
        reviewer.submit_report(home, second_run, str(uuid.uuid4()), report_v2(second_row))
        reviewer.finalize_run(home, second_run, 'exited')
        assert material in reviewer._reviewed_material(home, scope)


def test_report_v2_destination_project_context_is_episode_specific():
    with lab() as (home, _):
        command(home, 'project', 'add', 'other', '--source', str(home.parent / 'origin.git'))
        first_native, _, _ = completed_session(home, role='worker', task_id='first', project='sample')
        second_native, _, _ = completed_session(home, role='worker', task_id='second', project='other')
        run_id, manifest = stage(home)
        first_row, _, first_citation = report_v2_inputs(home, run_id, manifest, first_native)
        second_row, _, second_citation = report_v2_inputs(home, run_id, manifest, second_native)
        first_target = target(home, run_id, scope='project', project_id='sample', path='value.txt')
        second_target = target(home, run_id, scope='project', project_id='other', path='value.txt')
        wrong = report_v2_finding(first_row, second_target['target_ref'], first_citation, 'project')
        invalid = {'schema': 2, 'summary': 'Wrong project.', 'episode_reviews': [first_row, second_row],
                   'findings': [wrong]}
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), invalid)
        findings = [report_v2_finding(first_row, first_target['target_ref'], first_citation, 'project'),
                    report_v2_finding(second_row, second_target['target_ref'], second_citation, 'project')]
        result = reviewer.submit_report(home, run_id, str(uuid.uuid4()),
                                        {'schema': 2, 'summary': 'Two projects.',
                                         'episode_reviews': [first_row, second_row], 'findings': findings})
        assert len(set(result['proposal_ids'])) == 2


def test_report_v2_historical_report_and_proposal_bytes_unchanged():
    with lab() as (home, _):
        old_run, packet = produced_run(home)
        old = reviewer.submit_report(home, old_run['review_run_id'], str(uuid.uuid4()), build_report(packet))
        reviewer.finalize_run(home, old_run['review_run_id'], 'exited')
        old_report = reviewer.report_path(home, old['report_id'])
        old_proposal = reviewer.proposal_path(home, old['proposal_ids'][0])
        saved = old_report.read_bytes(), old_proposal.read_bytes()
        native, _, _ = completed_session(home, texts=('new schema evidence',))
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        dest = target(home, run_id, scope='distro', component='model-routing',
                      basis_source_ids=(citation,), authority_hint='unknown')
        reviewer.submit_report(home, run_id, str(uuid.uuid4()),
                               report_v2(row, [report_v2_finding(row, dest['target_ref'], citation, 'distro')]))
        reviewer.finalize_run(home, run_id, 'exited')
        assert (old_report.read_bytes(), old_proposal.read_bytes()) == saved


def test_report_v2_coverage_langfuse_content_never_marks_native_material(monkeypatch):
    with lab() as (home, _):
        native, _, _ = completed_session(home, texts=('native-answer',))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        telemetry_fixture(home, monkeypatch, episode)
        inspection().inspect(home, run_id, episode['episode_id'], source='langfuse')
        input_page = inspection().inspect(home, run_id, episode['episode_id'], source='langfuse',
                                          record_id='selected-observation', part='input')
        output_page = inspection().inspect(home, run_id, episode['episode_id'], source='langfuse',
                                           record_id='selected-observation', part='output')
        row = {'episode_id': episode['episode_id'], 'assessment': 'reflected',
               'intent_sources': [input_page['source_id']], 'action_sources': [output_page['source_id']],
               'outcome_sources': [output_page['source_id']], 'limitations': []}
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))
        reviewer.finalize_run(home, run_id, 'exited')
        scope = reviewer._source_scope(home, {'role': 'central', 'session_id': native['session_id']})
        material = reviewer._material_id(scope, {'message_id': native['session_id'] + '-final-0',
                                                  'text': 'native-answer'})
        assert material not in reviewer._reviewed_material(home, scope)


def test_report_v2_target_guard_rejects_tampered_issued_target():
    with lab() as (home, _):
        hook = home / 'config' / 'hooks' / 'guarded.sh'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('guarded content\n')
        native, _, _ = completed_session(home)
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        dest = target(home, run_id, scope='distro', path='config/hooks/guarded.sh')
        path = reviewer.paths(home)['inspections'] / run_id / 'targets' / (dest['target_ref'] + '.json')
        saved = json.loads(path.read_text())
        saved['guard']['source_sha256'] = None
        path.write_text(json.dumps(saved))
        finding = report_v2_finding(row, dest['target_ref'], citation, 'distro')
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row, [finding]))


def test_report_v2_destination_same_episode_disjoint_evidence_is_uncertain_distinct():
    with lab() as (home, _):
        hook = home / 'config' / 'hooks' / 'same.sh'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('hook\n')
        native, _, _ = completed_session(home, texts=('different outcome evidence',))
        first_run, first_manifest = stage(home)
        first_row, first_prompt, _ = report_v2_inputs(home, first_run, first_manifest, native)
        first_target = target(home, first_run, scope='distro', path='config/hooks/same.sh')
        second_run, second_manifest = stage(home)
        second_row, _, second_final = report_v2_inputs(home, second_run, second_manifest, native)
        second_target = target(home, second_run, scope='distro', path='config/hooks/same.sh')
        first = reviewer.submit_report(home, first_run, str(uuid.uuid4()),
                                       report_v2(first_row, [report_v2_finding(first_row, first_target['target_ref'], first_prompt, 'distro')]))
        reviewer.finalize_run(home, first_run, 'exited')
        second = reviewer.submit_report(home, second_run, str(uuid.uuid4()),
                                        report_v2(second_row, [report_v2_finding(second_row, second_target['target_ref'], second_final, 'distro')]))
        assert first['proposal_ids'][0] != second['proposal_ids'][0]
        assert reviewer.read_proposal(home, second['proposal_ids'][0])['uncertain_distinct']


def test_report_v2_reflection_revalidates_linked_intent_artifact():
    with lab() as (home, _):
        native, _, task = completed_session(home, role='worker', task_id='intent')
        brief = tasks.task_file(home, task['id']).parent / 'brief.md'
        brief.write_text('Original task intent.\n')
        run_id, manifest = stage(home, cutoff=time.time() + 100)
        row, _, _ = report_v2_inputs(home, run_id, manifest, native)
        episode = next(item for item in manifest['episodes'] if item['episode_id'] == row['episode_id'])
        locators = reviewer.read_selection_locators(home, manifest['manifest_id'], episode['episode_id'])['rows']
        intent = next(item for item in locators if item['kind'] == 'intent')
        page = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                    record_id=intent['sha256'], part='content')
        row['intent_sources'] = [page['source_id']]
        brief.write_text('Changed task intent.\n')
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))


@pytest.mark.parametrize('hint', ['unknown', 'external'])
def test_report_v2_destination_authority_resolution_requires_explicit_supersession(hint):
    with lab() as (home, _):
        locator = 'config/hooks/route.sh'
        hook = home / locator
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('native owned hook\n')
        native, _, _ = completed_session(home, texts=('upstream vendor/route failed',))
        first_run, first_manifest = stage(home)
        first_row, _, first_citation = report_v2_inputs(home, first_run, first_manifest, native)
        basis = capture(home, first_run, 'vendor/route describes config/hooks/route.sh')
        options = {'authority_hint': hint}
        if hint == 'external':
            options['authority_locator'] = 'vendor/route'
        described = target(home, first_run, scope='distro', component=locator,
                           basis_source_ids=(basis,), **options)
        second_run, second_manifest = stage(home)
        second_row, _, second_citation = report_v2_inputs(home, second_run, second_manifest, native)
        owned = target(home, second_run, scope='distro', path=locator)
        assert record(home, first_run, described)['identity']['locator'] == record(home, second_run, owned)['identity']['locator']
        assert record(home, second_run, owned)['identity']['authority']['status'] == 'verified-distro'
        first = reviewer.submit_report(home, first_run, str(uuid.uuid4()),
                                       report_v2(first_row, [report_v2_finding(first_row, described['target_ref'], first_citation, 'distro')]))
        reviewer.finalize_run(home, first_run, 'exited')
        second = reviewer.submit_report(home, second_run, str(uuid.uuid4()),
                                        report_v2(second_row, [report_v2_finding(second_row, owned['target_ref'], second_citation, 'distro')]))
        assert first['proposal_ids'][0] != second['proposal_ids'][0]
        assert reviewer.read_triage(home, first['proposal_ids'][0])['current'] != 'superseded'
        result = reviewer.dispose(home, first['proposal_ids'][0], 'superseded', 'Ownership confirmed.',
                                  second['proposal_ids'][0], {'uid': os.getuid(), 'identity': str(os.getuid())})
        assert result['current'] == 'superseded'
        assert result['history'][-1]['replacement'] == second['proposal_ids'][0]


def test_report_v2_destination_fingerprint_separates_authority_only():
    # Pure identity invariant: these inputs are not claimed as admitted targets.
    finding = {'finding': 'Observed retry failure.', 'applicability': 'distro'}
    identity = {'scope': 'distro', 'context': {'home_id': 'home', 'project_id': None,
                                                'identity_sha256': 'a' * 64},
                'authority': {'status': 'unknown', 'locator': None},
                'kind': 'component', 'locator': 'logical/tool'}
    target_record = {'schema': 1, 'identity': identity, 'surface': 'tooling', 'operation': 'modify',
                     'guard': {'state': 'described', 'revision': None, 'source_sha256': None,
                               'observed_at': 1.0}, 'basis_source_ids': ['b' * 64]}
    external = {**target_record, 'identity': {**identity, 'authority': {'status': 'external', 'locator': None}}}
    verified = {**target_record, 'identity': {**identity, 'authority': {'status': 'verified-distro', 'locator': None}}}
    fingerprints = {reviewer._finding_fingerprint(finding, target) for target in (target_record, external, verified)}
    assert len(fingerprints) == 3
    changed_guard = {**target_record, 'guard': {**target_record['guard'], 'observed_at': 2.0}}
    assert reviewer._finding_fingerprint(finding, target_record) == reviewer._finding_fingerprint(finding, changed_guard)


def test_report_v2_reflection_revalidates_feedback_link_at_submit():
    with lab() as (home, _):
        native, _, task = completed_session(home, role='worker', task_id='linked-feedback')
        task_dir = tasks.task_file(home, task['id']).parent
        question = task_dir / 'question.txt'
        question.write_text('Which approach?\n')
        workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
        supervision.reconcile(home)
        answer = task_dir / 'answer.txt'
        answer.write_text('Original linked feedback.\n')
        supervision.resolve(home, task['id'], 'choice', answer)
        run_id, manifest = stage(home, cutoff=time.time() + 100)
        row, _, _ = report_v2_inputs(home, run_id, manifest, native)
        locators = reviewer.read_selection_locators(home, manifest['manifest_id'], row['episode_id'])['rows']
        feedback = next(item for item in locators if item['kind'] == 'feedback')
        exposed = inspection().inspect(home, run_id, row['episode_id'], source='native',
                                       record_id=feedback['sha256'], part='content')
        row['outcome_sources'] = [exposed['source_id']]
        (home / feedback['link_path']).write_text('{}')
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))


def test_report_v2_rejects_secret_origin_even_with_consistent_evidence_hash():
    with lab() as (home, _):
        native, _, _ = completed_session(home)
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        dest = target(home, run_id, scope='distro', component='tool',
                      basis_source_ids=(citation,), authority_hint='unknown')
        safe_id = capture(home, run_id, 'nonsecret fixture text')
        safe = reviewer.read_evidence(home, run_id)['sources'][safe_id]
        forged = dict(safe)
        forged['origin'] = {'run_id': run_id, 'path': '.env'}
        forged['source_id'] = reviewer._digest(reviewer._evidence_identity(run_id, forged['origin'], forged['text_sha256']))
        forged['integrity_sha256'] = reviewer._evidence_integrity(forged)
        reviewer.publish_immutable(reviewer.paths(home)['evidence'] / run_id / (forged['source_id'] + '.json'), forged)
        finding = report_v2_finding(row, dest['target_ref'], forged['source_id'], 'distro')
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row, [finding]))


@pytest.mark.parametrize('change', ['journal', 'deadline', 'run-scope', 'target-baseline'])
def test_report_v2_freeze_rechecks_journal_scope_and_deadline(monkeypatch, change):
    with lab() as (home, _):
        native, _, _ = completed_session(home)
        run_id, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, run_id, manifest, native)
        hook = home / 'config' / 'hooks' / 'race.sh'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('original hook\n')
        dest = target(home, run_id, scope='distro', path='config/hooks/race.sh')
        report = report_v2(row, [report_v2_finding(row, dest['target_ref'], citation, 'distro')])
        original = inspection().read_journal
        invoked = False
        def race(home_arg, run_arg):
            nonlocal invoked
            snapshot = original(home_arg, run_arg)
            if not invoked:
                invoked = True
                if change == 'journal':
                    inspection().inspect(home, run_id, row['episode_id'], source='native')
                elif change == 'target-baseline':
                    hook.write_text('changed hook\n')
                else:
                    path = reviewer.paths(home)['receipts'] / (run_id + '.json')
                    receipt = json.loads(path.read_text())
                    if change == 'deadline':
                        receipt['deadline_epoch_ms'] = int((time.time() - 1) * 1000)
                    else:
                        receipt['dispatch_state'] = 'final'
                    store.atomic_json(path, receipt)
            return snapshot
        monkeypatch.setattr(inspection(), 'read_journal', race)
        with pytest.raises(store.Refusal):
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report)
        assert invoked
        assert not list(reviewer.paths(home)['reports'].glob('*.json'))
        episode = manifest['episodes'][0]
        progress = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
        assert not progress['screened'] and not progress['reflected']


def test_report_v2_coverage_changed_version_gap_and_failed_ranges():
    with lab() as (home, _):
        native, path, _ = completed_session(home, texts=('final answer',))
        old_text = 'old prompt ' * 700
        new_text = 'new prompt ' * 700
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        prompt_row = next(row for row in rows if row.get('id') == native['session_id'] + '-prompt-0')
        prompt_row['message']['content'] = old_text
        path.write_text('\n'.join(json.dumps(item) for item in rows) + '\n')
        rid = native['session_id'] + '-prompt-0'
        scope = reviewer._source_scope(home, {'role': 'central', 'session_id': native['session_id']})
        old_material = reviewer._material_id(scope, {'message_id': rid, 'text': old_text})
        new_material = reviewer._material_id(scope, {'message_id': rid, 'text': new_text})

        def inspect_pages(run_id, episode, *, all_pages):
            page = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                        record_id=rid, part='content')
            pages = [page]
            while all_pages and page['next_cursor']:
                page = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                            record_id=rid, part='content', cursor=page['next_cursor'])
                pages.append(page)
            return pages

        def submit_with_prompt(run_id, manifest, pages):
            episode = manifest['episodes'][0]
            outcome = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                           record_id=native['session_id'] + '-final-0', part='content')
            row = {'episode_id': episode['episode_id'], 'assessment': 'reflected',
                   'intent_sources': [pages[-1]['source_id']], 'action_sources': [outcome['source_id']],
                   'outcome_sources': [outcome['source_id']], 'limitations': ['Prompt may be partial.']}
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))

        first, first_manifest = stage(home)
        pages = inspect_pages(first, first_manifest['episodes'][0], all_pages=False)
        assert pages[0]['next_cursor']
        submit_with_prompt(first, first_manifest, pages)
        reviewer.finalize_run(home, first, 'exited')
        assert old_material not in reviewer._reviewed_material(home, scope)
        prompt_row['message']['content'] = new_text
        path.write_text('\n'.join(json.dumps(item) for item in rows) + '\n')
        failed, changed = stage(home, cutoff=201)
        assert changed['episodes'][0]['basis'] != first_manifest['episodes'][0]['basis']
        failed_pages = inspect_pages(failed, changed['episodes'][0], all_pages=True)
        assert len(failed_pages) > 2
        submit_with_prompt(failed, changed, failed_pages)
        reviewer.close_run(home, failed, 'failed')
        assert new_material not in reviewer._reviewed_material(home, scope)
        partial, partial_manifest = stage(home, cutoff=202)
        partial_pages = inspect_pages(partial, partial_manifest['episodes'][0], all_pages=False)
        submit_with_prompt(partial, partial_manifest, partial_pages)
        reviewer.finalize_run(home, partial, 'exited')
        assert new_material not in reviewer._reviewed_material(home, scope)
        detail, detail_manifest = stage(home, cutoff=203, detail=(partial_manifest['episodes'][0]['episode_id'],))
        all_pages = inspect_pages(detail, detail_manifest['episodes'][0], all_pages=True)
        assert all_pages[0]['range']['end'] < all_pages[-1]['range']['start']
        submit_with_prompt(detail, detail_manifest, all_pages)
        reviewer.finalize_run(home, detail, 'exited')
        assert new_material in reviewer._reviewed_material(home, scope)
        assert old_material not in reviewer._reviewed_material(home, scope)


@pytest.mark.parametrize('reopen', ['limits', 'feedback'])
def test_report_v2_coverage_successful_limited_hold_reopens_for_new_basis(reopen):
    with lab() as (home, _):
        _, _, task = completed_session(home, role='worker', task_id='held')
        cutoff = time.time() + 100
        run_id, manifest = stage(home, cutoff=cutoff)
        episode = next(item for item in manifest['episodes'] if item.get('task_id') == task['id'])
        meta = inspection().inspect(home, run_id, episode['episode_id'], source='native')
        row = {'episode_id': episode['episode_id'], 'assessment': 'limited',
               'intent_sources': [meta['source_id']], 'action_sources': [], 'outcome_sources': [],
               'limitations': ['Linked feedback unavailable.']}
        reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))
        reviewer.finalize_run(home, run_id, 'exited')
        config = reviewer.read_config(home)
        held = reviewer.select_manifest(home, cutoff=cutoff + 1, config_snapshot=config)
        assert episode['episode_id'] not in {item['episode_id'] for item in held['episodes']}
        assert any(item['episode_id'] == episode['episode_id'] for item in held['held'])
        if reopen == 'limits':
            config['limits']['max_input_bytes'] += 1024
        else:
            task_dir = tasks.task_file(home, task['id']).parent
            question = task_dir / 'question.txt'
            question.write_text('Which?\n')
            workers.event(home, task['id'], task['incarnation'], 'decision', 'choice', question)
            supervision.reconcile(home)
            answer = task_dir / 'answer.txt'
            answer.write_text('Newly linked feedback.\n')
            supervision.resolve(home, task['id'], 'choice', answer)
        reopened = reviewer.select_manifest(home, cutoff=cutoff + 2, config_snapshot=config)
        found = next(item for item in reopened['episodes'] if item['episode_id'] == episode['episode_id'])
        if reopen == 'feedback':
            assert found['basis'] != episode['basis']
        else:
            assert found['limits_sha256'] != episode['limits_sha256']
        assert not found['progress']['reflected']


def test_report_v2_coverage_successful_screening_feeds_fair_rotation():
    with lab() as (home, _):
        completed_session(home, start_at=10)
        run_id, manifest = stage(home, max_candidates=1)
        old = manifest['episodes'][0]
        def screen_and_finalize(current_run, episode):
            metadata = inspection().inspect(home, current_run, episode['episode_id'], source='native')
            row = {'episode_id': episode['episode_id'], 'assessment': 'screened',
                   'intent_sources': [metadata['source_id']], 'action_sources': [], 'outcome_sources': [],
                   'limitations': []}
            reviewer.submit_report(home, current_run, str(uuid.uuid4()), report_v2(row))
            reviewer.finalize_run(home, current_run, 'exited')
        screen_and_finalize(run_id, old)
        seen = []
        for index in range(6):
            completed_session(home, start_at=20 + index * 20)
            run_id, manifest = stage(home, cutoff=201 + index, max_candidates=1)
            episode = manifest['episodes'][0]
            seen.append(episode['episode_id'])
            screen_and_finalize(run_id, episode)
        assert old['episode_id'] in seen
        assert len(set(seen) - {old['episode_id']}) >= 2


def test_report_v2_historical_prepared_recovery_coexists_with_new_submission(monkeypatch):
    with lab() as (home, _):
        old_run, packet = produced_run(home)
        old_report = build_report(packet)
        request = str(uuid.uuid4())
        original = reviewer.publish_immutable
        fired = False
        def interrupt(path, value):
            nonlocal fired
            original(path, value)
            if not fired and Path(path).parent == reviewer.paths(home)['prepared']:
                fired = True
                raise RuntimeError('prepared')
        monkeypatch.setattr(reviewer, 'publish_immutable', interrupt)
        with pytest.raises(RuntimeError):
            reviewer.submit_report(home, old_run['review_run_id'], request, old_report)
        assert fired
        monkeypatch.setattr(reviewer, 'publish_immutable', original)
        prepared_path = reviewer._prepared_path(home, old_run['review_run_id'], request)
        prepared_bytes = prepared_path.read_bytes()
        native, _, _ = completed_session(home, texts=('schema two native outcome',))
        new_run, manifest = stage(home)
        row, _, citation = report_v2_inputs(home, new_run, manifest, native)
        dest = target(home, new_run, scope='distro', component='tool',
                      basis_source_ids=(citation,), authority_hint='unknown')
        reviewer.submit_report(home, new_run, str(uuid.uuid4()),
                               report_v2(row, [report_v2_finding(row, dest['target_ref'], citation, 'distro')]))
        reviewer.recover_submissions(home, old_run['review_run_id'])
        old_result = reviewer.submit_report(home, old_run['review_run_id'], request, old_report)
        old_report_path = reviewer.report_path(home, old_result['report_id'])
        old_proposal_path = reviewer.proposal_path(home, old_result['proposal_ids'][0])
        preserved = (prepared_path.read_bytes(), old_report_path.read_bytes(), old_proposal_path.read_bytes())
        assert preserved[0] == prepared_bytes
        reviewer.finalize_run(home, old_run['review_run_id'], 'exited')
        reviewer.finalize_run(home, new_run, 'exited')
        assert (prepared_path.read_bytes(), old_report_path.read_bytes(), old_proposal_path.read_bytes()) == preserved


def test_report_v2_coverage_noncontiguous_issued_ranges_leave_middle_gap():
    with lab() as (home, _):
        text = 'independent page evidence ' * 400
        native, _, _ = completed_session(home, texts=(text,))
        run_id, manifest = stage(home)
        episode = manifest['episodes'][0]
        rid = native['session_id'] + '-final-0'
        page = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                    record_id=rid, part='content')
        pages = [page]
        while page['next_cursor']:
            page = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                        record_id=rid, part='content', cursor=page['next_cursor'])
            pages.append(page)
        assert len(pages) >= 3
        version = hashlib.sha256(text.encode()).hexdigest()
        def interval(page):
            return {'message_id': rid, 'text_sha256': version, **page['range']}
        first, middle, last = interval(pages[0]), interval(pages[1]), interval(pages[-1])
        assert first['end'] == middle['start'] and middle['end'] <= last['start']
        assert reviewer._has_new_exposure([first, last], [middle])
        assert not reviewer._has_new_exposure([first, middle, last], [middle])
        other_version = {**middle, 'text_sha256': hashlib.sha256(b'other version').hexdigest()}
        assert reviewer._has_new_exposure([first, middle, last], [other_version])


def test_report_v2_coverage_native_resume_uses_only_successful_prior_ranges():
    with lab() as (home, _):
        native, path, _ = completed_session(home, texts=('short final outcome',))
        rid = native['session_id'] + '-prompt-0'
        text = 'prompt-α-boundary ' * 500
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        prompt_row = next(item for item in rows if item.get('id') == rid)
        prompt_row['message']['content'] = text
        path.write_text('\n'.join(json.dumps(item) for item in rows) + '\n')
        raw = text.encode()
        scope = reviewer._source_scope(home, {'role': 'central', 'session_id': native['session_id']})
        material = reviewer._material_id(scope, {'message_id': rid, 'text': text})

        def metadata_row(run_id, episode_id):
            cursor = None
            while True:
                args = ['reviewer', 'inspect', run_id, '--episode', episode_id, '--source', 'native']
                if cursor is not None:
                    args += ['--cursor', cursor]
                page = command(home, *args)
                bounded(page)
                assert text not in json.dumps(page, ensure_ascii=False)
                match = next((item for item in page['rows'] if item.get('message_id') == rid), None)
                if match is not None:
                    return match
                cursor = page['next_cursor']
                assert cursor is not None

        def read_page(run_id, episode_id, cursor=None):
            return inspection().inspect(home, run_id, episode_id, source='native',
                                        record_id=rid, part='content', cursor=cursor)

        def commit_reflection(run_id, episode, prompt_ids):
            final = inspection().inspect(home, run_id, episode['episode_id'], source='native',
                                         record_id=native['session_id'] + '-final-0', part='content')
            row = {'episode_id': episode['episode_id'], 'assessment': 'reflected',
                   'intent_sources': prompt_ids, 'action_sources': [final['source_id']],
                   'outcome_sources': [final['source_id']], 'limitations': ['Prompt is range-bounded.']}
            reviewer.submit_report(home, run_id, str(uuid.uuid4()), report_v2(row))
            reviewer.finalize_run(home, run_id, 'exited')

        failed_run, failed_manifest = stage(home)
        failed_episode = failed_manifest['episodes'][0]
        failed_prefix = read_page(failed_run, failed_episode['episode_id'])
        assert failed_prefix['next_cursor']
        reviewer.close_run(home, failed_run, 'failed')

        first_run, first_manifest = stage(home, cutoff=201)
        episode = first_manifest['episodes'][0]
        assert episode['basis'] == failed_episode['basis']
        first_meta = metadata_row(first_run, episode['episode_id'])
        assert not first_meta.get('resume_cursor')
        with pytest.raises(store.Refusal):
            read_page(first_run, episode['episode_id'], failed_prefix['next_cursor'])
        prefix = read_page(first_run, episode['episode_id'])
        assert prefix['range']['start'] == 0 and prefix['range']['end'] < len(raw)
        assert prefix['text'].encode() == raw[:prefix['range']['end']]
        commit_reflection(first_run, episode, [prefix['source_id']])
        assert material not in reviewer._reviewed_material(home, scope)

        resumed_run, resumed_manifest = stage(home, cutoff=202, detail=(episode['episode_id'],))
        resumed_episode = resumed_manifest['episodes'][0]
        assert resumed_episode['basis'] == episode['basis']
        row = metadata_row(resumed_run, episode['episode_id'])
        resume_cursor = row['resume_cursor']
        assert isinstance(resume_cursor, str) and resume_cursor != prefix['next_cursor']
        journal = inspection().read_journal(home, resumed_run)
        assert all(source.get('provenance', {}).get('view') != 'native-content'
                   for source in journal['sources'].values())
        assert material not in reviewer._reviewed_material(home, scope)
        with pytest.raises(store.Refusal):
            read_page(resumed_run, episode['episode_id'], prefix['next_cursor'])
        with pytest.raises(store.Refusal):
            inspection().inspect(home, resumed_run, episode['episode_id'], source='native',
                                 record_id=native['session_id'] + '-final-0', part='content', cursor=resume_cursor)
        page = read_page(resumed_run, episode['episode_id'], resume_cursor)
        assert page['range']['start'] == prefix['range']['end']
        pages = [page]
        while page['next_cursor']:
            page = read_page(resumed_run, episode['episode_id'], page['next_cursor'])
            pages.append(page)
        offset = prefix['range']['end']
        for page in pages:
            assert page['range']['start'] == offset
            assert page['text'].encode() == raw[offset:page['range']['end']]
            offset = page['range']['end']
        assert offset == len(raw)
        journal = inspection().read_journal(home, resumed_run)
        prompt_ranges = [source['provenance'] for source in journal['sources'].values()
                         if source.get('provenance', {}).get('view') == 'native-content'
                         and source['provenance'].get('record_id') == rid]
        assert prompt_ranges and all(span['start'] >= prefix['range']['end'] for span in prompt_ranges)
        commit_reflection(resumed_run, resumed_episode, [page['source_id'] for page in pages])
        assert material in reviewer._reviewed_material(home, scope)

        reread_run, reread_manifest = stage(home, cutoff=203, detail=(episode['episode_id'],))
        reread = read_page(reread_run, reread_manifest['episodes'][0]['episode_id'])
        assert reread['range'] == prefix['range'] and reread['text'] == prefix['text']
        reviewer.close_run(home, reread_run, 'failed')

        prompt_row['message']['content'] = 'changed version ' * 500
        path.write_text('\n'.join(json.dumps(item) for item in rows) + '\n')
        changed_run, changed_manifest = stage(home, cutoff=204)
        changed_episode = changed_manifest['episodes'][0]
        assert changed_episode['episode_id'] == episode['episode_id']
        assert changed_episode['basis'] != episode['basis']
        changed_meta = metadata_row(changed_run, changed_episode['episode_id'])
        assert not changed_meta.get('resume_cursor')
        with pytest.raises(store.Refusal):
            read_page(changed_run, changed_episode['episode_id'], resume_cursor)
        changed_prefix = read_page(changed_run, changed_episode['episode_id'])
        assert changed_prefix['range']['start'] == 0
        assert changed_prefix['text'].startswith('changed version ')
