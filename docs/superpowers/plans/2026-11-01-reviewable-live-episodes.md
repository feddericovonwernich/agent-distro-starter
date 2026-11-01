# Reviewable Live Episodes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Review proved final-response episodes from living workers and the managed orchestrator, without consuming unfinished later interactions.

**Architecture:** A shared native hook persists an invisible interaction binding and publishes a small, source-bound terminal record through AGD. The reviewer correlates that record with actual native entries, freezes only the completed branch/prefix, and reuses existing reports, budgets, broker tools and transactional coverage. Do not append completion entries to OMP's mutable current session.

**Tech Stack:** Existing Python 3.12+ standard library, TypeScript/Bun, official OMP 18.14.0 public extension/session APIs, pytest and existing Bun tests. No new dependency, service, installer option or SDK patch.

**Spec:** `docs/superpowers/specs/2026-11-01-reviewable-live-episodes-design.md` (user-approved).

## Global Constraints

- The user explicitly selected **Only final responses**. An intentional handoff/wait for background work is not a completed review episode.
- Existing supervision and inbox lifecycle semantics stay unchanged.
- Do not persist owner tokens, provider credentials, raw environment or duplicate transcript bodies.
- Input/candidate truncation must remain explicitly partial so omitted evidence stays eligible later.
- Failure, timeout, interruption and an empty/no-work run must not consume new episodes.
- Capture remains independent of Langfuse/telemetry enablement and does not introduce another installation option.
- No remote push/PR/merge, unrelated worktree cleanup, schedule change or skill application is authorized by this design alone.
- Work only in task `reviewable-live-episodes`, branch `agd/reviewable-live-episodes`, leased worktree `/home/feddericokz/.treehouse/distro-38def4/7/distro`; do not modify live jarvis-14 or return its task lease during implementation.

## Review Focus

1. An immediate first central prompt can beat the watcher's owner publication: capture must await bounded, generation-specific ownership readiness rather than silently losing the first final response. Task 3's native first-prompt case covers it.
2. A completion record can arrive before its native marker/final entry: the packet stays empty/pending until both exist, without consuming coverage. Tasks 1–2 cover arrival ordering.
3. Repeated answer text, reused short native entry IDs across sessions, and title rewrites must not conflate sources or repeat already reviewed material. Tasks 1 and 4 cover these separately.
4. Rewind/fork, delayed end notification after a session switch, or binding loss during compaction/revive must not attach a response to the current unrelated session. Tasks 1 and 3 cover rejection/original-source binding.
5. Old successful coverage and a capped packet containing several episodes from one session must neither discard unseen rows nor replay consumed rows. Task 4 covers legacy reconstruction, failure and partial finalization.

## File Responsibilities and Shared Data

- `bin/agd_lib/reviewer_episodes.py` (new): native JSONL reader, scoped source registration, terminal-record publication/discovery. One domain module; no second journal framework.
- `bin/agd_lib/reviewer.py`: packet admission, role/session-scoped material deduplication, existing finalization. Remove migrated reader helpers rather than leave wrappers.
- `bin/agd`: guarded internal `reviewer episode bind|complete` JSON-stdin operations; both remain under retirement admission.
- `bin/agd_lib/uninstall.py`: include the new Python module in the isolated recovery runtime's explicit module manifest.
- `lib/omp-episodes.ts` (new): shared native capture/filter hooks; `.omp/extensions/agd-primary.ts` and `lib/omp-worker.ts` install them without replacing protocol hooks.
- `tests/test_reviewer_episodes.py` (new), `tests/test_reviewer.py`, `tests/episodes-native.test.ts` (new): reader/authority, packet/finalization, and actual native lifecycle/persistence behavior respectively. Existing worker, primary, reviewer and uninstall suites remain applicable.
- `README.md`, `docs/operations.md`, `docs/compatibility.md`, `CHANGELOG.md`: update after smoke proof, including deployment/coverage limits and exercised evidence only.

Use plain dictionaries/structural types, not a new class hierarchy. Source receipts live at `state/reviewer/episodes/sources/<source_id>.json`; terminal records at `state/reviewer/episodes/completions/<source_id>/<interaction_id>.json`. Reuse existing private-directory, process-identity, task/incarnation, atomic-write and immutable-publication patterns.

