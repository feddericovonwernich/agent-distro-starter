# Task 1 report — reviewer evidence packets

## Implemented interfaces and files

- `bin/agd_lib/reviewer.py`: strict reviewer config, nonblocking whole-run lease, immutable intent/config/instruction/packet records, reserve-before-collection receipts, receipt completion, and bounded native JSONL collection from closed worker incarnation evidence.
- `bin/agd_lib/reviewer_runner.py`: freezes config and instruction bytes, reserves the receipt before collection, and completes it with the configured model without invoking a provider.
- `bin/agd_lib/observability.py`: create-only reviewer native binding at the fixed `data/observability/reviewer/<runUUID>/omp` directory, independent of worker environment variables.
- `lib/agd-reviewer-instructions.md`: frozen observer-only instruction bundle.
- `tests/test_reviewer.py`: consumer behavior coverage for successful/failed episodes, stale closed incarnations, byte bounds/provenance, immutable receipt progression, native binding, and reserve-before-collection runner behavior.
- `CHANGELOG.md`: records the new private collection boundary only.

No `tests/reviewer_bridge.py` was created: its meaningful restricted native-host/broker behavior belongs to Task 3, and an empty bridge would be a false implementation claim.

## Red/green evidence

- RED: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q` initially failed during collection with `ImportError: cannot import name 'reviewer'`.
- RED: `... pytest tests/test_reviewer.py::test_reviewer_binding_uses_native_session_id_without_worker_environment -q` failed with missing `observability.reviewer_binding`.
- GREEN: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q` → `6 passed in 1.15s`.
- Smoke: `PATH=/tmp/agd-treehouse-v3.1.6:$PATH PYTHONPATH=bin:tests uv run --python 3.12 --with pytest python -c "..."` created a disposable private home and native JSONL corpus, then emitted `{"episodes": 1, "sources": 1, "excerpt": "native smoke evidence", "coverage": "unused"}`.

## Commit

- `feat: persist reviewer evidence packets` (Task 1 files only; exact commit ID reported from `HEAD` after commit).

## Concerns/deviations

- Native host session construction, restricted broker operations, report persistence, timer activation, CLI wiring, and central feedback-source parsing are intentionally deferred to their assigned later tasks. Task 1 records central feedback as explicit `unknown` coverage unless a later trusted collector defines and supplies a retained explicit link; it never infers a link from proximity.
- `review_model: null` is refused by the Task 1 runner because concrete native default-model resolution is part of the Task 3 host integration; explicit configured models work without a provider call.
