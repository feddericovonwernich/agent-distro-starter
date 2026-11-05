# Reviewer execution contract

Read trusted `request.json` and its selection manifest first. Use ordinary native tools and the skill's boundaries. The commands below describe the frozen interface; replace capitalized placeholders with values returned for this run, never fabricated IDs. Do not run an example against an unrelated project or component.

The trusted request identifies `manifest_id`, `report_schema` (2), `allowed_scope.episodes`, `inspection_bounds`, `deadline_epoch_ms`, `evidence.artifact_root`, and `report_path`. At launch the private run has the request, selection manifest and dedicated reviewer skill; there is no eager packet or catalog. Optional Langfuse availability is sanitized. Never dump secret-bearing files.

## Bounded inspection

```sh
$AGD_CLI --home "$AGD_HOME" reviewer inspect "$AGD_REVIEW_RUN_ID" --episode EPISODE_ID
$AGD_CLI --home "$AGD_HOME" reviewer inspect "$AGD_REVIEW_RUN_ID" --episode EPISODE_ID --source native --include-reviewed
$AGD_CLI --home "$AGD_HOME" reviewer inspect "$AGD_REVIEW_RUN_ID" --episode EPISODE_ID --source native --record MESSAGE_OR_ARTIFACT_ID --part content
$AGD_CLI --home "$AGD_HOME" reviewer inspect "$AGD_REVIEW_RUN_ID" --episode EPISODE_ID --source langfuse --record OBSERVATION_ID --part output
$AGD_CLI --home "$AGD_HOME" reviewer inspect "$AGD_REVIEW_RUN_ID" --episode EPISODE_ID --source SOURCE --cursor CURSOR
$AGD_CLI --home "$AGD_HOME" reviewer inspect "$AGD_REVIEW_RUN_ID" --episode EPISODE_ID --source native --record MESSAGE_ID --part content --cursor RESUME_CURSOR
```

Metadata is the default; details require an explicit source, record and part. Langfuse accepts `input` or `output`, native accepts `content`. Use scoped Langfuse when available and native fallback otherwise. Fetch intent, decisive action/result and outcome/feedback separately; typed native artifact locators are not invented message IDs. Follow bounded cursors only as relevant; preserve ranges, versions and incompleteness. Feedback after cutoff forms a fresh linked basis, not retroactive evidence.

Follow metadata pagination with the returned source (`native` or `langfuse`). A native message row may also return a current-run `resume_cursor` continuing after a prefix exposed in successful runs; use it with that message ID and `--part content`. The cursor itself exposes no content. Omit the cursor to reread from the beginning; never reuse another run's cursor.

Default native metadata, including native fallback, omits fully covered unchanged messages while retaining new or partial material. Full exposure is not a reflected assessment: screened work can remain pending even when its native metadata is empty. Use `--source native --include-reviewed` for bounded discovery of earlier covered context, then explicitly read relevant records to ground reflection. This flag applies only to native metadata, not record details or Langfuse. Follow an expanded-view pagination cursor with `--source native --include-reviewed` again; a default-view cursor cannot be used in the expanded view or vice versa.

Only after a skill hypothesis, inspect the relevant skill/version:

```sh
$AGD_CLI --home "$AGD_HOME" reviewer skills list "$AGD_REVIEW_RUN_ID"
$AGD_CLI --home "$AGD_HOME" reviewer skills show "$AGD_REVIEW_RUN_ID" SKILL_ID
```

For implicated targets, for example:

```sh
$AGD_CLI --home "$AGD_HOME" reviewer target inspect "$AGD_REVIEW_RUN_ID" --scope project --project PROJECT_ID --path AGENTS.md --operation modify --surface instructions
$AGD_CLI --home "$AGD_HOME" reviewer target inspect "$AGD_REVIEW_RUN_ID" --scope project --project PROJECT_ID --path scripts/validate.sh --operation modify --surface validation
$AGD_CLI --home "$AGD_HOME" reviewer target inspect "$AGD_REVIEW_RUN_ID" --scope distro --path lib/hook.py --operation modify --surface hooks
$AGD_CLI --home "$AGD_HOME" reviewer target inspect "$AGD_REVIEW_RUN_ID" --scope distro --component model-routing --basis-source SOURCE_ID --operation modify --surface configuration --authority-hint unknown
```