The native carrier is `customType: 'agd:episode-binding'`, `display: false`, with a small valid content string and `details: {schema: 1, source_id, interaction_id}`. The context hook removes this carrier before provider conversion; it remains in native history/active state. `interaction_id` is a fresh UUID per ordinary prompt, never a fresh UUID for `reason: 'revive'`.

A source receipt binds canonical home, role, native session ID and relative file, producer PID/start/UID, and worker task/incarnation where applicable. `source_id` hashes that identity, excluding registration time. A completion stores schema/source/interaction IDs, `recorded_at` publication time, false continuation/wait flags and `final: {timestamp, text_sha256, stop_reason: 'stop'}`. Message timestamps must be finite native millisecond values. Its fingerprint uses the exact concatenated native visible text, not a summary; raw response content is accepted transiently on stdin but is not stored in the completion. Reject ambiguous matches rather than add provider-dependent heuristics.

## Task 1: One Native Reader With Proved Boundaries

**Files:** Create `bin/agd_lib/reviewer_episodes.py`, `tests/test_reviewer_episodes.py`; modify `bin/agd_lib/reviewer.py:175-238,327-342,418-425`, `tests/test_reviewer.py:43-53`, `bin/agd_lib/uninstall.py:14-20`.

**Interfaces:** Produce `read_native(session_path: Path, cutoff: float, budget: int, deadline_epoch_ms: int | None = None, *, completion: dict | None = None) -> dict`. Return `session_id`, `byte_watermark`, `content_sha256`, `rows`, `late`, `limited`, `withheld`, `status` (`ready` or `pending`) and `boundary` (`None` or `{interaction_id, entry_id, parent_id, closed_at}`). Each row retains message ID/text/physical coordinates. A supplied completion includes its validated source receipt. `closed_at` is the later of completion publication and the native final entry's recorded timestamp; both timestamps must be within cutoff, and both records must actually be visible at selection. Do not claim a native disk-flush timestamp or fence. Bad provenance/ambiguous identity raises `store.Refusal`; absent not-yet-persisted binding/final data yields pending with no admitted rows.

- [ ] **Step 1: Write failing reader regressions.** Add a small `write_native(path, session_id, entries, *, title=None)` test helper using the installed version-3 native record shapes. Cover header-first and title-prefixed input, UTF-8 physical coordinates, final-boundary exclusion of a later active prompt/partial tail, pending-then-arriving records, and wrong/ambiguous branch binding. Assertions include:

```python
assert result['boundary']['entry_id'] == '22222222'
assert [row['message_id'] for row in result['rows']] == ['11111111', '22222222']
assert raw[row['byte_start']:row['byte_end']] == admitted_native_line
assert pending['status'] == 'pending' and pending['rows'] == []
```

The fixture has user `11111111`, the binding carrier, final assistant `22222222`, then later unfinished `33333333`. Marker/title text is never an excerpt. Duplicate matching finals or a final on a branch not descending through the captured binding are refusals. A partial tail after a proved final does not invalidate that earlier boundary.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_reviewer_episodes.py tests/test_reviewer.py`; expect failure at the new reader interface/behaviors, not an unrelated prerequisite. Do not reconfirm the user's reported live failure.
- [ ] **Step 3: Implement `read_native`.** Move `_epoch`, `_render_content` and `_messages` functionality into the new module; migrate every caller and remove old helpers/imports. Accept the known optional native title preamble before the required version-3 session header. Preserve bounded reads/deadline checks and legacy malformed/late-row coverage. For completion mode, establish the unique native binding/final entry and parent-chain prefix before admitting rows; stop evidence coordinates/hash at that boundary, not EOF. Pending/native gaps do not authorize bytes. Keep title-dependent physical proof separate from title-independent material identity. Add the module to the uninstall bootstrap manifest in this same deliverable.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_reviewer_episodes.py tests/test_reviewer.py tests/test_uninstall.py tests/test_uninstall_state.py`; expect applicable tests passing. Preserve existing physical-prefix regressions instead of repinning incidental wording.
- [ ] **Step 5: Commit** the Task 1 files: `refactor: share bounded native reviewer evidence reader`.

