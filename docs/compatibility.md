# Runtime compatibility observations

Development host: Linux x86-64. Treehouse v3.1.6 linux-amd64 archive SHA-256 `e89891caf913b3b228387c7dccf53ff908327fca47839b7957681084578cb1a1` matched the published asset digest. Installed Treehouse v2.3.0 lacks the `get --branch` and `--base` flags and is not used for task allocation. Isolated tests place the pinned v3.1.6 binary first on their process PATH; the global installation was not modified.

`python3 tests/test_projects.py` and `PATH=/tmp/agd-treehouse-v3.1.6:$PATH python3 tests/test_tasks.py` exercised local project registration and two distinct leased worktrees on 2026-10-25. These checks do not validate worker harnesses, Herdr, or background supervision.

`python3 tests/test_backends.py tmux` exercised a real tmux 3.4 server with an isolated socket namespace, verified endpoint identity, bounded output capture, literal input delivery, refusal of a wrong owner token, and owned-pane closure. The process was a shell probe, not an agent.
