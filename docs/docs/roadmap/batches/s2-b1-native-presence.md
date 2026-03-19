---
title: S2-B1 Native Presence
---

# S2-B1: Native Presence

## Intent

Give Seraph a native-feeling local presence so it no longer depends on a browser tab and manual startup habits.

## Capabilities in scope

- Tauri desktop shell or equivalent native wrapper
- tray presence
- system notifications
- startup and background lifecycle handling
- packaging that feels closer to an app than a dev environment

## Non-goals

- full cross-platform parity on day one
- complete replacement of the Python backend
- deep mobile support

## Required architectural changes

- define the desktop shell boundary and process model
- unify lifecycle management for backend, daemon, and UI
- add notification and tray interfaces
- harden local packaging and startup paths

## Likely files/systems touched

- desktop packaging layer
- manage/run lifecycle assumptions
- notification and status surfaces
- setup and docs for installation

## Acceptance criteria

- Seraph can be launched and kept present as a local app
- notifications work without relying on an open browser tab
- local lifecycle is simpler than the current Docker-first path
- desktop presence does not break existing web development flow

## Dependencies on earlier batches

- depends on Season 1 making the runtime worth packaging

## Open risks

- packaging complexity can slow momentum
- desktop shell work can distract from core product behavior if not tightly scoped
- process orchestration may become brittle without good observability
