import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from agd_lib import store


Refusal = store.Refusal
_TREEHOUSE_URL = ('https://github.com/kunchenguid/treehouse/releases/download/v3.1.6/'
                  'treehouse-v3.1.6-linux-amd64.tar.gz')
_TREEHOUSE_DIGEST = 'e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1'
_TREEHOUSE_ASSET = 'treehouse-v3.1.6-linux-amd64.tar.gz'
_RELEASES = {
    'omp': ('https://api.github.com/repos/can1357/oh-my-pi/releases/latest', 'omp-linux-x64'),
    'herdr': ('https://api.github.com/repos/herdrdev/herdr/releases/latest', 'herdr-linux-x86_64'),
    'treehouse': (_TREEHOUSE_URL, _TREEHOUSE_ASSET),
}
_CAPABILITIES = {
    'omp': ['models-json', 'login'],
    'herdr': ['session-list-json', 'named-status'],
    'treehouse': ['version'],
}


def _receipt_path(home):
    return Path(home) / 'data' / 'dependencies.json'

def _validate_entry(tool, entry):
    if tool not in _RELEASES or not isinstance(entry, dict):
        raise Refusal('dependency receipt is malformed')
    common = {'source', 'path', 'version', 'capabilities'}
    if entry.get('source') == 'existing':
        expected = common
    elif entry.get('source') == 'managed':
        expected = common | {'asset', 'tag', 'artifact_sha256', 'installed_sha256'}
    else:
        raise Refusal('dependency receipt is malformed')
    if set(entry) != expected or not isinstance(entry['path'], str) or not Path(entry['path']).is_absolute():
        raise Refusal('dependency receipt is malformed')
    try:
        identity = os.lstat(entry['path'])
    except OSError as error:
        raise Refusal('dependency receipt is malformed') from error
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode) or not identity.st_mode & 0o111:
        raise Refusal('dependency receipt is malformed')
    if not isinstance(entry['version'], str) or not entry['version'] or entry['capabilities'] != _CAPABILITIES[tool]:
        raise Refusal('dependency receipt is malformed')
    if entry['source'] == 'managed' and (
        entry['asset'] != _RELEASES[tool][1] or not isinstance(entry['tag'], str) or not entry['tag']
        or any(not isinstance(entry[key], str) or not re.fullmatch(r'[0-9a-f]{64}', entry[key])
               for key in ('artifact_sha256', 'installed_sha256'))
        or (tool == 'treehouse' and (entry['tag'] != 'v3.1.6'
                                     or entry['artifact_sha256'] != _TREEHOUSE_DIGEST))
    ):
        raise Refusal('dependency receipt is malformed')


def _validate_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get('schema') != 1 or set(receipt) - {'schema', *_RELEASES}:
        raise Refusal('dependency receipt is malformed')
    for tool in _RELEASES:
        if tool in receipt:
            _validate_entry(tool, receipt[tool])
    return receipt


def read_dependencies(home):
    path = _receipt_path(home)
    if not path.exists():
        return {'schema': 1}
    if path.is_symlink() or not path.is_file():
        raise Refusal('dependency receipt is unsafe')
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal('dependency receipt is malformed') from error
    return _validate_receipt(value)


def write_dependencies(home, receipt):
    _validate_receipt(receipt)
    store.atomic_json(_receipt_path(home), receipt)


def _result(run, argv):
    try:
        return run(argv, check=False)
    except (OSError, store.DependencyError) as error:
        raise Refusal(f'{Path(argv[0]).name} is unavailable') from error


def _version(path, run, label):
    result = _result(run, [str(path), '--version'])
    text = (result.stdout or result.stderr or '').strip()
    if result.returncode or not text:
        raise Refusal(f'{label} version check failed')
    return text


