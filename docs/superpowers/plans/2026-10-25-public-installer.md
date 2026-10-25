# Public Named-Instance Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offer a tested one-command prompt-and-clone distro installation, then publish its reviewed source as a public GitHub repository for user testing.

**Architecture:** A root Bash script prompts on a real terminal, validates a single directory name, clones the template with origin intact, then invokes the existing `agd init`. One behavioral Python script uses a local Git URL substitution and a pseudoterminal to exercise the actual installer without publishing first. The existing feature worktree is isolated; release fast-forwards the separate clean `main` checkout only after verification.

**Tech Stack:** Bash, Git, Python 3.12+, `script(1)` in the Linux test only, GitHub CLI for authorized publication.

**Spec:** `docs/superpowers/specs/2026-10-25-public-installer-design.md`

## Global Constraints

- Public template: `feddericovonwernich/agent-distro-starter`, default branch `main`; retain cloned `origin`.
- Names: `[a-z][a-z0-9-]{0,47}`; reject any existing path including a broken symlink; no destructive rollback.
- `bin/agd init` creates a unique ignored home; no project registration, worker launch, automatic dependency installation or login.
- Tested Linux OMP/tmux and OMP/Herdr only; Cursor paths await authentication and live verification.
- Do not publish ignored private state, test data, credentials or any other remote; no PR merge.

---

### Task 1: Installer, behavioral check and honest documentation

**Files:** Create `install.sh`, `tests/test_install.py`; modify `README.md`, `docs/compatibility.md`, `CHANGELOG.md`.

**Interfaces:** The public command is `bash -c 'installer=$(curl -fsSL https://raw.githubusercontent.com/feddericovonwernich/agent-distro-starter/main/install.sh) && bash -c "$installer"'`. The fetch must succeed before executing Bash. The script reads a single name from `/dev/tty`, clones `https://github.com/feddericovonwernich/agent-distro-starter.git` into `./NAME`, runs `./NAME/bin/agd init`, and exits nonzero without changing an existing target.

- [ ] **Step 1: Write one behavioral check before the script.** Create `tests/test_install.py`:

```python
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://github.com/feddericovonwernich/agent-distro-starter.git'


def test_named_install():
    with tempfile.TemporaryDirectory(prefix='agd-install-') as directory:
        root = Path(directory)
        origin = root / 'template.git'
        subprocess.run(['git', 'clone', '--bare', str(ROOT), str(origin)],
                       check=True, capture_output=True)
        (root / 'decoy/my-distro').mkdir(parents=True)
        env = dict(os.environ, AGD_HOME=str(root / 'unrelated-home'), CDPATH=str(root / 'decoy'),
                   GIT_CONFIG_COUNT='1',
                   GIT_CONFIG_KEY_0=f'url.file://{origin}.insteadOf',
                   GIT_CONFIG_VALUE_0=SOURCE)

        def install(name):
            return subprocess.run(
                ['script', '-q', '-e', '-c',
                 f'bash {shlex.quote(str(ROOT / "install.sh"))}', '/dev/null'],
                input=name + '\n', cwd=root, env=env, capture_output=True,
                text=True, timeout=90)

        first = install('my-distro')
        assert first.returncode == 0, (first.stdout, first.stderr)
        first_home = root / 'my-distro'
        first_id = json.loads((first_home / 'data/home.json').read_text())['home_id']
        assert not (root / 'unrelated-home').exists()
        assert subprocess.check_output(
            ['git', '-C', str(first_home), 'config', '--get', 'remote.origin.url'],
            text=True).strip() == SOURCE
        assert subprocess.check_output(
            ['git', '-C', str(first_home), 'status', '--porcelain=v1'],
            text=True) == ''
        second = install('another-distro')
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert json.loads((root / 'another-distro/data/home.json').read_text())['home_id'] != first_id
        sentinel = first_home / 'keep'
        sentinel.write_text('mine')
        assert install('my-distro').returncode != 0
        assert sentinel.read_text() == 'mine'
        assert install('Bad Name').returncode != 0
        assert not (root / 'Bad Name').exists()


if __name__ == '__main__':
    test_named_install()
```

- [ ] **Step 2: Run `python3 tests/test_install.py` and require the missing-script failure.** A test failure for a different reason requires fixing the test first.
- [ ] **Step 3: Implement `install.sh` with Bash `set -euo pipefail` and exactly the approved behavior:**

