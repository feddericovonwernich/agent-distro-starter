"""Run-scoped, immutable reviewer observations and target receipts."""
import codecs
import contextlib
import datetime
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path

from . import langfuse_cli, projects, reviewer, reviewer_episodes, store, worker_skills
from .uninstall_state import admission

MAX_RESPONSE = 16384
MAX_RAW = 262144
PAGE_BYTES = 2000


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _root(home, run_id):
    try:
        if str(uuid.UUID(str(run_id))) != run_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as error:
        raise store.Refusal('invalid review run') from error
    return reviewer.paths(home)['inspections'] / run_id


def _load(path):
    try:
        return reviewer._read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise store.Refusal('inspection record unavailable') from error


def _manifest_locked(home, receipt):
    manifest = reviewer.read_manifest(home, receipt.get('manifest_id'))
    from . import observability
    if manifest.get('home_id') != observability._home_id(home):
        raise store.Refusal('selection belongs to another home')
    config = reviewer.validate_config(_load(reviewer.paths(home)['manifests'] / (receipt['config_snapshot_ref'] + '.json')))
    if (reviewer._digest(config) != receipt['config_snapshot_ref'] or
            manifest['config_snapshot_sha256'] != receipt['config_snapshot_ref'] or
            manifest['instruction_sha256'] != receipt['instruction_snapshot_ref']):
        raise store.Refusal('run snapshot identity changed')
    instruction = _load(reviewer.paths(home)['manifests'] / (receipt['instruction_snapshot_ref'] + '.json'))
    if (not isinstance(instruction.get('text'), str) or
            _sha(instruction['text'].encode()) != receipt['instruction_snapshot_ref'] or
            instruction.get('raw_utf8_sha256') != receipt['instruction_snapshot_ref']):
        raise store.Refusal('instruction snapshot changed')
    return manifest, config


def _category_dir(home, run_id, category):
    directory = (reviewer.paths(home)['evidence'] / 'inspection' / run_id if category == 'sources'
                 else _root(home, run_id) / category)
    _root(home, run_id)
    current = store.home(home)
    for part in directory.relative_to(current).parts:
        current /= part
        if os.path.lexists(current):
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise store.Refusal('inspection directory unsafe')
    return directory


def _records(home, run_id, category):
    directory = _category_dir(home, run_id, category)
    if not os.path.lexists(directory):
        return {}
    if directory.is_symlink() or not directory.is_dir():
        raise store.Refusal('inspection directory unsafe')
    result = {}
    for path in sorted(directory.iterdir()):
        reviewer._hex_id(path.stem)
        if path.suffix != '.json' or path.is_symlink() or not path.is_file():
            raise store.Refusal('inspection record unsafe')
        value = _load(path)
        if not isinstance(value, dict) or reviewer._digest(value) != path.stem:
            raise store.Refusal('inspection record integrity mismatch')
        result[path.stem] = value
    return result


def _input_used_locked(home, receipt):
    manifest, _ = _manifest_locked(home, receipt)
    used = len(json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    catalog_id = manifest.get('catalog_id')
    if catalog_id is not None:
        reviewer._hex_id(catalog_id)
        raw = reviewer._read_regular_bytes(reviewer.paths(home)['catalogs'] / (catalog_id + '.json'))
        if reviewer._digest(json.loads(raw)) != catalog_id:
            raise store.Refusal('catalog identity changed')
        used += len(raw)
    used += sum(row['bytes'] for row in reviewer._evidence_records(home, receipt['review_run_id']).values())
    for row in _records(home, receipt['review_run_id'], 'sources').values():
        used += row['bytes']
    for row in _records(home, receipt['review_run_id'], 'targets').values():
        used += len(reviewer._json_bytes(row))
    return used


@contextlib.contextmanager
def _active(home, run_id):
    home = store.home(home)
    _root(home, run_id)
    with admission(home):
        with store.lock(home):
            receipt = reviewer._receipt(home, run_id)
            if receipt.get('dispatch_state') != 'begun' or receipt.get('review_run_id') != run_id:
                raise store.Refusal('review run is not active')
            reviewer._check_deadline(receipt.get('deadline_epoch_ms'))
            manifest, config = _manifest_locked(home, receipt)
            yield home, receipt, manifest, config


def _save(home, receipt, category, value, *, charge=0):
    ident = reviewer._digest(value)
    path = _category_dir(home, receipt['review_run_id'], category) / (ident + '.json')
    if path.exists():
        if _load(path) != value:
            raise store.Refusal('inspection immutable collision')
        return ident
    _, config = _manifest_locked(home, receipt)
    if _input_used_locked(home, receipt) + charge > config['limits']['max_input_bytes']:
        raise store.Refusal('cumulative reviewer input exceeds limit')
    reviewer.publish_immutable(path, value)
    return ident


def _source(home, receipt, *, text, provenance):
    body = text.encode()
    existing = _read_sources(home, receipt)
    for ident, row in existing.items():
        if row['text'] == text and row.get('provenance') == provenance:
            return ident
    value = {'schema': 1, 'run_id': receipt['review_run_id'], 'manifest_id': receipt['manifest_id'],
             'text': text, 'bytes': len(body), 'text_sha256': _sha(body), 'provenance': provenance,
             'captured_at': time.time()}
    return _save(home, receipt, 'sources', value, charge=len(body))


def _read_sources(home, receipt):
    rows = _records(home, receipt['review_run_id'], 'sources')
    for row in rows.values():
        if (row.get('run_id') != receipt['review_run_id'] or row.get('manifest_id') != receipt['manifest_id'] or
                not isinstance(row.get('text'), str) or row.get('bytes') != len(row['text'].encode()) or
                row.get('text_sha256') != _sha(row['text'].encode())):
            raise store.Refusal('inspection source invalid')
    return rows


def _cursor(home, receipt, state):
    return _save(home, receipt, 'cursors', {'schema': 1, 'run_id': receipt['review_run_id'],
                                           'manifest_id': receipt['manifest_id'], **state})


def _resume(home, receipt, cursor, expected):
    if cursor is None:
        return 0
    reviewer._hex_id(cursor)
    value = _records(home, receipt['review_run_id'], 'cursors').get(cursor)
    if (value is None or value.get('run_id') != receipt['review_run_id'] or
            value.get('manifest_id') != receipt['manifest_id'] or
            any(value.get(k) != v for k, v in expected.items()) or
            type(value.get('offset')) is not int or value['offset'] < 0):
        raise store.Refusal('cursor scope mismatch')
    return value['offset']


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=False, indent=2).encode()) + 1 > MAX_RESPONSE:
        raise store.Refusal('inspection response exceeds limit')
    return value


