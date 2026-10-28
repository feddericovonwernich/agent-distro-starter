import uuid
from pathlib import Path

from agd_lib import store


def initialize(home: Path, backend: str) -> None:
    config = home / 'data' / 'launcher.json'
    if backend not in {'tmux', 'herdr'} or home.is_symlink() or config.exists() or config.is_symlink():
        raise store.Refusal('fresh non-symlink launcher home required')
    store.atomic_json(config, {
        'schema': 1,
        'backend': backend,
        'herdr_session': f'agd-{uuid.uuid4().hex}',
    })