```bash
#!/usr/bin/env bash
set -euo pipefail
command -v git >/dev/null || { echo 'Git required' >&2; exit 1; }
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
    { echo 'Python 3.12+ required' >&2; exit 1; }
[[ -t 0 && -r /dev/tty ]] || { echo 'Interactive terminal required' >&2; exit 1; }
read -r -p 'Distro name: ' name </dev/tty
[[ $name =~ ^[a-z][a-z0-9-]{0,47}$ ]] || { echo 'Invalid distro name' >&2; exit 1; }
[[ ! -e $name && ! -L $name ]] || { echo 'Destination exists' >&2; exit 1; }
git clone -- https://github.com/feddericovonwernich/agent-distro-starter.git "$name"
(cd -- "./$name" && bin/agd --home "$PWD" init)
printf 'Distro ready: cd -- ./%q\n' "$name"
```

- [ ] **Step 4: Run `python3 tests/test_install.py` and `bash -n install.sh` to green.** Verify the actual pseudoterminal path, not installer source text.
- [ ] **Step 5: Update public docs and commit.** Put the fetch-checked one-liner from Interfaces above in `README.md` with prerequisites, `cd NAME && bin/agd doctor`, the warning to review downloaded scripts, and the fact `doctor` does not authenticate accounts. Correct `docs/compatibility.md` and `CHANGELOG.md` to state the approved local landing/discard and deferred Cursor. Run existing Python scripts, `bun test tests/primary.test.ts`, and `git diff --check`. Commit only `install.sh`, `tests/test_install.py`, README, compatibility, changelog and this plan:

```bash
git add install.sh tests/test_install.py README.md docs/compatibility.md CHANGELOG.md docs/superpowers/plans/2026-10-25-public-installer.md
git diff --cached --check
git commit -m 'feat: install named distro instances from public template'
```

### Task 2: Release review, public repository, and actual one-liner smoke

**Files:** No new source files; only add publication facts to `README.md` or docs if verified reality requires a correction, and commit those changes before pushing.

**Interfaces:** Resolve the main checkout from the shared Git directory: `MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")`. `gh repo create feddericovonwernich/agent-distro-starter --public --source "$MAIN" --remote origin` creates the authorized public repository. A clean local `main` checkout fast-forwards to `feat/agent-distro`; `git -C "$MAIN" push -u origin main` publishes that exact head. User-owned changes must not be reset or overwritten.

- [ ] **Step 1: Audit tracked paths and Git history.** Run `git ls-files`, `git log --all --oneline`, and `git status --porcelain=v1` in the implementation worktree and clean main checkout; inspect suspicious content with `git show` and targeted searches. Verify `/data/`, `/state/`, `/projects/`, `/config/` and `.env` remain ignored with `git check-ignore -v`. Stop on sensitive history or unexpected edits.
- [ ] **Step 2: Verify candidate and fast-forward local main.** Run these commands in the implementation worktree:

```bash
python3 tests/test_install.py
PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_projects.py
PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_tasks.py
PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_delivery.py
PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_backends.py tmux
PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_workers.py
PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_supervision.py
bun test tests/primary.test.ts
git merge-base --is-ancestor main feat/agent-distro
```

Herdr live tests require a separately started named non-default Herdr server and `HERDR_ENV=1`; do not silently use a default or unavailable server. Resolve `MAIN` as above, verify that checkout is on clean `main`, then run `git -C "$MAIN" merge --ff-only feat/agent-distro` and compare both exact HEADs.
- [ ] **Step 3: Create and push the public repository.** Recheck `gh auth status` and the target repository name. If it does not already exist, run:

```bash
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
gh repo create feddericovonwernich/agent-distro-starter --public --source "$MAIN" --remote origin
git -C "$MAIN" push -u origin main
```

Do not create or push any other repository or branch.
- [ ] **Step 4: Exercise the actual public one-liner.** Run `gh repo view feddericovonwernich/agent-distro-starter --json url,visibility,defaultBranchRef`; in a fresh disposable directory launch the fetch-checked command in Task 1 Interfaces in a terminal, answer `release-smoke`, and confirm `release-smoke/data/home.json`, retained Git origin, and `release-smoke/bin/agd doctor` output. Inspect the published Git head and report exact URL/commit, verified paths and remaining Cursor limits.
