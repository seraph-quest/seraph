# Workstream 06: Embodied Interface

## Status On `develop`

- [ ] Workstream 06 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [07. Embodied Interface](/research/embodied-interface)

## Shipped On `develop`

- [x] browser guardian cockpit shell with dense multi-pane layout, fixed composer, session rail, guardian-state panel, intervention feedback, audit surface, and live trace
- [x] cockpit-first default browser mode with explicit fallback to the legacy village shell
- [x] retro village UI with Phaser-based world rendering
- [x] animated avatar states with visible casting behavior during tool use
- [x] RPG-style dialog presentation for chat
- [x] quest and settings surfaces in the current UI
- [x] standalone village editor support

## Working On Now

- [x] this workstream is back near the front of the repo-wide horizon through cockpit-density and operator-control work
- [x] this workstream owns `cockpit-linked-evidence-panels-v2` and `saved-layouts-and-keyboard-control-v1` in the master 10-PR queue

## Still To Do On `develop`

- [ ] richer linked widgets for evidence, artifacts, workflow state, and approvals inside the cockpit
- [ ] saved layouts, stronger keyboard workflow control, and denser operator ergonomics
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
- [ ] the cockpit has strong linked evidence, artifact, workflow, and approval density rather than only a first shell
- [ ] the environment reflects the human’s life state and Seraph’s guidance with much higher fidelity
