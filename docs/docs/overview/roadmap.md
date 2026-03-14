---
sidebar_position: 2
title: Master Plan
---

# Seraph Master Plan

Seraph now uses one planning structure only:

- one **master plan** file
- one file per **workstream**

The old `sections / seasons / batches` split is no longer the live planning surface. If an old roadmap file still exists in the repo, treat it as legacy reference material, not the current plan.

## How To Read This

- `[x]` means the capability already has meaningful shipped foundations
- `[ ]` means the work is not finished yet

If you want the current truth, use this page plus the workstream files linked below.

## Workstreams

### 01. [Trust Boundaries](../plan/trust-boundaries)

- [x] meaningful foundations are shipped
- [ ] the workstream is not complete
- focus: make Seraph safer and more governable before expanding autonomy further

### 02. [Execution Plane](../plan/execution-plane)

- [x] execution foundations are shipped
- [ ] the workstream is not complete
- focus: make Seraph better at doing real work, not just reasoning about it

### 03. [Runtime Reliability](../plan/runtime-reliability)

- [x] baseline hardening is shipped
- [ ] the workstream is still active
- focus: routing, fallbacks, observability, evals, and degraded-mode behavior

### 04. [Presence And Reach](../plan/presence-and-reach)

- [x] browser and local-observer foundations exist
- [ ] native presence and external reach are still ahead
- focus: make Seraph reachable outside a browser tab

### 05. [Guardian Intelligence](../plan/guardian-intelligence)

- [x] memory and strategist foundations exist
- [ ] the deeper guardian model is still ahead
- focus: move from retrieval + heuristics toward richer human understanding and adaptation

### 06. [Embodied UX](../plan/embodied-ux)

- [x] the village UX and ambient shell are real
- [ ] the full life-OS layer is still ahead
- focus: make Seraph feel alive, legible, and motivating without becoming gimmicky

### 07. [Ecosystem And Leverage](../plan/ecosystem-and-leverage)

- [x] skills, MCP, and delegation foundations exist
- [ ] the workstream is still early
- focus: compound Seraph through reusable extensions without losing product clarity

## Order Of Execution

This is the intended order:

1. Trust Boundaries
2. Execution Plane
3. Runtime Reliability
4. Presence And Reach
5. Guardian Intelligence
6. Embodied UX
7. Ecosystem And Leverage

This is not a claim that only one workstream can move at a time. It is the order that resolves the biggest product and runtime risks first.
