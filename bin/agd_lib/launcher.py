import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agd_lib import observability, store, supervision


@dataclass(frozen=True)
class AttachTarget:
    attach_argv: list[str]
    env: dict[str, str]


def initialize(home: Path, backend: str) -> None:
    config = home / 'data' / 'launcher.json'
    if backend not in {'tmux', 'herdr'} or home.is_symlink() or config.exists() or config.is_symlink():
        raise store.Refusal('fresh non-symlink launcher home required')
    store.atomic_json(config, {
        'schema': 1,
        'backend': backend,
        'herdr_session': f'agd-{uuid.uuid4().hex}',
    })


def resolve_backend(home: Path, explicit_backend: str | None) -> str:
    path = home / 'data' / 'launcher.json'
    if path.is_symlink() or not path.is_file():
        raise store.Refusal('launcher config is unsafe')
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise store.Refusal('launcher config is malformed') from error
    if (not isinstance(config, dict) or set(config) != {'schema', 'backend', 'herdr_session'}
            or config.get('schema') != 1 or config.get('backend') not in {'tmux', 'herdr'}
            or not isinstance(config.get('herdr_session'), str)
            or not re.fullmatch(r'agd-[0-9a-f]{32}', config['herdr_session'])):
        raise store.Refusal('launcher config is malformed')
    return explicit_backend or config['backend']


def _receipt_path(home: Path) -> Path:
    return home / 'state' / 'central-launcher.json'


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def _read_receipt(home: Path) -> tuple[dict, bytes] | tuple[None, None]:
    path = _receipt_path(home)
    if not path.exists():
        return None, None
    if not path.is_file() or path.is_symlink():
        raise store.Refusal('launcher receipt is malformed')
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise store.Refusal('launcher receipt is malformed') from error
    _validate_receipt(value)
    if raw != _bytes(value):
        raise store.Refusal('launcher receipt bytes changed')
    return value, raw


def _validate_receipt(value: object) -> None:
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('backend') not in {'tmux', 'herdr'}:
        raise store.Refusal('launcher receipt is malformed')
    try:
        uuid.UUID(value.get('launch_id'))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('launcher receipt is malformed') from error
    if (not isinstance(value.get('endpoint_token'), str) or not value['endpoint_token']
            or value.get('phase') not in {'reserved', 'spawned', 'ready'}
            or not isinstance(value.get('endpoint'), dict)
            or not all(isinstance(item, str) for item in value.get('managed_command', ()))
            or not all(isinstance(item, str) for item in value.get('expected_process_argv', ()))
            or value.get('owner') is not None and not isinstance(value['owner'], dict)
            or value.get('watcher') is not None and not isinstance(value['watcher'], dict)):
        raise store.Refusal('launcher receipt is malformed')


def _owner(home: Path, receipt: dict, required: bool) -> dict | None:
    path = home / 'state' / 'owner.json'
    if not path.exists():
        if required:
            raise store.Refusal('central owner is missing')
        return None
    if not path.is_file() or path.is_symlink():
        raise store.Refusal('central owner is malformed')
    try:
        owner = json.loads(path.read_text())
        keys = ('owner_pid', 'owner_start', 'token', 'watcher_pid', 'watcher_start', 'heartbeat')
        if not isinstance(owner, dict) or any(key not in owner for key in keys):
            raise ValueError
        if owner['token'] != receipt['endpoint_token']:
            raise store.Refusal('foreign central owner')
        if supervision.process_start(owner['owner_pid']) != owner['owner_start']:
            raise store.Refusal('central owner identity changed')
        if supervision.process_start(owner['watcher_pid']) != owner['watcher_start']:
            raise store.Refusal('central watcher identity changed')
        if not isinstance(owner['heartbeat'], (int, float)) or time.time() - owner['heartbeat'] >= 12:
            raise store.Refusal('central watcher is unhealthy')
        return owner
    except (ValueError, TypeError, KeyError, OSError) as error:
        if isinstance(error, store.Refusal):
            raise
        raise store.Refusal('central owner is malformed') from error


def _advance(home: Path, expected: dict, replacement: dict) -> None:
    _validate_receipt(replacement)
    with store.lock(home):
        current, raw = _read_receipt(home)
        if current != expected or raw != _bytes(expected):
            raise store.Refusal('launcher receipt changed')
        _owner(home, expected, False)
        store.atomic_json(_receipt_path(home), replacement)


def _inheritance(home: Path, receipt: dict, inherited_env: Mapping[str, str] | None) -> dict[str, str]:
    token_hash = hashlib.sha256(receipt['endpoint_token'].encode()).hexdigest()
    result = {'AGD_HOME': str(home), 'AGD_LAUNCHER_BACKEND': receipt['backend'],
              'AGD_LAUNCH_ID': receipt['launch_id'], 'AGD_ENDPOINT_TOKEN_HASH': token_hash}
    if inherited_env:
        for key, value in result.items():
            if key in inherited_env and inherited_env[key] != value:
                raise store.Refusal('inherited launcher environment differs')
    return result


