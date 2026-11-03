# Reviewer execution contract

The native reviewer receives a private run directory as its working directory. Read `request.json` first; it is the trusted scope that names the admitted packet and the run artifact root. Then read `packet.json` and `catalog.json`, whose bytes are frozen for this run, before using evidence files. Use ordinary native `read`, `bash`, and `write` tools; do not install packages, call external services, alter model/auth configuration, or apply proposals.

## Evidence workflow

1. Read `request.json`, then the supplied packet and catalog, retaining admitted source IDs and catalog hashes shown by native read results.
2. Form a hypothesis about the execution trace and inspect counterexamples before proposing a process or skill improvement. Use a bounded shell command only when it validates an observed process fact; quote the result in the finding's validation and limits.
3. For useful command or query output, deliberately write a bounded UTF-8 artifact under the `evidence.artifact_root` named by the request. Capture it after writing:

```sh
agd --home "$AGD_HOME" reviewer evidence add "$AGD_REVIEW_RUN_ID" \
  --file "$AGD_REVIEW_RUN_ID/relative-artifact-path" --label "what this proves"
```

The CLI accepts only regular UTF-8 files owned by this run. Do not pass shell command strings to an AGD executor, capture arbitrary live source or credential paths, print secrets, install a Langfuse client, override endpoints, or claim arbitrary text was redacted. Use official Langfuse CLI read/query operations only when available; native evidence is the fallback and missing or delayed data is a limitation, not a negative finding.

4. Write `report-draft.json` in the run directory. The file must be UTF-8 JSON, at most 65536 bytes, and have exactly this top-level shape:

```json
{"schema":1,"summary":"...","findings":[{"kind":"new-skill","target":"...","finding":"...","applicability":"...","citations":["<admitted seed or captured source id>"],"counterexamples":[],"uncertainty":"...","resolution":"...","validation":"...","validation_limits":["..."]}]}
```

A `new-skill` finding additionally needs `new_skill` with `id`, `draft`, `triggers`, `scope`, and `prerequisites`; its draft starts with the skill frontmatter (`---`, `name`, `description`, `---`). Use an `existing-skill` only when its frozen catalog version is present in the packet. Empty findings are valid when no recommendation is justified.

Citations and counterexamples may name only packet source IDs or evidence IDs captured for this run. Distinguish original observations from later experiments and state the observed command, revision, trace/session IDs, query scope, and uncertainty where relevant. Recommendations are inert review output: never publish, install, apply, or rewrite the reviewed workspace.
