# Worker profiles and native skill provisioning

## Approval and scope

The user approved flat worker profiles, deterministic profile-to-skill mapping, an explicit orchestrator-selected curated mode, task-local immutable snapshots, and native harness discovery. This document records that design for implementation. It does not authorize implementation, publication, merging, unattended execution or changes to a running instance.

Task kind (`ship` or `scout`) remains independent of worker profile. Skills are instructions, not authority, isolation or a guarantee of compliance. Project/global/profile skills may remain visible through native discovery.

## Operator contract

Instance-local `config/workers.json` defines `default_profile` and `profiles`, each containing an explicit `skills` array. `config/worker-skills/<id>/` contains complete native skill bundles (`SKILL.md` plus optional scripts/references). These paths are relative to the effective private home, not necessarily the distro checkout. This feature implements their first real consumer; it does not make `config/` a generic settings system.

```json
{
  "default_profile": "implementer",
  "profiles": {
    "implementer": {"skills": ["agd-python", "agd-code-review"]},
    "reviewer": {"skills": ["agd-code-review", "agd-security-review"]},
    "researcher": {"skills": []}
  }
}
```

The `agd-` prefix is reserved for provisioned skill identities. Catalog directory and frontmatter `name` must match. The examples are operator-created bundles, not a promise to ship Python/security curricula.

- `worker launch TASK --profile implementer`: exact profile selection.
- `worker launch TASK`: configured default profile.
- `worker launch TASK --skill-mode curated --skill agd-python --skill agd-code-review`: explicit selection solely from the catalog; the orchestrator, not the CLI, chooses.
- Curated mode requires at least one skill; an empty profile is the deliberate no-skills choice. Profiles and curated selections cannot be combined.
- `worker skills list` exposes sorted IDs/descriptions; `worker profiles list` exposes profiles/default; `worker inspect TASK` includes resolved selection and hashes.
- No configuration and no selection preserves existing launch behavior with an explicitly recorded empty selection. Explicit profile/skill requests must never silently degrade to this case. Legacy tasks without selection records are treated as having no provisioned skills on relaunch, not assigned a new default.

Missing/invalid profiles, missing bundles, malformed frontmatter, duplicate entries, unsafe paths, collisions or unsupported native loading fail before a new endpoint starts. Unknown config fields are errors. No profile inheritance, wildcard selection, remote fetching, automatic skill updates or profile-plus-overrides.

## Provisioning and lifecycle

Resolve and snapshot before spawn. Snapshot the entire bundle into private task data, including relative resources and executable permission bits. Validate actual copied bytes and store deterministic SHA-256 bundle hashes. The snapshot is immutable by lifecycle convention; it is not an OS security boundary.

Record selection mode, profile when present, bundle names/hashes, provisioned paths and artifact ownership. Relaunch reuses and verifies the recorded snapshot, even if config/catalog changes or disappears. Missing generated copies may be restored from the snapshot; altered copies or snapshots are refused, never overwritten silently.

Materialize regular copies in a harness's verified native skill root before it starts. Never link to the mutable catalog, rewrite project instructions, or load the central extension into workers. Preserve name and resource paths. Name the assigned skills in the launch prompt and require the worker to read them before substantive work; do not label this enforced compliance.

Refuse existing destination directories, symlinked parents, tracked destinations and duplicate skill names found in native project discovery roots. Global/profile collisions and precedence require harness-specific evidence; fail closed on detectable ambiguity, and document unavailable visibility rather than claim exclusivity.

Generated files must not enter project commits or change tracked `.gitignore`. Use exact local exclusion entries with shared-Git-directory ownership tracking. Verify generated artifacts independently of Git cleanliness. Readiness, landing/publication and lease return must reject tracked or modified generated artifacts. Preserve unexpected content, including during approved task discard, until separately resolved. Remove only owned, unchanged generated content after worker absence is proved. Retain snapshot/manifest evidence after lease return.

## Harness and verification boundary

Verify installed OMP and Cursor native discovery, metadata/name handling, full resource lookup, duplicate precedence and relevant user/global sources. OMP's extension directory is not a skill directory. Provisioning support is enabled per verified adapter, not inferred from Firstmate's layout. An unverified adapter must explicitly refuse a nonempty skill set; empty selections preserve existing compatibility behavior. Cursor live acceptance requires authentication and must not be represented as passing without it.

Demonstrate discovery through the real harness and separately exercise a worker reading its skill/resource. Use disposable homes/projects and scoped user approval for any credentialed/autonomous worker run. No mutation of the current central session or registered user projects.

## Completion criteria

Both selection modes, catalog/profile inspection, validation, crash-safe snapshot/materialization, frozen relaunch, safe Git handling, collision preservation, cleanup and worker prompt integration must be implemented end-to-end. Existing unconfigured launches remain valid. Docs and the customization skill explain the new config consumer and deterministic-set versus exclusive-set distinction. Native support claims must name exercised harness versions; unmet Cursor prerequisites remain explicit acceptance gates, not a claimed completion.
