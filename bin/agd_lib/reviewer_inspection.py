"""Run-scoped, immutable reviewer observations and target receipts."""
import codecs
import contextlib
import contextvars
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
# ponytail: cap locked freshness scans at 1s; larger histories need a native change index.
MAX_WATCH_SECONDS = 1.0

# A capture stages immutable records until its complete response is bounded.
# The receipt is the native issuance authority; file hashes alone are not.
_TRANSACTION = contextvars.ContextVar('reviewer_inspection_transaction', default=None)
_VALIDATION_WATCHES = contextvars.ContextVar('reviewer_inspection_validation_watches', default=None)
_CATEGORIES = ('sources', 'cursors', 'targets')


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
    tx = _TRANSACTION.get()
    if tx is not None and tx['home'] == home and tx['run_id'] == run_id:
        return {**tx['base'][category], **tx['staged'][category]}
    directory = _category_dir(home, run_id, category)
    receipt = reviewer._receipt(home, run_id)
    issuance = receipt.get('inspection_issuance')
    if issuance is None:
        expected = set()
    else:
        if (not isinstance(issuance, dict) or issuance.get('schema') != 1 or
                issuance.get('manifest_id') != receipt.get('manifest_id') or
                any(not isinstance(issuance.get(key), list) for key in _CATEGORIES)):
            raise store.Refusal('inspection issuance invalid')
        ids = issuance[category]
        for ident in ids:
            reviewer._hex_id(ident)
        if len(ids) != len(set(ids)):
            raise store.Refusal('inspection issuance invalid')
        expected = set(ids)
    if not os.path.lexists(directory):
        if expected:
            raise store.Refusal('issued inspection records unavailable')
        return {}
    if directory.is_symlink() or not directory.is_dir():
        raise store.Refusal('inspection directory unsafe')
    result = {}
    for ident in sorted(expected):
        path = directory / (ident + '.json')
        if path.is_symlink() or not path.is_file():
            raise store.Refusal('issued inspection record unavailable or unsafe')
        value = _load(path)
        if not isinstance(value, dict) or reviewer._digest(value) != ident:
            raise store.Refusal('inspection record integrity mismatch')
        result[ident] = value
    return result


