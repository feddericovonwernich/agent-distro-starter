# Task 1 — native reviewer component report

## Status

Task 1's standalone native reviewer component is complete and controller-verified. The public reviewer coordinator remains on its existing engine; B2 owns the public schema-2 cutover and removal of historical SDK/counter paths. This component provides the one-shot child supervisor, native role extension, durable handoff, reserved CLI operations, and lifecycle proof. No paid-provider quality, authentication, or policy claim is made.

## Native metadata boundary

The native child stores the configured reviewer selector separately as `model_selector`; observed receipt metadata remains `model: null` until native session evidence supplies an actual model. OMP version metadata comes only from the validated dependency receipt and remains `null` when the disposable fake executable has no dependency receipt. The component does not invent `"native"` model or version identities; B2 owns any schema migration needed to expose these nullable fields.

## Task-local skill-authoring checklist

- [x] Preserve the upstream native skill names and mandatory reserved `agd-reviewer` role name.
- [x] Keep the bundled skill overview concise; place actual commands/schema in `references/commands.md`.
- [x] Include one strong process-trace example, with no invented capture/probe commands or future claims.
- [x] Exercise native skill discovery and application with a meaningful missing-core-contract RED case and a native GREEN case.
- [x] Verify reserved core-skill provenance and private-run skill isolation; name filtering is not treated as an allowlist.
- [x] Keep the proposal recommendation-only and inert; do not publish or apply it.

## Authoring and consumer evidence

The `writing-skills` reference and discovery/testing guidance were applied to the bundled skill: concise overview, reserved name/frontmatter, native discovery by skill name, bounded references, one process-trace example, and no publish/apply path. Controller's disposable native baseline failed meaningfully when the reserved core contract was absent; the post-authoring native runs discovered and applied the bundled contract while using private run directories nested under the distro checkout. The explicit and default native runs retained reserved-core provenance and did not inherit unrelated plugin skills.

Consumer inventory kept the public coordinator and historical SDK engine unchanged for B2's atomic cutover. The changed B1 exports are the private `execute`, `bind_session`, and `launch_argv` component boundaries plus CLI dispatch. Python LSP lookup had no server available; repository reference search covered the changed exports and no public migration was made.

## Component and acceptance coverage

`tests/test_reviewer_lifecycle.py` now contains behavioral lifecycle cases for:

- real temporary fake-OMP child execution without SDK/Bun capability probing;
- valid draft followed by non-zero child exit (no report commit);
- missing/invalid draft retryability and exact retained seed identity;
- duplicate child execution rejection without a second dispatch;
- no-work admission without endpoint allocation;
- foreign reviewer session-directory refusal using `store.Refusal`;
- timeout diagnostic retention from the actual child and exact retained seed identity;
- crash-after-exit parent collection recovery without redispatch;
- refusal to recover success without durable child-exit proof.
- real lifecycle rejection when the supervisor remains live after child exit;
- real lifecycle rejection when an owned child process group survives supervisor exit;
- native session-header prelude handling (`title` followed by `session`) and self-process fatal startup isolation;

The tests use a pre-admitted reserved receipt, a real temporary executable selected through the OMP executable dependency boundary, real OS child processes, and the private `_collect_native_result` parent boundary after child absence. Fixture modes are read only by the temporary fake executable; no product branch reads test mode flags. Tests inspect durable receipts, reports, runtime artifacts, and exact packet identities rather than source text, argv forwarding, wording, or mock process outcomes.

Native default-model selection is not asserted by Python argv wiring; the deterministic fixture smoke uses its explicit local reviewer selector to avoid ambient credentials.

`tests/native_reviewer_fixture.ts` is a disposable native acceptance extension. It registers a static `agd-fixture/reviewer` model through the native extension API, serves the built-in OpenAI-compatible adapter from a loopback HTTP/SSE endpoint, first reads the reserved `agd-reviewer` commands reference, then reads `evidence.txt`, executes a real shell diagnostic, and only after receiving the sentinel/tool results writes a valid skill/process finding citing a packet source. It verifies a write result before returning terminal text. It has no SDK/product imports, custom stream implementation, paid credentials, or cloud endpoint.

## Controller component command and observed output

```bash
python3 -m pytest -q tests/test_reviewer_lifecycle.py tests/test_reviewer_fake_child.py tests/test_reviewer.py -k 'standalone or review_model or nonzero or report or execute or binding or timeout or no_work or recovery or supervisor or surviving or group'
```

Observed controller output: `25 passed, 66 deselected in 52.13s`.

The controller observed the initial RED from missing `execute` / `bind_session`; after implementation it observed the covering GREEN suite above and the native smoke evidence below.

