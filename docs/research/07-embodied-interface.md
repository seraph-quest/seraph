# 07. Embodied Interface

## Goal

Seraph should feel like a living guardian system with legible state, not a chat box wearing a theme.

## Locked Direction

The current village and avatar shell is **not** the future primary interface direction.

It is a shipped surface on `develop`, but it is the wrong default operating surface for an information-heavy guardian product. The future primary interface is a dense guardian cockpit with a command layer, linked evidence views, and explicit intervention controls.

## Why The Village Direction Falls Short

- it makes the least information-dense surface the largest one
- it hides traces, evidence, approvals, and active system state behind overlays
- it is weak for comparing multiple live threads, artifacts, memories, and queues at once
- it makes keyboard-first operator control feel secondary

The village can be emotionally distinctive, but distinctiveness is not enough. The primary interface has to help the user steer and audit the system quickly.

## Primary Interface Shape

### Top rail

A persistent guardian status rail showing:

- current user state and attention budget
- active model/runtime and policy mode
- queue size, urgent alerts, and degraded-state warnings
- tool activity and intervention mode

### Left rail

Navigation and triage:

- sessions
- today / timeline
- goals and projects
- proactive queue
- alerts and pending approvals
- saved workspaces

### Center canvas

A linked widget grid that can show:

- current plan
- observer signals
- guardian-state summary
- memory evidence
- artifacts and notes
- files, web findings, and workflow state

### Right rail

Copilot and trace:

- conversation
- current task/workflow trace
- tool feed
- citations and rationale
- approval and interrupt controls

### Bottom composer

A fixed command bar for chat, slash commands, structured actions, and quick redirection.

## Interface Principles

- keyboard-first by default
- dense but legible
- provenance visible near every important claim
- urgent interrupts clearly separated from queued suggestions
- saved layouts and linked widgets instead of one giant scene
- human override and mixed-initiative control must stay explicit

## Embodiment After The Pivot

If any embodiment remains, it should be subordinate to the cockpit:

- ambient status and mood cues
- optional motivational reflection
- lightweight ritual or home-state affordances

It should not gate core workflows or consume the main screen real estate.

## Reference Systems

The strongest verified interface references for this direction are:

- [Godel Terminal docs](https://docs.godelterminal.com/)
- [Hermes CLI docs](https://hermes-agent.nousresearch.com/docs/user-guide/cli/)
- [OpenBB Workspace docs](https://docs.openbb.co/workspace)
- [Quantower DOM Surface docs](https://help.quantower.com/quantower/analytics-panels/dom-surface)
- [SpreadFighter](https://spreadfighter.com/) and [Scalper.Ai](https://spreadfighter.com/scalperai)

## Research Questions

- which widgets belong in the default cockpit versus saved secondary layouts?
- how much of the guardian trace should stay visible by default?
- what is the right split between urgent interrupts, queued suggestions, and ambient state?
- which parts of the current embodiment survive as optional secondary surfaces?
