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
  - subagent review attempts:
    - requested from `Ptolemy` (`019d24c7-9a43-7860-8b66-b5d77adc3187`)
    - requested from `Dalton` (`019d24c9-0610-7102-a84f-a58874fb38f9`)
    - both review agents timed out within the turn window, so this slice currently relies on local validation plus the explicit regression fix above
  - deferred to later Batch A slices:
    - project/entity linking still relies on names embedded in metadata, not real entity ids
    - bounded snapshot generation still does not project this richer memory into a stable session-start snapshot

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
