# Capability Import Wave 5

## Scope

Wave 5 covers the operator-surface, proof, and hardening slices from the
capability import program:

31. `capability-operator-surface-v1`
32. `capability-budget-and-cost-attribution-v1`
33. `capability-approval-and-policy-integration-v1`
34. `capability-benchmark-refresh-v1`
35. `capability-evals-v1`
36. `capability-cleanup-and-legacy-path-removal-v1`

This wave is the proof-and-hardening capstone for the Hermes/OpenClaw import
program. The goal is to make the imported capability families visible inside the
guardian workspace, attribute LLM spend to the runtime paths that actually
caused it, tighten policy visibility for packaged reach, refresh the benchmark
mirror, add deterministic proof for the imported surfaces, and remove the last
legacy operator seams that no longer need to exist.

## Slice Log

### 31. capability-operator-surface-v1

- status: complete
- intent:
  - make the imported capability families from Waves 2-4 visible in the
    operator terminal instead of leaving them fragmented across extension,
    browser, automation, node, and canvas APIs
  - expose those imported families in the same control surface as runbooks,
    recommendations, and lifecycle actions
- implementation:
  - extended the cockpit operator terminal to surface imported capability-family
    reach directly from `/api/extensions`, including browser providers,
    messaging connectors, toolset presets, automation triggers, node adapters,
    canvas outputs, workflow runtimes, channel adapters, and observer sources
  - added active-vs-installed family summaries so the workspace no longer
    counts planned, unloaded, or disabled packaged reach as if it were usable
  - added imported-family inspector payloads and extension-governance rows so
    permission, approval, connector-health, and package-boundary issues stay in
    the same operator surface as existing workflow and repair controls
- validation:
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`

### 32. capability-budget-and-cost-attribution-v1

- status: complete
- intent:
  - stop reporting LLM spend as one opaque aggregate by attributing it to the
    runtime paths and capability families that actually incurred it
  - make that attribution visible in the Activity Ledger and operator-facing
    accountability surfaces
- implementation:
  - enriched the activity-ledger projection with request-scoped runtime routing
    metadata so LLM calls now carry runtime path, source, task class, budget
    tier, cost tier, latency tier, and capability-family attribution
  - added summary breakdowns for `llm_cost_by_runtime_path` and
    `llm_cost_by_capability_family`, and surfaced the leading buckets in the
    Activity Ledger window
  - tightened attribution to use the latest routing event per `request_id`,
    trim bucket keys, and classify missing routing as `unattributed` instead of
    overclaiming `conversation`
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_activity_api.py -q`
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`

### 33. capability-approval-and-policy-integration-v1

- status: complete
- intent:
  - expose extension approval, permission, and connector-boundary state inside
    the operator surface so imported reach stays grounded in Seraph’s trust
    model
  - avoid inventing a second approval vocabulary for packaged capabilities
- implementation:
  - surfaced extension permission summaries, approval profiles, connector
    summaries, and contribution permission/health state directly in the
    operator terminal and manifest-inspector payloads
  - added governance queue rows for missing permissions, runtime approval,
    lifecycle approval, and degraded connector state, with direct handoff into
    Extension Studio for inspection
  - preserved the last successful activity-ledger view on same-session refresh
    failures while still clearing stale ledger state on cross-session changes
- validation:
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`

### 34. capability-benchmark-refresh-v1

- status: complete
- intent:
  - refresh the implementation-side benchmark mirror now that the capability
    import program has landed all five waves
- implementation:
  - refreshed the implementation-side benchmark mirrors in the roadmap,
    workstream summary, benchmark mirror, and status snapshot so the shipped
    state now explicitly credits the five-wave capability import program,
    imported reach surfaces, operator governance, runtime-path spend
    attribution, and the remaining post-import hardening gaps
  - tightened the benchmark language to reflect the real post-import position:
    Seraph now has shipped reach and proof across every benchmark axis, but the
    repo still does not claim broad implementation superiority without deeper
    execution hardening, denser operator/debug ergonomics, stronger guardian
    learning, and production-grade hardening for the broadened reach surface
- validation:
  - `cd docs && npm run build`

### 35. capability-evals-v1

- status: complete
- intent:
  - add deterministic proof that the imported capability families remain
    visible, attributable, and policy-bounded on `develop`