def probe_omp(path, run):
    version = _version(path, run, 'OMP')
    models = _result(run, [str(path), 'models', '--json'])
    try:
        parsed = json.loads(models.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise Refusal('OMP models output is invalid') from error
    if models.returncode or not isinstance(parsed, (list, dict)):
        raise Refusal('OMP models output is invalid')
    login = _result(run, [str(path), 'login', '--help'])
    if login.returncode:
        raise Refusal('OMP login capability is unavailable')
    return {'path': str(path), 'version': version, 'capabilities': _CAPABILITIES['omp']}


def _parse_herdr_status(text):
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition(': ')
        value = value.strip()
        if not separator or not key or not value or key in fields:
            raise Refusal('Herdr named status output is invalid')
        fields[key] = value
    status = fields.get('status')
    if status == 'not running' and set(fields) == {'status', 'socket'}:
        if Path(fields['socket']).is_absolute():
            return
    if status != 'running' or not Path(fields.get('socket', '')).is_absolute():
        raise Refusal('Herdr named status output is invalid')
    legacy = {'status', 'protocol', 'compatible', 'socket'}
    current = {'status', 'version', 'endpoint_compatible', 'private_protocol',
               'private_protocol_compatible', 'socket'}
    if set(fields) in (legacy, legacy | {'version'}):
        if fields.get('protocol') and fields.get('compatible') == 'yes':
            return
    elif set(fields) == current:
        if (fields.get('version') and fields.get('private_protocol')
                and fields.get('endpoint_compatible') == 'yes'
                and fields.get('private_protocol_compatible') == 'yes'):
            return
    raise Refusal('Herdr named status output is invalid')


def probe_herdr(path, session, run):
    version = _version(path, run, 'Herdr')
    sessions = _result(run, [str(path), 'session', 'list', '--json'])
    try:
        parsed = json.loads(sessions.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise Refusal('Herdr session list output is invalid') from error
    if sessions.returncode or not isinstance(parsed, (list, dict)):
        raise Refusal('Herdr session list output is invalid')
    status = _result(run, [str(path), '--session', session, 'status', 'server'])
    if status.returncode:
        raise Refusal('Herdr named status output is invalid')
    _parse_herdr_status((status.stdout or status.stderr or '').strip())
    return {'path': str(path), 'version': version, 'capabilities': _CAPABILITIES['herdr']}


def probe_treehouse(path, run):
    version = _version(path, run, 'Treehouse')
    if version != 'v3.1.6':
        raise Refusal('Treehouse version check failed')
    return {'path': str(path), 'version': version, 'capabilities': _CAPABILITIES['treehouse']}


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_release(fetch_json, tool):
    release = fetch_json(_RELEASES[tool][0])
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise Refusal('latest release is not stable')
    if not isinstance(release.get('tag_name'), str) or not release['tag_name']:
        raise Refusal('release tag is missing')
    if not isinstance(release.get('assets'), list):
        raise Refusal('release assets are invalid')
    return release


def _https_url(asset):
    url = asset.get('browser_download_url') if isinstance(asset, dict) else None
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != 'https' or not parsed.netloc:
        raise Refusal('release asset URL must use HTTPS')
    return url


def _selected_asset(release, tool):
    name = _RELEASES[tool][1]
    assets = [asset for asset in release['assets'] if isinstance(asset, dict) and asset.get('name') == name]
    if len(assets) != 1:
        raise Refusal('release asset is missing or ambiguous')
    _https_url(assets[0])
    return assets[0]




def _digest(release, asset, tool, stream_url):
    value = asset.get('digest')
    if value is None and tool == 'omp':
        return _checksum_asset_from_stream(release, asset['name'], stream_url)
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise Refusal('release digest is malformed')
    return value[7:]


def _checksum_asset_from_stream(release, name, stream_url):
    assets = [asset for asset in release['assets'] if isinstance(asset, dict) and asset.get('name') == 'SHA256SUMS.txt']
    if len(assets) != 1:
        raise Refusal('release digest is missing')
    url = _https_url(assets[0])
    matcher = re.compile(rb'([0-9a-f]{64})  \*?' + re.escape(name.encode()))
    matches, remainder = [], b''
    for chunk in stream_url(url):
        if not isinstance(chunk, bytes):
            raise Refusal('release digest is malformed')
        lines = (remainder + chunk).split(b'\n')
        remainder = lines.pop()
        matches.extend(match.group(1).decode() for line in lines if (match := matcher.fullmatch(line)))
    if remainder:
        match = matcher.fullmatch(remainder)
        if match:
            matches.append(match.group(1).decode())
    if len(matches) != 1:
        raise Refusal('release digest is malformed')
    return matches[0]


def _target(tool):
    return Path(os.environ.get('HOME', '~')).expanduser() / '.local' / 'bin' / tool


def _install(release, tool, stream_url, run):
    asset = _selected_asset(release, tool)
    expected = _digest(release, asset, tool, stream_url)
    target = _target(tool)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f'.{tool}-', delete=False) as output:
            temporary = output.name
            digest = hashlib.sha256()
            for chunk in stream_url(asset['browser_download_url']):
                if not isinstance(chunk, bytes):
                    raise Refusal('download stream is invalid')
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise Refusal('artifact digest differs from release digest')
        os.chmod(temporary, 0o755)
        probe = probe_omp if tool == 'omp' else lambda path, runner: probe_herdr(path, 'agd-probe', runner)
        details = probe(temporary, run)
        installed = _sha256(temporary)
        os.replace(temporary, target)
        temporary = None
        return {'source': 'managed', **details, 'path': str(target), 'asset': asset['name'], 'tag': release['tag_name'],
                'artifact_sha256': expected, 'installed_sha256': installed}
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def install_omp(home, fetch_json, stream_url, run):
    return _install(_stable_release(fetch_json, 'omp'), 'omp', stream_url, run)


def install_herdr(home, fetch_json, stream_url, run):
    return _install(_stable_release(fetch_json, 'herdr'), 'herdr', stream_url, run)


def _extract_treehouse(archive_path, expected_digest, destination):
    if _sha256(archive_path) != expected_digest:
        raise Refusal('Treehouse archive digest differs from pinned digest')
    temporary = None
    try:
        with tarfile.open(archive_path, 'r:gz') as archive:
            members = iter(archive)
            member = next(members, None)
            if (member is None or next(members, None) is not None or member.name != 'treehouse'
                    or not member.isreg() or member.issym() or member.islnk()):
                raise Refusal('Treehouse archive is unsafe')
            source = archive.extractfile(member)
            if source is None:
                raise Refusal('Treehouse archive is unsafe')
            with source, tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.treehouse-',
                                                      delete=False) as output:
                temporary = output.name
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
        os.chmod(temporary, 0o755)
        version = store.run([temporary, '--version'], check=False)
        if version.returncode or (version.stdout or version.stderr or '').strip() != 'v3.1.6':
            raise Refusal('Treehouse version check failed')
        os.replace(temporary, destination)
        temporary = None
    except (OSError, tarfile.TarError) as error:
        raise Refusal('Treehouse archive is invalid') from error
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def install_treehouse(home, stream_url):
    target = _target('treehouse')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.treehouse-', delete=False) as output:
            archive = output.name
            digest = hashlib.sha256()
            for chunk in stream_url(_TREEHOUSE_URL):
                if not isinstance(chunk, bytes):
                    raise Refusal('Treehouse download stream is invalid')
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != _TREEHOUSE_DIGEST:
            raise Refusal('Treehouse archive digest differs from pinned digest')
        _extract_treehouse(archive, _TREEHOUSE_DIGEST, target)
        installed = _sha256(target)
        return {'source': 'managed', 'path': str(target), 'version': 'v3.1.6',
                'capabilities': _CAPABILITIES['treehouse'], 'asset': _TREEHOUSE_ASSET,
                'tag': 'v3.1.6', 'artifact_sha256': _TREEHOUSE_DIGEST,
                'installed_sha256': installed}
    finally:
        if archive and os.path.exists(archive):
            os.unlink(archive)


