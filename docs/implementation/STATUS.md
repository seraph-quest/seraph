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
- [x] A first guardian cockpit shell now exists alongside the legacy village mode.
- [ ] No workstream is complete yet.
- [ ] Seraph is not yet the finished guardian product described in the research docs.

## Docs Contract

- [x] `docs/research/00-synthesis.md` defines what Seraph is trying to become.
- [x] `docs/research/10-competitive-benchmark.md` owns the comparative judgment.
- [x] `docs/research/11-superiority-program.md` owns the design-level superiority program.
- [x] this file owns the fastest shipped snapshot on `develop`.
- [x] `docs/implementation/00-master-roadmap.md` owns the live 10-PR queue.
- [x] `docs/implementation/08-docs-contract.md`, `docs/implementation/09-benchmark-status.md`, and `docs/implementation/10-superiority-delivery.md` are the implementation-side mirrors of the research evidence/benchmark/program docs.
- [x] `docs/implementation/01` through `07` remain the workstream docs; `08` through `10` are meta mirrors, not extra workstreams.

## Current Focus On `develop`

- [x] The repo-wide active delivery batch is now led by stronger native/operator continuity, deeper guardian-quality work, and the remaining post-cockpit-baseline items after the first saved-layout pass shipped.
- [ ] That batch is not complete yet.
- [x] Guardian Intelligence remains central inside the current batch, but it is no longer the only active workstream.
- [x] Runtime Reliability now has a strong baseline on `develop`, but it is not fully complete.
- [x] The repo-wide 10-PR horizon is tracked in `docs/implementation/00-master-roadmap.md`.
- [x] The next strategic focus is deeper execution hardening, cockpit workflow density, and stronger adaptive guardian quality on top of the newly shipped world-model and behavioral-proof foundations.
- [x] The first item in that refreshed batch, `execution-safety-hardening-v2`, is now shipped on this branch and narrows both secret-injection scope and workflow declaration safety.
- [x] The published 10-PR horizon should be refreshed whenever landed PR count from that queue is divisible by 5.

## Current Target Shape

- [x] dense guardian cockpit as the primary operator surface
- [x] typed longitudinal memory and explicit guardian state
- [x] policy-driven interventions with clear defer / bundle / act / request-approval decisions
- [x] non-browser presence through a first coherent desktop surface, notifications, and native reach
- [x] reusable workflow composition plus explicit feedback capture and future improvement loops

## Shipped On `develop`

### Core guardian platform

- [x] browser-based guardian cockpit with legacy village mode
- [x] FastAPI backend with chat, WebSocket, goals, tools, observer, settings, audit, approvals, vault, skills, and MCP APIs
- [x] native macOS observer daemon for screen/window ingest
- [x] persistent soul, vector memory, sessions, and goal storage

### Trust and control

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP policy modes for `disabled`, `approval`, and `full`
- [x] approval-gated high-risk actions in chat and WebSocket flows
- [x] explicit execution-boundary metadata and approval behavior surfaced for tools and reusable workflows
- [x] structured audit logging for approval, tool, and runtime events
- [x] secret redaction and scoped secret-reference handling
- [x] secret-reference resolution now stays limited to explicit injection-safe surfaces instead of resolving into arbitrary tool calls

### Execution and integrations

- [x] 17 built-in tool capabilities in the registry
- [x] shell execution via sandboxed tool path
- [x] browser automation foundation
- [x] filesystem, soul, goals, vault, and web-search tool foundations
- [x] MCP server management and runtime-managed server configuration
- [x] visible tool execution streaming in chat and agent flows
- [x] first-class reusable workflows loaded from defaults and workspace files, exposed through a workflows API and `workflow_runner` specialist
- [x] forced approval wrapping for high-risk and approval-mode MCP workflow paths
- [x] first operator workflow-control layer with workflow list/toggle/reload plus draft-to-cockpit support
- [x] workflow loader/runtime metadata now derive from actual step tools and reject underdeclared workflow definitions
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
- [x] deterministic runtime eval harness for fallback, routing, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, workflow composition behavior, storage, observer, and integration seam contracts, including vault repository, the MCP test API, skills API, screen repository boundaries, and daily-briefing, activity-digest, plus evening-review degraded-input audit behavior

### Guardian intelligence and proactive behavior

