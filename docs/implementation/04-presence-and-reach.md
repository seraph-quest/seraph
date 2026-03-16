# Workstream 04: Presence And Reach

## Status On `develop`

- [ ] Workstream 04 is only partially shipped on `develop`.

## Shipped On `develop`

- [x] browser-based village and chat surface
- [x] WebSocket conversation path
- [x] native macOS observer daemon for screen and OCR ingest
- [x] observer refresh pipeline across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle delivery inside the current product

## Working On Now

- [ ] this workstream is not the repo-wide active focus while Runtime Reliability is still being hardened

## Still To Do On `develop`

- [ ] native-feeling desktop shell beyond the current browser-plus-daemon split
- [ ] notifications and interruption channels outside the current in-product delivery surface
- [ ] broader external communication channels
- [ ] better cross-surface continuity between ambient observation and deliberate interaction

## Non-Goals

- channel sprawl before timing quality is strong
- ambient delivery without stronger trust and interruption controls

## Acceptance Checklist

- [x] Seraph can observe and update state outside the immediate chat loop
- [x] Seraph can proactively surface output in the current product
- [ ] Seraph feels present outside a browser tab