def _prove(home: Path, receipt: dict, inspect) -> dict:
    observed = inspect(home, receipt)
    if not isinstance(observed, dict) or observed.get('endpoint') != receipt['endpoint']:
        raise store.Refusal('central endpoint continuity is unproven')
    foreground = observed.get('foreground')
    if not isinstance(foreground, dict) or any(foreground.get(key) != receipt[key]
                                                for key in ('launch_id', 'endpoint_token')):
        raise store.Refusal('central foreground correlation is unproven')
    owner = _owner(home, receipt, True)
    if (foreground.get('pid') != owner['owner_pid'] or foreground.get('start') != owner['owner_start']
            or foreground.get('argv') != receipt['expected_process_argv']):
        raise store.Refusal('central foreground process differs')
    if receipt['phase'] == 'ready' and (receipt['owner'] != {'pid': owner['owner_pid'], 'start': owner['owner_start']}
                                        or receipt['watcher'] != {'pid': owner['watcher_pid'], 'start': owner['watcher_start']}):
        raise store.Refusal('central owner snapshot differs')
    return owner


def _backend_inspect(home: Path, receipt: dict) -> dict:
    module = __import__(f'agd_lib.{receipt["backend"]}', fromlist=['inspect_central'])
    inspect = getattr(module, 'inspect_central', None)
    if not callable(inspect):
        raise store.Refusal('central backend is unavailable')
    return inspect(home, receipt)


def _backend_create(backend: str, home: Path, launch_id: str, endpoint_token: str, managed: list[str],
                    env: dict[str, str], argv: list[str]) -> dict:
    module = __import__(f'agd_lib.{backend}', fromlist=['create_central'])
    create = getattr(module, 'create_central', None)
    if not callable(create):
        raise store.Refusal('central backend is unavailable')
    return create(home, launch_id, endpoint_token, managed, env, argv)


def _backend_finalize(home: Path, receipt: dict) -> None:
    module = __import__(f'agd_lib.{receipt["backend"]}', fromlist=['finalize_central'])
    finalize = getattr(module, 'finalize_central', None)
    if not callable(finalize):
        raise store.Refusal('central backend is unavailable')
    finalize(home, receipt)


def _backend_attach(home: Path, receipt: dict) -> list[str]:
    module = __import__(f'agd_lib.{receipt["backend"]}', fromlist=['attach_argv'])
    attach = getattr(module, 'attach_argv', None)
    if not callable(attach):
        raise store.Refusal('central backend is unavailable')
    return attach(home, receipt)


def active_backend(home: Path, inherited_env: Mapping[str, str] | None = None) -> str | None:
    resolve_backend(home, None)
    receipt, _ = _read_receipt(home)
    if receipt is None:
        return None
    if receipt['phase'] != 'ready':
        raise store.Refusal('central launch was interrupted')
    _inheritance(home, receipt, inherited_env)
    _prove(home, receipt, _backend_inspect)
    return receipt['backend']


def prepare_start(home: Path, explicit_backend: str | None, inherited_env: Mapping[str, str] | None,
                  create, inspect, attach) -> AttachTarget:
    selected = resolve_backend(home, explicit_backend)
    receipt, _ = _read_receipt(home)
    if receipt is not None:
        if receipt['phase'] != 'ready':
            raise store.Refusal('central launch was interrupted')
        if selected != receipt['backend']:
            raise store.Refusal('explicit backend differs from active central')
        env = _inheritance(home, receipt, inherited_env)
        _prove(home, receipt, inspect)
        return AttachTarget(attach(home, receipt), env)
    argv, central_env, _warning = observability.central_argv(home, ())
    launch_id, endpoint_token = str(uuid.uuid4()), uuid.uuid4().hex
    managed = [str(Path(__file__).resolve().parents[1] / 'agd'), '--home', str(home), 'observability', 'central']
    reserved = {'schema': 1, 'launch_id': launch_id, 'endpoint_token': endpoint_token, 'backend': selected,
                'phase': 'reserved', 'endpoint': {}, 'managed_command': managed,
                'expected_process_argv': argv, 'owner': None, 'watcher': None}
    with store.lock(home):
        existing, _ = _read_receipt(home)
        if existing is not None:
            raise store.Refusal('launcher receipt appeared during reservation')
        _owner(home, reserved, False)
        store.atomic_json(_receipt_path(home), reserved)
    endpoint = create(selected, home, launch_id, endpoint_token, managed,
                      {**central_env, 'AGD_LAUNCH_ID': launch_id, 'AGD_ENDPOINT_TOKEN': endpoint_token}, argv)
    spawned = {**reserved, 'phase': 'spawned', 'endpoint': endpoint}
    _advance(home, reserved, spawned)
    if create is _backend_create:
        _backend_finalize(home, spawned)
    owner = _prove(home, spawned, inspect)
    ready = {**spawned, 'phase': 'ready',
             'owner': {'pid': owner['owner_pid'], 'start': owner['owner_start']},
             'watcher': {'pid': owner['watcher_pid'], 'start': owner['watcher_start']}}
    _advance(home, spawned, ready)
    env = _inheritance(home, ready, inherited_env)
    return AttachTarget(attach(home, ready), env)