- [x] soul-backed persistent identity
- [x] vector memory retrieval and consolidation
- [x] hierarchical goals and progress APIs
- [x] explicit guardian-state synthesis for chat, WebSocket, and strategist paths
- [x] first explicit guardian world model for focus, commitments, open loops or pressure, focus alignment, and intervention receptivity
- [x] observer salience, confidence, and interruption-cost scoring for observer refresh, guardian state, and proactive policy
- [x] explicit intervention-policy decisions for proactive delivery, including act / bundle / defer / request-approval / stay-silent classifications
- [x] persisted guardian intervention outcome tracking plus explicit feedback capture, including notification acknowledgement and feedback API flows
- [x] first feedback-driven guardian learning loop that reduces interruption eagerness for similar low-urgency interventions after recent negative outcomes
- [x] second-layer salience calibration that promotes aligned active-work signals and allows grounded high-salience nudges to cut through generic high-interruption bundling outside focus mode
- [x] deterministic guardian behavioral proof that grounded high-salience observer state can still deliver through high interruption cost while degraded observer confidence defers before transport
- [x] strategist agent and strategist scheduler tick
- [x] daily briefing, evening review, activity digest, and weekly review surfaces
- [x] observer refresh across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle behavior
- [x] first coherent desktop presence surface with daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, native-notification fallback delivery when browser sockets are unavailable but the daemon is connected, and browser-side inspect/dismiss controls for queued desktop notifications

### Current interface surface

- [x] browser-based guardian cockpit with session rail, guardian-state panel, interventions feed, audit surface, trace view, pending approvals, recent outputs, operations inspector, artifact round-trip into the command bar, fixed composer, and persisted layout presets with keyboard switching
- [x] legacy Phaser village mode with chat, quest, and settings overlays
- [x] visible tool use and agent activity in the current world surface
- [x] settings and management surfaces for tools, MCP, and system state
- [x] macOS daemon-backed desktop presence card plus browser-side inspect/dismiss controls for native notifications and notification fallback for non-browser proactive reach

### Ecosystem foundations

- [x] `SKILL.md` support and runtime skill loading
- [x] MCP-powered extension surface
- [x] recursive delegation foundations behind a flag
- [x] reusable workflow runtime with tool, skill, specialist, and MCP-aware gating

## Still To Do On `develop`

### Runtime and execution

- [ ] richer provider selection policy beyond weighted scoring, path patterns, explicit overrides, ordered fallbacks, and cooldown rerouting
- [ ] broader eval coverage beyond the shipped REST, WebSocket, observer refresh, delivery policy, salience/confidence delivery, consolidation, proactive, tool/MCP guardrail, delegated workflow, and workflow-composition behavioral contracts
- [ ] stronger execution isolation and privileged-path hardening beyond the first workflow/tool boundary pass

### Guardian intelligence

- [ ] stronger learning and feedback loops beyond the first negative-feedback interruption bias
- [ ] deeper guardian world modeling, learning loops, and stronger intervention quality beyond the first explicit focus or commitments layer
- [ ] stronger salience calibration and confidence quality beyond the first aligned-work/high-salience pass

### Interface and presence

- [ ] richer cockpit density with dedicated workflow operating views and broader keyboard/operator control beyond the first linked-evidence plus saved-layout shell
- [ ] richer cross-surface continuity and broader non-browser presence beyond the first desktop-presence and browser/native notification-control layer
- [ ] stronger cross-surface continuity between ambient observation and deliberate interaction beyond the new pending-notification bridge

### Workflow and leverage

- [ ] deeper operator-facing workflow control and artifact round-tripping beyond the first settings/cockpit loop
- [ ] stronger extension ergonomics around reusable capabilities and workflows

## Practical Summary

- [x] Seraph already has a serious local guardian core: memory, observer loop, strategy, tools, approvals, runtime audit, and deterministic evals.
- [x] The strongest current moat is guardian-oriented state plus proactive scaffolding, not the UI.
- [ ] The biggest gaps against the reference systems are deeper execution hardening, workflow-control ergonomics, stronger intervention learning and world modeling, native reach, and still-denser cockpit operation.
- [ ] The next major step is to deepen the new cockpit shell into a denser, more legible, more stateful guardian workspace without losing the existing trust and memory foundations.

## Workstream View

- [ ] Workstream 01: Trust Boundaries is only partially complete
- [ ] Workstream 02: Execution Plane is only partially complete
- [ ] Workstream 03: Runtime Reliability is only partially complete
- [ ] Workstream 04: Presence And Reach is only partially complete
- [ ] Workstream 05: Guardian Intelligence is only partially complete
- [ ] Workstream 06: Embodied Interface is only partially complete
- [ ] Workstream 07: Ecosystem And Delegation is only partially complete