## Task 2: Guarded Source and Terminal Recording

**Files:** Modify `bin/agd_lib/reviewer_episodes.py`, `bin/agd:14,154-164,232-240,416-437`, `tests/test_reviewer_episodes.py`.

**Interfaces:** Consume Task 1's reader and completion shape. Produce `register_source(home: Path, request: dict) -> dict`, `record_completion(home: Path, request: dict) -> dict`, and `completion_records(home: Path, cutoff: float, coverage: dict, deadline_epoch_ms: int | None = None) -> list[dict]`. Discovered completions include their validated source receipt. Bind request: `{role, session_id, session_file}`. Complete request: `{source_id, interaction_id, will_continue: false, awaiting_async_work: false, message: {role, timestamp, stopReason, content}}`; only native assistant text/thinking content is accepted for fingerprinting, with actual visible text required and tool calls/errors/truncation refused.

- [ ] **Step 1: Write failing authority/publication regressions.** Add `source_case` test setup that uses real private home/task bindings and launch/owner records, with controlled caller PID/start/environment. Test central registration without a worker, worker exact task/incarnation, missing/mismatched owner or managed process receipt, PID reuse, wrong header/CWD/home, symlink/file substitution, reviewer-role rejection and retirement fencing. Test idempotent versus conflicting publication and pending native persistence:

```python
assert repeated == original  # publication time/identity is unchanged
assert saved['final']['text_sha256'] == sha256(answer.encode()).hexdigest()
assert 'content' not in saved and 'message' not in saved
assert before_native['status'] == 'pending'
assert after_native['boundary']['entry_id'] == '22222222'
```

Assert refusals on changed source identity, malformed/oversized input, true wait/continuation flags, empty/error/aborted/tool-only/length-limited assistant responses. Tokens and transcript bodies are absent from persisted source/completion records.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_reviewer_episodes.py`; expect the missing recording functions to fail.
- [ ] **Step 3: Implement the functions and CLI operations.** Derive caller PID from `os.getppid()` and verify its `/proc` start/UID against a retained managed launch process receipt. Validate and publish under retirement admission plus `store.lock`, also respecting the worker runner's child-receipt publication barrier. Worker identity comes from exact environment/task/incarnation binding, never caller-supplied task metadata. Central registration additionally requires `AGD_MANAGED_CENTRAL=1` and the live owner PID/start/token match; `store.require_owner` alone is insufficient because it permits absent/dead owners. Validate exact dedicated session path, native header/session ID and expected initial CWD. Compare existing records and reuse private atomic storage under the lock for immutable/idempotent publication; repeated identical records return the original, conflicts refuse. On completion, verify the captured source's caller identity; reading historical authorized receipts does not require a live owner. Add JSON-stdin `reviewer episode bind|complete` dispatch before timer fallthrough, cap stdin at 1048576 bytes, and include these writes in uninstall admission without misclassifying worker-scoped capture as an ordinary central mutation. Discovery applies cutoff, validation and explicit coverage, never global-session scanning.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_reviewer_episodes.py tests/test_reviewer.py tests/test_uninstall.py tests/test_uninstall_state.py`; expect passes. Exercise both new operations through the real CLI in disposable homes, including `--home .`, and observe stored records/refusal exit status.
- [ ] **Step 5: Commit** Task 2 files: `feat: record home-scoped native completion evidence`.

## Task 3: Capture Real Worker and Central Final Responses

**Files:** Create `lib/omp-episodes.ts`, `tests/episodes-native.test.ts`; modify `lib/omp-worker.ts:86-99,167-195,266-288`, `.omp/extensions/agd-primary.ts:49-56,110-187`.

**Interfaces:** Consume Task 2's CLI JSON requests/results. Produce `registerEpisodeCapture(api: EpisodeAPI, options: EpisodeOptions): void`. `EpisodeAPI` is the existing structural `on` event-registration contract, extended only with the public session-manager identity/ensure-on-disk methods needed in context. `EpisodeOptions` contains `{home: string, cli: string, role: 'worker' | 'central', diagnostic: (message: string) => void, beforeBind?: () => Promise<boolean>}`. Register independent `before_agent_start`, `context` and `agent_end` handlers; retain all existing protocol handlers.

