# Scoped worker PR relay

## Status and scope

The user approved local implementation in the designated feature worktree. This does **not** authorize a live GitHub push, PR creation, merge, Relay installation, or change to any running instance. A disposable pinned Relay custom-event smoke proved correlated POST/poll delivery; its delayed 30-second lease reply expired with `lease authority expired`.

The feature applies only to a `ship` task with an orchestrator-recorded PR publication grant from a user instruction that names that exact task and `<remote>:<assigned-branch>` target. This is a new task action, distinct from the existing `publish` approval: existing `publish` remains bound to one ready `HEAD` and therefore cannot authorize an iterative repair loop. The existing worker type remains in use; a PR-capable worker is not a new trust class. Normal task launches and their default skills remain unchanged.

Success is an open, unmerged PR at the task's assigned branch whose *current* head is the worker's local `HEAD` and which satisfies GitHub's effective required checks and reviews. The worker never merges. An unresolvable condition is durable `blocked`, not a fabricated success or an endless busy loop.

## Alternatives considered

1. **Central-only publication after readiness.** This preserves the current prompt prohibition, but it separates a worker from the feedback loop it must repair and makes each repair require a central handoff. It does not meet the requested worker polling and repair behavior.
2. **A new PR worker type or unrestricted publishing skill.** This would create a second lifecycle and broadens authority beyond a single user instruction. It also makes ordinary worker defaults harder to reason about.
3. **Recommended: an orchestrator-owned scoped PR publication operation with a selectively assigned Relay worker skill.** The orchestrator validates the dedicated task/target grant and writes durable facts; the existing worker performs only the bounded operations on its leased branch. Relay is feedback transport, and GitHub is final authority. This reuses task authorization-record, workspace-verification, durable-state, worker-relaunch, and skill-snapshot patterns without making prompt text a security control.

## Architecture and responsibilities

### Orchestrator

The orchestrator owns the task record, authorization record, Relay lifecycle, and all authority checks. Before the worker receives PR capability, it must record a dedicated `pr-publish` grant containing:

- the exact task ID;
- action `pr-publish`;
- the exact target `<remote>:<task.branch>`;
- the verbatim user instruction; and
- the task's assigned branch, base, and `HEAD` at grant time as audit evidence.

Unlike existing `publish`, the grant scopes an iterative PR lifecycle for that one task and target rather than one immutable final head. Each candidate head must still be a descendant of the grant head on the same leased task branch; a reset, divergent history, changed branch/base/remote, or a task/target mismatch refuses publication. Repair commits therefore remain authorized without silently widening to another task, branch, or remote. The orchestrator must not reinterpret a broad instruction, a worker report, Relay event, or a previous PR as a `pr-publish` grant.

A new task-local PR receipt (under the existing private task state) records workflow observations, grant ID, remote/repository/base/branch, local head, remote head, PR number/URL/node ID, Relay project correlation, latest processed feedback/lease receipt, and verification evidence. It is not a second task phase or state machine: the task remains `running` under the existing lifecycle until the final `ready` transition. Writes are atomic and protected by the existing home lock. The receipt is evidence, not editable configuration.

The orchestrator exposes one narrowly scoped worker operation, conceptually `worker publish-pr`, rather than asking the worker to run raw forge commands. It is permitted only for the current task incarnation and only while the grant remains valid. The operation:

1. revalidates task, incarnation, lease, `pr-publish` grant, clean worktree, grant-head ancestry, assigned branch, remote/repository/base, and local head;
2. non-force pushes only `HEAD` to the assigned branch; it refuses if the remote branch cannot be advanced safely;
3. records the resulting remote head;
4. finds and reconciles an existing PR for the exact repository/head branch/base before attempting creation; and
5. creates at most one PR and durably records its identity and forge response.

After a runner crash or relaunch, the operation reconciles the forge first. A matching PR is adopted only when repository, head branch, base branch, and recorded/published head agree. Ambiguous candidate PRs, a closed PR, a mismatched branch/base/repository, or an unknown create outcome block for human resolution; they never cause duplicate PR creation. The operation never force-pushes, merges, tags, changes a base branch, or operates on another task's branch.

The current `publication_record` is intentionally a post-`ready` record. This design changes the lifecycle so readiness occurs **after** final PR verification at the final head; publication evidence remains durable in the PR receipt. The implementation plan must define the smallest compatible extension of `tasks.ready`/publication recording so the final ready transition cannot accept an unverified or superseded head.

