# Workstream 05: Guardian Intelligence

## Status On `develop`

- [ ] Workstream 05 is only partially shipped on `develop`.

## Paired Research

- primary design docs: [01. Guardian Thesis](/research/guardian-thesis), [02. Human Model And Memory](/research/human-model-and-memory), [11. Superiority Program](/research/superiority-program), and [14. Seraph Memory SOTA Roadmap](/research/seraph-memory-sota-roadmap)
- synthesis context: [00. Research Synthesis](/research)

## Shipped On `develop`

- [x] soul-backed persistent identity
- [x] long-term vector memory and consolidation
- [x] hierarchical goals and progress tracking
- [x] strategist agent with restricted guardian tool set
- [x] daily briefing, evening review, activity digest, and weekly activity review foundations
- [x] observer-driven user-state and attention-budget modeling
- [x] observer salience, confidence, and interruption-cost scoring that feeds guardian state and proactive policy
- [x] explicit guardian-state synthesis that unifies observer context, memory, current session, recent sessions, confidence, and observer salience signals for downstream agent paths
- [x] explicit intervention policy that distinguishes act, bundle, defer, request-approval, and stay-silent outcomes for proactive guardian messages, including low-salience suppression and high-interruption bundling
- [x] persisted guardian intervention records and explicit user-feedback capture that flow back into guardian-state summaries
- [x] first multi-signal outcome-learning loop that uses recent outcomes on the same intervention type to reduce interruptions after negative feedback and prefer direct delivery or native reroute after repeated positive/acknowledged outcomes
- [x] second-layer salience calibration that promotes aligned active-work signals and allows grounded high-salience nudges to bypass generic high-interruption bundling outside focus mode
- [x] deeper guardian behavioral eval coverage that proves grounded high-salience delivery versus degraded-confidence defer behavior at the delivery gate
- [x] deeper guardian behavioral eval coverage that proves strategist tick can combine learned delivery bias, native delivery, and continuity-state visibility in one deterministic contract
- [x] guardian world model that now carries current focus, active commitments, active projects, active constraints, recurring patterns, active routines, collaborators, recurring obligations, project timelines, memory signals, corroboration sources, continuity threads, open loops or pressure, recent execution pressure, focus alignment, and intervention receptivity inside guardian state
- [x] guardian state now also carries learned communication guidance derived from recent intervention outcomes, including timing, suppression, blocked-state, and thread-preference bias, instead of only raw outcome history
- [x] guardian world-model receptivity and intervention policy can now learn blocked-state async handling instead of only direct/native/timing bias

## Working On Now

- [x] this workstream remains central in the repo-wide horizon through stronger learning quality after the corroboration-aware world-model and richer thread-guidance pass shipped
- [x] the `observer-salience-and-confidence-model` foundation is now shipped on `develop`
- [x] the first multi-signal learning layer and first salience-calibration pass are now shipped, and the next major gap is deeper modeling plus richer long-horizon learning rather than more missing observer fields
- [x] `world-model-memory-fusion-v9`, `guardian-learning-policy-v9`, and `guardian-behavioral-evals-v9` are now represented in the shipped batch, so the next gap shifts to project-graph quality, longer-horizon learning, and stronger cross-thread policy adaptation rather than more missing first-pass structure

## Still To Do On `develop`

- [ ] richer human world modeling that goes beyond the new project/routine/collaborator/obligation/timeline-aware world-model layer plus active blockers, next-up, dominant-thread synthesis, memory buckets, and corroboration-source grounding
- [ ] stronger learning loops based on intervention outcomes beyond the first multi-signal delivery/channel/escalation plus phrasing/cadence/timing/suppression/blocked-state/thread layer
- [ ] stronger salience calibration and confidence quality beyond the first aligned-work/high-salience pass
- [ ] stronger linkage between guardian state, execution choices, and feedback-driven policy adaptation

## Next Memory Upgrade Program

The canonical PR queue for the upgraded memory system lives in [00. Master Roadmap](./00-master-roadmap.md).

The delivery shape should stay split into three implementation batches:

### Batch A: Structured memory foundation

- `memory-eval-harness-v1`
- `typed-memory-schema-v1`
- `memory-kinds-and-provenance-v1`
- `entity-and-project-linking-v1`
- `bounded-memory-snapshots-v1`

### Batch B: Episodic and observer-driven retrieval

- `episodic-memory-events-v1`
- `observer-episodic-fusion-v1`
- `session-search-fts-and-event-index-v1`
- `hybrid-memory-retrieval-v1`
- `guardian-state-retrieval-planner-v1`

### Batch C: Learning, consolidation, and decay

- `memory-flush-lifecycle-hooks-v1`
- `multi-stage-memory-consolidation-v1`
- `soul-projection-and-structured-profile-v1`
- `procedural-memory-from-outcomes-v1`
- `memory-decay-contradiction-and-archive-v1`
- `guardian-memory-behavioral-evals-v1`