## Controller native evidence

The controller's native baseline artifact `native-baseline-6mra7hfz` recorded the expected RED: the reserved core reviewer contract was absent, so the local provider refused the core-skill read before evidence tools or report writing. The product-fatal probes `tx_9bbt8` (missing context) and `vvks9b4m` (bad binding) exited with signal 9, produced zero assistant/core-tool/report activity, and retained the run.

The explicit-selector native component artifact `native-component-rn7m6bl9` and private-profile default-selection artifact `native-component-252zjh4q` each proved supervisor exit 0, native session binding, reserved core skill discovery, real read/bash/write tool results, structured report handoff, and collection outcome `exited`. The default run retained a native `model_change` event showing default selection. The controller's absence diagnostic artifact records that the supervisor waited for the owned child process group to settle before exit handoff; collection therefore did not mistake transient descendants for success.

These are disposable, controller-owned artifacts only; no paid provider, external endpoint, credential, or quality claim is part of the evidence.

## Disposable native fixture usage

Before authoring the bundled skill, controller should run a private native baseline with the same credential-free local fixture provider and explicit fixture model. Do not load the product extension or any ambient account configuration; the expected failure is the mandatory core-skill read error:

```bash
RUN_ROOT="$(mktemp -d)"
RUN_DIRECTORY="$RUN_ROOT/run"
mkdir -p "$RUN_DIRECTORY" "$RUN_ROOT/home" "$RUN_ROOT/config" "$RUN_ROOT/data" "$RUN_ROOT/state" "$RUN_ROOT/cache" "$RUN_ROOT/agent"
env -i PATH="$PATH" HOME="$RUN_ROOT/home" \
  XDG_CONFIG_HOME="$RUN_ROOT/config" XDG_DATA_HOME="$RUN_ROOT/data" \
  XDG_STATE_HOME="$RUN_ROOT/state" XDG_CACHE_HOME="$RUN_ROOT/cache" \
  PI_CODING_AGENT_DIR="$RUN_ROOT/agent" \
  omp --print --mode json --no-extensions \
  --skills agd-reviewer --cwd "$RUN_DIRECTORY" \
  --session-dir "$REVIEWER_SESSION_DIR" \
  --model agd-fixture/reviewer \
  -e "$REPO/tests/native_reviewer_fixture.ts" --max-time 10
```

The baseline must exit nonzero before any evidence, bash, or report-write request and identify the failed reserved `agd-reviewer` core-skill read. A nonzero result caused only by missing model credentials, ambient auth, or a nonexistent product extension is not valid baseline evidence.

After implementation authorization and skill authoring, create the sentinel file and use a throwaway pre-admitted run directory:

```bash
printf 'AGD_NATIVE_REVIEWER_SENTINEL\n' > "$RUN_DIRECTORY/evidence.txt"
env -i PATH="$PATH" HOME="$RUN_ROOT/home" \
  XDG_CONFIG_HOME="$RUN_ROOT/config" XDG_DATA_HOME="$RUN_ROOT/data" \
  XDG_STATE_HOME="$RUN_ROOT/state" XDG_CACHE_HOME="$RUN_ROOT/cache" \
  PI_CODING_AGENT_DIR="$RUN_ROOT/agent" \
  omp --print --mode json --no-extensions \
  --skills agd-reviewer --cwd "$RUN_DIRECTORY" \
  --session-dir "$REVIEWER_SESSION_DIR" \
  --model agd-fixture/reviewer \
  -e "$REPO/lib/omp-reviewer.ts" \
  -e "$REPO/tests/native_reviewer_fixture.ts" --max-time 60
```

The smoke must verify the reserved core skill read, actual file and shell tool results containing both markers, native binding/session file, bounded structured `report-draft.json` with an admitted citation, true process exit/absence, and durable reviewer finalization. This fixture is deterministic local-only evidence, not a real-provider quality/authentication claim.

## Files changed

- `bin/agd_lib/reviewer_runner.py`
- `bin/agd_lib/observability.py`
- `bin/agd`
- `lib/omp-reviewer.ts`
- `bundled/reviewer-skills/agd-reviewer/SKILL.md`
- `bundled/reviewer-skills/agd-reviewer/references/commands.md`
- `tests/test_reviewer_lifecycle.py`
- `tests/native_reviewer_fixture.ts`
- this report

The worker did not independently rerun checks, build, formatter, or native runtime smoke. Controller-owned verification and artifacts above are the exercised evidence; this report and the listed B1 files are ready for the component commit. Public dispatch/schema-2 migration remains B2 scope.
