# Workstream 04: Presence And Reach

## Status On `develop`

- [ ] Workstream 04 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [06. Presence And Reach](/research/presence-and-reach)

## Shipped On `develop`

- [x] browser-based guardian cockpit as the only supported browser shell
- [x] WebSocket conversation path
- [x] native macOS observer daemon for screen and OCR ingest
- [x] observer refresh pipeline across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle delivery inside the current product
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, desktop-notification fallback when browser delivery is unavailable, browser-side controls for pending native notifications, a first learning-driven native-channel preference layer, one continuity snapshot that exposes daemon state, deferred bundle items, pending native notifications, and recent interventions together, plus action-card continuation payloads and an actionable cockpit desktop-shell card with dismiss/follow-up/continue controls
- [x] cross-surface continuity now also exposes explicit open-thread and continue flows across native notifications, queued interventions, and recent interventions
- [x] route-health snapshots now expose whether browser websocket and native delivery are actually reachable at runtime, including ready, fallback, and unavailable states with operator-readable repair hints instead of only static route bindings
- [x] queued-bundle native delivery now preserves same-thread resume when every deferred item belongs to the same session, and the shared continuity snapshot now carries one continuation contract across notifications, queued insights, and recent interventions
- [x] the shared continuity snapshot now also synthesizes continuity health, grouped follow-through threads, and explicit recovery actions, and the cockpit desktop shell now uses that richer contract instead of reconstructing cross-surface recovery from raw notifications, queued items, and route rows
- [x] the shared continuity snapshot now also carries imported capability-family attention and typed source-adapter degradation, so browser/native route health is no longer the only reach surface visible in observer continuity or cockpit recovery flows
- [x] broader reach continuity now also propagates into the operator timeline and Activity Ledger, so typed source-adapter and imported-reach recovery is not stranded in the observer endpoint or cockpit-only surfaces
- [x] observer continuity now also carries explicit presence-surface inventory across channel adapters, messaging connectors, node adapters, and observer definitions, with ready versus attention summaries, follow-up prompts, repair hints, and the same presence recovery surfaced into cockpit triage, the desktop shell, the threaded operator timeline, and the Activity Ledger
- [x] observer continuity now also carries inventory-backed browser-provider and node-adapter reach surfaces with selected-versus-fallback state, network or daemon prerequisites, and the same repair or follow-up recovery contract propagated into cockpit triage, the desktop shell, the threaded operator timeline, and the Activity Ledger
- [x] cross-surface continuity now also lands in an explicit operator continuity graph that ties notification and deferred-guardian follow-through back to session, workflow, approval, artifact, and intervention state instead of leaving the thread graph implicit across APIs
- [x] backend reach/integration proof now runs through isolated per-file backend shard execution instead of one shared shard-wide pytest process, reducing async teardown contamination in CI

## Working On Now

- [x] this workstream shipped `native-desktop-shell-v1`
- [x] this workstream shipped `cross-surface-continuity-and-notification-controls`
- [x] this workstream now ships `cross-surface-continuity-v2`
- [x] this workstream now ships `native-desktop-shell-v2`
- [x] this workstream now ships `native-channel-expansion-v1`
- [x] this workstream now ships `native-channel-expansion-v2`
- [x] this workstream now ships `production-reach-hardening-v1`
- [x] this workstream now also ships `cross-surface-recovery-summary-v1`
- [x] this workstream now also ships `broader-channel-adapter-surface-v1`
- [x] this workstream now also ships `reach-evals-and-integration-hardening-v2`
- [x] this workstream now also ships `cross-surface-presence-contracts-v1`
- [x] this workstream now also ships `broader-reach-inventory-continuity-v2`

## Still To Do On `develop`

- [ ] richer interruption channels outside the browser/native desktop shell, imported capability reach, and typed source-adapter continuity layer
- [ ] broader external communication channels
- [ ] better cross-surface continuity between ambient observation and deliberate interaction beyond the new synthesized continuity summary, imported reach/source-adapter recovery, thread groups, recovery actions, action-card continuation model, and desktop-shell follow-through controls

## Non-Goals

- channel sprawl before timing quality is strong
- ambient delivery without stronger trust and interruption controls

## Acceptance Checklist

