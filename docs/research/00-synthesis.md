---
slug: /
title: Seraph Research Synthesis
---

# Seraph Research Synthesis

**Status:** canonical design synthesis for the current Seraph superiority program

## Purpose

This research tree defines what Seraph is trying to become. It is not the shipped-status ledger. For shipped truth, use `docs/implementation/`.

The working model is:

- `docs/research/` answers: what should Seraph become?
- `docs/implementation/` answers: what is true on `develop` right now?

This tree now has a narrower standard than before: every major product claim should either be grounded in direct Seraph repo evidence, official reference-system docs, or primary research. If that evidence is missing, the claim should be marked `Unknown`, not rounded into confidence.

## Locked Product Direction

Seraph is being optimized for a **power-user guardian cockpit**, not a broad consumer chat assistant.

That means:

- dense, linked, evidence-backed operator surfaces matter more than charm-first presentation
- memory, intervention quality, and real execution matter more than one-shot conversation polish
- the current village/game shell is treated as a shipped legacy surface, not the future primary product direction

## Core Thesis

Seraph is not just an agent runtime. It is a guardian system:

1. it observes the human and their environment continuously
2. it maintains a living model of the human, their goals, and their state
3. it reasons about leverage, risk, timing, and intervention quality
4. it acts through tools, channels, and interfaces that fit the moment
5. it learns whether its interventions were helpful

The product only works if all five loops reinforce each other.

## Benchmark Axes

Seraph should be judged against OpenClaw, Hermes, and IronClaw on explicit axes:

- operator visibility and legibility
- longitudinal memory and human modeling
- intervention quality and timing
- safe real-world execution
- runtime reliability and eval rigor
- workflow composition and delegation
- dense interface efficiency
- presence and reach across surfaces

“Best agent in the world” here does not mean winning a generic model benchmark. It means building the strongest integrated guardian product for those axes.

## Design Principles

- local-first where it meaningfully improves trust, speed, and availability
- explicit trust boundaries before broader autonomy
- real execution, not simulated competence
- observability before optimism
- product embodiment matters because guardian systems need legibility, not just capability
- superiority claims require evidence plus an implementation consequence

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
- [Competitive Benchmark](./10-competitive-benchmark.md)
- [Superiority Program](./11-superiority-program.md)

## Implementation Mapping

- Workstream 01 maps to trust and governance
- Workstream 02 maps to execution plane
- Workstream 03 maps to runtime and reliability
- Workstream 04 maps to presence and reach
- Workstream 05 maps to human model and guardian intelligence
- Workstream 06 maps to embodied interface
- Workstream 07 maps to ecosystem and delegation

## What “Superior” Means Here

For Seraph, superiority means:

- it knows the human over time better than today’s terminal-first and gateway-first competitors
- it notices and ranks important things with better timing, not just more activity
- it can act safely and effectively through real tools and channels
- it is legible enough to trust under failure, routing changes, and proactive behavior
- it compounds usefulness instead of resetting every session

That requires a product system, not just a stronger model endpoint.