def validate_dependencies(home, run):
    receipt = read_dependencies(home)
    result = {'schema': 1}
    for tool, entry in receipt.items():
        if tool == 'schema':
            continue
        path = Path(entry['path'])
        if entry['source'] == 'managed' and _sha256(path) != entry['installed_sha256']:
            raise Refusal('installed digest differs from receipt')
        if tool == 'omp':
            probe = probe_omp(path, run)
        elif tool == 'herdr':
            probe = probe_herdr(path, 'agd-probe', run)
        else:
            probe = probe_treehouse(path, run)
        result[tool] = {**entry, **probe}
    return _validate_receipt(result)


def setup(home, backend, prompt, fetch_json, stream_url, run):
    if backend not in {'tmux', 'herdr'}:
        raise Refusal('selected backend is invalid')
    tools = ['omp'] + (['herdr'] if backend == 'herdr' else [])
    receipt = {'schema': 1}
    for tool in tools:
        path = shutil.which(tool)
        probe = probe_omp if tool == 'omp' else lambda item, runner: probe_herdr(item, 'agd-probe', runner)
        existing = None
        if path:
            try:
                existing = {'source': 'existing', **probe(path, run)}
            except Refusal:
                pass
        release = _stable_release(fetch_json, tool)
        message = f'{tool} {release["tag_name"]} is available; install or update?'
        if not prompt(message):
            if existing is None:
                raise Refusal(f'{tool} requirement is unmet')
            receipt[tool] = existing
            continue
        receipt[tool] = _install(release, tool, stream_url, run)
    write_dependencies(home, receipt)
    return receipt