- implementation:
  - added deterministic eval scenarios for activity-ledger attribution and
    imported capability surfaces, so runtime-path spend, capability-family
    attribution, imported reach summaries, and extension-governance queues stay
    pinned on `develop`
  - hardened the observer delivery eval seams by neutralizing persisted
    guardian-learning bias and forcing the websocket adapter active inside the
    delivery scenarios, which keeps the Wave 5 proof deterministic under pytest
    instead of depending on leaked runtime state
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_eval_harness.py -q`

### 36. capability-cleanup-and-legacy-path-removal-v1

- status: complete
- intent:
  - remove transitional operator/capability seams that are no longer needed
    after the new surfaces are first-class
- implementation:
  - removed the cockpit fallback to `/api/operator/timeline`, making
    `/api/activity/ledger` the only supported source for grouped operator
    history and attributed LLM-budget visibility
  - tightened the cockpit-side summary derivation and grouped-row metadata so
    missing routing data stays explicitly `unattributed` instead of being
    overclaimed, while same-session ledger refresh failures preserve the last
    good view instead of wiping the operator surface mid-session
- validation:
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`

## Subagent Review

- Review 1: `Wegener` found five real issues in the first 31-33 pass:
  - request-scoped routing metadata used `setdefault`, so later routing events
    could not update runtime path or budget attribution
  - capability-family attribution overclaimed `conversation` when routing
    metadata was missing
  - imported capability reach counted planned or disabled contributions as
    active
  - `/api/activity/ledger` failure blanked the ledger immediately instead of
    preserving the last good same-session view
  - permission-copy pluralization rendered `boundaryies`
- Review 2: `Kepler` found four more issues in the same slice batch:
  - runtime-family attribution missed common runtime paths such as
    `agent_generate`, `onboarding_agent`, `orchestrator_agent`,
    `session_consolidation`, and `session_title_generation`
  - whitespace on runtime-path metadata could fragment spend buckets
  - LLM child rows in grouped ledger entries lost their fallback meta line when
    no policy-tier metadata was present
  - the cockpit test file still pointed one failure path at the removed
    `/api/operator/timeline` fallback instead of the real activity-ledger
    endpoint
- Follow-up:
  - all findings above were fixed in the current 31-33 batch
  - focused backend/frontend validation passed after the fixes
### Review 3

- reviewer: `Anscombe`
- scope:
  - Wave 5 batch B (`34-36`): benchmark mirror refresh, deterministic eval
    proof, and cleanup of the last legacy activity-ledger/operator seams
- findings:
  - activity-ledger capability-family fallback still defaulted to `system`
    instead of explicitly `unattributed`, which would overclaim spend routing
    for missing metadata
  - grouped `llm_call` rows replaced their base meta line when policy-tier
    metadata was present, so runtime-path attribution could disappear from
    child rows
  - the new ledger summary and grouped-child frontend assertions were brittle
    against the actual rendered badge and button structure
- resolution:
  - fallback capability-family attribution now uses `unattributed`, matching
    the backend activity projection semantics
  - grouped `llm_call` rows now append policy-tier metadata to the base row
    metadata instead of replacing it
  - the cockpit assertions now target the real badge/button structure instead
    of fragile text-shape assumptions

### Review 4

- reviewer: `Darwin`
- scope:
  - Wave 5 eval-harness stabilization after the 34-36 follow-up fixes
- findings:
  - observer delivery evals could still fail under pytest because persisted
    guardian-learning bias leaked into the decision path and flipped the
    expected deliver-vs-queue outcome
- resolution:
  - the affected observer-delivery eval scenarios now patch
    `guardian_feedback_repository.get_learning_signal()` to a neutral
    advisory signal and force the websocket adapter active, making the delivery
    contracts deterministic under pytest as well as standalone runs

### Review 5

- reviewer: `Helmholtz`
- scope:
  - full Wave 5 diff after the 34-36 follow-up fixes and final validation pass
- findings:
  - no findings
- resolution:
  - none required

## Wave Validation

- focused Activity Ledger and eval-harness suite:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_activity_api.py tests/test_eval_harness.py -q`
  - result: `16 passed`
- focused cockpit Wave 5 suite:
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`
  - result: `47 passed`
- backend full suite:
  - `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - result: `1196 passed`, `4 warnings`
- frontend full suite:
  - `cd frontend && npm test`
  - result: `157 passed`
- frontend build:
  - `cd frontend && npm run build`
  - result: passed
- docs build:
  - `cd docs && npm run build`
  - result: passed
- diff hygiene:
  - `git diff --check`
  - result: passed

## Current Validation State

- Wave 5 implementation is complete across slices `31-36`.
- The operator surface now shows imported reach, extension governance, and
  attributed runtime-path/capability-family spend.
- The benchmark/status mirrors and deterministic eval harness have been
  refreshed to match the shipped five-wave import program.
- The full branch validation matrix is green, so the branch is ready to be
  committed and published as the Wave 5 PR.