- [x] Seraph can observe and update state outside the immediate chat loop
- [x] Seraph can proactively surface output in the current product
- [x] Seraph has at least one real non-browser presence path outside the browser tab
- [x] Seraph now has a coherent first desktop presence surface rather than only a browser app plus hidden daemon fallback
- [x] browser and native pending-notification state now have a first shared control surface
- [x] browser and native continuity now share one operator-readable snapshot for daemon, notification, queued-bundle, and recent intervention state
- [x] browser and native continuity now also share one explicit continuation contract plus runtime route-health state instead of reconstructing thread or fallback semantics per surface
- [x] the cockpit now exposes a first actionable desktop shell surface for pending alerts, queued items, and recent interventions
- [x] native continuation payloads can now resume work back into the cockpit instead of only showing a passive notification

## Batch I Branch Review Log

### Slice: production reach hardening v1

- status: complete on `feat/production-reach-hardening-batch-i-v1`, intended for the aggregate Batch I PR for `#234`
- root cause addressed:
  - channel routing only surfaced configured bindings and active adapter ownership, not whether websocket or native delivery were actually reachable at runtime, so operators and delivery audit could not distinguish ready, fallback, and unavailable states
  - native queued-bundle delivery always reopened ambiently even when every deferred item belonged to the same session, so bundle delivery could break cross-surface thread continuity at the moment the queue drained
  - the continuity snapshot derived queued bundle thread metadata only from recent intervention lookups and could drop the still-correct queued `session_id` when an intervention fell outside the recent window
- delivered in this slice:
  - runtime route-health payloads for channel routing and observer continuity, including per-route summaries, selected transport, fallback/unavailable state, and repair hints
  - runtime-aware delivery selection that uses currently reachable transports rather than static configured order alone
  - same-thread native bundle continuation when queued items share one session, plus explicit continuation metadata for queued insights and recent interventions
  - cockpit/settings reach visibility sourced from the same continuity snapshot contract
- validation:
  - `python3 -m py_compile backend/src/extensions/channel_routing.py backend/src/api/extensions.py backend/src/observer/delivery.py backend/src/api/observer.py backend/tests/test_delivery.py backend/tests/test_observer_api.py backend/tests/test_extensions_api.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_delivery.py -k "falls_back_to_native_when_browser_runtime_is_unavailable or preserves_shared_thread_continuity or deliver_queued_bundle_formats_correctly or no_active_channel_adapters_retains_queue" -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_observer_api.py::TestObserverAPI::test_observer_continuity_snapshot tests/test_extensions_api.py::test_channel_routing_defaults_surface_active_adapters -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_delivery.py -k "preserves_shared_thread_continuity or partitions_mixed_sessions_into_separate_notifications" tests/test_observer_api.py::TestObserverAPI::test_observer_continuity_recovers_queued_thread_from_intervention_outside_recent_window -q`
  - `cd backend && .venv/bin/python -c "import asyncio, json; from src.evals.harness import run_runtime_evals; summary = asyncio.run(run_runtime_evals(['cross_surface_continuity_behavior'])); print(json.dumps({'failed': summary.failed, 'details': summary.results[0].details}, indent=2))"`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/settings/DaemonStatus.test.tsx`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
  - `cd frontend && npm run build`
- review follow-up captured during validation:
  - the new runtime-health path now reads `ws_manager.active_count`, so delivery tests that only mocked broadcast results needed explicit active-count fixtures to model a real connected browser runtime
  - the synthetic “no active adapters” bundle-failure contract now correctly reports `inactive+inactive` because route health records the actual unavailable transports instead of collapsing everything to the old websocket-disabled code
  - GitHub CI then exposed the same contract drift inside the eval harness: several reach and delivery scenarios were still using bare websocket or daemon mocks with non-scalar state, so runtime reach inspection could read `MagicMock` values as transport health instead of explicit connected/disconnected state
  - that follow-up was fixed in the Batch I branch by normalizing non-scalar websocket and daemon runtime inputs at the `channel_routing` helper boundary and by pinning explicit `active_count` fixtures in eval scenarios that are meant to model a live browser websocket path
  - reviewer: `Einstein` (`019d39cb-8f3b-7e83-8874-9659016bf404`)
  - reviewer findings fixed on the same branch:
    - mixed-session native bundle delivery was still collapsing unrelated sessions into one ambient notification, so the native bundle path now partitions queued items by session before enqueuing notifications and preserves thread-specific continuation for each session-owned group
    - queued insights without a stored `session_id` could still lose thread recovery once the source intervention fell outside the latest-8 recent window, so the continuity snapshot now performs a direct intervention fallback lookup for missing thread context instead of trusting recency alone
    - the cockpit desktop shell was truncating route-health visibility to the first two routes, so `bundle_delivery` and later routes are now rendered instead of being silently hidden
  - full `uv run pytest tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios -q` is still failing in this environment for unrelated authenticated-provider scenarios outside Batch I; the Batch I proof therefore uses the scenario-specific `cross_surface_continuity_behavior` harness run above instead of claiming an unrelated full-harness green result
  - a subagent review was requested against the uncommitted Batch I reach-hardening diff for bugs, regressions, and hallucinated assumptions; if that review does not return before PR preparation, this branch record should rely on the direct review findings plus the targeted validation above instead of claiming an unreturned clean review

## Batch AP Branch Review Log

### Slice: cross-surface presence contracts v1

- status: complete on `feat/production-reach-cross-surface-batch-ap-v1`, intended for the aggregate Batch AP PR for `#346`
- root cause addressed:
  - broader reach continuity still treated route health, imported capability attention, and typed source-adapter degradation as the only actionable non-thread surfaces, so messaging connectors, channel adapters, node adapters, and observer definitions could still disappear behind separate inventories instead of showing up in the same operator recovery loop
  - observer continuity, cockpit triage, and the desktop shell did not carry a truthful presence-surface-ready versus attention summary, so follow-up-capable surfaces could remain invisible while only repair-oriented reach issues surfaced
  - the operator timeline and Activity Ledger were still missing the presence-surface counts and recovery kinds needed to keep the widened reach contract visible outside the observer endpoint and cockpit shell
