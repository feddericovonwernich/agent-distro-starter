# Public named-instance installer design

## Scope and readiness

Publish this existing distro source as a public repository at `feddericovonwernich/agent-distro-starter`, with the tested feature branch fast-forwarded to `main`. This is for user testing, not a claim that all worker combinations work: live OMP/tmux and OMP/Herdr paths were exercised on Linux; both Cursor paths await authentication and live verification. Repository creation and push are authorized by the user's explicit request, but no other remote publication, task approval, or PR merge follows from it.

## Named local instance

The README's one-command install invokes a root-level `install.sh` fetched over HTTPS from this repository's `main` branch. The script requires Bash, Git and Python 3.12+, asks on the terminal for a name, and accepts only a single lowercase ID matching the distro's existing `[a-z][a-z0-9-]{0,47}` rule. It rejects noninteractive input, empty or invalid names, and existing paths (including symlinks) before making changes. It clones the published repository into a new directory with that name in the caller's current directory, **preserves the template origin** so updates remain possible, and runs `bin/agd init` inside the clone. `init` creates ignored, instance-local private state and a unique home ID; it neither registers projects nor starts workers. Failures are reported without removing a partially created clone, so nothing user-owned can be deleted accidentally.

The README gives the exact one-liner, the Git/OMP/tmux/Treehouse v3.1.6/Python prerequisites, the commands to enter the new directory and run `bin/agd doctor`, and the separate named Herdr requirement. The installer does not install tools, configure accounts, change global Git state, grant project trust, or impersonate Cursor readiness. Users should review the script before running a downloaded command.

## Verification and release boundary

Exercise the script against a local Git source by redirecting only its template clone URL during the test, with a terminal-driven name: verify named path, unique initialized home, retained `origin`, independent ignored state, and refusal for an existing path or invalid name. Exercise the published one-liner against the actual public repository after pushing. Before making a repository public, inspect all tracked files and history for private state, secrets and accidental local test data; update compatibility/operations text describing previously retained ship tasks to reflect the approved local landing/discard. Commit only reviewed changes, publish `main` to the authenticated account, and confirm GitHub visibility and public install URL. Never publish ignored private operator state or claim Cursor combinations tested.
