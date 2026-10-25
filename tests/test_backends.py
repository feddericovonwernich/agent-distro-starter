import importlib
import sys
import tempfile
import time
from pathlib import Path
from support import command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'bin'))
from agd_lib.store import Refusal


def check_backend(name):
    assert name in ('tmux', 'herdr')
    backend = importlib.import_module('agd_lib.' + name)
    with tempfile.TemporaryDirectory(prefix='agd-pane-') as tmp:
        home = Path(tmp)/'home'
        command(home, 'init')
        endpoint = backend.create(home, 'probe', 'incarnation-one', home,
                                  ['/bin/sh', '-c', 'printf "agd-live-check\\n"; sleep 30'])
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if 'agd-live-check' in backend.capture(endpoint):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('probe output never appeared')
            assert backend.inspect(endpoint)['presence'] == 'present'
            foreign = dict(endpoint, token='not-the-owner')
            try:
                backend.close(foreign)
            except Refusal:
                pass
            else:
                raise AssertionError('foreign close was accepted')
            assert backend.inspect(endpoint)['presence'] == 'present'
        finally:
            backend.close(endpoint)
        if name == 'tmux':
            endpoint = backend.create(home, 'input', 'incarnation-two', home, ['/bin/cat'])
            try:
                sentinel = home/'must-not-exist'
                payload = f'literal; touch {sentinel}'
                backend.send_text(endpoint, payload)
                backend.send_key(endpoint, 'enter')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if payload in backend.capture(endpoint):
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError('literal input not delivered')
                assert not sentinel.exists()
            finally:
                backend.close(endpoint)


if __name__ == '__main__':
    check_backend(sys.argv[1])