def _page_text(home, receipt, text, identity, cursor):
    if reviewer._EVIDENCE_SECRET_TEXT.search(text):
        raise store.Refusal('inspection source contains credentials')
    raw = text.encode()
    offset = _resume(home, receipt, cursor, identity)
    if offset > len(raw):
        raise store.Refusal('cursor outside source')
    try:
        raw[:offset].decode('utf-8')
    except UnicodeDecodeError as error:
        raise store.Refusal('cursor splits UTF-8') from error
    end = min(len(raw), offset + PAGE_BYTES)
    while end > offset:
        try:
            chunk = raw[offset:end].decode('utf-8')
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        chunk = ''
    provenance = {**identity, 'start': offset, 'end': end, 'version_sha256': _sha(raw),
                  'previous_cursor': cursor, 'complete': end == len(raw)}
    source_id = _source(home, receipt, text=chunk, provenance=provenance)
    next_cursor = _cursor(home, receipt, {**identity, 'offset': end, 'previous_cursor': cursor,
                                         'source_id': source_id}) if end < len(raw) else None
    result = {'source_id': source_id, 'text': chunk, 'range': {'start': offset, 'end': end},
              'complete': end == len(raw), 'next_cursor': next_cursor}
    return _bounded(result)


def _cursor_offset(home, receipt, cursor):
    if cursor is None:
        return 0
    reviewer._hex_id(cursor)
    value = _records(home, receipt['review_run_id'], 'cursors').get(cursor)
    if (value is None or value.get('run_id') != receipt['review_run_id'] or
            value.get('manifest_id') != receipt['manifest_id'] or
            type(value.get('offset')) is not int or value['offset'] < 0):
        raise store.Refusal('cursor scope mismatch')
    return value['offset']


def _scan_source(chunks, offset, deadline):
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder('utf-8')()
    selected = bytearray()
    position = 0
    secret_tail = ''
    try:
        for raw in chunks:
            reviewer._check_deadline(deadline)
            digest.update(raw)
            decoded = decoder.decode(raw)
            secret_tail = re.sub(r'\s+', ' ', secret_tail + decoded)
            if reviewer._EVIDENCE_SECRET_TEXT.search(secret_tail):
                raise store.Refusal('inspection source contains credentials')
            secret_tail = secret_tail[-512:]
            left = max(offset - position, 0)
            right = min(offset + PAGE_BYTES - position, len(raw))
            if left < right:
                selected.extend(raw[left:right])
            position += len(raw)
        decoder.decode(b'', final=True)
    except UnicodeDecodeError as error:
        raise store.Refusal('inspection source is not UTF-8') from error
    if offset > position:
        raise store.Refusal('cursor outside source')
    while selected:
        try:
            text = selected.decode('utf-8')
            break
        except UnicodeDecodeError as error:
            if error.reason != 'unexpected end of data':
                raise store.Refusal('cursor splits UTF-8') from error
            del selected[error.start:]
    else:
        text = ''
    return {'text': text, 'start': offset, 'end': offset + len(selected), 'size': position,
            'sha256': digest.hexdigest()}


def _page_scanned(home, receipt, scanned, identity, cursor, *, reserve=0):
    identity = {**identity, 'version_sha256': scanned['sha256']}
    if _resume(home, receipt, cursor, identity) != scanned['start']:
        raise store.Refusal('cursor scope mismatch')
    complete = scanned['end'] == scanned['size']
    provenance = {**identity, 'start': scanned['start'], 'end': scanned['end'],
                  'previous_cursor': cursor, 'complete': complete}
    if reserve:
        existing = _read_sources(home, receipt)
        duplicate = any(row['text'] == scanned['text'] and row.get('provenance') == provenance
                        for row in existing.values())
        _, config = _manifest_locked(home, receipt)
        added = 0 if duplicate else len(scanned['text'].encode())
        if _input_used_locked(home, receipt) + added + reserve > config['limits']['max_input_bytes']:
            raise store.Refusal('cumulative reviewer input exceeds limit')
    source_id = _source(home, receipt, text=scanned['text'], provenance=provenance)
    token = (_cursor(home, receipt, {**identity, 'offset': scanned['end'], 'previous_cursor': cursor,
                                    'source_id': source_id}) if not complete else None)
    return _bounded({'source_id': source_id, 'text': scanned['text'],
                     'range': {'start': scanned['start'], 'end': scanned['end']},
                     'complete': complete, 'next_cursor': token})


def _episode(manifest, episode_id):
    reviewer._hex_id(episode_id)
    episode = next((e for e in manifest['episodes'] if e['episode_id'] == episode_id), None)
    if episode is None:
        raise store.Refusal('episode is not selected')
    return episode


def _locators(home, manifest, episode):
    result, cursor = [], 0
    while cursor is not None:
        page = reviewer.read_selection_locators(home, manifest['manifest_id'], episode['episode_id'], cursor=cursor)
        result.extend(page['rows'])
        cursor = page['next_cursor']
    return result


def _safe_home_file(home, relative):
    if not isinstance(relative, str):
        raise store.Refusal('invalid owned path')
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in ('', '.', '..') for p in path.parts):
        raise store.Refusal('invalid owned path')
    current = home
    for part in path.parts:
        current = current / part
        try:
            st = os.lstat(current)
        except OSError as error:
            raise store.Refusal('owned path unavailable') from error
        if stat.S_ISLNK(st.st_mode):
            raise store.Refusal('redirected owned path')
    return current


def _native(home, receipt, manifest, episode):
    locators = _locators(home, manifest, episode)
    locator = next((item for item in locators if item['kind'] == 'native'), None)
    if not locator:
        raise store.Refusal('native source unavailable')
    path = _safe_home_file(home, locator['path'])
    completion = locator.get('completion')
    if completion:
        paths = reviewer_episodes._episode_paths(home)
        source = reviewer_episodes._read_private_json(paths['sources'] / (locator['source_id'] + '.json'))
        if (reviewer_episodes.source_fingerprint(source) != locator['source_id'] or
                source.get('path') != locator['path']):
            raise store.Refusal('native source receipt changed')
        reviewer_episodes._validate_persisted_source(home, source)
        saved = reviewer_episodes._read_private_json(paths['completions'] / locator['source_id'] / (completion['interaction_id'] + '.json'))
        reviewer_episodes._valid_record(source, saved, locator['source_id'], completion['interaction_id'])
        if saved != completion:
            raise store.Refusal('completion receipt changed')
        completion = {**saved, 'source': source}
    _, config = _manifest_locked(home, receipt)
    meta = reviewer_episodes.read_native(path, manifest['cutoff'], config['limits']['max_input_bytes'],
                                         receipt['deadline_epoch_ms'], completion=completion, metadata_only=True)
    if (meta.get('status') != 'ready' or meta.get('session_id') != episode['session_id'] or
            meta.get('material_sha256') != locator['projection_sha256'] or
            meta.get('boundary') != episode.get('boundary')):
        raise store.Refusal('native projection changed')
    return locator, locators, path, completion, meta


