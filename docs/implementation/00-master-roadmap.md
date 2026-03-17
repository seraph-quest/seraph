---
slug: /
title: Seraph Master Roadmap
---

# Seraph Master Roadmap

## Summary

Seraph uses the same high-level documentation split as `maas`:

- `docs/research/` is the design, benchmark, and product-thesis surface
- `docs/implementation/` is the shipped-state and delivery surface
- `docs/implementation/STATUS.md` is the live status ledger

This implementation tree is the canonical answer to four questions:

1. What is shipped on `develop`?
2. How does the target product shape translate into delivery on `develop`?
3. What is still left before Seraph reaches that target?
4. What are the next most valuable PRs?

## Docs Contract

- `docs/research/` defines target product shape, evidence rules, benchmark logic, and superiority program logic.
- `docs/implementation/STATUS.md` is the fastest shipped-state snapshot.
- this roadmap owns the live 10-PR queue and queue refresh rule.
- `docs/implementation/08-docs-contract.md` explains the boundary between research truth and implementation truth.
- `docs/implementation/09-benchmark-status.md` mirrors the benchmark axes from research as shipped implementation status.
- `docs/implementation/10-superiority-delivery.md` mirrors the superiority program from research as delivery ownership and implementation translation.
- `docs/implementation/01` through `07` are the only workstream docs; `08` through `10` are cross-cutting implementation mirrors, not extra workstreams.
- if research adds a new benchmark/program layer without an implementation mirror, the docs are incomplete.

## Current Status

Read this roadmap together with [Development Status](./STATUS.md).
For the implementation-side mirrors of the evidence, benchmark, and superiority layers, also read [08. Docs Contract](./08-docs-contract.md), [09. Benchmark Status](./09-benchmark-status.md), and [10. Superiority Delivery](./10-superiority-delivery.md).

Legend for the checklist column:

- `[x]` shipped on `develop`
- `[ ]` not fully shipped on `develop`

| Workstream | Checklist | Notes |
|---|---|---|
| 01. Trust Boundaries | `[ ]` | Policy modes, approvals, audit logging, and secret handling are shipped; deeper isolation and narrower privileged execution paths are still left |
| 02. Execution Plane | `[ ]` | Real tools, MCP, browser, shell, filesystem, goals, vault, and web search are shipped; richer workflow execution and stronger execution safety are still left |
| 03. Runtime Reliability | `[ ]` | Fallback chains, routing rules, local runtime paths, provider scoring, broad audit visibility, and guardian-behavior runtime evals are shipped; richer provider policy and still broader eval depth are still left |
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, native daemon foundations, and first native notifications are shipped; broader channel reach and a richer desktop shell are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, goals, strategist, briefings, reviews, observer-driven state, explicit guardian state, and intervention policy are shipped foundations; salience modeling and feedback loops are still left |
| 06. Embodied Interface | `[ ]` | The current village UI is shipped, but the target interface is now a dense guardian cockpit rather than a village-first shell |
| 07. Ecosystem And Delegation | `[ ]` | Skills, MCP, catalog/install surfaces, and delegation foundations are shipped; workflow composition and stronger extension ergonomics are still left |

## Progress Summary

- [x] Seraph is already a real local guardian prototype with observer, memory, goals, tools, approvals, MCP, and proactive scheduling.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability are the strongest shipped foundations on `develop`.
- [x] The research tree now defines Seraph as a power-user guardian cockpit, not a village-first product.
- [ ] Seraph is still behind the strongest reference systems on dense interface efficiency, workflow composition, native reach, and execution hardening.
- [ ] No workstream is complete yet.

## Next Recommended PR Sequence

This is the rolling execution queue. It should always show the next 10 most valuable PRs, and a checked item may remain visible until the next scheduled refresh.

1. [x] `provider-policy-scoring`:
   add weighted capability scoring on top of the current routing stack so target selection reflects value, not only explicit preference order
2. [x] `behavioral-evals-guardian-flows`:
   extend behavioral evals into observer refresh, consolidation, proactive delivery, and guardrail-sensitive guardian flows
3. [x] `guardian-state-synthesis`:
   merge observer signals, memory, goals, sessions, and confidence into one structured guardian-state input
4. [x] `intervention-policy-v1`:
   make intervene, defer, bundle, request-approval, and stay-silent decisions explicit and state-aware
5. [x] `native-presence-notifications`:
   add the first real non-browser presence path with desktop notifications and interrupt-aware reach
6. [ ] `workflow-composition-v1`:
   add first-class reusable multi-step workflows across tools, specialists, skills, and MCP
7. [ ] `guardian-feedback-loop`:
   capture intervention outcomes and user feedback so timing and action quality can improve
8. [ ] `operator-cockpit-v1`:
   replace the village as the default workflow surface with a dense multi-pane guardian cockpit
9. [ ] `observer-salience-and-confidence-model`:
   score observer inputs by confidence, urgency, and interruption cost before they reach strategy and delivery layers
10. [ ] `execution-safety-hardening-v1`:
   deepen isolation, policy visibility, and privileged execution boundaries to close the gap versus stronger execution-first systems

## Queue Maintenance Rule

- keep exactly 10 future PRs visible here
- rerank and rewrite the queue whenever the number of landed PRs from the published queue is divisible by 5
- rerank earlier if new evidence from `docs/research/` materially changes the priority order

## Delivery Order

1. Trust Boundaries
2. Execution Plane
3. Runtime Reliability
4. Presence And Reach
5. Guardian Intelligence
6. Embodied Interface
7. Ecosystem And Delegation

Implementation docs `08` through `10` are supporting mirror layers for this roadmap, not additional workstreams.

## Stable Interfaces

- the browser and WebSocket chat surface
- the observer daemon ingest path
- `SKILL.md`-based skill loading
- runtime-path-based LLM routing and fallback settings
- runtime audit and eval harness contracts
- MCP server configuration and server-management APIs

## Current Shipped Slice On `develop`

- [x] local guardian stack with browser UI, backend APIs, WebSocket chat, scheduler, observer loop, and native macOS daemon
- [x] first native desktop-notification fallback path when browser delivery is unavailable but the daemon is connected
- [x] 17 built-in tool capabilities exposed through the registry, with native and MCP-backed execution surfaces
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing across helper, scheduled, agent, delegation, and MCP-specialist paths
- [x] explicit guardian-state synthesis across chat, WebSocket, and strategist paths, combining observer context, memory recall, session history, recent sessions, and confidence into one structured downstream input
- [x] explicit intervention policy at the proactive delivery boundary, with first-class act, bundle, defer, request-approval, and stay-silent classifications plus policy audit reasons
- [x] runtime audit visibility across chat, session-bound helper and agent LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths
- [x] deterministic eval harness coverage for core runtime, audit, REST and WebSocket chat behavior, guardian-state synthesis, intervention policy behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP guardrail behavior, proactive flow behavior, delegated workflow behavior, observer, storage, tool-boundary, vault repository, MCP test API, skills API, screen repository, and daily-briefing, activity-digest, plus evening-review degraded-input contracts

## Recommended Reading Order

1. Read [Development Status](./STATUS.md) for the live shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read [08. Docs Contract](./08-docs-contract.md), [09. Benchmark Status](./09-benchmark-status.md), and [10. Superiority Delivery](./10-superiority-delivery.md) for the implementation-side mirrors of the research benchmark/program docs.
4. Read `01` through `07` for detailed per-workstream checklists.
5. Read the research docs for the benchmark and superiority target.
6. Treat `/legacy` docs as supporting history, not the live source of truth.