### Worker and prompt

The launch prompt remains prohibitive by default. Only a task with the recorded PR grant receives an AGD-owned concise skill that names the permitted lifecycle and directs the worker to the guarded AGD operations. It explicitly says that the skill is neither authority nor an OS sandbox, that no merge/force/tag is allowed, and that unavailable or ambiguous conditions must be reported as blocked.

The skill is selected in curated mode only after the grant is present; it is not part of `DEFAULT_SKILLS`, a default profile, or ordinary task selection. It is frozen and materialized using the existing worker-skill catalog and per-task snapshot mechanism. On delivery, the worker promptly verifies identity, durably records pending feedback, and sends `start` plus a schema-valid `complete` acknowledgement before any potentially lengthy repair or test. That acknowledgement means receipt, not completed repair: a crash afterwards resumes the persisted pending feedback. The worker then makes the smallest justified repair in its assigned worktree, commits it, and invokes the guarded non-force publication operation again. It stays active until final GitHub verification succeeds or it emits an actionable durable blocked/decision event. It settles each lease before another poll; an uncertain or expired reply authority is durable `blocked`, never silently discarded.

The worker polls both the standard-CI `pr.ci.settled@1` and AGD-owned human-feedback `agd.pr.review@1` definitions using its task's exact lowercase `github:<owner/repository>:pull-request:<number>` correlation ID. `pr.automation.settled@1` is not a required wake: the pinned default `configured-tools` aggregate waits for all configured Sonar/Copilot/Bugbot recognizers and can never settle for a standard Actions-only repository. The worker's Relay client commands use only:

```text
relay agent poll AGD_WORKER_ID --project-root CENTRAL_RELAY_ROOT --definitions pr.ci.settled@1,agd.pr.review@1 --structured-output --context-tokens 5000 --correlation-id github:owner/repository:pull-request:NUMBER
relay agent reply LEASE_ID ACTION --project-root CENTRAL_RELAY_ROOT [--json OBJECT]
```

The stable project root belongs to the central Relay runtime, not the task worktree. The poll carries the recorded task/PR correlation; replies settle only their persisted lease ID, which Relay bound to that poll. This prevents feedback from being applied to another task. The prompt contains neither a Relay server URL nor connector/admin tokens. Credentials remain in the central runtime's existing protected credential locations and are consumed by central services, not supplied to the model.

### Relay runtime and supply chain

There is one Open Relay server and one read-only GitHub connector per registered project, owned and supervised centrally. The pinned built-in connector is configured `generic-check-runs` for a standard Actions repository, producing `pr.ci.settled@1`; optional configured bot triggers MAY use combined mode only after their identities are explicitly configured, and they never gate CI or human-review wakes. The built-in connector has no general human-review signal, so AGD adds one central read-only GitHub human-review producer. Neither connector nor producer edits GitHub; all repository mutation stays behind the AGD task grant and guarded publication operation. Individual workers do not start servers, configure connectors, administer Relay, or receive producer/admin credentials.
This chooses one AGD central producer over an Open Relay source change: it keeps the upstream pin/vendor surface intact and confines GitHub human-review polling, deduplication, and health responsibility to AGD; the tradeoff is AGD must preserve the producer's durable cursor and availability.
The central producer watches GitHub reviews, review comments, review threads, and `reviewDecision` for the exact open PR/current head. It treats all review text and states as untrusted data, emits on any changed current-head review evidence (comment, requested changes, approval, dismissal, thread change, or decision change), and does not filter to approvals. It persists its cursor, immutable review/comment/thread identities, current-head digest, and idempotency keys so restart cannot duplicate a notification. Its AGD-owned `.relay/events/agd-pr-review.v1.json` definition has `handler.kind: "agent"`, a strict Ajv input schema, strict output schema, and an instruction file; its payload includes bounded review text plus identity/evidence metadata and a current-head SHA, never credentials.

The producer obtains a producer-scope credential centrally and POSTs to the loopback Relay API, because pinned `relay emit` cannot send a correlation ID: `POST /v1/events`, `Authorization: Bearer PRODUCER_TOKEN`, `Content-Type: application/json`, and `{type:"agd.pr.review",version:1,payload:PAYLOAD,correlationId:"github:owner/repository:pull-request:number",idempotencyKey:"repository/pr/head/artifact-id"}`. Relay returns 201 for a new event or 200 for the same idempotent event. Producer authentication, API, schema, or durable-dedup failure makes the PR task blocked and the central service unhealthy; it does not silently fall back to polling or claim feedback coverage.