The batch split is the right implementation shape because the dependencies are real:

- Batch A creates the durable typed substrate
- Batch B turns sessions and observer signals into usable episodic recall
- Batch C makes that memory updateable, policy-relevant, and behaviorally testable

Each Batch A internal slice should close with:

- targeted validation commands
- a subagent review pass for bugs, regressions, and misleading claims
- a short implementation log entry in this document before the slice is treated as complete

## Batch A Branch Review Log

This section records the internal Batch A slices on the feature branch before the aggregate GitHub PR is opened.

### `memory-eval-harness-v1`

- status: complete on `feat/memory-batch-a-v1`, pending inclusion in the aggregate Batch A PR
- scope:
  - expanded `backend/src/evals/harness.py` with deterministic memory-behavior scenarios for linked commitment recall, linked collaborator recall, bounded snapshot stability, and supersession filtering
  - taught the eval harness DB patch helpers about `src.memory.repository.get_session` and cleared the bounded snapshot cache around each scenario so the new memory scenarios run in isolated process state
  - updated the existing session-consolidation evals so they still represent a clean successful dual-write path under the new structured-memory substrate instead of tripping `partially_succeeded` from mock-only vector writes
  - extended `backend/tests/test_eval_harness.py` so the aggregate runtime-eval contracts now assert the new memory scenario details directly
- validation:
  - `backend/.venv/bin/python -m py_compile backend/src/evals/harness.py backend/tests/test_eval_harness.py`
  - `backend/.venv/bin/python -m pytest backend/tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios -q`
  - `backend/.venv/bin/python -m pytest backend/tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details -q`
  - `backend/.venv/bin/python -m pytest backend/tests/test_eval_harness.py -q`
  - `backend/.venv/bin/python -m pytest backend/tests/test_memory_repository.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py backend/tests/test_memory_snapshots.py backend/tests/test_eval_harness.py -q`
- subagent review:
  - reviewer: `Zeno` (`019d24e8-51f8-7402-b24e-82c872dd4813`)
  - initial findings:
    - the first linked commitment and collaborator scenarios could still pass from the generic structured kind buckets, so they did not actually prove the project-linked recall path
    - the first bounded snapshot scenario built the “new session” state without creating that session first, so it could fall back to the global snapshot lane instead of exercising real per-session freezing
    - the compactness check was measured before later memory changes landed, which made the “remains compact after later changes” claim weaker than intended
  - fixed before the slice stayed marked complete:
    - seeded higher-importance unrelated commitments and collaborators, captured a baseline no-active-project structured bundle, and then asserted the Atlas-linked items only reappear once active-project linking is in play
    - created a real `snapshot-new` session before the fresh-state build and asserted that the scenario uses an actual session record
    - moved the bounded line-count check to the post-change states so the compactness contract is evaluated after later memory writes land
    - narrowed the commitment-continuity scenario description so it now claims only the memory-context recall behavior that the eval actually proves
  - final recheck:
    - no remaining material issue was found after the linked-recall proof, real-session snapshot path, and wording fixes

### `typed-memory-schema-v1`

- status: complete on `feat/memory-batch-a-v1`, pending inclusion in the aggregate Batch A PR
- scope:
  - added structured SQLite memory tables and enums for typed memories, entities, sources, edges, and snapshots
  - added `memory_repository` CRUD helpers for entities, memories, edges, and snapshots
  - made session consolidation dual-write into the structured store while keeping the existing vector path alive
  - enabled SQLite foreign-key enforcement in both runtime and test engines
- validation:
  - `python3 -m py_compile backend/src/db/engine.py backend/src/db/models.py backend/src/memory/repository.py backend/src/memory/consolidator.py backend/tests/conftest.py backend/tests/test_db_engine.py backend/tests/test_memory_repository.py backend/tests/test_consolidator.py backend/tests/test_consolidation_reliability.py`
  - `backend/.venv/bin/python -m pytest backend/tests/test_consolidator.py -q`
  - `backend/.venv/bin/python -m pytest backend/tests/test_db_engine.py backend/tests/test_memory_repository.py backend/tests/test_consolidation_reliability.py backend/tests/test_vector_store.py backend/tests/test_guardian_state.py -q`
- subagent review:
  - reviewer: `Euclid` (`019d24ad-f0fa-7700-af86-85b79cd7aea5`)
  - initial findings:
    - legacy `category -> kind` backfill was missing
    - dual-write inconsistencies were reported as clean success
    - SQLite foreign keys were declared but not enforced
    - the substrate exposed richer kinds and links than the live writer actually used
  - fixed before commit:
    - backfilled legacy `kind` values from `category`
    - changed consolidation audit outcome to `background_task_partially_succeeded` when vector and structured writes diverge
    - added foreign-key PRAGMA setup to runtime and tests
    - added tests for legacy backfill, partial-success audit reporting, and enforced entity links
  - deferred to later Batch A slices:
    - richer memory kinds beyond the legacy four writer buckets
    - entity and project linking in the live writer
    - bounded snapshot generation and consumption

