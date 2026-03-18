# 07. Embodied Interface

## Goal

Seraph should feel like a living guardian system with legible state, not a chat box wearing a theme.

## Locked Direction

The primary interface direction is a cockpit/terminal operator surface.

The village and avatar shell can remain as a secondary or optional presence surface, but it is not the active shell for the current product direction. The main surface should optimize for command throughput, operator visibility, auditability, and fast intervention across multiple live threads.

## Why The Village Direction Falls Short

- it makes the least information-dense surface the largest one
- it hides traces, evidence, approvals, and active system state behind overlays
- it is weak for comparing multiple live threads, artifacts, memories, and queues at once
- it makes keyboard-first operator control feel secondary

The village can be emotionally distinctive, but distinctiveness is not enough. The primary interface has to help the user steer and audit the system quickly.

## Primary Interface Shape

The near-term shape is a packed pane workspace, not a scenic dashboard. It should behave more like a dense operator desk: snap-aligned panes, a persistent command layer, and enough visible state that the user can steer the guardian without opening stacks of overlays.

### Workspace frame

A persistent guardian status rail showing:

- current user state and attention budget
- active model/runtime and policy mode
- queue size, urgent alerts, and degraded-state warnings
- tool activity and intervention mode

### Packed panes

The core workspace should support snap-aligned panes for:

- sessions and active threads
- command/composer
- workflow runs and operator controls
- approvals and intervention queues
- audit and live trace
- artifacts, notes, and files
- memory evidence
- goals, projects, and saved workspaces

These panes should stay useful in compact form, be easy to rearrange, and hold a denser working set than the village-overlay model.

### Command layer

A persistent command surface should stay available for:

- chat and redirection
- slash-style operator commands
- workflow launch and replay
- artifact round-tripping
- quick approvals and interrupt handling

## Interface Principles

- keyboard-first by default
- dense enough for continuous operator use
- legible under high information load
- provenance visible near every important claim
- urgent interrupts clearly separated from queued suggestions
- snap-aligned panes and saved layouts instead of one giant scene
- controls should favor fast steering over decorative motion
- human override and mixed-initiative control must stay explicit

## Embodiment After The Pivot

If any embodiment remains, it should be subordinate to the cockpit:

- ambient status and mood cues
- optional motivational reflection
- lightweight ritual or home-state affordances
- an occasional distinct village or presence surface when the user explicitly wants it

It should not gate core workflows or consume the main screen real estate.

## Reference Systems

The strongest verified interface references for this direction are:

- [Godel Terminal docs](https://docs.godelterminal.com/)
- [Hermes CLI docs](https://hermes-agent.nousresearch.com/docs/user-guide/cli/)
- [OpenBB Workspace docs](https://docs.openbb.co/workspace)
- [Quantower DOM Surface docs](https://help.quantower.com/quantower/analytics-panels/dom-surface)
- [SpreadFighter](https://spreadfighter.com/) and [Scalper.Ai](https://spreadfighter.com/scalperai)

## Research Questions

- which panes belong in the default operator workspace versus saved secondary layouts?
- what is the right snapping model for dense panes without creating layout thrash?
- how much of the guardian trace should stay visible by default in a packed workspace?
- what is the right split between urgent interrupts, queued suggestions, and ambient state?
- which parts of the current embodiment survive as optional secondary surfaces?
