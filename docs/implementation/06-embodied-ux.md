# Workstream 06: Embodied Interface

## Status On `develop`

- [ ] Workstream 06 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [07. Embodied Interface](/research/embodied-interface)

## Shipped On `develop`

- [x] browser guardian cockpit shell with dense multi-pane operator surfaces, fixed composer, sessions, goals, recent outputs, pending approvals, latest response, guardian state, workflow runs, interventions, audit surface, live trace, desktop continuity, operator controls, and an operations inspector
- [x] dedicated cockpit workflow-run views with richer workflow inspector actions and artifact-lineage links
- [x] cockpit artifact and workflow inspectors can now draft compatible follow-on workflows directly from selected artifact paths
- [x] first-class draggable and resizable cockpit panes with persisted positions and z-order
- [x] cockpit panes now snap to a shared 16px grid during drag, resize, and packed layout reset
- [x] persisted cockpit workspace presets for `default`, `focus`, and `review`, now implemented as function-based packed pane layouts with inspector visibility persistence and keyboard switching
- [x] cockpit-native bridge cues through live desktop status, pending native-notification state, deferred bundle visibility, recent intervention continuity, and browser-side native-presence controls in the operator surfaces
- [x] cockpit-native operator surface for workflow availability, skills, MCP servers, and live policy state with direct reload controls
- [x] cockpit is now the active browser shell on load rather than merely the default mode
- [x] retro village UI with Phaser-based world rendering
- [x] animated avatar states with visible casting behavior during tool use
- [x] RPG-style dialog presentation for chat
- [x] quest and settings overlays now use cockpit modal styling rather than legacy overlay frames
- [x] standalone village editor support

## Working On Now

- [x] this workstream is back near the front of the repo-wide horizon through cockpit-density and operator-control work
- [x] this workstream shipped `cockpit-linked-evidence-panels-v2` and `saved-layouts-and-keyboard-control-v1`
- [x] this workstream partnered on `native-desktop-shell-v1`
- [x] this workstream partnered on `cross-surface-continuity-and-notification-controls`
- [x] this workstream now ships `cockpit-workflow-views-v1`
- [x] this workstream now ships `artifact-evidence-roundtrip-v2`
- [x] this workstream now ships `extension-operator-surface-v1`

## Still To Do On `develop`

- [ ] richer workflow history, broader keyboard/operator control, and more flexible workspace ergonomics inside the cockpit beyond the first dedicated workflow-run layer, pane model, direct artifact/workflow draft handoff, and first operator surface
- [ ] richer ambient indicators and any surviving embodiment strictly subordinate to the cockpit
- [ ] stronger mobile and cross-surface UX coherence

## Non-Goals

- cosmetic polish detached from guardian value
- treating the legacy village shell as the active or long-term primary interface
- game aesthetics without meaningful life-state reflection

## Acceptance Checklist

- [x] the interface feels intentionally different from a generic chatbot shell
- [x] tool use and agent activity are visible in the world
- [x] the primary workflow surface is now a guardian cockpit and the browser no longer boots into the village shell
- [x] the cockpit now has linked evidence, artifact, and approval density beyond the first shell
- [x] cockpit artifacts can now round-trip back into the command bar for the next operator step
- [x] cockpit artifacts can now also seed compatible follow-on workflow drafts directly from the inspector
- [x] the cockpit now supports draggable/resizable panes with 16px grid snapping
- [x] the cockpit now supports persisted packed `default`, `focus`, and `review` layouts plus keyboard switching for core navigation
- [x] the cockpit now exposes first-class operator visibility for workflows, skills, MCP servers, and live policy state
- [x] settings and goals now present as cockpit-styled modal overlays instead of legacy shell overlays
- [ ] the cockpit still needs broader workflow history, denser workflow control, and more flexible workspace ergonomics
- [ ] the environment reflects the human’s life state and Seraph’s guidance with much higher fidelity
