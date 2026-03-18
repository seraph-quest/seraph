# Workstream 04: Presence And Reach

## Status On `develop`

- [ ] Workstream 04 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [06. Presence And Reach](/research/presence-and-reach)

## Shipped On `develop`

- [x] browser-based guardian cockpit with legacy village fallback
- [x] WebSocket conversation path
- [x] native macOS observer daemon for screen and OCR ingest
- [x] observer refresh pipeline across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle delivery inside the current product
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, desktop-notification fallback when browser delivery is unavailable, browser-side controls for pending native notifications, a first learning-driven native-channel preference layer, one continuity snapshot that exposes daemon state, deferred bundle items, pending native notifications, and recent interventions together, plus action-card continuation payloads and an actionable cockpit desktop-shell card with dismiss/follow-up/continue controls

## Working On Now

- [x] this workstream shipped `native-desktop-shell-v1`
- [x] this workstream shipped `cross-surface-continuity-and-notification-controls`
- [x] this workstream now ships `cross-surface-continuity-v2`
- [x] this workstream now ships `native-desktop-shell-v2`
- [x] this workstream now ships `native-channel-expansion-v1`

## Still To Do On `develop`

- [ ] richer interruption channels outside the browser/native desktop shell and continuity layer
- [ ] broader external communication channels
- [ ] better cross-surface continuity between ambient observation and deliberate interaction beyond the new continuity snapshot, action-card continuation model, and desktop-shell control card

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
- [x] the cockpit now exposes a first actionable desktop shell surface for pending alerts, queued items, and recent interventions
- [x] native continuation payloads can now resume work back into the cockpit instead of only showing a passive notification
