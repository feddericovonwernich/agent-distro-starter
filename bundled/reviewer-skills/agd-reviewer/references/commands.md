# Reviewer execution contract

The native reviewer receives a private run directory as its working directory. Read `request.json` first; it is the trusted scope that names the admitted packet. Then read `packet.json` and `catalog.json`, whose bytes are frozen for this run, before using evidence files. Use ordinary native `read`, `bash`, and `write` tools; do not install packages, call external services, alter model/auth configuration, or apply proposals.

## Evidence workflow

1. Read `request.json`, then the supplied packet and catalog, retaining admitted source IDs and catalog hashes shown by native read results.
2. Use a bounded shell command only when it validates an observed process fact; quote the result in the finding's validation and limits.
3. Write `report-draft.json` in the run directory. The file must be UTF-8 JSON, at most 65536 bytes, and have exactly this top-level shape:

```json
{"schema":1,"summary":"...","findings":[{"kind":"new-skill","target":"...","finding":"...","applicability":"...","citations":["<admitted source id>"],"counterexamples":[],"uncertainty":"...","resolution":"...","validation":"...","validation_limits":["..."]}]}
```

A `new-skill` finding additionally needs `new_skill` with `id`, `draft`, `triggers`, `scope`, and `prerequisites`; its draft starts with the skill frontmatter (`---`, `name`, `description`, `---`). Use an `existing-skill` only when its frozen catalog version is present in the packet. Empty findings are valid when no recommendation is justified.

Citations and counterexamples must be packet source IDs. Recommendations are inert review output: never publish, install, apply, or rewrite the reviewed workspace.
