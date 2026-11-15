# Test determinism review

Date: 2026-11-15. Read-only review; nothing in the repository was changed except this file.

## Verdict

The suite is not deterministic by construction, and one-at-a-time flake fixes will not converge. Three structural causes explain why:

1. **Time is used as a synchronisation primitive and as an oracle.** There is no clock seam, so tests sleep, poll against real deadlines, and assert on elapsed wall-clock time.
2. **Isolation is hand-maintained.** Database routing, process-global state and ambient configuration are isolated by patch lists and per-file fixtures that drift.
3. **CI cannot see flakes coming.** There is no repeat run, no order check, no per-test timeout, and no lint that stops a new sleep or elapsed-time assert from landing.

The seven recent incidents sort as follows. Process ownership is a fourth, narrower cause:

- Time as oracle or bound: #1191, #1196, and the #1215 scheduler-lifecycle failure (a production liveness bound tripping under load, per the issue).
- Isolation: #1197 (shared database connection plus an unsettled background task) and the leaked broker lease.
- Process ownership: #1193 / #1212 (killed supervisor never reaped by its owner).
- Not a flake: #1188 `test_home_continuation`, a deterministic defect.

The fix is a small set of primitives plus a ratchet, then conversion by category. It needs a handful of production seams, none of which adds a time-based guard.

Three things were confirmed by running tests, not only by reading:

- `test_extension_metadata_worker.py:33` fails 3 of 30 runs under CPU contention and 0 of 30 without it.
- Three tests fail deterministically in the main checkout because the operator's live `.env.dev` is loaded into the test process. They pass when the setting is neutralised.
- A 12-file, 257-test sample gave identical results in one process in file order, fully reversed, and shuffled across files. Order dependence was **not** reproduced in that sample.

## Scope and base

- Line numbers are from `.agent-worktrees/fix-flakes-1191-1196-1197` at `5fee71e9`, which is `origin/develop` (`8c71b9ee`) plus the in-flight #1191/#1196/#1197 commit. Files that commit does not touch are identical to `origin/develop`.
- The main checkout's local `develop` (`58e0d67a`) is 50 commits behind `origin/develop`. Experiments ran there because it has a virtualenv and is not another worker's tree. The tests behind each reproduced failure, and the conftests and settings module, are identical between the two commits.
- Backend: 605 test files, 6,970 test functions, about 300,000 lines, against 603 source files and about 449,000 lines.
- Frontend: 126 test files, surveyed briefly.
- Linux-only supervisor tests did not run on this arm64 Mac. Findings about them are from reading the code and issue #1212.

## Inventory

Counts are grep-based over `backend/tests/**/*.py`.

| Pattern | Occurrences | Files |
| --- | --- | --- |
| `time.sleep(` (74 are Python statements; about 32 are keep-alive sleeps inside child scripts) | 99 | 31 |
| `asyncio.sleep(` with a nonzero value | 124 | 56 |
| `asyncio.sleep(0)` | 47 | 27 |
| Standalone nonzero `await asyncio.sleep` followed by an `assert` within two lines | 48 | — |
| Assertions on elapsed wall-clock time | 29 | 20 |
| `while … < deadline` polling loops | 30 | 12 |
| Bounded `for _ in range(N)` polls with a sleep | 29 | — |
| `asyncio.wait_for(` with a guessed timeout (most common: 5 s ×21, 10 s ×11, 20 s ×10) | 183 | 67 |
| `await event.wait()` with no timeout and no watchdog | 91 | — |
| `datetime.now(` / `utcnow(` | 796 | 217 |
| `time.time()` / `time.monotonic()` | 93 / 162 | 40 / 36 |
| Synchronous `subprocess.*` calls | 102 | 42 |
| Local patches of some module's `get_session`, outside `conftest.py` | 197 | 95 |
| Test files that build their own SQLAlchemy engine | — | 55 |
| Direct `settings.x = …` assignment instead of `monkeypatch` | 46 | 9 |
| `skip` / `skipif` / `xfail` | 68 | 38 |
| Fake-clock libraries (`freezegun`, `time_machine`) | 0 | 0 |
| `gc.collect` / `weakref` / `__del__` in tests | 0 | 0 |
| Unseeded `random.*` in tests | 0 | 0 |

78 test files contain at least one nonzero sleep, elapsed-time assert or deadline poll.

Production side, over `backend/src/**/*.py`:

| Pattern | Occurrences | Files |
| --- | --- | --- |
| `time.time()` / `time.monotonic()` / `perf_counter(` | 105 / 152 / 102 | 36 / 33 / 19 |
| `datetime.now(` / `utcnow(` | 514 | 189 |
| Private `_now()`-style helpers, one per module | 44 | 44 |
| Constructor-level clock or sleep parameters | 6 | 6 |
| Functions with a `now=` parameter | 90 | 36 |
| Module-level timing constants (`*_SECONDS`, `*_TIMEOUT`, …) | 127 | — |
| Module-level mutable containers | 48 | — |
| Module-level locks, semaphores and events | 23 | 19 |
| Stateful module-level singletons (manager, broker, queue, host, …) | 19 | — |
| Raw `asyncio.create_task` / `ensure_future` | 53 | 32 |
| `track_task(` call sites | 6 | — |
| `subprocess.Popen(` | 12 | 11 |

## Findings

### 1. Time is the synchronisation primitive and the oracle

There is no shared clock abstraction. Tests therefore do three things that depend on machine speed.

**Elapsed-time asserts used as a proxy for "did not block".** The tightest:

- `tests/test_extension_metadata_worker.py:30-33` sleeps 5 ms and asserts less than 25 ms elapsed. Reproduced: 3 failures in 30 runs with 24 busy processes on 8 cores, measured 25.6 to 30.5 ms.
- `tests/test_browser_task_runtime.py:1833-1838` asserts a 10 ms bounded close returns in under 100 ms, then sleeps 180 ms and asserts the slow close finished.
- `tests/test_inference_accounting.py:738-740`, `tests/test_read_path_write_locks.py:111,186,215,307,361` and `tests/test_cockpit_db_contention.py:211,243` assert a read returned in under 1 to 4 s while a foreign writer holds the SQLite lock. The property under test is "this path takes no write lock". The tests already capture the SQL statements; the time bound adds only load sensitivity.

**Sleeps to let another task reach a point.** `tests/test_scheduler_lifecycle_1025.py` has 12 nonzero sleeps. Line 496 sleeps 10 ms so a gated tick can start, line 499 sleeps 50 ms so a deactivation can begin draining, then line 500 asserts on the state. Lines 318-326 race a 50 ms task against a 300 ms drain bound. The tasks do database I/O through aiosqlite's worker thread, so their progress is real-time.

**Polls that fall through.** Several bounded polls do not assert that the awaited condition was reached:

- `tests/test_scheduler_lifecycle_1025.py:625-628` polls 200 × 20 ms for a transition to finish and then reads the result regardless. This is the #1215 test.
- `tests/test_inference_accounting.py:647-652` polls for three queued operations, then asserts `order == ["stream"]`. If the poll times out, that assert still passes, so under load the test checks nothing.

**Negative proofs by sleeping.** "Sleep, then assert the task is not done" passes vacuously whenever the task is merely slow. Examples: `tests/test_research_phase_freeze.py:81-82,126-127`, `tests/test_inference_contact_atomicity.py:80-81`, `tests/test_repo_work_original_limits.py:161-162`, `tests/test_communication_source_callbacks.py:40-42,76-78`, `tests/test_cordis_governed_services.py:622-623`. These cannot flake red, but they also cannot catch the regression they describe.

**Real deadlines.** `tests/test_repo_work_original_limits.py:205,244,309` sleep until a wall-clock cutoff passes. `tests/test_connection_sync.py:645-653` sleeps 3 real seconds to prove a 2 s contact timeout and asserts `1.9 <= elapsed < 5`. `tests/test_discovery_search_receipts.py:142,168` assert both a lower and an upper elapsed bound against a real HTTP server.

**Time-based self-skip.** `tests/test_durable_workflow_remediation.py:942-945,958` skip the test when a subprocess takes longer than 25 s. A slow host turns the proof green without running it.

### 2. Database isolation is a patch list, and it is incomplete

`tests/conftest.py:41-76` lists 34 import sites of `get_session` and patches each one. 49 source modules bind `get_session` at import. Sixteen are not on the list, including `src.api.sessions`, `src.api.mail` (34 call sites), `src.api.documents` (21), `src.integrations.connection_sync` (11), `src.memory.m5`, `src.workflows.manager` and `src.operator.home_projection`.

A module missing from the list silently uses the real module-level engine created at import (`src/db/engine.py:22`), which points at the session-wide workspace database. Tests compensate with 197 local patches in 95 files (for example `tests/test_connection_sync.py:41-42`, `tests/test_home_continuation.py:110`), and 55 files build their own engine. Whether a given test reaches the right database depends on its author remembering every module the code path touches.

Two further problems sit in the same fixture:

- **The default database is one shared connection.** `tests/conftest.py:103-107` uses in-memory SQLite with `StaticPool`. Every session shares one DBAPI connection, so one session's rollback discards another session's flushed rows. This is the root cause the in-flight fix identified for #1197. Production uses a file database with WAL and a 20-connection pool, so the default fixture has different concurrency semantics from the product.
- **The replacement bypasses production code.** The fixture's `_get_session` (`tests/conftest.py:121-129`) skips the state-provider gate and the post-commit work-board event publication in the real `get_session` (`src/db/engine.py:2211-2230`).

The seam needed already exists. `get_session` consults `_session_factory_override` at call time (`src/db/engine.py:156,196-210,2218`). Six test files use it. Setting it once makes every import site correct without a list.

Schema creation is also order-dependent. `SQLModel.metadata.create_all` creates only the tables whose models have been imported so far; `tests/test_inference_accounting.py:35-39` documents a workaround for exactly this.

### 3. Process-global state is reset by hand, in part

Source has 48 module-level mutable containers, 23 module-level locks and 19 stateful singletons. `tests/conftest.py` has 12 autouse fixtures that reset about ten of them. The rest are reset ad hoc inside individual test files, or not at all.

The shared inference broker is the clearest case. `remote_inference_admission_broker` is a module singleton (`src/model_fabric/remote_inference_admission.py:525`), and six source modules bind it by name. No conftest fixture resets it. Tests replace it per file, patching up to three import sites (`tests/test_near_tee_text_slot.py:169-173`), and several use the live singleton directly (`tests/test_near_accounting_work_journey.py:98,186,348`). A lease left held or quarantined by one test blocks every later test in that process. That matches the reported serial-lease hang. `GpuAdmissionBroker.reset()` refuses to run while a lease is active (`src/model_fabric/gpu_admission.py:1596-1600`), so a leaked lease cannot be cleared by resetting.

Other unreset state with the same shape: `_ACTIVE_WORKER_TASKS` and `_dispatcher` (`src/work_board/dispatcher.py:112,12885`), `_QUARANTINED_LANES` (`src/workflows/repair_capacity.py:24`, `src/browser/task_lane.py:27`), `_executions` (`src/guardian/opportunity_runtime.py:24`), `_cordis_inflight` (`src/runtime_plugins/governed_services.py:44`), and the module-level `asyncio.Lock` and `asyncio.Semaphore` objects that outlive each test's event loop (`src/auth/service.py:312`, `src/api/mail.py:91-92`, `src/api/calendar.py:63-64`).

This is a structural risk, not a measured failure rate. The 12-file reversed and shuffled runs found no order dependence, so the existing autouse resets work for that sample. CI also runs each test file in its own pytest process (`backend/scripts/run_backend_test_shard.py:614-675`), which hides cross-file leaks in CI and leaves them possible in a local `uv run pytest`.

### 4. Killed subprocesses are not reaped by their owner

Three production paths signal a supervisor and return without `wait()`:

- `src/execution/repo_node.py:714-720` sends SIGTERM, waits 2 s, sends SIGKILL, and raises.
- `src/execution/repo_sandbox.py:3384-3390` does the same.
- `src/execution/tool_package_runner.py:138` breaks out after SIGKILL, and `:177-178` sends SIGTERM in `finally` with no wait.

The zombie is reaped only when `Popen.__del__` or the next `Popen()` call polls it. That is issue #1212. The test shows the dependence directly: `tests/test_repo_node_vertical.py:208` calls `os.waitpid` unguarded, while `:271` wraps the same call in `suppress(ChildProcessError)`. Line 209 then sleeps 2.2 s and asserts the child is gone, a 200 ms margin over the script's 2 s timer.

Tests also observe process state by polling. About 20 loops wait for a marker file with `while not marker.exists(): sleep(.01)` (`tests/test_repo_node.py:126,162,321,448`, `tests/test_process_tools.py:366,428,559`, `tests/test_repo_repair_executors.py:243,291,332,375`). No fixture checks that a test left no child process behind.

### 5. Background work is mostly untracked

`src/utils/background.py` provides `track_task` and `drain_tracked_tasks`. It has 6 call sites; source has 53 raw `asyncio.create_task` / `ensure_future` calls in 32 files, four of which discard the task (`src/observer/manager.py:333`, `src/memory/task_lessons.py:772`, `src/browser/task_runner.py:2168,2176`).

The `async_db` fixture drains tracked tasks only at teardown, with a 5 s bound after which it cancels them (`tests/conftest.py:152`). Nothing settles background work between two requests inside a test, which is the #1197 race. Abandoned `to_thread` workers also outlive their test: `tests/test_settings_api.py:676` leaves a thread sleeping for 2 s.

### 6. Tests read ambient machine state

**The operator's live configuration.** `backend/config/settings.py:9,247-255` loads `REPO_ROOT/.env.dev` at import. The main checkout has that file; worktrees and CI do not. In the main checkout these fail every time, in isolation:

- `tests/test_inference_accounting.py::test_authenticated_settings_canary_unknown_manual_readback`
- `tests/test_timeouts.py::TestRestChatTimeout::test_returns_504_on_timeout`
- `tests/test_timeouts.py::TestRestChatTimeout::test_guardian_state_timeout_falls_back_to_minimal_agent`

The error is `OPERATOR_AUTH_LOCAL_NO_LOGIN is permitted only when DEPLOYMENT_ENVIRONMENT=development`. With `OPERATOR_AUTH_LOCAL_NO_LOGIN=false` exported, all three pass. The conftest already carries two autouse fixtures that paper over other leaked settings (`isolate_operator_auth_defaults`, `clear_ambient_screenshot_analysis_provider`), so this has been patched one setting at a time. I did not read `.env.dev`; the setting name comes from the error.

**Wall clock, date and timezone.** Production derives behaviour from the local hour and date (`src/observer/sources/time_source.py:14-28`, `src/observer/manager.py:430-435`, `src/guardian/programme_digest.py:206`, and `date.today()` in four scheduler jobs). The conftest comment at `tests/conftest.py:301-304` records that an evening run changes delivery behaviour. I could not reproduce a timezone failure: 228 time-of-day tests passed under UTC, UTC−11 and UTC+14. That run was at about 06:40 UTC, so it does not cover evening or midnight-boundary behaviour. Treat this as a latent risk that the hermetic-inputs slice closes cheaply, not as a measured defect.

**Coverage tracing.** `backend/pyproject.toml` sets `--cov=src` in `addopts`, and CI passes `--no-cov`. Local runs are therefore slower than CI by default, which shifts every timing-sensitive test.

### 7. CI does not detect or contain nondeterminism

- No test runs more than once. A new race lands if it passes once.
- No order randomisation, and no single-process run to expose cross-file leaks.
- No per-test timeout. A hang surfaces as exit 124 for the whole file after 900 to 1,500 s, with no stack (`backend/scripts/run_backend_test_shard.py:137-146,657-670`).
- No lint. AGENTS.md records that the backend has no configured lint command, so nothing stops a new `time.sleep` or elapsed-time assert.
- No per-test history. The workflow uploads no JUnit report, so there is no flake rate to read.
- `pytest` runs without `--strict-markers` and without treating `ResourceWarning` or "coroutine was never awaited" as errors. `Popen.__del__` emits a `ResourceWarning` for exactly the #1212 case.

Of the last 60 `Tests` workflow runs, 17 succeeded, 21 failed, 20 were cancelled and 2 were in progress. Most failures before #1180 merged were all-shard regressions, so this is not a flake rate. Since then most failing runs have had one or two red shards.

### 8. Some "flakes" are product timing sensitivity

Two open issues describe product behaviour that changes under load, not a test artefact:

- #1215: the Cordis child stops admitting on a loaded runner, so the lifecycle stays blocked. `src/runtime_plugins/bridge.py` computes every deadline from `time.time()` inline (12 sites between lines 287 and 581).
- #1128: Vault redaction reports unavailable at load average 40.

The #1188 `test_home_continuation` case went the other way: filed as a flake, closed as a deterministic defect. Both directions argue for a fixed triage step before anything is labelled flaky.

### 9. Frontend is in reasonable shape

126 test files use `waitFor` / `findBy` (776 / 1,483 uses) with `asyncUtilTimeout` raised to 5 s (`frontend/src/test/setup.ts`) and a 20 s test timeout. Fake timers are used in 13 files, and each restores real timers. There are no skipped or focused tests. Every file that stubs a global also unstubs.

Residual real-timer waits: `src/components/auth/OperatorAuthGate.test.tsx:405` (300 ms), `src/hooks/useWebSocket.test.ts:188` (50 ms), `src/components/settings/ForgejoTitlePanel.test.tsx:69` (50 ms). `CockpitView.test.tsx` is 16,103 lines in one file, which makes it the slowest and most load-exposed unit. No test pins the system time, and 18 files call `Date.now()` or `new Date()`.

## Production seams today