def _artifact(home, locator, *, offset, deadline):
    if type(locator.get('size')) is not int or locator['size'] < 0:
        raise store.Refusal('native artifact size invalid')
    _safe_home_file(home, locator['path'])
    scanned = _distro_file(home, Path(locator['path']), 'modify', offset=offset, deadline=deadline)
    if scanned['size'] != locator['size'] or scanned['sha256'] != locator['sha256']:
        raise store.Refusal('native artifact changed')
    if locator.get('link_path'):
        link = _safe_home_file(home, locator['link_path'])
        if _sha(reviewer._read_regular_bytes(link, require_current_uid=True)) != locator['link_sha256']:
            raise store.Refusal('feedback provenance changed')
    return scanned


def _run_cli(argv, env, deadline):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, start_new_session=True)
    selector = selectors.DefaultSelector()
    stdout, stderr = bytearray(), bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        while selector.get_map():
            remaining = reviewer.deadline_remaining_seconds(deadline)
            if remaining <= 0:
                raise store.Refusal('telemetry deadline exceeded')
            events = selector.select(remaining)
            if not events:
                raise store.Refusal('telemetry deadline exceeded')
            for key, _ in events:
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                dest = key.data
                if dest is stdout:
                    dest.extend(data)
                    if len(dest) > MAX_RAW:
                        raise store.Refusal('telemetry output exceeds limit')
                elif len(dest) < 4096:
                    dest.extend(data[:4096 - len(dest)])
        remaining = reviewer.deadline_remaining_seconds(deadline)
        if remaining <= 0:
            raise store.Refusal('telemetry deadline exceeded')
        if process.wait(timeout=remaining):
            raise store.Refusal('telemetry query failed')
        return json.loads(stdout)
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        raise store.Refusal('telemetry query unavailable') from error
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _telemetry(home, receipt, episode, window, *, env, record_id=None, provider_cursor=None):
    start, end = window['start'], window['end']
    fmt = lambda value: datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
    filters = [
        {'type': 'string', 'column': 'sessionId', 'operator': '=', 'value': episode['session_id']},
        {'type': 'datetime', 'column': 'startTime', 'operator': '>=', 'value': fmt(start)},
        {'type': 'datetime', 'column': 'startTime', 'operator': '<', 'value': fmt(end + 0.000001)},
    ]
    if record_id is not None:
        filters.append({'type': 'string', 'column': 'id', 'operator': '=', 'value': record_id})
    argv = [str(langfuse_cli._paths(home)['launcher']), '--api-version', 'auto', 'api', 'observations', 'list',
            '--limit', '20', '--json', '--filter', json.dumps(filters, separators=(',', ':')),
            '--fields', 'core,basic,time,model,usage,metadata' + (',io' if record_id else '')]
    if provider_cursor is not None:
        argv += ['--cursor', provider_cursor]
    value = _run_cli(argv, env, receipt['deadline_epoch_ms'])
    if not isinstance(value, dict) or value.get('status') != 200 or not isinstance(value.get('body'), dict):
        raise store.Refusal('telemetry response invalid')
    body = value['body']
    rows, pagination = body.get('data'), body.get('meta')
    if not isinstance(rows, list) or len(rows) > 20 or not isinstance(pagination, dict):
        raise store.Refusal('telemetry response invalid')
    next_cursor = pagination.get('cursor')
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096):
        raise store.Refusal('telemetry cursor invalid')
    selected = []
    expected_metadata = {'agd.home_id': episode['home_id'], 'agd.role': episode['role']}
    if episode['role'] == 'worker':
        expected_metadata.update({'agd.task_id': episode['task_id'], 'agd.incarnation': episode['incarnation']})
    for row in rows:
        if not isinstance(row, dict) or row.get('sessionId') != episode['session_id']:
            continue
        if row.get('projectId') not in (None, env.get('LANGFUSE_PROJECT_ID')):
            continue
        supplied = row.get('metadata')
        if supplied is not None and not isinstance(supplied, dict):
            raise store.Refusal('telemetry metadata ambiguous')
        if supplied and any(key in supplied and supplied[key] != expected
                            for key, expected in expected_metadata.items()):
            continue
        try:
            started = reviewer_episodes._epoch(row.get('startTime'))
            finished = reviewer_episodes._epoch(row.get('endTime'))
        except (TypeError, ValueError, OverflowError):
            raise store.Refusal('telemetry span time unavailable')
        if started < start or started > end:
            continue
        if finished < started or finished > end:
            raise store.Refusal('telemetry span overlaps interaction boundary')
        if record_id is not None and row.get('id') != record_id:
            raise store.Refusal('telemetry detail scope mismatch')
        selected.append(row)
    return selected, next_cursor


def _telemetry_window(home, receipt, manifest, episode, meta):
    window = meta.get('interaction_window')
    if not isinstance(window, dict) or not episode.get('interaction_id'):
        raise store.Refusal('telemetry interaction window unavailable')
    for other in manifest['episodes']:
        if other['episode_id'] == episode['episode_id'] or other.get('session_id') != episode['session_id']:
            continue
        other_meta = _native(home, receipt, manifest, other)[4]
        other_window = other_meta.get('interaction_window')
        if not other_window or (window['start'] <= other_window['end'] and other_window['start'] <= window['end']):
            raise store.Refusal('telemetry interaction windows overlap')
    locator = next((row for row in _locators(home, manifest, episode) if row.get('kind') == 'native'), None)
    if not locator or not locator.get('source_id'):
        raise store.Refusal('telemetry source proof unavailable')
    paths = reviewer_episodes._episode_paths(home)
    source_id = locator['source_id']
    source = reviewer_episodes._read_private_json(paths['sources'] / (source_id + '.json'))
    directory = paths['completions'] / source_id
    if directory.is_symlink() or not directory.is_dir():
        raise store.Refusal('telemetry completion set unavailable')
    _, config = _manifest_locked(home, receipt)
    native_path = _safe_home_file(home, locator['path'])
    for record_path in sorted(directory.glob('*.json')):
        reviewer._check_deadline(receipt['deadline_epoch_ms'])
        if record_path.stem == episode['interaction_id']:
            continue
        other = reviewer_episodes._valid_record(
            source, reviewer_episodes._read_private_json(record_path), source_id, record_path.stem)
        if other['recorded_at'] > manifest['cutoff'] or other['final']['timestamp'] / 1000 > manifest['cutoff']:
            continue
        proof = reviewer_episodes.read_native(
            native_path, manifest['cutoff'], config['limits']['max_input_bytes'], receipt['deadline_epoch_ms'],
            completion={**other, 'source': source}, metadata_only=True)
        other_window = proof.get('interaction_window')
        if proof.get('status') != 'ready' or not other_window:
            raise store.Refusal('telemetry interaction boundaries uncertain')
        if window['start'] <= other_window['end'] and other_window['start'] <= window['end']:
            raise store.Refusal('telemetry interaction windows overlap')
    return window


