# Workstream 06: Embodied Interface

## Status On `develop`

- [ ] Workstream 06 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [07. Embodied Interface](/research/embodied-interface)

## Shipped On `develop`

- [x] browser guardian cockpit shell with dense multi-pane operator surfaces, fixed composer, sessions, goals, recent outputs, pending approvals, latest response, guardian state, workflow runs, interventions, audit surface, live trace, desktop continuity, operator controls, and an operations inspector
- [x] dedicated cockpit workflow-run views with richer workflow inspector actions and artifact-lineage links
- [x] cockpit artifact and workflow inspectors can now draft compatible follow-on workflows directly from selected artifact paths
- [x] cockpit capability discovery now exposes tools, skills, workflows, MCP servers, starter packs, and blocked-state reasons in one operator-readable surface
- [x] first-class draggable and resizable cockpit panes with persisted positions and z-order
- [x] cockpit panes now snap to a shared 16px grid during drag, resize, and packed layout reset
- [x] persisted cockpit workspace presets for `default`, `focus`, and `review`, now implemented as function-based packed pane layouts with inspector visibility persistence, keyboard switching, and per-layout save/reset composition
- [x] cockpit-native bridge cues through live desktop status, pending native-notification state, deferred bundle visibility, recent intervention continuity, and browser-side native-presence controls in the operator surfaces
- [x] cockpit-native operator surface for workflow availability, skills, MCP servers, and live policy state with direct reload controls
- [x] cockpit-threaded operator timeline that links workflow runs, approvals, notifications, queued interventions, recent guardian outputs, and surfaced failures back into one live browser control surface
- [x] cockpit capability preflight and autorepair flows for runbooks, starter packs, and workflows before the operator drafts or reruns them
- [x] cockpit session continuity now restores the active thread on reload, preserves explicit fresh-thread semantics, and marks background thread activity in the session list
- [x] cockpit approvals, workflow runs, native notifications, queued interventions, and recent interventions now expose explicit continue/open-thread controls instead of forcing continuity guesswork
- [x] cockpit is now the active browser shell on load rather than merely the default mode
- [x] quest and settings overlays now use cockpit modal styling rather than legacy overlay frames
- [x] village/editor code is now treated as retired legacy code rather than a supported surface

## Working On Now

- [x] this workstream is back near the front of the repo-wide horizon through cockpit-density and operator-control work
- [x] this workstream shipped `cockpit-linked-evidence-panels-v2` and `saved-layouts-and-keyboard-control-v1`
- [x] this workstream partnered on `native-desktop-shell-v1`
- [x] this workstream partnered on `cross-surface-continuity-and-notification-controls`
- [x] this workstream now ships `cockpit-workflow-views-v1`
- [x] this workstream now ships `artifact-evidence-roundtrip-v2`
- [x] this workstream now ships `extension-operator-surface-v1`
- [x] this workstream now ships the denser operator-terminal layer with live operator feed, saved runbook macros, approval-aware workflow timeline actions, and a threaded operator timeline
- [x] this workstream now hands the queue forward to retiring the village/editor line so the repo matches the cockpit-only product contract

## Still To Do On `develop`

- [ ] remove dormant village/editor runtime code and any remaining legacy browser entry points so the shipped UI surface matches the documented product direction exactly
- [ ] richer capability installation, recommendation, and command-surface guidance inside the cockpit so shipped tools, skills, workflows, and blocked states become easier to bootstrap automatically, not only preflight and repair
- [ ] richer workflow history, broader keyboard/operator control, step-level replay/debug visibility, and more flexible workspace ergonomics inside the cockpit beyond the first dedicated workflow-run layer, operator timeline, pane model, direct artifact/workflow draft handoff, operator terminal, and saved-layout composition model
- [ ] richer ambient indicators and any surviving embodiment strictly subordinate to the cockpit
- [ ] stronger mobile and cross-surface UX coherence

## Non-Goals

- cosmetic polish detached from guardian value
- reviving the retired village/editor line as an active product surface
- game aesthetics without meaningful life-state reflection

## Acceptance Checklist

- [x] the interface feels intentionally different from a generic chatbot shell
- [x] tool use and agent activity are visible in the cockpit
- [x] the primary workflow surface is now a guardian cockpit and the browser no longer boots into the village shell
- [x] the cockpit now has linked evidence, artifact, and approval density beyond the first shell
- [x] cockpit artifacts can now round-trip back into the command bar for the next operator step
- [x] cockpit artifacts can now also seed compatible follow-on workflow drafts directly from the inspector
- [x] the cockpit now supports draggable/resizable panes with 16px grid snapping
- [x] the cockpit now supports persisted packed `default`, `focus`, and `review` layouts plus keyboard switching for core navigation
- [x] the cockpit now supports saving and resetting each core workspace layout rather than only switching between fixed presets
- [x] the cockpit now exposes first-class operator visibility for workflows, skills, MCP servers, and live policy state
- [x] the cockpit now exposes first-class operator visibility for tools, starter packs, blocked-state reasons, session continuity state, and preflight/autorepair outcomes
- [x] the cockpit now exposes first-class operator visibility for approval-thread recovery and cross-surface continue/open-thread actions
- [x] the cockpit now exposes a first-class threaded operator timeline instead of leaving workflow, approval, notification, intervention, and failure continuity in separate panes
- [x] settings and goals now present as cockpit-styled modal overlays instead of legacy shell overlays
- [x] the docs now treat the village/editor line as retired rather than as a fallback product branch
- [ ] the cockpit still needs richer install/recommend/repair guidance beyond the first "what can I do now?" capability surface, blocked-state explanation, starter-pack view, and preflight/autorepair layer
- [ ] the cockpit still needs broader workflow history, deeper step-level workflow control, and more flexible workspace ergonomics
- [ ] the environment reflects the human’s life state and Seraph’s guidance with much higher fidelity