| Concern | What exists | Gap |
| --- | --- | --- |
| Clock | `GpuAdmissionBroker(clock=…)` (`gpu_admission.py:420`); clock parameters in `repo_repair.py:1081`, `goal_programmes.py:131`, `goal_snapshot_to_file.py:450`, `project_source.py:235`; injectable `sleep` in `github_followthrough.py:622`; 90 `now=` parameters | No shared type. 44 private `_now()` helpers. Lifecycle, Cordis bridge, dispatcher, settings API, auth and observer read the clock inline |
| Timers and timeouts | 127 module constants that tests shrink with `monkeypatch` (26 test files) | Shrinking a real timeout to 10 ms still races real time. 38 inline numeric timeouts cannot be reached at all |
| Database session | `override_session_factory` context variable (`engine.py:196-210`) | Tests mostly do not use it |
| Process handles | `process_runtime_manager.reset_for_tests()`; Linux subreaper proof in `repo_supervisor.py` | No owner type that guarantees reap after kill; three kill-without-wait paths |
| Background tasks | `track_task`, `background_task_scope`, `drain_tracked_tasks` | Used by 6 of 59 spawn sites |
| Broker and dispatcher state | `broker.status()`, `broker.reset()` | No way to await a state change, so tests poll. Singletons are bound by name in importers |
| Settings | Pydantic settings object | Env file is fixed at import; no test opt-out |

## Recommended strategy

### Rules