def _telemetry_metadata(home, receipt, episode, window, env, cursor, projection_sha256):
    query_scope = {'session_id': episode['session_id'], 'window': window,
                   'project_id': env['LANGFUSE_PROJECT_ID'], 'cli_version': langfuse_cli.VERSION,
                   'installation_sha256': reviewer._digest(langfuse_cli.read_installation(home))}
    identity = {'view': 'langfuse-metadata', 'episode_id': episode['episode_id'],
                'query_scope': query_scope,
                'version_sha256': reviewer._digest({'query_scope': query_scope, 'projection': projection_sha256})}
    offset = _resume(home, receipt, cursor, identity)
    prior = _records(home, receipt['review_run_id'], 'cursors').get(cursor) if cursor else None
    accepted = prior.get('accepted') if prior else []
    if not isinstance(accepted, list) or len(accepted) != offset:
        raise store.Refusal('telemetry cursor invalid')
    progress = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
    completed = {(item['record_id'], item['metadata_sha256']) for item in progress.get('ranges', [])
                 if isinstance(item, dict) and item.get('provider') == 'langfuse' and
                 isinstance(item.get('record_id'), str) and
                 isinstance(item.get('metadata_sha256'), str) and
                 re.fullmatch(r'[0-9a-f]{64}', item['metadata_sha256'])}
    provider_cursor, visited, matched, found = None, set(), 0, []
    more = False
    while True:
        rows, following = _telemetry(home, receipt, episode, window, env=env, provider_cursor=provider_cursor)
        rows = [{key: row[key] for key in ('id', 'traceId', 'sessionId', 'startTime', 'endTime',
                                           'type', 'model', 'usageDetails', 'costDetails')
                 if key in row} for row in rows]
        for row in rows:
            if not isinstance(row.get('id'), str) or not 1 <= len(row['id']) <= 256:
                raise store.Refusal('telemetry observation ID invalid')
            fingerprint = {'id': row['id'], 'sha256': reviewer._digest(row)}
            if (fingerprint['id'], fingerprint['sha256']) in completed:
                continue
            if matched < len(accepted):
                if fingerprint != accepted[matched]:
                    raise store.Refusal('telemetry metadata changed during pagination')
                matched += 1
                continue
            if (len(found) >= 20 or
                    len(json.dumps(found + [row], ensure_ascii=False).encode()) > 7000):
                if not found:
                    raise store.Refusal('telemetry metadata row exceeds limit')
                more = True
                break
            if any(item['id'] == row['id'] for item in accepted) or any(item['id'] == row['id'] for item in found):
                raise store.Refusal('duplicate telemetry observation')
            found.append(row)
        if more:
            break
        if matched == len(accepted) and found:
            more = following is not None
            break
        if following is None:
            break
        if following in visited:
            raise store.Refusal('telemetry cursor cycle')
        visited.add(following)
        provider_cursor = following
    if matched != len(accepted):
        raise store.Refusal('telemetry metadata prefix unavailable')
    if not found and cursor is None:
        raise store.Refusal('telemetry correlation unavailable')
    end = offset + len(found)
    text = json.dumps(found, ensure_ascii=False, sort_keys=True)
    source_id = _source(home, receipt, text=text, provenance={
        **identity, 'start': offset, 'end': end, 'previous_cursor': cursor, 'complete': not more})
    fingerprints = accepted + [{'id': row['id'], 'sha256': reviewer._digest(row)} for row in found]
    token = _cursor(home, receipt, {**identity, 'offset': end, 'previous_cursor': cursor,
                                   'source_id': source_id, 'accepted': fingerprints}) if more else None
    return _bounded({'source': 'langfuse', 'source_id': source_id, 'rows': found,
                     'complete': token is None, 'next_cursor': token})


