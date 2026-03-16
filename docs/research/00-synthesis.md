---
slug: /
---

# Seraph Research Synthesis

**Status:** canonical design synthesis for the current Seraph thesis

## Purpose

This research tree defines what Seraph is trying to become. It is not the shipped-status ledger. For shipped truth, use `docs/implementation/`.

The working model is:

- `docs/research/` answers: what should Seraph become?
- `docs/implementation/` answers: what is true on `develop` right now?

## Core Thesis

Seraph is not just an agent runtime. It is a guardian system:

1. it observes the human and their environment continuously
2. it maintains a living model of the human, their goals, and their state
3. it reasons about leverage, risk, timing, and intervention quality
4. it acts through tools, channels, and interfaces that fit the moment
5. it learns whether its interventions were helpful

The product only works if all five loops reinforce each other.

## Design Principles

- local-first where it meaningfully improves trust, speed, and availability
- explicit trust boundaries before broader autonomy
- real execution, not simulated competence
- observability before optimism
- product embodiment matters because guardian systems need legibility, not just capability

## System Loops

### Guardian loop

`observe -> model -> reason -> decide -> act -> learn`

### Runtime loop

`route -> execute -> fall back -> audit -> evaluate -> improve`

### Product loop

`ambient presence -> timely intervention -> user response -> updated trust calibration`

## Architecture Pillars

- [Guardian Thesis](./01-guardian-thesis.md)
- [Human Model And Memory](./02-human-model-and-memory.md)
- [Runtime And Reliability](./03-runtime-and-reliability.md)
- [Trust And Governance](./04-trust-and-governance.md)
- [Execution Plane](./05-execution-plane.md)
- [Presence And Reach](./06-presence-and-reach.md)
- [Embodied Interface](./07-embodied-interface.md)
- [Ecosystem And Delegation](./08-ecosystem-and-delegation.md)
- [Reference Systems And Evidence](./09-reference-systems-and-evidence.md)

## Implementation Mapping

- Workstream 01 maps to trust and governance
- Workstream 02 maps to execution plane
- Workstream 03 maps to runtime and reliability
- Workstream 04 maps to presence and reach
- Workstream 05 maps to human model and guardian intelligence
- Workstream 06 maps to embodied interface
- Workstream 07 maps to ecosystem and delegation

## What “Best Agent In The World” Means Here

For Seraph, “best” does not mean benchmark theater. It means:

- it knows the human over time
- it notices important things before the human asks
- it can act safely and effectively
- it is legible enough to trust
- it compounds usefulness instead of resetting every session

That requires a product system, not just a stronger model endpoint.
