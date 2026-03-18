# Workstream 06: Embodied Interface

## Status On `develop`

- [ ] Workstream 06 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [07. Embodied Interface](/research/embodied-interface)

## Shipped On `develop`

- [x] browser guardian cockpit shell with dense multi-pane layout, fixed composer, session rail, guardian-state panel, intervention feedback, audit surface, live trace, pending approvals, recent outputs, and an operations inspector
- [x] persisted cockpit workspace presets for `default`, `focus`, and `review`, with inspector visibility persistence and keyboard switching
- [x] cockpit-native bridge cues through live desktop status, pending native-notification state, and browser-side native-presence controls in the operator surfaces
- [x] cockpit-first default browser mode with explicit fallback to the legacy village shell
- [x] retro village UI with Phaser-based world rendering
- [x] animated avatar states with visible casting behavior during tool use
- [x] RPG-style dialog presentation for chat
- [x] quest and settings surfaces in the current UI
- [x] standalone village editor support

## Working On Now

- [x] this workstream is back near the front of the repo-wide horizon through cockpit-density and operator-control work
- [x] this workstream shipped `cockpit-linked-evidence-panels-v2` and `saved-layouts-and-keyboard-control-v1`
- [x] this workstream partnered on `native-desktop-shell-v1`
- [x] this workstream partnered on `cross-surface-continuity-and-notification-controls`

## Still To Do On `develop`

- [ ] richer dedicated workflow-state widgets and broader keyboard/operator control inside the cockpit beyond the first saved-layout layer
- [ ] richer ambient indicators and any surviving embodiment strictly subordinate to the cockpit
- [ ] stronger mobile and cross-surface UX coherence

## Non-Goals

- cosmetic polish detached from guardian value
- treating the current village shell as the long-term primary interface
- game aesthetics without meaningful life-state reflection

## Acceptance Checklist

- [x] the interface feels intentionally different from a generic chatbot shell
- [x] tool use and agent activity are visible in the world
- [x] the primary workflow surface is now a guardian cockpit instead of defaulting to the village
- [x] the cockpit now has linked evidence, artifact, and approval density beyond the first shell
- [x] cockpit artifacts can now round-trip back into the command bar for the next operator step
- [x] the cockpit now supports persisted workspace presets and keyboard switching for core navigation
- [ ] the cockpit still needs stronger workflow control density and more flexible workspace ergonomics
- [ ] the environment reflects the human’s life state and Seraph’s guidance with much higher fidelity