At first PR-scoped use, the orchestrator checks for a compatible system Relay installation using package metadata, not executable existence. Open Relay 0.1.0 supports non-mutating `--help`/`help` usage output, which MAY be used as a smoke check but is not version or provenance evidence; `--version` and `version` are unsupported (exit 1) and MUST NOT be used for detection. A compatible installation must identify the pinned package/version and meet Node >= 22.19. If it is absent or incompatible, the orchestrator automatically stages a private installation under AGD home using:

```text
npm install --global --prefix <AGD_HOME private staging> --ignore-scripts --no-audit --no-fund https://github.com/feddericovonwernich/open-relay/archive/dac2865837bcab9444a8636053f4f1406a3f9291.tar.gz
```

It validates staging metadata and expected compiled entry point, then atomically publishes the staged runtime under AGD home. A lock serializes installers; a marker and package metadata identify the installed source/version. It never uses `sudo`, mutates the user's global npm packages or global skills, runs package scripts, or replaces a compatible system installation. Existing partial/unsafe staging or final directories are refused rather than overwritten. Node/npm/network/auth/install failures block the PR-scoped task with the recorded reason; ordinary tasks neither install Relay nor become blocked by its absence.

The archive pin is `github.com/feddericovonwernich/open-relay` commit `dac2865837bcab9444a8636053f4f1406a3f9291`, Apache-2.0, package version 0.1.0, Node >= 22.19, with committed `dist/src/cli.js`. The distro vendors the upstream canonical five **central** Relay skills under `.agents/skills`; it ships no upstream worker bundles, because the worker catalog requires `agd-` IDs and upstream `commit and push` conflicts with guarded publication. The only worker bundle is AGD-authored `bundled/worker-skills/agd-scoped-pr-relay`, provided only for an explicit PR grant. `third_party/OPEN_RELAY_LICENSE` tracks Apache-2.0, and `third_party/PROVENANCE.md` records source, commit, license, and central-file hashes. The upstream archive pin does **not** fully lock npm runtime dependency resolution: `package.json` declares `ajv: ^8.17.1`, including its transitives. That remains a supply-chain tradeoff, not a guarantee this design falsely claims to eliminate.

### GitHub verification authority

Relay's `pr.verification.settled@1` is not success evidence because it omits required human-review and branch-protection completeness. At the current local `HEAD`, AGD queries GitHub directly and fails closed on unknown/missing data. Final success requires all of the following at one observation:

- the PR is OPEN and unmerged;
- the PR repository, head branch, and base branch exactly match the recorded assigned remote/repository/branch/base;
- the PR head SHA, remote branch SHA, and local worktree SHA are identical;
- GitHub effective required checks are green for that head;
- `mergeStateStatus` is `CLEAN`; and
- if effective required reviews are nonzero, `reviewDecision` is `APPROVED`; if zero reviews are required, a null review decision is permitted.

GitHub and Git do not provide a cross-service atomic snapshot. Immediately before calling `ready`, AGD re-reads the remote branch head and PR head and compares both to the local worktree SHA used for the verification result. If either changed, it rejects that observation, records it, and retries reconciliation/verification; success reflects only the observed current head.


GitHub restrictions can make `CLEAN` conservative (for example, merge queue/ruleset or transient calculation states). The system therefore blocks or continues polling on a non-CLEAN state according to a bounded wait policy and does not infer success from individually green checks. Authentication loss, inaccessible required-check/review policy, GraphQL ambiguity, a closed/merged PR, or a mismatched head are decision/block conditions with captured evidence. The worker may repair actionable feedback; it does not guess at policy or bypass it.

## Lifecycle and data flow

