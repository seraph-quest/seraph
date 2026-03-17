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
- [x] The target product shape is now a power-user guardian cockpit, not a village-first shell.
- [ ] No workstream is complete yet.
- [ ] Seraph is not yet the finished guardian product described in the research docs.

## Current Focus On `develop`

- [x] Guardian Intelligence is now the active implementation track.
- [ ] Guardian Intelligence is not complete yet.
- [x] Runtime Reliability now has a strong baseline on `develop`, but it is not fully complete.
- [x] The repo-wide 10-PR horizon is tracked in `docs/implementation/00-master-roadmap.md`.
- [x] The next strategic focus after the runtime baseline is guardian-state quality, intervention quality, operator cockpit quality, workflow composition, and native reach.
- [x] The published 10-PR horizon should be refreshed whenever landed PR count from that queue is divisible by 5.

## Current Target Shape

- [x] dense guardian cockpit as the primary operator surface
- [x] typed longitudinal memory and explicit guardian state
- [x] policy-driven interventions with clear defer / bundle / act / request-approval decisions
- [x] non-browser presence through notifications and native reach
- [x] stronger workflow composition and feedback-driven improvement

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
- [x] first-class local runtime routing for helper, all current scheduled completion jobs, core agent, delegation, and connected MCP-specialist paths
- [x] runtime audit visibility across chat, WebSocket, session-bound helper LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallback paths, strategist, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, observer plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search flows
- [x] deterministic runtime eval harness for fallback, routing, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, storage, observer, and integration seam contracts, including vault repository, the MCP test API, skills API, screen repository boundaries, and daily-briefing, activity-digest, plus evening-review degraded-input audit behavior

### Guardian intelligence and proactive behavior

- [x] soul-backed persistent identity
- [x] vector memory retrieval and consolidation
- [x] hierarchical goals and progress APIs
- [x] explicit guardian-state synthesis for chat, WebSocket, and strategist paths
- [x] explicit intervention-policy decisions for proactive delivery, including act / bundle / defer / request-approval / stay-silent classifications
- [x] strategist agent and strategist scheduler tick
- [x] daily briefing, evening review, activity digest, and weekly review surfaces
- [x] observer refresh across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle behavior
- [x] native-notification fallback delivery when browser sockets are unavailable but the daemon is connected

### Current interface surface

- [x] browser-based village UI with chat, quest, and settings overlays
- [x] visible tool use and agent activity in the current world surface
- [x] settings and management surfaces for tools, MCP, and system state
- [x] macOS daemon notification fallback for non-browser proactive reach

### Ecosystem foundations

- [x] `SKILL.md` support and runtime skill loading
- [x] MCP-powered extension surface
- [x] recursive delegation foundations behind a flag

## Still To Do On `develop`

### Runtime and execution

- [ ] richer provider selection policy beyond weighted scoring, path patterns, explicit overrides, ordered fallbacks, and cooldown rerouting
- [ ] broader eval coverage beyond the shipped REST, WebSocket, observer refresh, delivery policy, consolidation, proactive, tool/MCP guardrail, and delegated behavioral contracts
- [ ] stronger execution isolation and privileged-path hardening

### Guardian intelligence

- [ ] stronger learning and feedback loops so proactive behavior improves over time instead of staying policy-only
- [ ] deeper guardian world modeling, learning loops, and stronger intervention quality
- [ ] observer salience and confidence modeling for better strategy and delivery

### Interface and presence

- [ ] primary dense guardian cockpit instead of the current village as the default workflow surface
- [ ] richer native desktop shell and broader non-browser presence beyond the first notification fallback path
- [ ] stronger cross-surface continuity between ambient observation and deliberate interaction

### Workflow and leverage

- [ ] stronger workflow composition and extension ergonomics
- [ ] clearer operator-facing workflow control and artifact round-tripping

## Practical Summary

- [x] Seraph already has a serious local guardian core: memory, observer loop, strategy, tools, approvals, runtime audit, and deterministic evals.
- [x] The strongest current moat is guardian-oriented state plus proactive scaffolding, not the UI.
- [ ] The biggest gaps against the reference systems are operator cockpit density, workflow composition, native reach, and execution hardening.
- [ ] The next major step is to turn the current prototype into a denser, more legible, more stateful guardian cockpit without losing the existing trust and memory foundations.

## Workstream View

- [ ] Workstream 01: Trust Boundaries is only partially complete
- [ ] Workstream 02: Execution Plane is only partially complete
- [ ] Workstream 03: Runtime Reliability is only partially complete
- [ ] Workstream 04: Presence And Reach is only partially complete
- [ ] Workstream 05: Guardian Intelligence is only partially complete
- [ ] Workstream 06: Embodied UX is only partially complete
- [ ] Workstream 07: Ecosystem And Leverage is only partially complete
