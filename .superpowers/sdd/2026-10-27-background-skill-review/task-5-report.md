# Task 5 report

## Status

DONE_WITH_CONCERNS

## Implemented

- Added reviewer CLI commands for manual/timer run, status, immutable report access, proposal access/triage, and explicit timer management. Reviewer operations bypass central-owner mutation guards.
- Added production Bun JSONL host and Python parent lifecycle: reserve/freeze before selection, durable `no-work` return before capability/child work, child process group/deadline cleanup, sanitized environment, safe selector-only input, and model persistence transition.
- Wired resolver capability URL and `persistModel` through the bridge fixture. The host resolves explicit/default models locally and only sends `provider/id` to Python.
- Existing generic primary event handling was retained; it already renders the reviewer durable event as one ordinary follow-up.
## Commit

`be6c44e feat: expose reviewer inbox`


## Verification

```text
$ PATH=/tmp/agd-treehouse-v3.1.6:$PATH uv run --python 3.12 --with pytest pytest tests/test_reviewer.py -q
42 passed in 12.32s

$ bun build lib/agd-reviewer-host.ts --outdir /tmp/agd-reviewer-host-build
Bundled 2 modules in 2ms

$ PATH=/tmp/agd-treehouse-v3.1.6:$PATH bun test tests/primary.test.ts
3 pass, 0 fail

$ python3 bin/agd --home "$TMP" reviewer run
... "dispatch_state": "reserved", "state": "no-work" ...
```

No timer lifecycle command or provider call was executed. The production real-AgentSession acceptance remains blocked by installed stale `pi_natives`: the required host test fails during capability import because the linux-x64 addon is missing. No installed package/cache was modified and no fallback was added.

## Task 6 handoff

Use a repaired normal native installation to perform the real AgentSession/exporter inventory acceptance. Add the requested fake-child lifecycle smoke coverage for timeout/process-group and clean-finalize ordering before relying on production provider execution.