```mermaid
sequenceDiagram
    participant U as User
    participant A as AGD orchestrator
    participant W as Existing worker
    participant R as Central Open Relay
    participant G as GitHub

    U->>A: Explicit PR grant for task/remote:assigned-branch
    A->>A: Record task-target grant and PR state
    A->>A: Verify/install private Relay only if needed
    A->>W: Launch existing worker + scoped Relay PR skill
    W->>A: Guarded publish-pr
    A->>G: Non-force push; reconcile/create one exact PR
    A->>A: Persist remote/head/PR identity
    A->>G: Read current-head human review evidence
    A->>R: Emit deduped agd.pr.review event
    loop Feedback and repair
        W->>R: poll with project-root and PR correlation
        R-->>W: leased review/check feedback
        W->>W: Verify and persist pending feedback
        W->>R: start + schema-valid complete acknowledgement
        W->>W: Repair, commit; resume pending work after crash
        W->>A: Guarded publish-pr
        A->>G: Non-force push and reconcile PR
    end
    A->>G: Query current PR, policy, checks, reviews
    alt all final conditions hold
        A->>A: Existing ready at final verified head
    else unresolvable or unknown
        A->>A: Durable blocked/decision with evidence
    end
```

The receipt's workflow observations are `granted -> relay-ready -> published -> awaiting-feedback -> repairing -> verifying -> ready` or `blocked`; they do not alter the task's existing `running` phase before its final `ready` transition. Repeated polls with no actionable feedback use a bounded backoff and a durable idle/busy observation; they do not spin indefinitely. Any new commit returns the receipt to `published`/`awaiting-feedback` and invalidates prior verification evidence. Relaunch keeps the same task record but a new worker incarnation; it must reconcile persisted publication/Relay state and GitHub before resuming. Incarnation checks reject stale workers.

## Security model

AGD's existing safety model remains explicit: approval records, task identity, branch/remote/base validation, clean-worktree checks, and durable state constrain accidental misuse. Workers and the orchestrator run as the same Unix identity, so this is **not OS isolation** and cannot stop a malicious or compromised worker from invoking available local tools directly. The design minimizes the capability exposed in prompts and normal paths, but it does not claim a CLI guard is a sandbox. Secrets are never placed in briefs, skills, task records, or Relay messages; connector tokens remain central and read-only.

## Failure handling

- **Grant/task target/branch ancestry changed:** refuse publication; require a newly recorded explicit instruction for any new task or target, and never continue divergent history.
- **Relay unavailable or install fails:** block only the PR-scoped task with install/runtime diagnostics.
- **Push rejected or remote diverged:** refuse non-force publish and block; never rewrite remote history.
- **Crash before/after create:** reconcile exact PR and remote state first; block if identity cannot be proven.
- **Feedback lease/reply uncertainty:** retry idempotently using the persisted correlation/receipt; block if Relay cannot establish settlement.
- **GitHub policy/verification unknown or PR closed:** capture the response and emit blocked/decision; never call ready.
- **Worker exits:** existing supervision retains the workspace and emits durable blocked evidence; relaunch follows the recorded state after absence is proved.

## Tests and acceptance evidence

Implementation must add focused deterministic tests for: `pr-publish` grant task/target binding, grant-head ancestry, and rejection of divergent history; scoped skill selection only with a valid grant; installer metadata detection, lock/staging/atomic publication and no mutation of global npm state; Relay correlation and lease settlement; guarded publish refusal paths; non-force-only behavior; exact PR reconciliation and crash recovery; persistence/relaunch/incarnation rejection; and final GitHub verifier truth tables, including null review decision only when zero reviews are effective and failure on unknown policy data.

Use fakes only at the GitHub/Relay/npm process boundary; test the orchestrator's persisted state and arguments rather than mock echoing. Add a disposable-home integration smoke path that exercises private Relay provisioning (or a pre-provisioned compatible runtime), one guarded PR lifecycle against a test repository, feedback polling/repair, and final direct GitHub verification. It must not use the running installation or a user project. Credentialed GitHub/Relay acceptance testing remains explicitly gated by scoped user approval.

## Documentation and completion boundary

Update the task/publication and customization documentation to describe the explicit grant, same-identity security limitation, private Relay runtime, supported version detection, normal-task non-impact, feedback/repair lifecycle, blocked outcomes, and no-merge guarantee. Update third-party provenance and bundled-license notices for the five canonical Relay skills and Apache-2.0 source.

Completion requires the end-to-end state machine, all guards, durable recovery, final GitHub authority check, scoped skill provisioning, private automatic Relay install, operational documentation, and the specified tests/smoke evidence. It does not include merge automation, force pushes, tags, user-global installation, a Relay admin surface, arbitrary worker publishing, or changes to normal worker defaults.
