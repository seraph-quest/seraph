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
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, and desktop-notification fallback when browser delivery is unavailable

## Working On Now

- [x] this workstream shipped `native-desktop-shell-v1`
- [x] this workstream now owns `cross-surface-continuity-and-notification-controls` in the master 10-PR queue

## Still To Do On `develop`

- [ ] richer notification controls and broader interruption channels outside the current in-product delivery surface
- [ ] broader external communication channels
- [ ] better cross-surface continuity between ambient observation and deliberate interaction

## Non-Goals

- channel sprawl before timing quality is strong
- ambient delivery without stronger trust and interruption controls

## Acceptance Checklist

- [x] Seraph can observe and update state outside the immediate chat loop
- [x] Seraph can proactively surface output in the current product
- [x] Seraph has at least one real non-browser presence path outside the browser tab
- [x] Seraph now has a coherent first desktop presence surface rather than only a browser app plus hidden daemon fallback