### `memory-kinds-and-provenance-v1`

- status: complete on `feat/memory-batch-a-v1`, pending inclusion in the aggregate Batch A PR
- scope:
  - added `backend/src/memory/types.py` to normalize kind/category mapping, bucket mapping, and consolidation payload parsing
  - upgraded session consolidation to accept typed memory objects with kind, summary, confidence, importance, and last-confirmed provenance while remaining backward-compatible with the legacy four lists
  - started feeding richer structured memory kinds into guardian state and the world model instead of dropping everything back to coarse vector categories
- validation:
  - `python3 -m py_compile backend/src/memory/types.py backend/src/memory/consolidator.py backend/src/memory/repository.py backend/src/guardian/state.py backend/src/guardian/world_model.py backend/tests/test_memory_repository.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py`
  - `backend/.venv/bin/python -m pytest backend/tests/test_memory_repository.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py -q`
  - `backend/.venv/bin/python -m pytest backend/tests/test_consolidation_reliability.py -q`
  - `backend/.venv/bin/python -m pytest backend/tests/test_guardian_state.py backend/tests/test_consolidator.py backend/tests/test_memory_repository.py backend/tests/test_consolidation_reliability.py -q`
- review notes:
  - local regression caught and fixed before commit:
    - project-memory enrichment briefly leaked execution-pressure lines into `world_model.active_projects`; the final slice keeps those lines in `project_state` while `active_projects` stays project-only
  - subagent review:
    - reviewer: `Ptolemy` (`019d24c7-9a43-7860-8b66-b5d77adc3187`)
    - findings:
      - caught the `project_state` / `active_projects` regression while guardian-state tests were failing
      - flagged that richer kinds still collapse through the vector category lane, so only the structured bundle preserves typed recall behavior for now
    - fixed before the slice stayed marked complete:
      - separated `active_project_signals` from `project_state` so execution-pressure lines stay out of `active_projects`
  - secondary review:
    - reviewer: `Dalton` (`019d24c9-0610-7102-a84f-a58874fb38f9`)
    - findings:
      - confirmed the structured-memory bundle does change runtime behavior even though the vector layer still uses coarse categories
      - requested a typed `commitment` / `project` test to prove structured kinds survive while vector writes keep coarse categories
    - fixed after review:
      - added a typed project-and-commitment consolidation test that proves structured `kind` values are preserved while vector writes still route through coarse `fact` / `goal` categories
  - deferred to later Batch A slices:
    - project/entity linking still relies on names embedded in metadata, not real entity ids
    - the vector recall path still uses coarse categories until Batch B hybrid retrieval work lands
    - bounded snapshot generation still does not project this richer memory into a stable session-start snapshot

### `entity-and-project-linking-v1`

- status: complete on `feat/memory-batch-a-v1`, pending inclusion in the aggregate Batch A PR
- scope:
  - added `backend/src/memory/linking.py` to resolve conservative subject and project entity links during typed consolidation writes
  - extended `memory_repository` with exact or alias entity resolution plus linked-memory lookup by project or subject ids
  - upgraded guardian-state synthesis to boost project-linked structured memories for currently active projects instead of relying only on global importance ordering
  - kept the runtime claim narrow: subject-side entity ids are now stored, but live guardian recall currently consumes project-linked recall only
- validation:
  - `python3 -m py_compile backend/src/memory/linking.py backend/src/memory/repository.py backend/src/memory/consolidator.py backend/src/guardian/state.py backend/tests/test_memory_repository.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py`
  - `backend/.venv/bin/python -m pytest backend/tests/test_memory_repository.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py -q`
- subagent review:
  - reviewer: `Volta` (`019d24d3-df48-7700-9943-e69cbfaf93aa`)
  - initial findings:
    - entity linking had moved outside the per-item structured-write fault boundary, which would have let a single link failure abort the whole consolidation run
    - the runtime slice only consumes project-linked recall today, so broader “entity recall” wording would overstate what is live
    - the original tests proved project-link persistence but not the collaborator subject-link path
    - project-linked recall could still fragment when a project memory landed without an explicit `project` field and the observer only exposed a shorter active-project token
  - fixed before the slice stayed marked complete:
    - moved `resolve_memory_links(...)` back inside the per-item structured-write `try` block so link failures are counted as partial writes instead of aborting the whole run
    - added a consolidation test that forces entity-link failure and proves the audit outcome stays `background_task_partially_succeeded`
    - added collaborator-link assertions so subject and project ids are both exercised in tests
    - added a conservative unique project-token fallback in entity resolution plus a guardian-state test that proves `Atlas` can still resolve linked memories stored under `Atlas launch` when that match is unique
  - final recheck:
    - no remaining high-severity issue was found after the fault-isolation and unique-project-fallback fixes
  - deferred to later slices:
    - guardian-state recall still does not consume subject-side entity ids, only project-linked ids
    - richer entity-driven retrieval beyond active-project boosts belongs in the later hybrid retrieval planner work

