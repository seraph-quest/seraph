---
title: 15. Reference Systems Refresh 2026-04
---

# 15. Reference Systems Refresh 2026-04

## Purpose

Refresh the benchmark picture against Hermes, OpenClaw, and IronClaw using current primary-source materials, then identify what changed enough to justify roadmap corrections.

This report is a snapshot read as of April 5, 2026.

This is a refresh report, not the canonical benchmark. The canonical comparison remains:

- `docs/research/10-competitive-benchmark.md`

## Snapshot

The reference systems all moved materially in late March and early April 2026.

- Hermes is no longer only a bounded-memory terminal agent. Its official repo and docs now show a larger platform shape built around skills, scheduled automations, background work, browser control, messaging reach, and a pluginized external memory-provider layer.
- OpenClaw still presents the broadest ecosystem surface in the reviewed official materials, with more task-flow substrate, plugin-owned boundaries, and runtime/provider control depth on top of an already much larger operator and channel surface than Seraph.
- IronClaw is no longer only a security-themed fork in the benchmark picture. Its official site and parity matrix show a credible competing runtime with dashboard, routines, skills, channels, memory CLI, and a much stronger security posture than the original OpenClaw baseline.

The net result is:

- Seraph still has a real moat in guardian-shaped memory, intervention policy, and policy-time use of memory.
- Seraph should stop assuming that bounded-memory plus proactive scaffolding alone is enough to stay clearly ahead of Hermes on the memory axis.
- The roadmap should explicitly prioritize provider-neutral capability contracts, authenticated source adapters, and memory-provider extensibility instead of drifting into bespoke source-specific product pipelines.

## Hermes Refresh

### What changed

Official sources now describe Hermes as a broader agent platform, not only a CLI with bounded memory.

- the official homepage says Hermes combines persistent memory, automated skill creation, multi-platform gateway, scheduled automations, parallel sub-agents, and full browser control, with 40+ built-in skills and five chat platforms
- the GitHub repo describes Hermes as “the only agent with a built-in learning loop”
- the memory docs still show the bounded built-in `MEMORY.md` plus `USER.md` design, but now explicitly pair that with seven external memory provider plugins
- the memory-provider docs describe additive external providers for Honcho, OpenViking, Mem0, Hindsight, Holographic, RetainDB, and ByteRover, with provider-specific tools, automatic sync, and provider context injection

### Why it matters for Seraph

The structural pressure from Hermes is no longer “copy bounded markdown memory.”

The real pressure is:

- memory as a stable prompt-friendly core plus additive provider ecosystem
- learning loops that create reusable skills from experience
- broad automation and cross-channel reach tied to one agent runtime

That means Seraph should not react by adding one more internal memory subsystem. It should react by:

- preserving its guardian-first canonical memory/state model
- adding a provider/plugin adapter layer for external memory systems where useful
- letting Seraph compose routines from atomic capabilities instead of building one-off provider-specific product features

## OpenClaw Refresh

### What changed

OpenClaw remains the reference system with the broadest official runtime and product breadth.

- the repo README/docs surface still shows an enormous gateway-centered system: multi-channel inbox, gateway WebSocket control plane, control UI, browser control, cron, canvas host, and many messaging transports
- the docs show plugin channels, plugin bundles, OpenProse workflow composition, and memory exposed through an active memory plugin model rather than a single fixed backend

### Why it matters for Seraph

The biggest OpenClaw pressure is still not memory quality by itself. It is system breadth:

- operator control plane depth
- channel/reach breadth
- task-flow and orchestration primitives
- plugin-owned runtime seams

This keeps validating Seraph’s need to improve workflow-operating density, richer operator debugging, and broader adapter-backed execution and source access.

## IronClaw Refresh

### What changed

IronClaw is now a more serious benchmark input than the earlier “security-first OpenClaw reimplementation” label implied.

