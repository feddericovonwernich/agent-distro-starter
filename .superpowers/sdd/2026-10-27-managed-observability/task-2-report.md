# Task 2 report

## Red/green

- RED: `uv run --python 3.12 --with pytest pytest tests/test_observability.py::test_rendered_langfuse_environment_has_required_migration_and_bucket_settings -q` failed for missing pinned Compose fields.
- RED: `...::test_rendered_langfuse_waits_for_upstream_health_checks -q` failed for absent health-gated dependencies.
- GREEN: `uv run --python 3.12 --with pytest pytest tests/test_observability.py -q` — 15 passed.
- Full-suite attempt with pinned Treehouse timed out after 300 seconds; prior full run before the final lifecycle changes passed 161 tests.

## Stack smoke

Initial cold enable retained home `/tmp/tmp.OTWdNAoG60`, project `agdobsc6fea38c7057`, and exposed missing upstream ClickHouse/MinIO configuration. Its volumes remain retained.

Corrected fresh home `/tmp/tmp.lVAPa8vKmL`, project `agdobs487d6400891c`, reached all running services, Langfuse `/api/public/health` 200 and authenticated projects API 200. Recovery smoke:

```text
observability enable -> desired enabled, runtime healthy, generation 1
observability status -> desired enabled, runtime healthy, generation 1
observability stop -> desired enabled, runtime stopped, generation 1
```

Both disposable homes and their named volumes remain retained. Known concern: cold initial migrations consumed roughly 329 seconds in the observed failed attempt; bounded readiness is 420 seconds. No model calls were made.