### `bounded-memory-snapshots-v1`

- status: complete on `feat/memory-batch-a-v1`, pending inclusion in the aggregate Batch A PR
- scope:
  - added `backend/src/memory/snapshots.py` to build a compact bounded guardian snapshot from soul identity plus structured memory kinds
  - refreshes that snapshot after consolidation so durable semantic changes project into the always-on guardian prefix without rebuilding it ad hoc every call
  - changed guardian-state synthesis to use a session-frozen bounded snapshot plus a live todo overlay, with structured-memory recomputation as the degraded fallback if snapshot load or save fails
  - keyed the in-process session freeze by session lifecycle (`session_id + created_at`) so reused session ids do not keep serving stale bounded recall
- validation:
  - `python3 -m py_compile backend/src/memory/snapshots.py backend/src/memory/consolidator.py backend/src/guardian/state.py backend/tests/conftest.py backend/tests/test_memory_snapshots.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py`
  - `backend/.venv/bin/python -m pytest backend/tests/test_memory_snapshots.py backend/tests/test_consolidator.py backend/tests/test_guardian_state.py -q`
- subagent review:
  - reviewer: `Laplace` (`019d24da-bfeb-7360-8b3a-6e9bcef8fcb7`)
  - initial findings:
    - the first cut reused one global snapshot blindly, so it was neither fresh on new sessions nor stable against unrelated snapshot churn
    - the degraded path fell back to soul-only lines and silently dropped structured bounded recall when snapshot IO failed
    - the first session-freeze pass still needed an invalidation path for reused session ids in the same process
  - fixed before the slice stayed marked complete:
    - `get_or_create_bounded_guardian_snapshot(...)` now recomputes source hash against live soul and structured memory, refreshes stale global snapshots, and freezes the resolved content per session
    - guardian-state fallback now recomputes bounded recall from structured memory via `render_bounded_guardian_snapshot(...)` instead of collapsing to soul-only memory
    - the session cache key now includes the session lifecycle (`created_at`) so deleting and recreating the same session id invalidates the frozen snapshot
    - added tests for stale snapshot refresh, per-session freeze, session-id reuse invalidation, consolidation-triggered snapshot refresh, and structured-memory fallback when snapshot load fails
  - final recheck:
    - no remaining high-severity or medium-severity issue was reported after the session-lifecycle and degraded-fallback fixes
  - deferred to later slices:
    - snapshot promotion still runs from consolidation boundaries; broader lifecycle hooks near compaction and workflow boundaries belong in the later flush-lifecycle slice
    - bounded snapshot contents are still semantic-only and do not yet include procedural memory or episodic recall planning

## Non-Goals

- marketing “guardian intelligence” before the learning loop is real
- confusing retrieval volume with understanding quality

## Acceptance Checklist

- [x] Seraph can retain identity, memory, and goals across sessions
- [x] Seraph can generate proactive guardian outputs from that context
- [x] Seraph has an explicit guardian-state object rather than spreading that reasoning across call sites
- [x] Seraph has an explicit intervention policy rather than only deliver-versus-queue heuristics
- [x] Seraph records intervention outcomes and explicit user feedback in durable guardian state
- [x] Seraph learns at least one policy-relevant lesson from intervention outcomes and explicit user feedback
- [x] Seraph scores observer state by salience, confidence, and interruption cost before guardian strategy and delivery
- [x] Seraph uses calibrated high-salience observer signals to change real delivery outcomes instead of only logging them
- [x] Seraph has deterministic behavioral proof that the calibrated high-salience deliver path and degraded-confidence defer path stay distinct at the delivery gate
- [x] Seraph has deterministic behavioral proof that strategist nudges can follow learned native-delivery bias and still remain visible through continuity surfaces
- [x] Seraph has a first explicit world model inside guardian state instead of relying only on retrieval plus prompt prose
- [x] Seraph's world model now reflects recent active projects, active constraints, recurring patterns, active routines, collaborators, recurring obligations, project timelines, structured memory signals, corroboration sources, continuity threads, and degraded execution signals instead of only static focus/commitment text
- [x] Seraph now feeds learned communication guidance back into guardian state and intervention policy instead of leaving recent outcomes as passive history
- [ ] Seraph reliably learns from intervention outcomes in a way that improves future policy quality beyond the first delivery/channel bias layer
- [ ] Seraph reliably models the human well enough to intervene at consistently high quality