- the official site now frames IronClaw as a secure OpenClaw alternative with encrypted enclaves on NEAR AI Cloud, encrypted vault, Wasm-per-tool isolation, leak detection, endpoint allowlisting, and Rust implementation
- the repo and public materials show that IronClaw is not only a vault wrapper; it exposes a real operator/runtime surface with jobs, routines, skills, channels, memory, hooks, and diagnostics, alongside ongoing parity work against OpenClaw

### Why it matters for Seraph

IronClaw should no longer be treated as only “OpenClaw but safer.”

It is now a live competitive pressure on:

- execution hardening
- authenticated secret handling
- isolation boundaries
- routines/jobs/operator surfaces

Seraph still has a stronger guardian-specific product shape than IronClaw, but IronClaw’s security posture is now strong enough that Seraph should treat execution-boundary hardening as one of the highest-pressure gaps, not a later cleanup item.

## Seraph Current Read

Seraph on `develop` now ships:

- structured guardian memory, world-model synthesis, and policy-time memory use
- explicit intervention policy, learning-conditioned delivery behavior, and broader guardian behavioral evals
- reusable workflows, workflow runs, checkpoint-aware branch/resume control, and branch-family supervision
- a real guardian cockpit, grouped activity ledger, active triage, evidence shortcuts, and keyboard-first control
- stronger browser/native continuity and production reach hardening than the older Seraph baseline

But the reference refresh changes how the remaining gaps should be read:

- Seraph’s guardian/memory moat is still real, but Hermes now puts real pressure on memory-provider extensibility and learn-by-doing skill growth
- Seraph’s workflow/operator/control-plane surfaces are stronger than before, but OpenClaw still sets the bar for breadth and runtime operating surface
- Seraph’s hardening story improved, but IronClaw now raises the competitive bar for architectural secret isolation and tool containment

## Roadmap Corrections

### 1. Add provider-neutral capability contracts as a top-level strategic priority

Seraph should not answer every new external source or work-review use case by shipping one bespoke pipeline.

The platform should provide:

- atomic, provider-neutral capabilities
- thin adapters/connectors for authenticated sources
- composition owned by Seraph

This is now roadmap-level, not just implementation taste.

### 2. Add memory-provider extensibility as the next memory follow-through

Seraph’s memory core should stay guardian-first and canonical, but the repo should now explicitly plan for:

- additive memory providers
- provider-backed retrieval or user-model augmentation
- safe mapping between canonical guardian memory and provider-specific storage/search

Hermes has made this a real competitive surface.

### 3. Keep execution hardening near the top of the queue

IronClaw’s current posture makes it harder to treat execution isolation as a later refinement.

The next hardening wave should continue to narrow:

- privileged execution seams
- connector credential handling
- authenticated external access boundaries
- tool/runtime isolation assumptions

### 4. Keep workflow/operator density in the top tier

OpenClaw still wins on operating-surface breadth. Seraph should continue to deepen:

- workflow debugging density
- step-level control
- richer artifact and execution history
- denser operator control over cross-surface execution and recovery

## Sources

### Hermes

- [Hermes homepage](https://hermes-agent.org/)
- [Hermes Agent GitHub repo](https://github.com/NousResearch/hermes-agent)
- [Hermes persistent memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)
- [Hermes memory providers docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers/)

### OpenClaw

- [OpenClaw GitHub repo](https://github.com/openclaw/openclaw)
- [OpenClaw memory docs](https://docs.openclaw.ai/concepts/memory)
- [OpenClaw control UI docs](https://docs.openclaw.ai/web/control-ui)
- [OpenClaw architecture docs](https://docs.openclaw.ai/concepts/architecture)
- [OpenClaw plugins](https://docs.openclaw.ai/plugins)
- [OpenClaw memory CLI](https://docs.openclaw.ai/cli/memory)
- [OpenClaw OpenProse](https://docs.openclaw.ai/prose)

### IronClaw

- [IronClaw official site](https://www.ironclaw.com/)
- [IronClaw GitHub repo](https://github.com/nearai/ironclaw)
- [IronClaw feature parity matrix](https://github.com/nearai/ironclaw/blob/staging/FEATURE_PARITY.md)