- [ ] **Step 1: Write failing native behavior cases.** In `tests/episodes-native.test.ts`, use the existing provider-free official `AgentSession`/`SessionManager` strategy from `tests/worker-native.test.ts`, real AGD recording subprocesses, and disposable valid managed bindings. Record actual provider inputs/native files. Cover a final worker response while the session remains alive, an immediate first central prompt while its watcher starts, a nonterminal/wait notification, a delayed end carrying the original binding after switching native sessions, lost/ambiguous/revive bindings, and interruption. Assert persisted markers exist but never occur in provider inputs, no terminal record exists for waits/continuations/errors, and an old event cannot name the new source. Do not test callback forwarding or mock echoes.
- [ ] **Step 2: Run** the new native check with the compatible runtime described below; expect missing capture behavior to fail. Run it standalone as well as under Bun's test runner.
- [ ] **Step 3: Implement `registerEpisodeCapture` and install it in both extensions.** Capture session identity/file before any await; ensure native storage exists. Cache only successfully registered sources. For primary's first binding, await a generation-specific readiness promise resolved by the existing watcher `ready` event; resolve false on retirement/failure and cap the wait at 5000 ms. Do not change watcher restart/inbox/stop decisions or gate on delegated tasks. Recheck captured session identity after awaiting readiness before registration. Create one UUID marker for ordinary prompt preparation. Filter marker messages only from model context. On end, synchronously copy the latest applicable binding and final-response projection before publication; require false continuation/wait and successful nonempty `stop` response. Never use a current-leaf append, new `sendMessage` turn or guessed older binding. Identical duplicate end publication is safe; a different response cannot reopen a closed interaction. Missing bindings after compaction/revive are explicit gaps; document that native event-bound identities would be the upgrade path. Capture errors warn without aborting work or changing task/supervision state.
- [ ] **Step 4: Run** the native check and existing `tests/worker.test.ts`, `tests/primary.test.ts`, `tests/worker-native.test.ts`. Observe real native marker/final persistence, original-session binding and unchanged retained-worker behavior; expect assertions passing. If any capability is unavailable, resolve the actual prerequisite rather than replace native persistence with a fake.
- [ ] **Step 5: Commit** Task 3 files: `feat: capture final episodes in reusable native sessions`.

## Task 4: Admit Incremental Episodes and Preserve Coverage

**Files:** Modify `bin/agd_lib/reviewer.py:139-172,315-497`, `tests/test_reviewer.py`, `tests/episodes-native.test.ts`, `README.md:71-73`, `docs/operations.md:113-125`, `docs/compatibility.md:57-75`, `CHANGELOG.md`.

**Interfaces:** Consume `completion_records` and `read_native`. Keep `select_packet`, broker/report/proposal APIs and `mark_material_reviewed` signatures unchanged. Add `_source_candidate_id(scope: dict) -> str` and `_reviewed_material(home: Path, scope: dict) -> set[str]` inside `reviewer.py`. Scope is canonical home/role/native session plus task/incarnation for workers. Episode identity includes the proved final native entry; source candidate buckets track material across sequential episodes. A native material ID hashes that scope, native message ID and text SHA-256, not title/line/byte positions.

- [ ] **Step 1: Write failing packet/finalization regressions.** Extend existing reviewer fixtures with actual binding/completion shapes and a `complete_review(home, packet)` helper using real existing report submission/finalization, not a mark-reviewed shortcut. Assert living worker and taskless central admission, exclusion of later active turns, incremental second response, no duplicate same-source rows when two episodes share a packet, source isolation for repeated text/short entry IDs, and title-only changes producing no new material:

```python
assert {e['role'] for e in packet['episodes']} == {'worker', 'central'}
assert 'task_id' not in central_episode
assert 'unfinished' not in '\n'.join(e['text'] for e in packet['excerpts'])
assert second_packet_material.isdisjoint(first_reviewed_material)
assert unchanged['excerpts'] == []
```

