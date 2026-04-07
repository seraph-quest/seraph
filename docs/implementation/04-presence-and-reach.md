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
