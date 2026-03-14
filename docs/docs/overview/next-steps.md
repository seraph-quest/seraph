---
sidebar_position: 4
---

# Seraph Next Steps

This page is the short-horizon companion to the [long-term roadmap](./roadmap). It should summarize what we are actively optimizing for now, not compete with the master plan.

## Current Focus

Seraph is now in **Season 1: Trust + Capability**.

That means the next execution wave is focused on making the guardian thesis more credible by improving:

- trust boundaries
- execution breadth
- runtime reliability

This is the right priority because Seraph's biggest moat already exists at the product level, while its biggest gaps are operational.

## Current Batches

### 1. Trust Boundaries

- [S1-B1 Trust Boundaries](../roadmap/batches/s1-b1-trust-boundaries)
- shipped foundations now include native tool policy modes, MCP access policy modes, approval gates, structured audit, secret egress redaction, vault operation audit, and session-scoped secret refs for downstream tool use
- current open work is narrowing the remaining raw-secret escape hatches, plus clearer separation between planning and privileged execution

### 2. Execution Plane

- [S1-B2 Execution Plane](../roadmap/batches/s1-b2-execution-plane)
- real shell/process execution, stronger browser automation, workflow engine direction

### 3. Runtime Reliability

- [S1-B3 Runtime Reliability](../roadmap/batches/s1-b3-runtime-reliability)
- started with degraded-mode hardening for offline token counting in the context window path, shared provider-agnostic LLM runtime settings, and audit visibility into primary-vs-fallback completion behavior
- still open: broader model routing, stronger fallback coverage, deeper local-model paths, observability, and evaluation harness

## What Comes After

If Season 1 lands well, the next major execution arc is:

- [Season 2: Reach + Presence](../roadmap/seasons/season-2-reach-presence)

That season moves Seraph from a strong local product into something users can actually keep with them throughout the day via native presence, channel reach, and better ambient delivery.

## Guardrails

For the next phase of work, avoid prioritizing:

- cosmetic UX upgrades ahead of trust boundaries
- new channels before the execution plane is safer
- broad ecosystem work before the runtime is more reliable

Those ideas still matter. They just land better after the current season closes its credibility gap.