def _input_used_locked(home, receipt):
    manifest, _ = _manifest_locked(home, receipt)
    used = len(json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    catalog_bytes = manifest.get('catalog_bytes')
    if type(catalog_bytes) is not int or catalog_bytes < 0 or (
            manifest.get('catalog_id') is None and catalog_bytes != 0):
        raise store.Refusal('catalog input charge unavailable')
    used += catalog_bytes
    used += sum(row['bytes'] for row in reviewer._evidence_records(home, receipt['review_run_id']).values())
    for row in _records(home, receipt['review_run_id'], 'sources').values():
        used += row['bytes']
    for row in _records(home, receipt['review_run_id'], 'targets').values():
        used += len(reviewer._json_bytes(row))
    return used


def _fingerprint(path, *, directory=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise store.Refusal('inspection input unavailable') from error
    base = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
    return base if directory else (*base, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _watch(home, path):
    tx = _TRANSACTION.get()
    watched = tx['watched'] if tx is not None else _VALIDATION_WATCHES.get()
    if watched is None:
        return
    path = Path(path)
    try:
        parts = path.relative_to(home).parts
    except ValueError as error:
        raise store.Refusal('inspection input outside home') from error
    current = home
    for index, part in enumerate(parts):
        current = current / part
        directory = index < len(parts) - 1
        key = (current, directory)
        value = _fingerprint(current, directory=directory)
        if key in watched and watched[key] != value:
            raise store.Refusal('inspection input changed')
        watched[key] = value
        if value is None:
            break


def _watch_project_root(path):
    tx = _TRANSACTION.get()
    watched = tx['watched'] if tx is not None else _VALIDATION_WATCHES.get()
    if watched is None:
        return
    for item in (path, path / '.git'):
        key = (item, False)
        value = _fingerprint(item)
        if key in watched and watched[key] != value:
            raise store.Refusal('project identity changed')
        watched[key] = value


def _check_watches(watched, *, deadline_epoch_ms=None):
    reviewer._check_deadline(deadline_epoch_ms)
    until = time.monotonic() + MAX_WATCH_SECONDS
    for (path, directory), fingerprint in watched.items():
        current = _fingerprint(path, directory=directory)
        reviewer._check_deadline(deadline_epoch_ms)
        if time.monotonic() >= until:
            raise store.Refusal('input freshness validation exceeded its one-second window')
        if current != fingerprint:
            raise store.Refusal('inspection input changed before publication')
    reviewer._check_deadline(deadline_epoch_ms)


@contextlib.contextmanager
def _validation_scope():
    watched = {}
    token = _VALIDATION_WATCHES.set(watched)
    try:
        yield watched
    finally:
        _VALIDATION_WATCHES.reset(token)


def _active_receipt(home, run_id, deadline):
    receipt = reviewer._receipt(home, run_id)
    if receipt.get('dispatch_state') != 'begun' or receipt.get('review_run_id') != run_id:
        raise store.Refusal('review run is not active')
    if any(reviewer._read_json(path).get('schema') == 2
           for path in reviewer.paths(home)['prepared'].glob(f'{run_id}-*.json')):
        raise store.Refusal('review report already prepared')
    if receipt.get('deadline_epoch_ms') != deadline:
        raise store.Refusal('review run deadline changed')
    reviewer._check_deadline(deadline)
    return receipt


@contextlib.contextmanager
def _active(home, run_id):
    home = store.home(home)
    _root(home, run_id)
    nested = _TRANSACTION.get()
    if nested is not None:
        if nested['home'] != home or nested['run_id'] != run_id:
            raise store.Refusal('nested inspection scope mismatch')
        yield home, nested['receipt'], nested['manifest'], nested['config']
        return
    with admission(home):
        deadline = reviewer._receipt(home, run_id).get('deadline_epoch_ms')
        reviewer._check_deadline(deadline)
        with store.lock(home, deadline_epoch_ms=deadline):
            receipt = _active_receipt(home, run_id, deadline)
            manifest, config = _manifest_locked(home, receipt)
            base = {category: _records(home, run_id, category) for category in _CATEGORIES}
        tx = {'home': home, 'run_id': run_id, 'receipt': receipt, 'manifest': manifest,
              'config': config, 'base': base, 'staged': {key: {} for key in _CATEGORIES}, 'watched': {}}
        token = _TRANSACTION.set(tx)
        try:
            yield home, receipt, manifest, config
            with store.lock(home, deadline_epoch_ms=deadline):
                current = _active_receipt(home, run_id, deadline)
                if any(current.get(key) != receipt.get(key) for key in
                       ('manifest_id', 'config_snapshot_ref', 'instruction_snapshot_ref')):
                    raise store.Refusal('review run scope changed')
                _manifest_locked(home, current)
                _check_watches(tx['watched'], deadline_epoch_ms=deadline)
                # Read the latest native receipt and journal while holding the lock.
                _TRANSACTION.reset(token)
                token = None
                latest = {category: _records(home, run_id, category) for category in _CATEGORIES}
                used = _input_used_locked(home, current)
                for ident, value in tx['staged']['sources'].items():
                    if ident not in latest['sources']:
                        if any(row['text'] == value['text'] and row['provenance'] == value['provenance']
                               for row in latest['sources'].values()):
                            raise store.Refusal('concurrent inspection page already issued')
                        used += value['bytes']
                used += sum(len(reviewer._json_bytes(value)) for ident, value in tx['staged']['targets'].items()
                            if ident not in latest['targets'])
                if used > config['limits']['max_input_bytes']:
                    raise store.Refusal('cumulative reviewer input exceeds limit')
                issuance = current.get('inspection_issuance') or {
                    'schema': 1, 'manifest_id': current['manifest_id'], **{key: [] for key in _CATEGORIES}}
                for category in _CATEGORIES:
                    for ident, value in tx['staged'][category].items():
                        if ident not in latest[category]:
                            reviewer.publish_immutable(_category_dir(home, run_id, category) / (ident + '.json'), value)
                            issuance[category].append(ident)
                current['inspection_issuance'] = issuance
                store.atomic_json(reviewer.paths(home)['receipts'] / (run_id + '.json'), current)
        finally:
            if token is not None:
                _TRANSACTION.reset(token)


def _save(home, receipt, category, value, *, charge=0):
    ident = reviewer._digest(value)
    tx = _TRANSACTION.get()
    if tx is None or tx['home'] != home or tx['run_id'] != receipt['review_run_id']:
        raise store.Refusal('inspection publication requires an active capture')
    records = _records(home, receipt['review_run_id'], category)
    existing = records.get(ident)
    if existing is not None:
        if existing != value:
            raise store.Refusal('inspection immutable collision')
        return ident
    if _input_used_locked(home, receipt) + charge > tx['config']['limits']['max_input_bytes']:
        raise store.Refusal('cumulative reviewer input exceeds limit')
    tx['staged'][category][ident] = value
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


def _row_page(items, offset, envelope):
    if offset > len(items):
        raise store.Refusal('cursor outside metadata')
    rows = []
    for item in items[offset:offset + 20]:
        candidate = [*rows, item]
        try:
            _bounded({**envelope, 'rows': candidate})
        except store.Refusal:
            if not rows:
                raise store.Refusal('metadata row exceeds limit')
            break
        rows = candidate
    return rows


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
    _watch(home, reviewer.paths(home)['selection_details'] / (episode['locators_id'] + '.json'))
    result, cursor = [], 0
    while cursor is not None:
        page = reviewer.read_selection_locators(home, manifest['manifest_id'], episode['episode_id'], cursor=cursor)
        result.extend(page['rows'])
        cursor = page['next_cursor']
    return result


def _metadata_projection(home, episode, meta, include_reviewed):
    detail = reviewer._read_selection_detail(home, episode['locators_id'], 'locators')
    if detail.get('episode_id') != episode['episode_id'] or detail.get('basis') != episode['basis']:
        raise store.Refusal('selection locator identity mismatch')
    reviewed = detail.get('reviewed_message_ids', [])
    if not isinstance(reviewed, list) or any(not isinstance(item, str) for item in reviewed):
        raise store.Refusal('selection material inventory invalid')
    covered = set(reviewed)
    return [item for item in meta['projection'] if include_reviewed or item['message_id'] not in covered]


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
    _watch(home, path)
    completion = locator.get('completion')
    if completion:
        paths = reviewer_episodes._episode_paths(home)
        _watch(home, paths['sources'] / (locator['source_id'] + '.json'))
        _watch(home, paths['completions'] / locator['source_id'] / (completion['interaction_id'] + '.json'))
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
        _watch(home, home / locator['link_path'])
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
    _watch(home, directory)
    if directory.is_symlink() or not directory.is_dir():
        raise store.Refusal('telemetry completion set unavailable')
    _, config = _manifest_locked(home, receipt)
    native_path = _safe_home_file(home, locator['path'])
    for record_path in sorted(directory.glob('*.json')):
        _watch(home, record_path)
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


def inspect(home, run_id, episode_id, *, source='auto', record_id=None, part=None, cursor=None, include_reviewed=False):
    if source not in ('auto', 'native', 'langfuse'):
        raise store.Refusal('invalid inspection source')
    if type(include_reviewed) is not bool or (include_reviewed and (source != 'native' or record_id is not None or part is not None)):
        raise store.Refusal('include-reviewed requires native metadata')
    if (record_id is None and part is not None) or (record_id is not None and
            (source == 'auto' or part not in (('content',) if source == 'native' else ('input', 'output')))):
        raise store.Refusal('invalid inspection detail')
    with _active(home, run_id) as (home, receipt, manifest, config):
        episode = _episode(manifest, episode_id)
        locator, locators, path, completion, meta = _native(home, receipt, manifest, episode)
        limitation = None
        if source in ('auto', 'langfuse'):
            tx = _TRANSACTION.get()
            staged_before = {category: dict(tx['staged'][category]) for category in _CATEGORIES}
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
                tx['staged'] = staged_before
                reviewer._check_deadline(receipt['deadline_epoch_ms'])
                limitation = str(error)
        if record_id is None:
            identity = {'view': 'native-metadata', 'episode_id': episode_id,
                        'version_sha256': meta['material_sha256'], 'include_reviewed': include_reviewed}
            offset = _resume(home, receipt, cursor, identity)
            artifacts = [{key: item[key] for key in ('kind', 'sha256', 'size') if key in item}
                         for item in locators if item.get('kind') != 'native' and 'sha256' in item]
            progress_path = reviewer._progress_path(home, episode_id, episode['basis'])
            _watch(home, progress_path)
            progress = reviewer._episode_progress(home, episode_id, episode['basis'])
            items = []
            resume_offsets = {}
            for item in _metadata_projection(home, episode, meta, include_reviewed):
                prefix = reviewer._covered_prefix(progress['ranges'], item['message_id'], item['text_sha256'])
                sizes = {row.get('size') for row in progress['ranges']
                         if row.get('message_id') == item['message_id']
                         and row.get('text_sha256') == item['text_sha256']}
                if prefix and len(sizes) == 1 and type(next(iter(sizes))) is int and prefix < next(iter(sizes)):
                    resume_offsets[item['message_id']] = prefix
                    items.append({**item, 'resume_cursor': '0' * 64})
                else:
                    items.append(item)
            items.extend(artifacts)
            rows = _row_page(items, offset, {
                'source': 'native', 'source_id': '0' * 64, 'next_cursor': '0' * 64,
                'complete': False, 'limitations': [limitation] if limitation else []})
            for index, item in enumerate(rows):
                if 'resume_cursor' in item:
                    cursor_identity = {'view': 'native-content', 'episode_id': episode_id,
                                       'record_id': item['message_id'], 'version_sha256': item['text_sha256'],
                                       'projection_sha256': meta['material_sha256'],
                                       'offset': resume_offsets[item['message_id']], 'resume_basis': episode['basis']}
                    rows[index] = {**item, 'resume_cursor': _cursor(home, receipt, cursor_identity)}
            next_offset = offset + len(rows)
            rendered = json.dumps(rows, ensure_ascii=False, sort_keys=True)
            source_id = _source(home, receipt, text=rendered, provenance={
                **identity, 'start': offset, 'end': next_offset, 'previous_cursor': cursor,
                'complete': next_offset == len(items)})
            next_cursor = _cursor(home, receipt, {**identity, 'offset': next_offset, 'previous_cursor': cursor,
                                                  'source_id': source_id}) if next_offset < len(items) else None
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
    if deadline is None:
        deadline = int((time.time() + 30) * 1000)
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
                remaining = reviewer.deadline_remaining_seconds(deadline)
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
        remaining = reviewer.deadline_remaining_seconds(deadline)
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
    _watch_project_root(root)
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
    _watch(home, home / path)
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
                re.search(r'://[^/\s]*@', authority_locator)):
            raise store.Refusal('invalid authority locator')
    if component is not None and (not isinstance(component, str) or not 1 <= len(component) <= 256 or
                                  not component.isprintable() or reviewer._EVIDENCE_SECRET_TEXT.search(component)):
        raise store.Refusal('invalid component locator')
    with _active(home, run_id) as (home, receipt, manifest, config):
        basis = _basis(home, receipt, basis_source_ids)
        binding = None
        if scope == 'project':
            store.validate_id(project_id)
            _watch(home, projects.record(home, project_id))
            episodes = [e for e in manifest['episodes'] if e.get('project_binding', {}).get('project_id') == project_id]
            if not episodes:
                raise store.Refusal('project is not selected')
            for episode in episodes:
                _watch(home, home / 'data' / 'tasks' / store.validate_id(episode['task_id']) / 'task.json')
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


def _validate_target_context(home, manifest, value, *, deadline=None, live=True):
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
            if live:
                _watch(home, projects.record(home, context['project_id']))
                for episode in candidates:
                    _watch(home, home / 'data' / 'tasks' / store.validate_id(episode['task_id']) / 'task.json')
            bindings = ([reviewer._validate_project_binding(home, e, deadline_epoch_ms=deadline) for e in candidates]
                        if live else [e['project_binding'] for e in candidates])
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
                if live and guard['state'] == 'absent':
                    _project_file(home, bindings[0], relative, 'create', deadline=deadline)
                elif live:
                    scanned, _ = _project_file(home, bindings[0], relative, 'modify', deadline=deadline)
                    if scanned['sha256'] != guard['source_sha256']:
                        raise ValueError('project baseline changed')
            elif guard['revision'] is not None:
                raise ValueError('distro revision')
            elif live:
                scanned = _distro_file(home, relative, 'create' if guard['state'] == 'absent' else 'modify',
                                       deadline=deadline)
                if scanned is not None and scanned['sha256'] != guard['source_sha256']:
                    raise ValueError('distro baseline changed')
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
    with admission(home):
        deadline = reviewer._receipt(home, run_id).get('deadline_epoch_ms')
        with store.lock(home, deadline_epoch_ms=deadline):
            receipt = reviewer._receipt(home, run_id)
            if receipt.get('deadline_epoch_ms') != deadline:
                raise store.Refusal('review run deadline changed')
            manifest, _ = _manifest_locked(home, receipt)
            journal = _read_journal_locked(home, run_id)
            value = journal['targets'].get(target_ref)
            if value is None:
                raise store.Refusal('target not admitted')
        with _validation_scope() as watched:
            _validate_target_context(home, manifest, value, deadline=deadline)
            with store.lock(home, deadline_epoch_ms=deadline):
                _check_watches(watched, deadline_epoch_ms=deadline)
                current = _read_journal_locked(home, run_id)
                if current['targets'].get(target_ref) != value or current['manifest_id'] != receipt['manifest_id']:
                    raise store.Refusal('target changed during validation')
        return {'target_ref': target_ref, **value}


def _catalog(home, manifest):
    ident = manifest.get('catalog_id')
    reviewer._hex_id(ident)
    _watch(home, reviewer.paths(home)['catalogs'] / (ident + '.json'))
    catalog = _load(reviewer.paths(home)['catalogs'] / (ident + '.json'))
    if reviewer._digest(catalog) != ident or len(reviewer._json_bytes(catalog)) != manifest.get('catalog_bytes'):
        raise store.Refusal('skill catalog changed')
    return catalog


def list_skills(home, run_id, *, cursor=None):
    with _active(home, run_id) as (home, receipt, manifest, config):
        catalog = _catalog(home, manifest)
        identity = {'view': 'skills-list', 'version_sha256': manifest['catalog_id']}
        offset = _resume(home, receipt, cursor, identity)
        rows = _row_page(catalog['skills'], offset, {'source_id': '0' * 64, 'next_cursor': '0' * 64})
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
        frozen = next((row for row in catalog['skills'] if row['id'] == skill_id), None)
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
        identity = {'view': 'skill-show', 'skill_id': skill_id, 'version_sha256': manifest['catalog_id']}
        offset = _resume(home, receipt, cursor, identity)
        prior = _records(home, run_id, 'cursors').get(cursor) if cursor else None
        content_cursor = prior.get('content_cursor') if prior else None
        content_done = prior.get('content_done', False) if prior else False
        if content_done:
            target_ref = prior.get('target_ref')
            saved = _records(home, run_id, 'targets').get(target_ref)
            if saved is None:
                raise store.Refusal('skill target unavailable')
            result = {'target_ref': target_ref, 'identity': saved['identity'], 'guard': saved['guard'],
                      'source_ids': [], 'next_cursor': None}
        else:
            result = inspect_target(home, run_id, scope='distro', path=relative, surface='skill',
                                    operation='modify', cursor=content_cursor)
        content_cursor = result['next_cursor']
        content_done = content_cursor is None
        assigned = []
        limitations = []
        for assignment in catalog['assigned']:
            reviewer._check_deadline(receipt['deadline_epoch_ms'])
            for item in assignment['skills']:
                if item['id'] != skill_id:
                    continue
                row = {'task_id': assignment['task_id'], 'snapshot_sha256': item['snapshot_sha256'],
                       'usage': 'unknown'}
                try:
                    task_path = home / 'data' / 'tasks' / assignment['task_id'] / 'task.json'
                    _watch(home, task_path)
                    task = _load(task_path)
                    snapshot = next(s for s in worker_skills._manifest(home, task)['skills'] if s['id'] == skill_id)
                    snapshot_path = Path(snapshot['snapshot']) / 'SKILL.md'
                    _watch(home, snapshot_path)
                    raw = reviewer._read_regular_bytes(snapshot_path, require_current_uid=True)
                    if _sha(raw) != item['snapshot_sha256']:
                        raise ValueError('snapshot changed')
                except (OSError, ValueError, StopIteration, KeyError, store.Refusal):
                    limitations = ['historical skill snapshot unavailable or changed']
                assigned.append(row)
        if offset > len(assigned):
            raise store.Refusal('skill assignment cursor invalid')
        envelope = {**result, 'catalog_sha256': frozen['sha256'], 'assigned': [],
                    'limitations': limitations, 'source_id': '0' * 64, 'next_cursor': '0' * 64,
                    'complete': False, 'source_ids': [*result['source_ids'], '0' * 64]}
        rows = []
        for row in assigned[offset:offset + 20]:
            try:
                _bounded({**envelope, 'assigned': [*rows, row]})
            except store.Refusal:
                if not rows and not result.get('text'):
                    raise store.Refusal('skill assignment exceeds response limit')
                break
            rows.append(row)
        end = offset + len(rows)
        complete = content_done and end == len(assigned)
        source_id = _source(home, receipt, text=json.dumps(rows, ensure_ascii=False, sort_keys=True),
                            provenance={**identity, 'start': offset, 'end': end,
                                        'previous_cursor': cursor, 'complete': complete})
        token = None if complete else _cursor(home, receipt, {
            **identity, 'offset': end, 'previous_cursor': cursor, 'source_id': source_id,
            'content_cursor': content_cursor, 'content_done': content_done, 'target_ref': result['target_ref']})
        return _bounded({**result, 'catalog_sha256': frozen['sha256'], 'assigned': rows,
                         'limitations': limitations, 'source_id': source_id,
                         'source_ids': [*result['source_ids'], source_id],
                         'complete': complete, 'next_cursor': token})


def read_journal(home, run_id):
    """Validate issued journal records and live target baselines outside the mutation lock."""
    home = store.home(home)
    with admission(home):
        deadline = reviewer._receipt(home, run_id).get('deadline_epoch_ms')
        with store.lock(home, deadline_epoch_ms=deadline):
            receipt = reviewer._receipt(home, run_id)
            if receipt.get('deadline_epoch_ms') != deadline:
                raise store.Refusal('review run deadline changed')
            manifest, _ = _manifest_locked(home, receipt)
            journal = _read_journal_locked(home, run_id)
        with _validation_scope() as watched:
            for target in journal['targets'].values():
                _validate_target_context(home, manifest, target, deadline=deadline)
            with store.lock(home, deadline_epoch_ms=deadline):
                _check_watches(watched, deadline_epoch_ms=deadline)
                current = _read_journal_locked(home, run_id)
                if current['journal_sha256'] != journal['journal_sha256']:
                    raise store.Refusal('inspection journal changed during validation')
        return journal


def _read_journal_locked(home, run_id):
    """Read the issued, structurally checked snapshot while the caller holds the lock.

    Native source provenance and live target baselines require independent validation
    outside this lock before finalization; compare journal_sha256 on reacquisition.
    """
    receipt = reviewer._receipt(home, run_id)
    reviewer._check_deadline(receipt.get('deadline_epoch_ms'))
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
        if 'resume_basis' in cursor:
            episode = next((item for item in manifest['episodes']
                            if item['episode_id'] == cursor.get('episode_id')), None)
            if (episode is None or cursor.get('view') != 'native-content'
                    or cursor['resume_basis'] != episode['basis'] or cursor['offset'] <= 0
                    or cursor.get('source_id') is not None or cursor.get('previous_cursor') is not None):
                raise store.Refusal('inspection resume cursor invalid')
            progress = reviewer._episode_progress(home, episode['episode_id'], episode['basis'])
            if reviewer._covered_prefix(progress['ranges'], cursor.get('record_id'),
                                        cursor.get('version_sha256')) < cursor['offset']:
                raise store.Refusal('inspection resume progress unavailable')
            continue
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
        _validate_target_context(home, manifest, target, deadline=receipt.get('deadline_epoch_ms'), live=False)
        basis = _basis(home, receipt, target['basis_source_ids'])
        _validate_target_basis(target, basis)
        identity, guard = target['identity'], target['guard']
        if identity['kind'] == 'file' and guard['state'] == 'present':
            if not any(row.get('provenance', {}).get('view') == 'target-content' and
                       row['provenance'].get('target_identity_sha256') == reviewer._digest(identity) and
                       row['provenance'].get('version_sha256') == guard['source_sha256']
                       for row in basis):
                raise store.Refusal('target capture proof invalid')
    value = {'schema': 1, 'run_id': run_id, 'manifest_id': receipt['manifest_id'],
             'sources': sources, 'cursors': cursors, 'targets': targets}
    return {**value, 'journal_sha256': reviewer._digest(value)}