- delivered in this slice:
  - explicit presence-surface inventory in observer continuity across channel adapters, messaging connectors, node adapters, and observer definitions, including ready versus attention summaries, repair hints, and follow-up prompts
  - cockpit desktop-shell and triage visibility for presence repair plus follow-up flows, including a new top-presence summary line and direct follow-up drafting for ready surfaces
  - threaded operator timeline and Activity Ledger continuity metadata for presence-surface counts and presence recovery actions
  - deterministic backend and frontend coverage for the widened continuity contract, plus the pre-batch backend shard fix for the `cross_surface_continuity_behavior` eval-harness routing seam
- validation:
  - `python3 -m py_compile backend/scripts/run_backend_test_shard.py backend/src/api/activity.py backend/src/api/observer.py backend/src/api/operator.py backend/src/evals/harness.py backend/tests/test_activity_api.py backend/tests/test_eval_harness.py backend/tests/test_observer_api.py backend/tests/test_operator_api.py backend/tests/test_run_backend_test_shard.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_run_backend_test_shard.py -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_observer_api.py -q -k "surfaces_imported_reach_and_source_adapter_attention or partial_namespace_items"`
  - `cd backend && .venv/bin/python -m pytest tests/test_operator_api.py tests/test_activity_api.py -q -k "surfaces_observer_recovery_actions"`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "test_main_lists_available_scenarios"`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/seraphPresence.test.ts src/components/cockpit/CockpitView.test.tsx -t "surfaces active triage for approvals, workflows, queued guardian items, and reach failures|deriveSeraphPresenceState"`
  - `cd frontend && npm run build`
  - `cd docs && npm run build`
- review follow-up captured during validation:
  - the first triage proof assumed imported-reach follow-through would still land in the top visible rows after the new presence surfaces were added, but that ordering guarantee was no longer truthful once the widened continuity contract legitimately displaced lower-priority rows
  - the first desktop-shell pass truncated recovery rows early enough that the new `presence_follow_up` action could be produced by the backend but never rendered, so the recovery list now exposes enough rows for the new ready-surface follow-up contract to remain actionable
  - the observer continuity hardening from the pre-batch fix is folded into this slice because partial intervention rows were still failing response validation on missing legacy string fields, and that would have made the new broader continuity contract brittle under older stored payloads

## Batch AZ Branch Review Log

### Slice: broader reach inventory continuity v2

- status: complete on `feat/reach-cross-surface-batch-az-v1`, intended for the aggregate Batch AZ PR for `#364`
- root cause addressed:
  - broader reach continuity still left packaged browser-provider reach stranded in provider inventory instead of surfacing it through the same observer continuity and recovery contract as other reach surfaces
  - node adapters only showed up through generic contribution rows, so selected-versus-fallback reach truth, daemon prerequisites, and guarded follow-up posture were missing from the continuity payload
  - recovery actions only treated messaging and channel surfaces as follow-up-capable, so the widened reach inventory could still stop short of cockpit, timeline, and ledger follow-through control even when the observer payload carried the right reach state
