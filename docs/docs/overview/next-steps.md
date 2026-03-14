---
sidebar_position: 4
---

# Seraph Next Steps

This page is the short-horizon companion to the [long-term roadmap](./roadmap). It should summarize what we are actively optimizing for now, not compete with the master plan.

## Status Format

- `[x]` shipped
- `[ ]` not finished yet

Use the lists below as the live view of what is done now, what is being worked on now, and what is still open.

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
- [x] native tool policy modes
- [x] MCP access policy modes
- [x] approval gates
- [x] structured audit
- [x] secret egress redaction
- [x] vault operation audit
- [x] session-scoped secret refs for downstream tool use
- [ ] narrow the remaining raw-secret escape hatches
- [ ] separate planning from privileged execution more clearly

### 2. Execution Plane

- [S1-B2 Execution Plane](../roadmap/batches/s1-b2-execution-plane)
- [x] real shell/process execution
- [x] stronger browser automation foundations
- [ ] workflow engine direction
- [ ] broader execution-plane hardening still ahead of Season 2 work

### 3. Runtime Reliability

- [S1-B3 Runtime Reliability](../roadmap/batches/s1-b3-runtime-reliability)
- [x] degraded-mode fallback for offline token counting in the context window
- [x] shared provider-agnostic LLM runtime settings
- [x] direct LiteLLM fallback path
- [x] timeout-safe audit visibility for primary-vs-fallback LLM completions
- [x] fallback-capable `smolagents` model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for core guardian/tool reliability contracts
- [x] lifecycle audit coverage for REST chat, WebSocket chat, and scheduled proactive jobs
- [ ] current work: broaden observability beyond the first chat + proactive job coverage
- [ ] broader model/provider routing beyond the first shared fallback path
- [ ] deeper local-model-capable execution paths
- [ ] richer eval coverage beyond the first core scenarios

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