1. Tests synchronise on causes: an event, a gate, a state-change notification. Never on a duration.
2. Tests assert outcomes and causal traces: what ran, in what order, what was never called. Never elapsed time.
3. Time that production reads is an input. Tests supply it and advance it explicitly.
4. Every process-global is either owned by an object whose lifetime the test controls, or reset by one registered fixture.
5. Every subprocess has one owner that reaps it. Tests read a typed outcome.
6. Inputs are hermetic: no ambient env file, timezone or wall-clock date.
7. Where real time is unavoidable (kernel signal delivery, SQLite's busy handler, a real browser), the bound is a generous liveness watchdog that fails with a diagnostic. Those tests carry a `realtime` marker and run in their own serial lane.

This follows the AGENTS.md rule against weakening assertions or deadlines: each conversion replaces a timing proxy with a stronger causal assertion.

### Test-infrastructure primitives

Put these in one package, for example `backend/tests/support/`.

**Clock.** A `Clock` protocol in `src/utils/clock.py` with `now()`, `time()`, `monotonic()` and `async sleep()`, a `SystemClock` default, and a `FakeClock` for tests with `advance(seconds)`.

- Make `FakeClock` advance only when the test says so. An auto-advancing event loop is unsafe here: source has about 140 `to_thread` / executor calls plus aiosqlite's thread, so the loop is often idle while real work is in flight, and auto-advance would fire timeouts falsely.
- Inject by constructor into stateful services. For leaf helpers, collapse the 44 `_now()` copies into one function that reads the clock.
- Do not adopt `freezegun` or `time-machine` as the main tool. They patch time process-wide, which breaks asyncio, aiosqlite and child processes. SQLite also has its own clock (`CURRENT_TIMESTAMP` and similar appear at 9 source sites).

**Synchronisation helpers.**

- `Gate`: a hook a fake calls that sets `reached` and blocks until `release`. This replaces "sleep 10 ms so it gets there".
- `assert_blocked(task, at=gate)`: waits until the task is observed parked at a known point, then asserts it is not done. This replaces every vacuous negative proof.
- `settle()`: drains tracked tasks and waits for loop quiescence. Use it between requests.
- `wait_until(predicate, notifier)`: awaits a state-change notification. Where no notifier exists, a bounded poll that raises with the last observed state. It must never fall through.
- `started_or_raise` (`tests/async_wait_support.py`, 3 users today) stays and moves into this package.
- One global per-test watchdog (`pytest-timeout`, plus the built-in `faulthandler_timeout`). With it, the 183 guessed `wait_for(timeout=N)` values and 91 bare `await event.wait()` calls become plain waits under one generous bound that dumps stacks on a hang.

**Isolation fixtures.**

- `db`: one fixture that sets the session-factory override against a per-test file database with production pragmas. It replaces the 34-entry patch list and, over time, the 197 local patches.
- `isolated_process_state` (autouse): a test-side registry of every module-level mutable object with a reset or snapshot/restore. A guard test scans source with `ast` and fails when a new module-level mutable object is not registered.
- Leak checks (autouse, failing): no pending asyncio tasks beyond the test's own, no new non-daemon threads, and no child processes (`os.waitpid(-1, WNOHANG)` must raise `ChildProcessError`).
- `fresh_inference_broker`: a new broker per test, reachable through one accessor.
- Hermetic settings: the root conftest disables the env file before `config.settings` is imported, sets `TZ=UTC`, and snapshots and restores the settings object so the 46 direct assignments cannot leak.

**Subprocess helpers.**

- A `spawned` fixture that owns every `Popen` a test creates, and kills and reaps them at teardown.
- Readiness by pipe: the child writes one byte to an inherited descriptor and the test blocks on the read. This replaces marker-file polling.
- Exit by `process.wait()` for direct children, or `pidfd` on Linux for grandchildren. No `/proc` polling.

### Lint and CI guards

**A ratchet script.** `scripts/check_test_determinism.py`, standard library only, in the style of `scripts/check_docs_contract.py`. It parses tests with `ast`, so sleeps inside child-script strings are not counted. It reports:

- `time.sleep(...)` and nonzero `asyncio.sleep(...)` in test code;
- comparisons whose operands read `time.monotonic()`, `time.time()` or `perf_counter()`;
- `pytest.skip` inside an `except TimeoutExpired` handler;
- direct `settings.x = …` and `os.environ[...] = …`;
- `patch(...get_session...)` outside the support package;
- raw `subprocess.Popen` outside the `spawned` helper.

It compares against a checked-in per-file baseline. A count above baseline fails. A count below baseline also fails until the baseline is lowered, so the number only goes down. A `@pytest.mark.realtime(reason=…)` marker is the only exemption.

A second mode covers source: new direct clock reads outside `src/utils/clock.py`, and new module-level mutable state outside the registry.

**Pytest configuration.** `--strict-markers`; `ResourceWarning`, unraisable exceptions and never-awaited coroutines as errors; `faulthandler_timeout`; a per-test timeout; a JUnit report uploaded from every shard.

**Changed-test stress job.** On each pull request, collect the test files the diff touches and run them 20 times under CPU contention (twice as many busy processes as cores) with a printed random order seed. All 20 must pass. This is an ordinary regression check with intercepted transport; it is not an eval and makes no inference call.

**Scheduled full run.** Once a day on `develop`:

- each file in random order with the seed printed;
- each shard once as a single pytest process, to expose cross-file leaks;
- the `realtime` lane repeated under contention;
- one pass under a non-UTC timezone.

**No automatic retries on blocking lanes.** A retry hides a race. Use reruns only to classify: when a test fails, the job reruns that node 20 times in isolation and prints "deterministic" or "intermittent, k of 20". The job stays red.

**Quarantine policy.**

- A `quarantine` marker must name an issue.
- Quarantined tests still run in a non-blocking lane and report.
- A test leaves quarantine through a change: the fix lands with a 50-of-50 stress pass. There is no calendar expiry.
- Cap the list (ten is a reasonable start) so it cannot become a dumping ground.

**Triage step before the label.** Before an issue is titled "Flaky", run the failing node 50 times in isolation at the same commit and once in its original file order. Record the counts in the issue. #1188 would have been filed as deterministic.

### Production seams to add

In priority order. None adds an expiry or a new time bound; existing bounds keep their values and read time through the clock.

1. **Explicit reap after kill** (#1212) at `repo_node.py:714-720`, `repo_sandbox.py:3384-3390` and `tool_package_runner.py:138,177-178`, returning a typed outcome.
2. **Env-file selection** in `config/settings.py:9,247-248`, so a process can state which file it loads, or none.
3. **Session factory as the single database seam.** Already present; make it the documented route and stop tests replacing `get_session`.
4. **Broker and dispatcher reachable through an accessor or the app's composition root** instead of by-name module binding. This matches the AGENTS.md rule against hidden global state in new seams and the ADR-026 direction.
5. **State-change notification on the broker and lifecycle.** The broker already has a condition variable and async waiters (`gpu_admission.py:445-446`). Exposing "wait until the queue looks like this" removes about 15 polling loops.
6. **Clock adoption**, starting where tests currently race real time:
   - `src/runtime_plugins/lifecycle.py:310,351,405-407,433,601-605`
   - `src/runtime_plugins/bridge.py:287-581`
   - `src/work_board/dispatcher.py` (`_RESEARCH_CANCEL_WORKER_WAIT_SECONDS`)
   - `src/api/settings.py` (summary timeouts) and `src/api/extension_metadata.py` (`CALLER_WAIT_SECONDS`)
   - `src/browser/task_runner.py` (bounded close and cancel)
   - `src/integrations/connection_sync.py:444` and its contact timeout
   - `src/observer/sources/time_source.py:14` and `src/observer/manager.py:419-435`
   - `src/api/ws.py:188` and `src/model_fabric/near_tee_catalog.py:701-712`
7. **Background task ownership.** Route the 53 raw spawns through `track_task` or an owning task group.

## Migration plan

AGENTS.md asks for one tracked parent issue and discourages partial micro-PRs. I suggest one parent issue with these slices as its checklist, delivered as a user-directed stacked train. Each slice is independently reviewable and leaves the suite green.

| # | Slice | Contents | Acceptance |
| --- | --- | --- | --- |
| 1 | Ratchet and visibility | Determinism script with baseline; pytest strictness; per-test timeout; JUnit upload | A fixture PR that adds one `time.sleep` to a test fails CI. A deliberate hang reports the test name and a stack. Baseline equals today's counts |
| 2 | Hermetic inputs | Env file off in tests; `TZ=UTC`; settings snapshot/restore; import all models before `create_all` | The three tests in finding 6 pass in the main checkout with `.env.dev` present. Suite result is identical with and without that file. The two conftest fixtures that mask leaked settings are deleted |
| 3 | Stress and order lanes | Changed-test stress job; scheduled random-order, single-process and timezone runs; classification rerun; quarantine marker and policy | A PR touching `test_extension_metadata_worker.py` fails the stress job today. The scheduled run prints seeds and uploads results |
| 4 | Sync primitives and timing conversions | `tests/support` helpers; convert all 29 elapsed asserts, the 48 sleep-then-assert sites and the fall-through polls. Tests only | Zero elapsed-time asserts outside `realtime`. Every converted test passes 50 of 50 under contention. Each former negative proof fails when its guard is removed from the product (mutation check recorded in the PR) |
| 5 | Database seam | `db` fixture on the session-factory override; delete `_PATCH_TARGETS`; file database by default | No `get_session` patch in `conftest.py`. A sentinel on the canonical engine fails any test that reaches it. Local-patch count drops by an agreed first tranche and the ratchet holds the rest |
| 6 | Process state and leak checks | State registry with `ast` guard; task, thread and child leak checks; broker accessor and `fresh_inference_broker` | A deliberately leaky test fails each check. Each shard passes as one process, in file order and reversed |
| 7 | Subprocess ownership | #1212 reap; `spawned` fixture; pipe readiness; `ResourceWarning` as error | `test_durable_node_lifecycle_and_retained_slot[cleanup_deadline]` passes 50 of 50 on Linux with GC disabled and with a forced collection after the kill. No marker-file polling remains for direct children |
| 8 | Clock seam | `Clock`, `FakeClock`; adopt in the modules listed above; convert real-deadline tests | Listed modules have no direct clock reads (ratchet). `test_scheduler_lifecycle_1025.py` has no nonzero sleep. The connection-sync timeout test does no real waiting |

Order matters in two places. Slices 1 to 3 come first because they stop the count growing while the rest is converted. Slice 8 comes last because it touches the most production code and is the least urgent once tests stop asserting on elapsed time.

Slices 5 to 8 change production code or shared fixtures and each needs the independent critic review AGENTS.md requires.

## Worst offenders

Elapsed-time oracles, tightest margin first:

| Location | Bound |
| --- | --- |
| `tests/test_extension_metadata_worker.py:33` | under 25 ms after a 5 ms sleep (reproduced failing) |
| `tests/test_browser_task_runtime.py:1835` | under 100 ms |
| `tests/test_settings_api.py:593` | under 0.5 s |
| `tests/test_settings_api.py:699-700` | under 1.0 s, twice |
| `tests/test_inference_accounting.py:740` | under 1.0 s against a held SQLite lock |
| `tests/test_read_path_write_locks.py:111,186,215,307,361` | under 2.0 s |
| `tests/test_native_notification_queue.py:593` | under 2.0 s |
| `tests/test_repo_source_recovery_authority.py:564` | under 2 s |
| `tests/test_cordis_research_parity.py:192` | under 2 s |
| `tests/test_research_http_envelope.py:104` | under 3 s |
| `tests/test_process_tools.py:289,333` | under 4 s for a 1 s timeout, then `sleep(2.2)` at `:290,334` |
| `tests/test_cockpit_db_contention.py:211,243` | under busy-timeout minus 1 s |
| `tests/test_connection_sync.py:653` | `1.9 <= elapsed < 5` after a real 3 s sleep |
| `tests/test_discovery_search_receipts.py:142,168` | lower and upper bounds against a real server |
| `tests/test_scheduler_lifecycle_1025.py:691` | under 10 s |
| `tests/test_near_tee_streaming_tools.py:263` | under 10 s |

Sleep or poll used as synchronisation:

| Location | Problem |
| --- | --- |
| `tests/test_scheduler_lifecycle_1025.py:318-326,496-500,570-573,652-655,684-688` | Fixed sleeps before state asserts |
| `tests/test_scheduler_lifecycle_1025.py:625-628` | 4 s poll with no assertion that it finished (#1215) |
| `tests/test_repo_node_vertical.py:208-210` | Unguarded `waitpid`, then `sleep(2.2)` (#1193, #1212) |
| `tests/test_repo_node_vertical.py:182,346,354-358` | Marker-file and marker-state polls |
| `tests/test_inference_accounting.py:647-652` | Poll falls through to an assert that passes anyway |
| `tests/test_inference_accounting.py:677-682`, `tests/test_openrouter_setup_api.py:660-665,858-863` | 0.5 to 1 s polls on broker state |
| `tests/test_near_accounting_work_journey.py:527-556` | Three 4 s polls on the live shared broker |
| `tests/test_sync_gpu_admission.py:414-418,494-495,563-564` | Sleep, then assert on a thread's progress |
| `tests/test_extension_metadata_worker.py:75-76,199-200` | Unbounded 1 ms spin on a thread event |
| `tests/test_cordis_agent_loop_chat.py:337,593,632` | `time.sleep(0.05)` then assert |
| `tests/test_repo_work_original_limits.py:205,244,309` | Sleeps until a real cutoff passes |
| `tests/test_durable_workflow_remediation.py:942-945,958` | Skips when slow |

Vacuous negative proofs:

- `tests/test_research_phase_freeze.py:81-82,126-127`
- `tests/test_inference_contact_atomicity.py:80-81`
- `tests/test_repo_work_original_limits.py:161-162`
- `tests/test_communication_source_callbacks.py:40-42,76-78`
- `tests/test_cordis_governed_services.py:622-623`

Structural sites:

- `tests/conftest.py:41-76` (patch list), `:103-107` (shared connection), `:152` (5 s drain)
- `backend/config/settings.py:9,247-255` (env file at import)
- `src/db/engine.py:22` (engine at import)
- `src/model_fabric/remote_inference_admission.py:525`, `src/model_fabric/gpu_admission.py:1969` (broker singletons)
- `src/work_board/dispatcher.py:112,12885`
- `src/execution/repo_node.py:714-720`, `src/execution/repo_sandbox.py:3384-3390`, `src/execution/tool_package_runner.py:138,177-178`

## Note for the in-flight #1191 fix

The commit at `5fee71e9` converts one elapsed-time assert in `tests/test_settings_api.py`. Two sibling tests in the same file keep theirs: `:593` (`elapsed < 0.5`) and `:699-700` (`< 1.0`, twice), and `:676` still leaves a worker thread sleeping for 2 s after the test ends. The same event-gate pattern applies to both.

The #1196 change names the 5 s bound and lets the test set it to `None`. That is a reasonable interim seam; slice 8 would replace it with the clock.

## Experiments

All runs used `uv run --no-sync pytest -p no:cacheprovider --no-cov` in the main checkout with bytecode writing disabled.

| Experiment | Result |
| --- | --- |
| `test_extension_metadata_worker.py::test_worker_preserves_heartbeat…` and `test_browser_task_runtime.py::test_cleanup_timeout_is_bounded…`, 30 repeats each, ambient load | 60 passed |
| Same, with 24 busy processes on 8 cores for about 20 s | 57 passed, 3 failed, all at `test_extension_metadata_worker.py:33` |
| 12 files in one process, file order | 240 passed, 14 skipped, 1 failed, 2 errors |
| Same 12 files, fully reversed order | Identical: 240 passed, 14 skipped, the same 1 failure and 2 errors |
| Same 12 files, all tests shuffled across files with a fixed seed | Identical again |
| The 3 failing tests alone | Fail every time with the `.env.dev` error |
| The 3 failing tests alone with `OPERATOR_AUTH_LOCAL_NO_LOGIN=false` | 3 passed |
| 12 time-of-day test files (228 tests) under `TZ=UTC`, `Pacific/Pago_Pago` (UTC−11) and `Pacific/Kiritimati` (UTC+14), with the `.env.dev` setting neutralised | 228 passed under each |

The 12 files were `test_near_accounting_work_journey`, `test_near_text_work_journey`, `test_cordis_governed_services`, `test_guardian_opportunity_notifications`, `test_background_inference_toggles_1112`, `test_extension_metadata_worker`, `test_scheduler_lifecycle_1025`, `test_timeouts`, `test_delivery`, `test_tools`, `test_inference_accounting` and `test_observer_manager`.

Limits:

- The machine's load average was between 6 and 30 throughout, from other work, so the "ambient" runs were not idle runs.
- The browser-runtime 100 ms bound did not fail in 30 loaded runs. It is listed on margin, not on a reproduced failure.
- No Linux supervisor test ran.
- The timezone files were `test_evening_review`, `test_daily_briefing`, `test_observer_manager`, `test_activity_digest`, `test_observer_time`, `test_user_state`, `test_scheduler_job_audit`, `test_screen_observation`, `test_strategist_tick`, `test_915_approval_timezone`, `test_programme_digest` and `test_end_of_day_goal_report`. The time of day was not varied.
- The order sample is 12 of 605 files. It shows the existing resets work there; it does not show the suite is order-independent.
