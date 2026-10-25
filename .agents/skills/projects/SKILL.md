---
name: distro-projects
description: Register, refresh, trust or remove a managed project clone in the agent distro.
---

# Managed projects

Run from the distro checkout; set `AGD_HOME` if this is an external private home. `bin/agd doctor` checks selected executable versions. `bin/agd project list` and `project inspect ID` identify a registered clone. Add with `project add ID --source URL_OR_PATH --base BRANCH`, or register an already existing canonical clone under this home's `projects/ID` via `project register ID --path ABSOLUTE_PATH --base BRANCH`. `project refresh ID` fetches and fast-forwards only a clean registered base.

For Cursor trust, obtain the user's exact instruction in a file, then `project trust ID --instruction FILE`. Registration never implies trust. `project remove ID` unregisters but leaves clone bytes untouched. Clone deletion requires a separate instruction: `project approve ID --action delete-clone --instruction FILE`, then `project remove ID --delete-clone --approval APPROVAL_ID`. Retained tasks, dirty checkout, extra refs or unpublished reflog commits refuse deletion; do not bypass with a shell delete. Inspect the result and report preserved work.
