# Agent distro starter

Launch the central agent with `omp` from this directory. The versioned repository will provide procedures and tools; `projects/`, `config/`, `data/`, and `state/` are private to the local home.

The first implemented slice manages the local project registry:

```sh
bin/agd init
bin/agd project add app --source /path/to/repository.git --base main
bin/agd project list
bin/agd project inspect app
bin/agd project refresh app
bin/agd project remove app
```

`remove` without clone deletion unregisters the project and preserves the clone. Refresh refuses a dirty or diverged checkout. The agent must work in a task worktree, not in the stable clone; that task lifecycle is planned but not yet implemented.