Paths and component labels are illustrative, not asserted existing targets. Use the target's returned `target_ref`, basis source IDs and guard, not these example paths, as report authority. Project paths bind the registered identity and selected task revision; current distro files bind captured source identity/hash and observed time. Neither proves what was effective historically. A present guard includes source hash and Git revision when applicable; absence requires baseline proof; described components need basis evidence and may have null revision/hash. Applicability and destination ownership are separate: local verified authority requires native proof. An external/unknown component is not a writable local target; supply an evidenced upstream `--authority-locator` when known, otherwise leave it unknown. Inspect only relevant non-secret content, never whole credential or environment stores. Target inspection does not modify the target.

Supplementary output must be a bounded regular UTF-8 artifact under the run's `evidence.artifact_root`:

```sh
$AGD_CLI --home "$AGD_HOME" reviewer evidence add "$AGD_REVIEW_RUN_ID" --file "$PWD/relative-artifact-path" --label "what this proves"
$AGD_CLI --home "$AGD_HOME" reviewer evidence list "$AGD_REVIEW_RUN_ID"
```

Capture supplies a citable source, not native coverage or proof of target ownership. Do not expose secrets or invoke arbitrary command strings through an AGD executor. Probes follow the skill's exact workspace procedure and require separate cleanup authority.

## Schema 2 report examples

Write UTF-8 `report-draft.json` at the trusted destination. The following is a complete illustrative shape, not a runnable fixture: replace every ID and claim with admitted, relevant evidence and target references returned by this run; use only findings justified by that evidence. Every finding's episodes must have reflected rows supported by intent, action and outcome content. `applicability` is `project` or `distro` and must match the admitted target's scope; explain why in `scope_rationale`. `draft` is inert; it may be null. Empty counterexamples must be explained in uncertainty. No model should copy fictional IDs or guards into a report.

Keep IDs unique within each source or citation list. The same content source may support multiple grounding categories; cite it only once in a finding.