def inspect(home, run_id, episode_id, *, source='auto', record_id=None, part=None, cursor=None):
    if source not in ('auto', 'native', 'langfuse'):
        raise store.Refusal('invalid inspection source')
    if (record_id is None and part is not None) or (record_id is not None and
            (source == 'auto' or part not in (('content',) if source == 'native' else ('input', 'output')))):
        raise store.Refusal('invalid inspection detail')
    with _active(home, run_id) as (home, receipt, manifest, config):
        episode = _episode(manifest, episode_id)
        locator, locators, path, completion, meta = _native(home, receipt, manifest, episode)
        limitation = None
        if source in ('auto', 'langfuse'):
            try:
                admitted = []
                if record_id is not None:
                    if not isinstance(record_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', record_id):
                        raise store.Refusal('invalid observation ID')
                    for item in _read_sources(home, receipt).values():
                        if item['provenance'].get('view') == 'langfuse-metadata' and item['provenance'].get('episode_id') == episode_id:
                            admitted.extend(row for row in json.loads(item['text']) if row.get('id') == record_id)
                    if len(admitted) != 1:
                        raise store.Refusal('observation metadata not admitted')
                window = _telemetry_window(home, receipt, manifest, episode, meta)
                env, available = langfuse_cli.reviewer_environment(
                    home, os.environ, deadline_epoch_ms=receipt['deadline_epoch_ms'])
                if not available.get('available'):
                    raise store.Refusal('telemetry unavailable')
                if record_id is None:
                    return _telemetry_metadata(home, receipt, episode, window, env, cursor, meta['material_sha256'])
                rows, provider_cursor = _telemetry(home, receipt, episode, window, env=env, record_id=record_id)
                if len(rows) != 1 or provider_cursor is not None:
                    raise store.Refusal('observation correlation ambiguous')
                current = {key: rows[0][key] for key in ('id', 'traceId', 'sessionId', 'startTime', 'endTime',
                                                         'type', 'model', 'usageDetails', 'costDetails')
                           if key in rows[0]}
                if current != admitted[0]:
                    raise store.Refusal('observation metadata changed')
                body = rows[0].get(part)
                if body is None:
                    raise store.Refusal('observation part unavailable')
                text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                identity = {'view': 'langfuse-' + part, 'episode_id': episode_id, 'record_id': record_id,
                            'query_scope': {'session_id': episode['session_id'], 'window': window,
                                            'project_id': env['LANGFUSE_PROJECT_ID'],
                                            'cli_version': langfuse_cli.VERSION,
                                            'installation_sha256': reviewer._digest(langfuse_cli.read_installation(home))},
                            'version_sha256': _sha(text.encode())}
                return _page_text(home, receipt, text, identity, cursor)
            except store.Refusal as error:
                if source == 'langfuse':
                    raise
                reviewer._check_deadline(receipt['deadline_epoch_ms'])
                limitation = str(error)
        if record_id is None:
            identity = {'view': 'native-metadata', 'episode_id': episode_id, 'version_sha256': meta['material_sha256']}
            offset = _resume(home, receipt, cursor, identity)
            rows = meta['projection'][offset:offset + 20]
            next_offset = offset + len(rows)
            rendered = json.dumps(rows, ensure_ascii=False, sort_keys=True)
            source_id = _source(home, receipt, text=rendered, provenance={
                **identity, 'start': offset, 'end': next_offset, 'previous_cursor': cursor,
                'complete': next_offset == len(meta['projection'])})
            next_cursor = _cursor(home, receipt, {**identity, 'offset': next_offset, 'previous_cursor': cursor,
                                                  'source_id': source_id}) if next_offset < len(meta['projection']) else None
            return _bounded({'source': 'native', 'source_id': source_id, 'rows': rows, 'next_cursor': next_cursor,
                             'complete': next_cursor is None, 'limitations': [limitation] if limitation else []})
        artifact = next((item for item in locators if item.get('kind') != 'native' and item.get('sha256') == record_id), None)
        if artifact:
            offset = _cursor_offset(home, receipt, cursor)
            scanned = _artifact(home, artifact, offset=offset, deadline=receipt['deadline_epoch_ms'])
            identity = {'view': 'native-content', 'episode_id': episode_id, 'record_id': record_id,
                        'projection_sha256': meta['material_sha256']}
            return _page_scanned(home, receipt, scanned, identity, cursor)
        expected = next((row for row in meta['projection'] if row['message_id'] == record_id), None)
        if not expected:
            raise store.Refusal('native record outside selected episode')
        identity = {'view': 'native-content', 'episode_id': episode_id, 'record_id': record_id,
                    'version_sha256': expected['text_sha256'], 'projection_sha256': meta['material_sha256']}
        offset = _resume(home, receipt, cursor, identity)
        read = reviewer_episodes.read_native(
            path, manifest['cutoff'], PAGE_BYTES, receipt['deadline_epoch_ms'], completion=completion,
            content_range=(record_id, offset, PAGE_BYTES), reject_text=reviewer._EVIDENCE_SECRET_TEXT.search)
        row = next((row for row in read['rows'] if row['message_id'] == record_id), None)
        if row is None or row['text_sha256'] != expected['text_sha256']:
            raise store.Refusal('native content unavailable or changed')
        scanned = {'text': row['text'], 'start': row['start'], 'end': row['end'],
                   'size': row['size'], 'sha256': row['text_sha256']}
        return _page_scanned(home, receipt, scanned, identity, cursor)


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise store.Refusal('target path invalid')
    path = Path(value)
    if path.is_absolute() or str(path) != value or any(p in ('', '.', '..') for p in path.parts):
        raise store.Refusal('target path invalid')
    for part in path.parts:
        low = part.lower()
        if (reviewer._EVIDENCE_SECRET_NAME.search(part) or low in
                {'.git', '.ssh', '.env', 'auth.json', 'settings.json', 'config.json', 'credentials',
                 'secrets', 'environment', 'private-settings.json'} or low.endswith('.env')):
            raise store.Refusal('credential store is not a target')
    return path


def _git(project, *args, deadline=None):
    remaining = 30 if deadline is None else min(30, reviewer.deadline_remaining_seconds(deadline))
    if remaining <= 0:
        raise store.Refusal('reviewer deadline exceeded')
    try:
        result = subprocess.run(['git', '--literal-pathspecs', '-C', str(project), *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=remaining)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise store.Refusal('project revision unavailable') from error
    if result.returncode or len(result.stdout) > MAX_RAW:
        raise store.Refusal('project revision unavailable')
    return result.stdout


def _git_blob(project, oid, size, offset, deadline):
    try:
        process = subprocess.Popen(['git', '-C', str(project), 'cat-file', 'blob', oid],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        raise store.Refusal('project blob unavailable') from error
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        def chunks():
            while selector.get_map():
                remaining = 30 if deadline is None else reviewer.deadline_remaining_seconds(deadline)
                if remaining <= 0:
                    raise store.Refusal('reviewer deadline exceeded')
                events = selector.select(remaining)
                if not events:
                    raise store.Refusal('project blob deadline exceeded')
                raw = os.read(process.stdout.fileno(), 65536)
                if not raw:
                    selector.unregister(process.stdout)
                    break
                yield raw
        scanned = _scan_source(chunks(), offset, deadline)
        remaining = 30 if deadline is None else reviewer.deadline_remaining_seconds(deadline)
        if remaining <= 0 or process.wait(timeout=remaining) or scanned['size'] != size:
            raise store.Refusal('project blob changed')
        return scanned
    except (OSError, subprocess.TimeoutExpired) as error:
        raise store.Refusal('project blob unavailable') from error
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        process.stdout.close()


def _project_file(home, binding, path, operation, *, offset=0, deadline=None):
    project = (projects.read(home, binding['project_id']) if deadline is None else
               projects.read(home, binding['project_id'], deadline_epoch_ms=deadline))
    root = Path(project['path'])
    revision = binding['task_revision']
    # Each prefix must be an actual Git tree, not a symlink or gitlink.
    parts = path.parts
    for index in range(1, len(parts) + 1):
        relative = '/'.join(parts[:index])
        entry = _git(root, 'ls-tree', '-z', revision, '--', relative, deadline=deadline)
        if not entry:
            if index != len(parts) or operation != 'create':
                raise store.Refusal('target or parent unavailable at revision')
            return None, revision
        try:
            header, name = entry.rstrip(b'\0').split(b'\t', 1)
            mode, kind, oid = header.decode().split()
        except (ValueError, UnicodeError) as error:
            raise store.Refusal('project tree invalid') from error
        if name.decode('utf-8') != relative:
            raise store.Refusal('project tree path mismatch')
        if index < len(parts):
            if mode != '040000' or kind != 'tree':
                raise store.Refusal('project parent is not a tree')
        else:
            if mode not in ('100644', '100755') or kind != 'blob':
                raise store.Refusal('project target is not a regular file')
            if operation == 'create':
                raise store.Refusal('target already exists')
            size = int(_git(root, 'cat-file', '-s', oid, deadline=deadline).strip())
            scanned = _git_blob(root, oid, size, offset, deadline)
            return scanned, revision
    raise store.Refusal('target unavailable')


def _distro_file(home, path, operation, *, offset=0, deadline=None):
    directory = fd = None
    try:
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            identity = os.fstat(child)
            if identity.st_uid != os.getuid():
                os.close(child)
                raise store.Refusal('target parent is not owned')
            os.close(directory)
            directory = child
        try:
            fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            if operation == 'create':
                return None
            raise store.Refusal('target unavailable')
        identity = os.fstat(fd)
        if operation == 'create':
            raise store.Refusal('target already exists')
        if (not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.getuid() or identity.st_nlink != 1):
            raise store.Refusal('target is not an owned regular file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            scanned = _scan_source(iter(lambda: stream.read(65536), b''), offset, deadline)
            final = os.fstat(stream.fileno())
        if (scanned['size'] != identity.st_size or final.st_size != identity.st_size or
                final.st_mtime_ns != identity.st_mtime_ns or final.st_ctime_ns != identity.st_ctime_ns or
                final.st_ino != identity.st_ino or final.st_dev != identity.st_dev):
            raise store.Refusal('target changed during capture')
        return scanned
    except OSError as error:
        raise store.Refusal('target unavailable or unsafe') from error
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def _basis(home, receipt, ids):
    if not isinstance(ids, (list, tuple)) or len(ids) != len(set(ids)):
        raise store.Refusal('invalid basis sources')
    sources = _read_sources(home, receipt)
    extra = reviewer._evidence_records(home, receipt['review_run_id'])
    values = []
    for source_id in ids:
        reviewer._hex_id(source_id)
        row = sources.get(source_id) or extra.get(source_id)
        if row is None:
            raise store.Refusal('basis source not admitted')
        values.append(row)
    return values


def _matching_target(home, receipt, value, supplied_basis):
    # The first content page remains the record's capture proof; later pages
    # add journal entries without changing the destination reference.
    for ident, old in _records(home, receipt['review_run_id'], 'targets').items():
        before = {**old, 'guard': {**old['guard'], 'observed_at': None}, 'basis_source_ids': None}
        after = {**value, 'guard': {**value['guard'], 'observed_at': None}, 'basis_source_ids': None}
        old_basis = old['basis_source_ids']
        if before == after and old_basis[:len(supplied_basis)] == list(supplied_basis):
            if len(old_basis) == len(supplied_basis) + (old['guard']['state'] == 'present'):
                _basis(home, receipt, old_basis)
                return ident, old
    return None


def _target_record(home, receipt, value, supplied_basis):
    found = _matching_target(home, receipt, value, supplied_basis)
    if found is not None:
        return found
    ident = _save(home, receipt, 'targets', value, charge=len(reviewer._json_bytes(value)))
    return ident, value


def inspect_target(home, run_id, *, scope, project_id=None, path=None, component=None,
                   operation='modify', surface, basis_source_ids=(), authority_hint=None,
                   authority_locator=None, cursor=None):
    if scope not in ('project', 'distro') or operation not in ('create', 'modify', 'remove'):
        raise store.Refusal('invalid target scope or operation')
    if (scope == 'project') != (project_id is not None) or (path is None) == (component is None):
        raise store.Refusal('invalid target identity')
    if not isinstance(surface, str) or not 1 <= len(surface) <= 80 or not surface.isprintable():
        raise store.Refusal('invalid target surface')
    if authority_hint not in (None, 'external', 'unknown') or (path is not None and
            (authority_hint is not None or authority_locator is not None)):
        raise store.Refusal('invalid authority hint')
    if authority_locator is not None:
        if (authority_hint != 'external' or not isinstance(authority_locator, str) or
                not 1 <= len(authority_locator) <= 256 or not authority_locator.isprintable() or
                reviewer._EVIDENCE_SECRET_TEXT.search(authority_locator) or
                re.search(r'://[^/\\s]*@', authority_locator)):
            raise store.Refusal('invalid authority locator')
    if component is not None and (not isinstance(component, str) or not 1 <= len(component) <= 256 or
                                  not component.isprintable() or reviewer._EVIDENCE_SECRET_TEXT.search(component)):
        raise store.Refusal('invalid component locator')
    with _active(home, run_id) as (home, receipt, manifest, config):
        basis = _basis(home, receipt, basis_source_ids)
        binding = None
        if scope == 'project':
            store.validate_id(project_id)
            episodes = [e for e in manifest['episodes'] if e.get('project_binding', {}).get('project_id') == project_id]
            if not episodes:
                raise store.Refusal('project is not selected')
            bindings = [reviewer._validate_project_binding(home, e, deadline_epoch_ms=receipt['deadline_epoch_ms'])
                        for e in episodes]
            if any(b != bindings[0] for b in bindings):
                raise store.Refusal('ambiguous project revisions')
            binding = bindings[0]
            context = {'home_id': manifest['home_id'], 'project_id': project_id,
                       'identity_sha256': binding['identity_sha256']}
        else:
            from . import supervision
            context = {'home_id': manifest['home_id'], 'project_id': None,
                       'identity_sha256': reviewer._digest(supervision.runtime_home_identity(home))}
        scanned = None
        if path is not None:
            relative = _relative(path)
            offset = _cursor_offset(home, receipt, cursor)
            if binding:
                scanned, revision = _project_file(home, binding, relative, operation, offset=offset,
                                                  deadline=receipt['deadline_epoch_ms'])
                authority = {'status': 'verified-project', 'locator': None}
            else:
                scanned = _distro_file(home, relative, operation, offset=offset,
                                      deadline=receipt['deadline_epoch_ms'])
                revision = None
                authority = {'status': 'verified-distro', 'locator': None}
            guard = {'state': 'absent' if scanned is None else 'present', 'revision': revision,
                     'source_sha256': None if scanned is None else scanned['sha256'], 'observed_at': time.time()}
            kind, locator = 'file', path
        else:
            if not basis:
                raise store.Refusal('component requires admitted evidence')
            if authority_locator is not None and not any(authority_locator in row['text'] for row in basis):
                raise store.Refusal('authority locator is not evidenced')
            authority = {'status': 'external' if authority_hint == 'external' else 'unknown',
                         'locator': authority_locator if authority_hint == 'external' else None}
            guard = {'state': 'described', 'revision': None, 'source_sha256': None, 'observed_at': time.time()}
            kind, locator = 'component', component
        identity = {'scope': scope, 'context': context, 'authority': authority, 'kind': kind, 'locator': locator}
        page = None
        source_ids = list(basis_source_ids)
        if scanned is not None:
            page_identity = {'view': 'target-content', 'target_identity_sha256': reviewer._digest(identity),
                             'operation': operation}
            proposed = {'schema': 1, 'identity': identity, 'surface': surface, 'operation': operation,
                        'guard': guard, 'basis_source_ids': list(dict.fromkeys([*source_ids, '0' * 64]))}
            reserve = (0 if _matching_target(home, receipt, proposed, basis_source_ids) else
                       len(reviewer._json_bytes(proposed)))
            page = _page_scanned(home, receipt, scanned, page_identity, cursor, reserve=reserve)
            source_ids.append(page['source_id'])
        elif cursor is not None:
            raise store.Refusal('cursor does not apply to target')
        value = {'schema': 1, 'identity': identity, 'surface': surface, 'operation': operation,
                 'guard': guard, 'basis_source_ids': list(dict.fromkeys(source_ids))}
        target_ref, value = _target_record(home, receipt, value, basis_source_ids)
        result = {'target_ref': target_ref, 'identity': identity, 'guard': value['guard'],
                  'source_ids': source_ids, 'next_cursor': page['next_cursor'] if page else None}
        if page:
            result.update(text=page['text'], range=page['range'], complete=page['complete'])
        return _bounded(result)


def _validate_target_context(home, manifest, value):
    try:
        identity, guard = value['identity'], value['guard']
        context, authority = identity['context'], identity['authority']
        if (set(value) != {'schema', 'identity', 'surface', 'operation', 'guard', 'basis_source_ids'} or
                value['schema'] != 1 or set(identity) != {'scope', 'context', 'authority', 'kind', 'locator'} or
                set(context) != {'home_id', 'project_id', 'identity_sha256'} or
                set(authority) != {'status', 'locator'} or
                set(guard) != {'state', 'revision', 'source_sha256', 'observed_at'} or
                context['home_id'] != manifest['home_id'] or
                identity['scope'] not in ('project', 'distro') or identity['kind'] not in ('file', 'component') or
                authority['status'] not in ('verified-project', 'verified-distro', 'external', 'unknown')):
            raise ValueError('target schema')
        if (value['operation'] not in ('create', 'modify', 'remove') or
                not isinstance(value['surface'], str) or not 1 <= len(value['surface']) <= 80 or
                not value['surface'].isprintable() or not isinstance(guard['observed_at'], (int, float))):
            raise ValueError('target observation')
        if identity['scope'] == 'project':
            candidates = [e for e in manifest['episodes']
                          if e.get('project_binding', {}).get('project_id') == context['project_id']]
            if not candidates:
                raise ValueError('target project')
            bindings = [reviewer._validate_project_binding(home, e) for e in candidates]
            if any(b['identity_sha256'] != context['identity_sha256'] for b in bindings):
                raise ValueError('target project identity')
        else:
            from . import supervision
            if context['project_id'] is not None or context['identity_sha256'] != reviewer._digest(supervision.runtime_home_identity(home)):
                raise ValueError('target home identity')
        if identity['kind'] == 'file':
            relative = _relative(identity['locator'])
            expected = 'verified-project' if identity['scope'] == 'project' else 'verified-distro'
            if authority != {'status': expected, 'locator': None} or guard['state'] not in ('present', 'absent'):
                raise ValueError('target file authority')
            if identity['scope'] == 'project':
                if any(b['task_revision'] != guard['revision'] for b in bindings):
                    raise ValueError('target revision')
                if guard['state'] == 'absent':
                    _project_file(home, bindings[0], relative, 'create')
                else:
                    scanned, _ = _project_file(home, bindings[0], relative, 'modify')
                    if scanned['sha256'] != guard['source_sha256']:
                        raise ValueError('project baseline changed')
            elif guard['revision'] is not None:
                raise ValueError('distro revision')
            if guard['state'] == 'present':
                reviewer._hex_id(guard['source_sha256'])
            elif guard['source_sha256'] is not None:
                raise ValueError('absence proof')
        elif (authority['status'] not in ('external', 'unknown') or guard['state'] != 'described' or
              guard['source_sha256'] is not None or not value['basis_source_ids']):
            raise ValueError('target component authority')
        if identity['kind'] == 'component':
            locator = authority['locator']
            if authority['status'] == 'unknown' and locator is not None:
                raise ValueError('unknown authority locator')
            if locator is not None and (not isinstance(locator, str) or not 1 <= len(locator) <= 256 or
                    not locator.isprintable() or reviewer._EVIDENCE_SECRET_TEXT.search(locator) or
                    re.search(r'://[^/\s]*@', locator)):
                raise ValueError('authority locator')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise store.Refusal('target record invalid') from error


def _validate_target_basis(value, rows):
    authority = value['identity']['authority']
    locator = authority['locator']
    if locator is not None and not any(locator in row.get('text', '') for row in rows):
        raise store.Refusal('target authority is not supported by evidence')


def read_target(home, run_id, target_ref):
    home = store.home(home)
    reviewer._hex_id(target_ref)
    with store.lock(home):
        receipt = reviewer._receipt(home, run_id)
        manifest, _ = _manifest_locked(home, receipt)
        value = _records(home, run_id, 'targets').get(target_ref)
        if value is None:
            raise store.Refusal('target not admitted')
        _validate_target_context(home, manifest, value)
        rows = _basis(home, receipt, value['basis_source_ids'])
        _validate_target_basis(value, rows)
        identity, guard = value['identity'], value['guard']
        if identity['kind'] == 'file' and guard['state'] == 'present':
            if not any(row.get('provenance', {}).get('view') == 'target-content' and
                       row['provenance'].get('target_identity_sha256') == reviewer._digest(identity) and
                       row['provenance'].get('version_sha256') == guard['source_sha256'] for row in rows):
                raise store.Refusal('target capture proof invalid')
        return {'target_ref': target_ref, **value}


def _catalog(home, manifest):
    ident = manifest.get('catalog_id')
    reviewer._hex_id(ident)
    catalog = _load(reviewer.paths(home)['catalogs'] / (ident + '.json'))
    if reviewer._digest(catalog) != ident:
        raise store.Refusal('skill catalog changed')
    return catalog


def list_skills(home, run_id, *, cursor=None):
    with _active(home, run_id) as (home, receipt, manifest, config):
        catalog = _catalog(home, manifest)
        identity = {'view': 'skills-list', 'version_sha256': manifest['catalog_id']}
        offset = _resume(home, receipt, cursor, identity)
        rows = catalog['skills'][offset:offset + 20]
        end = offset + len(rows)
        text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        source_id = _source(home, receipt, text=text, provenance={
            **identity, 'start': offset, 'end': end, 'previous_cursor': cursor,
            'complete': end == len(catalog['skills'])})
        next_cursor = _cursor(home, receipt, {**identity, 'offset': end, 'previous_cursor': cursor,
                                             'source_id': source_id}) if end < len(catalog['skills']) else None
        return _bounded({'rows': rows, 'source_id': source_id, 'next_cursor': next_cursor})


def show_skill(home, run_id, skill_id, *, cursor=None):
    with _active(home, run_id) as (home, receipt, manifest, config):
        catalog = _catalog(home, manifest)
        frozen = next ( (row for row in catalog['skills'] if row['id'] == skill_id), None)
        if frozen is None:
            raise store.Refusal('skill is not in selected catalog')
        current = next((row for row in worker_skills.catalog(home) if row['id'] == skill_id), None)
        if current is None:
            raise store.Refusal('current skill unavailable')
        source = Path(current['source']) / 'SKILL.md'
        try:
            relative = source.relative_to(home).as_posix()
        except ValueError as error:
            raise store.Refusal('skill target outside home') from error
        assigned = []
        limitations = []
        for assignment in catalog['assigned']:
            for item in assignment['skills']:
                if item['id'] != skill_id:
                    continue
                row = {'task_id': assignment['task_id'], 'snapshot_sha256': item['snapshot_sha256'],
                       'usage': 'unknown'}
                try:
                    task = _load(home / 'data' / 'tasks' / assignment['task_id'] / 'task.json')
                    snapshot = next(s for s in worker_skills._manifest(home, task)['skills'] if s['id'] == skill_id)
                    raw = reviewer._read_regular_bytes(Path(snapshot['snapshot']) / 'SKILL.md', require_current_uid=True)
                    if _sha(raw) != item['snapshot_sha256']:
                        raise ValueError('snapshot changed')
                except (OSError, ValueError, StopIteration, KeyError, store.Refusal):
                    limitations.append('historical skill snapshot unavailable or changed')
                assigned.append(row)
    result = inspect_target(home, run_id, scope='distro', path=relative, surface='skill',
                            operation='modify', cursor=cursor)
    return _bounded({**result, 'catalog_sha256': frozen['sha256'], 'assigned': assigned,
                     'limitations': limitations})


def read_journal(home, run_id):
    """Read validated run-scoped exposure and target records."""
    home = store.home(home)
    with store.lock(home):
        return _read_journal_locked(home, run_id)


def _read_journal_locked(home, run_id):
    """Finalization caller must already hold the home mutation lock."""
    receipt = reviewer._receipt(home, run_id)
    manifest, _ = _manifest_locked(home, receipt)
    sources = _read_sources(home, receipt)
    cursors = _records(home, run_id, 'cursors')
    targets = _records(home, run_id, 'targets')
    episode_ids = {episode['episode_id'] for episode in manifest['episodes']}
    for row in sources.values():
        provenance = row.get('provenance', {})
        start, end = provenance.get('start'), provenance.get('end')
        view = provenance.get('view')
        previous = provenance.get('previous_cursor')
        if (not isinstance(provenance, dict) or not isinstance(view, str) or
                type(start) is not int or type(end) is not int or not 0 <= start <= end or
                not isinstance(provenance.get('complete'), bool) or
                ('episode_id' in provenance and provenance['episode_id'] not in episode_ids)):
            raise store.Refusal('inspection source provenance invalid')
        if view in ('native-content', 'target-content', 'langfuse-input', 'langfuse-output'):
            if end - start != row['bytes']:
                raise store.Refusal('inspection source range invalid')
        if previous is None:
            if start != 0:
                raise store.Refusal('inspection source chain invalid')
        else:
            parent = cursors.get(previous)
            if (parent is None or parent.get('offset') != start or parent.get('view') != view or
                    parent.get('version_sha256') != provenance.get('version_sha256') or
                    any(parent.get(key) != value for key, value in provenance.items()
                        if key not in ('start', 'end', 'complete', 'previous_cursor'))):
                raise store.Refusal('inspection source chain invalid')
    for cursor in cursors.values():
        if (cursor.get('run_id') != run_id or cursor.get('manifest_id') != receipt['manifest_id'] or
                type(cursor.get('offset')) is not int or cursor['offset'] < 0):
            raise store.Refusal('inspection cursor invalid')
        source = sources.get(cursor.get('source_id'))
        provenance = source.get('provenance', {}) if source else {}
        previous = cursor.get('previous_cursor')
        parent = cursors.get(previous) if previous is not None else None
        if (source is None or provenance.get('end') != cursor['offset'] or
                provenance.get('previous_cursor') != previous or
                provenance.get('view') != cursor.get('view') or
                provenance.get('version_sha256') != cursor.get('version_sha256') or
                (previous is not None and (parent is None or
                 parent.get('offset') != provenance.get('start') or
                 parent.get('view') != cursor.get('view') or
                 parent.get('version_sha256') != cursor.get('version_sha256'))) or
                (previous is None and provenance.get('start') != 0)):
            raise store.Refusal('inspection page chain invalid')
    for target in targets.values():
        _validate_target_context(home, manifest, target)
        basis = _basis(home, receipt, target['basis_source_ids'])
        _validate_target_basis(target, basis)
        identity, guard = target['identity'], target['guard']
        if identity['kind'] == 'file' and guard['state'] == 'present':
            if not any(row.get('provenance', {}).get('view') == 'target-content' and
                       row['provenance'].get('target_identity_sha256') == reviewer._digest(identity) and
                       row['provenance'].get('version_sha256') == guard['source_sha256']
                       for row in sources.values()):
                raise store.Refusal('target capture proof invalid')
    value = {'schema': 1, 'run_id': run_id, 'manifest_id': receipt['manifest_id'],
             'sources': sources, 'cursors': cursors, 'targets': targets}
    return {**value, 'journal_sha256': reviewer._digest(value)}