Cover failed/interrupted reviews, byte/candidate truncation followed by successful review of the retained subset, pending native records followed by a fresh-cutoff scan, and legacy successful coverage. Assert that the earlier frozen pending/no-work packet is unchanged rather than reselecting it in place. Retain existing exited-worker, linked-decision, catalog-provenance, frozen broker and explicit-retrospective cases.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_reviewer.py tests/test_reviewer_episodes.py`; expect new eligibility/incremental assertions to fail.
- [ ] **Step 3: Integrate one selection/finalization path.** Merge proved final completions with existing confirmed runner-exit evidence in deterministic completion-time/role/session/boundary order. Remove task/incarnation-only suppression for new episodes, deduplicate both across successful source coverage and within the current packet, and count only retained episodes against `max_candidates`. Preserve exit evidence without replaying previously reviewed final material or dropping unreviewed confirmed closed evidence. Keep direct central episodes taskless, worker frozen assignment provenance unchanged, central usage unknown, and explicit linked decisions contextual rather than full central coverage. Keep packets immutable and mark caps/gaps partial. Extend followup/candidate bookkeeping to source scope; advance only retained material after existing committed-report finalization. Reconstruct legacy reviewed native material from reviewed followups and their retained checkpoint/packet source associations, not unscoped short IDs alone; do not rewrite/delete old packets or widen old coverage. Missing reconstruction proof is explicit incomplete coverage, not invented consumption. Reviewer sources remain excluded unless the existing explicit retrospective flow requests them.
- [ ] **Step 4: Run integrated native smoke and regressions.** Extend the standalone native check to call actual production packet selection through Python while each native session remains alive: worker and central finals admitted, later active turn excluded, successful finalization consumes only frozen material, next final admits new rows and a repeated successful scan has zero excerpts. Use actual scoped CLI recording and native persistence; a scripted provider is lifecycle proof, not a paid-model obedience claim. Run the Python reviewer/uninstall/observability/worker/supervision suites and the existing Bun suite once after integration; record exact host prerequisite omissions, never relabel an unrun tmux/Herdr lifecycle as passed. Check old report/retrospective contracts remain intact and native source citations resolve to frozen packet material after title changes.
- [ ] **Step 5: After smoke proof, update the listed docs/changelog.** Explain final-response versus process lifecycle, manual `./bin/agd --home . reviewer run`, pending/coverage diagnostics, source authorization, title-independent dedup and selection-time citations. Document supported extension reload plus a subsequent proved response for existing live sessions; do not fabricate historical boundaries or stop agents. Record only exercised native evidence and distinguish unavailable backend/model checks. Commit Task 4 files: `feat: review completed episodes without stopping agents`.

## Verification Runtime and Handoff

The host already has the official importable SDK at `/tmp/agd-omp-reviewer-18.14.0/node_modules/@oh-my-pi/pi-coding-agent`; use its installed `omp` entry on the probe PATH, not the standalone release as an importable SDK. The existing managed executable was checked with `BUN_BE_BUN=1 /home/feddericokz/.local/bin/omp --version` and returned Bun `1.4.4`; standalone `bun` is older. For sanitized Python children that locate `bun`, use a disposable executable wrapper that sets `BUN_BE_BUN=1` and execs that existing binary. No download, global symlink/config change or SDK modification is needed.

With that wrapper first on PATH, followed by the installed SDK's `node_modules/.bin`, run:

```sh
python3 -m pytest -q tests/test_reviewer*.py tests/test_uninstall*.py tests/test_observability.py tests/test_workers.py tests/test_supervision.py
bun test tests/*.test.ts
AGD_NATIVE_EPISODES_CHECK=1 bun --no-env-file tests/episodes-native.test.ts
```

The new native file must expose that standalone mode, following the existing native probe pattern, and print concrete lifecycle/evidence assertions. Its disposable homes contain no account-global credentials, enabled timers or telemetry stack. Existing missing-tmux cases are already known host limitations; do not rerun them merely to reconfirm absence. Native SDK persistence is not backend recovery acceptance and makes no paid-model quality claim. After task completion, obtain the normal independent whole-branch review and use native task lifecycle/publication authority; do not invent approval or lease cleanup.

**Execution recommendation:** Native in this session, then one fresh whole-branch reviewer. These four tasks share reader/receipt/material interfaces and are sequential; per-task fresh implementation contexts would add handoff cost without independent parallel work. Wait for the user's plan review and execution-method choice before product edits.