- delivered in this slice:
  - inventory-backed browser-provider presence surfaces with selected-versus-fallback state, provider kind, execution mode, and network or daemon prerequisite truth
  - inventory-backed node-adapter presence surfaces with adapter kind, runtime-state follow-up eligibility, and daemon or network prerequisite truth
  - the same presence-follow-up recovery contract widened to browser-provider and node-adapter surfaces, so observer continuity can drive cockpit triage, the desktop shell, the threaded operator timeline, and the Activity Ledger from the same richer reach inventory
  - deterministic observer and eval-harness proof for the widened reach inventory contract
- validation:
  - `python3 -m py_compile backend/src/api/observer.py backend/src/evals/harness.py backend/tests/test_observer_api.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_observer_api.py -q -k "presence_surface_payload or continuity_surfaces_imported_reach_and_source_adapter_attention"`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "cross_surface_continuity_behavior or test_main_lists_available_scenarios"`
  - `cd docs && npm run build -- --out-dir /tmp/seraph-docs-build-az`
  - `git diff --check`
- review follow-up captured during validation:
  - verified subagent review found that removing browser providers and node adapters from the generic presence path made planned, overridden, and invalid packaged reach disappear whenever inventory rows were absent or sanitized, so observer continuity now preserves those generic fallback attention surfaces and lets richer inventory rows overwrite them only when they exist
  - verified subagent review also found that the first browser-provider follow-up proof asserted an impossible packaged `ready` state, so the continuity contract now treats selected `staged_local_fallback` providers as the truthful guarded follow-up path and pins that fallback-aware behavior in tests and the eval harness
  - the first inventory-backed observer pass also imported `default_manifest_roots_for_workspace` and `settings` from the wrong modules and referenced `package_label_by_id` before constructing it locally, so those direct validation bugs were fixed on the same branch before the review follow-up landed

## Batch AZ Branch Review Log

### Slice: broader reach inventory continuity v2

- status: complete on `feat/reach-cross-surface-batch-az-v1`, intended for the aggregate Batch AZ PR for `#364`
- root cause addressed:
  - broader reach continuity still left packaged browser providers out of the explicit presence-surface inventory, so selected browser reach and degraded remote-browser fallback could remain invisible in the same recovery loop that already carried messaging and adapter continuity
  - node adapters were still represented only through the generic presence surface contract, so companion or device reach could not expose adapter-specific follow-up or daemon/network prerequisite truth
  - the reach eval proof still only pinned the earlier messaging/adapter presence contract, so browser-provider and node-adapter continuity could drift without a deterministic observer or eval-harness seam catching it
- delivered in this slice:
  - inventory-backed browser-provider presence surfaces in observer continuity, including selected versus fallback state, execution mode, provider kind, and network/daemon prerequisite truth
  - inventory-backed node-adapter presence surfaces in observer continuity, including adapter kind, staged-link readiness, and network/daemon prerequisite truth
  - the same broader reach follow-up and repair contract now extends through browser-provider and node-adapter surfaces instead of stopping at generic messaging or adapter inventory
  - deterministic backend proof for the widened observer continuity payload and the cross-surface continuity eval details that consume it
- validation:
  - `python3 -m py_compile backend/src/api/observer.py backend/src/evals/harness.py backend/tests/test_observer_api.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_observer_api.py -q -k "presence_surface_payload or continuity_surfaces_imported_reach_and_source_adapter_attention"`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "cross_surface_continuity_behavior or test_main_lists_available_scenarios"`
  - `cd docs && npm run build -- --no-minify`
  - `git diff --check`
- review follow-up captured during validation:
  - the first inventory-backed observer pass referenced a package-label lookup that was never built inside `_observer_presence_surface_payload()`, so browser-provider and node-adapter continuity would have failed before returning any payload; the branch now builds that lookup directly from `list_extensions()` before constructing inventory-backed surfaces
  - a subagent review was requested twice for this Batch AZ diff, but both review passes timed out without returning findings before PR preparation; this branch record therefore relies on the direct validation findings above instead of claiming an unreturned clean review
