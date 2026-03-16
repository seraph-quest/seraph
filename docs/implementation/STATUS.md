---
slug: /status
title: Seraph Development Status
---

# Seraph Development Status

## Legend

- `[x]` shipped on `develop`
- `[ ]` not fully shipped on `develop`
- in-flight branch work should be tracked in open PRs, not in this file

## Current Snapshot

- [x] Seraph is usable today as a local guardian prototype with a real UI, memory, observer loop, and action layer.
- [x] The live planning surface is now `docs/research/` plus `docs/implementation/`.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability have strong foundations on `develop`.
- [ ] No workstream is complete yet.
- [ ] Seraph is not yet the finished guardian product described in the research docs.

## Current Focus On `develop`

- [x] Runtime Reliability is still the active hardening track.
- [ ] Runtime Reliability is not complete yet.

## Shipped On `develop`

### Core guardian platform

- [x] browser-based Seraph village UI
- [x] FastAPI backend with chat, WebSocket, goals, tools, observer, settings, audit, approvals, vault, skills, and MCP APIs
- [x] native macOS observer daemon for screen/window ingest
- [x] persistent soul, vector memory, sessions, and goal storage

### Trust and control

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP policy modes for `disabled`, `approval`, and `full`
- [x] approval-gated high-risk actions in chat and WebSocket flows
- [x] structured audit logging for approval, tool, and runtime events
- [x] secret redaction and scoped secret-reference handling

### Execution and integrations

- [x] 17 built-in tool capabilities in the registry
- [x] shell execution via sandboxed tool path
- [x] browser automation foundation
- [x] filesystem, soul, goals, vault, and web-search tool foundations
- [x] MCP server management and runtime-managed server configuration
- [x] visible tool execution streaming in chat and agent flows
- [x] catalog/install surfaces for skills and MCP servers

### Runtime and observability

- [x] shared provider-agnostic LLM runtime settings
- [x] ordered fallback chains across completion and agent-model paths
- [x] health-aware rerouting away from recently failed targets
- [x] runtime-path-specific profile preference chains across completion and agent-model paths
- [x] wildcard runtime-path routing rules, with exact-path overrides taking precedence
- [x] runtime-path-specific primary model overrides
- [x] runtime-path-specific fallback-chain overrides
- [x] first-class local runtime routing for helper, scheduler, core agent, delegation, and connected MCP-specialist paths
- [x] runtime audit visibility across chat, WebSocket, scheduler, strategist, proactive delivery transport, MCP, observer, embedding, vector store, soul file, filesystem, browser, sandbox, and web search flows
- [x] deterministic runtime eval harness for fallback, routing, storage, observer, and integration seam contracts

### Guardian intelligence and proactive behavior

- [x] soul-backed persistent identity
- [x] vector memory retrieval and consolidation
- [x] hierarchical goals and progress APIs
- [x] strategist agent and strategist scheduler tick
- [x] daily briefing, evening review, activity digest, and weekly review surfaces
- [x] observer refresh across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle behavior

### Ecosystem foundations

- [x] `SKILL.md` support and runtime skill loading
- [x] MCP-powered extension surface
- [x] recursive delegation foundations behind a flag

## Still To Do On `develop`

### Runtime Reliability

- [ ] richer provider selection policy beyond path patterns, explicit overrides, ordered fallbacks, and cooldown rerouting
- [ ] broader local-model routing into any remaining runtime paths that are worth it
- [ ] remaining edge observability beyond the already-covered chat, scheduler, observer, proactive delivery, storage, and integration boundaries
- [ ] broader eval coverage beyond deterministic seam checks

### Product expansion

- [ ] native desktop shell, notifications, and non-browser presence
- [ ] deeper guardian world modeling, learning loops, and stronger intervention quality
- [ ] fuller life-OS UX beyond the current village and quest surfaces
- [ ] stronger workflow composition and extension ergonomics

## Workstream View

- [ ] Workstream 01: Trust Boundaries is only partially complete
- [ ] Workstream 02: Execution Plane is only partially complete
- [ ] Workstream 03: Runtime Reliability is only partially complete
- [ ] Workstream 04: Presence And Reach is only partially complete
- [ ] Workstream 05: Guardian Intelligence is only partially complete
- [ ] Workstream 06: Embodied UX is only partially complete
- [ ] Workstream 07: Ecosystem And Leverage is only partially complete
