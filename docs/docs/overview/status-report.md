---
title: Development Status
---

# Seraph Development Status

## Legend

- `[x]` shipped on `develop`
- `[ ]` not fully shipped on `develop`
- In-flight branch work should be tracked in open PRs, not in this file.

## Current Snapshot

- [x] Seraph is usable today as a local guardian prototype with a real browser UI, observer loop, and action layer.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability have strong shipped foundations.
- [x] The live planning surface is now one master plan plus one file per workstream.
- [ ] No workstream is complete yet.
- [ ] Seraph is not yet a finished product or a production-ready guardian platform.

## Currently Active On `develop`

- [x] Runtime Reliability is the current hardening track.
- [ ] The active runtime workstream is not finished yet.

## Shipped On `develop`

### Trust and control

- [x] Tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP policy modes for `disabled`, `approval`, and `full`
- [x] High-risk approval gates for chat and WebSocket execution
- [x] Structured audit logging for tool, approval, and runtime events
- [x] Secret redaction and scoped secret-reference handling

### Execution and integrations

- [x] Shell execution foundation
- [x] Browser automation foundation
- [x] MCP integration and runtime-managed server configuration
- [x] Auto-discovered built-in tools and plugin loading
- [x] Visible tool-execution streaming in chat

### Runtime and observability

- [x] Shared provider-agnostic LLM runtime settings
- [x] Ordered fallback chains across completion and agent-model paths
- [x] Health-aware rerouting away from recently failed targets
- [x] Runtime-path-specific primary model overrides for completion and agent-model paths
- [x] Runtime-path-specific fallback-chain overrides for completion and agent-model paths
- [x] First-class local runtime routing for helper, scheduler, core agent, and delegation paths
- [x] Runtime audit visibility across chat, WebSocket, scheduler, strategist, MCP, observer, browser, sandbox, and web search flows
- [x] Deterministic runtime eval harness for fallback, local routing, context-window degradation, browser/sandbox/web-search tool, and observer contracts

### Product surfaces

- [x] Browser-based village UI
- [x] WebSocket session flow
- [x] Native macOS observer daemon
- [x] Proactive delivery inside the current product
- [x] Soul, memory, goals, strategist, daily briefing, and evening review foundations

### Ecosystem foundations

- [x] SKILL.md support
- [x] MCP-powered extension surface
- [x] Recursive delegation foundations behind a flag

## Still To Do On `develop`

### Runtime Reliability

- [ ] richer provider selection beyond explicit runtime-path primary and fallback overrides, ordered fallback chains, and cooldown rerouting
- [ ] broader local-model routing into any remaining runtime paths where it makes sense
- [ ] remaining edge observability beyond the already-covered agent, scheduler, observer, and integration paths
- [ ] broader eval coverage beyond deterministic seam checks

### Product expansion

- [ ] native desktop shell, notifications, and external channels
- [ ] deeper guardian world-modeling, learning loops, and stronger intervention quality
- [ ] richer embodied UX beyond the current village and quest surfaces
- [ ] stronger workflow composition and extension ergonomics

## Workstream View

- [ ] Workstream 01: Trust Boundaries is only partially complete
- [ ] Workstream 02: Execution Plane is only partially complete
- [ ] Workstream 03: Runtime Reliability is only partially complete
- [ ] Workstream 04: Presence And Reach is only partially complete
- [ ] Workstream 05: Guardian Intelligence is only partially complete
- [ ] Workstream 06: Embodied UX is only partially complete
- [ ] Workstream 07: Ecosystem And Leverage is only partially complete