```json
{
  "schema": 2,
  "summary": "Illustrative grounded review; recommendations require human triage.",
  "episode_reviews": [
    {"episode_id":"EPISODE_ID","assessment":"reflected","intent_sources":["INTENT_SOURCE"],"action_sources":["ACTION_SOURCE"],"outcome_sources":["OUTCOME_SOURCE"],"limitations":["Historical effective instructions not established."]}
  ],
  "findings": [
    {"episode_ids":["EPISODE_ID"],"target_ref":"PROJECT_AGENTS_TARGET_REF","finding":"The observed sequence missed a documented task constraint; unclear local guidance may have contributed.","proposed_change":"Clarify the specific project constraint in AGENTS.md.","expected_benefit":"Make the constraint discoverable at the relevant decision.","applicability":"project","scope_rationale":"The constraint depends on this project's conventions.","citations":["INTENT_SOURCE","ACTION_SOURCE","OUTCOME_SOURCE"],"counterexamples":[],"alternatives":"Consider a local validation check instead.","uncertainty":"No comparable counterexample was admitted; causal contribution is unproven.","validation":"Compare future constrained tasks against their final outcomes.","validation_limits":["No prospective evaluation yet."],"risks":["Extra guidance may be overlooked."],"draft":null},
    {"episode_ids":["EPISODE_ID"],"target_ref":"PROJECT_VALIDATION_TARGET_REF","finding":"The local validation script omitted a required check; this may allow the observed miss to recur.","proposed_change":"Add that check to the project validation script.","expected_benefit":"Surface the omission before completion.","applicability":"project","scope_rationale":"The check depends on project-specific tooling.","citations":["INTENT_SOURCE","ACTION_SOURCE","OUTCOME_SOURCE"],"counterexamples":[],"alternatives":"Manual verification could detect the same condition.","uncertainty":"No comparable counterexample was admitted; future effectiveness is unknown.","validation":"Exercise both passing and violating project cases.","validation_limits":["The proposed change has not been run."],"risks":["A poorly scoped check could reject valid work."],"draft":null},
    {"episode_ids":["EPISODE_ID"],"target_ref":"DISTRO_HOOK_TARGET_REF","finding":"The observed supervision hook lost a completion transition; the hook may need an explicit recovery path.","proposed_change":"Handle that transition in the distro hook.","expected_benefit":"Preserve completion state across the transition.","applicability":"distro","scope_rationale":"The transition is part of shared supervision, independent of the originating project.","citations":["INTENT_SOURCE","ACTION_SOURCE","OUTCOME_SOURCE"],"counterexamples":[],"alternatives":"Operator reconciliation could recover individual cases.","uncertainty":"No comparable counterexample was admitted; causality requires reproduction.","validation":"Exercise the transition and recovery with multiple session types.","validation_limits":["No prospective evaluation yet."],"risks":["Recovery could misclassify an active session."],"draft":null},
    {"episode_ids":["EPISODE_ID"],"target_ref":"MODEL_ROUTING_COMPONENT_TARGET_REF","finding":"The observed routing decision did not honor the requested model; the described routing component may be responsible.","proposed_change":"Investigate and correct the routing contract at its evidenced owner.","expected_benefit":"Honor explicit model selection.","applicability":"distro","scope_rationale":"Explicit model selection is independent of project-specific commands.","citations":["INTENT_SOURCE","ACTION_SOURCE","OUTCOME_SOURCE"],"counterexamples":[],"alternatives":"Report a routing limitation until the owner is established.","uncertainty":"No comparable counterexample was admitted; ownership and cause are not established by the component label.","validation":"Verify explicit selection and unavailable-model behavior with the responsible owner.","validation_limits":["Component is described rather than locally verified."],"risks":["Incorrect routing changes could select an unintended model."],"draft":null}
  ]
}
```

The target refs above stand for distinct admitted records: project `AGENTS.md` and validation file guards, a distro hook file guard, and a described model-routing component with admitted basis and explicit external or unknown authority if local ownership is unproven. The report does not embed or invent those records. Private project citations remain available to authorized triage; do not promote their contents into global instructions.

A limited review with no recommendations is valid; metadata alone is not reflected coverage:

```json
{"schema":2,"summary":"Insufficient grounding for a recommendation.","episode_reviews":[{"episode_id":"EPISODE_ID","assessment":"limited","intent_sources":[],"action_sources":["ACTION_SOURCE"],"outcome_sources":[],"limitations":["Intent and outcome were unavailable within the admitted scope."]}],"findings":[]}
```

A screened row uses the same six keys with `assessment` set to `screened`; do not label metadata-only evidence reflected. Omit unseen episode rows rather than inventing assessments. A later detail request is explicit and respects retrospective/self-exclusion and scheduling policy.

## Operator-only disclosure and detail admission

A no-work result's `selection` summarizes held episodes and incomplete coverage. Operators can enumerate its immutable disclosure records:

```sh
bin/agd --home "$AGD_HOME" reviewer selection disclosures MANIFEST_ID --category held --limit 20 --cursor 0
```

Use `next_cursor` for subsequent pages and `unavailable` or `withheld` for the other categories. Oversized rows carry `artifact_ref`: read its `category` and zero-based `index` from the verified detail file to obtain every reason. Resolve `path_base: home` paths against the distro home. If the full no-work receipt exceeds the output bound, `receipt_ref` identifies its home-relative stored file; the bounded summary remains in the response.

An operator may run `bin/agd --home "$AGD_HOME" reviewer run --detail-episode EPISODE_ID` to request further inspection in a new run; repeat `--detail-episode` for multiple episodes. Timers cannot request detail. This is not a reviewer command: the reviewer must not launch another reviewer.
