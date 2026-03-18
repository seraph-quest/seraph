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

This implementation tree is the canonical delivery-side answer to four questions:

1. What is shipped on `develop`?
2. How does the research-defined target product shape translate into delivery on `develop`?
3. What is still left on `develop` before Seraph reaches that research-defined target?
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
| 02. Execution Plane | `[ ]` | Real tools, MCP, browser, shell, filesystem, goals, vault, web search, and first-class reusable workflows are shipped; stronger execution safety and richer workflow control are still left |
| 03. Runtime Reliability | `[ ]` | Fallback chains, routing rules, local runtime paths, provider scoring, broad audit visibility, and guardian-behavior runtime evals are shipped; richer provider policy and still broader eval depth are still left |
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, native daemon foundations, and first native notifications are shipped; broader channel reach and a richer desktop shell are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, goals, strategist, briefings, reviews, observer-driven state, observer salience/confidence scoring, explicit guardian state, intervention policy, and feedback capture are shipped foundations; stronger learning loops are still left |
| 06. Embodied Interface | `[ ]` | A first guardian cockpit shell with legacy village fallback is shipped, but denser linked evidence, artifact, and operator-control surfaces are still left |
| 07. Ecosystem And Delegation | `[ ]` | Skills, MCP, catalog/install surfaces, delegation foundations, and reusable workflow composition are shipped; stronger extension ergonomics and clearer workflow control are still left |

## Progress Summary

- [x] Seraph is already a real local guardian prototype with observer, memory, goals, tools, approvals, MCP, and proactive scheduling.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability are the strongest shipped foundations on `develop`.
- [x] The research tree now defines Seraph as a power-user guardian cockpit, not a village-first product.
- [x] The first guardian cockpit shell is now shipped, with the village retained as a legacy mode rather than the default workflow.
- [ ] Seraph is still behind the strongest reference systems on workflow-operating density, deeper salience/intervention quality, native reach, deeper execution hardening, and operator workflow control.
- [ ] No workstream is complete yet.

## Next Recommended PR Sequence

This is the rolling execution queue. It should always show the next 10 most valuable PRs, and a checked item may remain visible until the next scheduled refresh.

1. [x] `execution-safety-hardening-v1`:
   deepen privileged execution isolation, policy visibility, and hardening boundaries before Seraph expands more leverage on top of the current action layer
2. [x] `cockpit-linked-evidence-panels-v2`:
   make the guardian cockpit materially denser with linked evidence, trace, approval, and artifact panes so operator visibility becomes a real strength instead of just a first shell
3. [x] `workflow-control-and-artifact-roundtrips-v1`:
   turn shipped workflow composition into something easy to steer by adding operator-facing workflow control, approval visibility, and artifact round-tripping
4. [x] `guardian-outcome-learning-v1`:
   make stored intervention outcomes and explicit feedback change future guardian behavior instead of only being recorded
5. [ ] `salience-calibration-v2`:
   improve interruption timing and proactive judgment by calibrating confidence, salience, and interruption cost beyond the first heuristic layer
6. [ ] `saved-layouts-and-keyboard-control-v1`:
   make the cockpit feel like a real operator workspace with saved workspaces, stronger keyboard control, and denser navigation ergonomics
7. [ ] `native-desktop-shell-v1`:
   move beyond browser-plus-daemon by shipping a more coherent native desktop presence around the existing observer and notification foundations
8. [ ] `cross-surface-continuity-and-notification-controls`:
   connect ambient observation, proactive delivery, and deliberate interaction with clearer notification controls and stronger continuity between browser and native surfaces
9. [ ] `guardian-behavioral-evals-v2`:
   expand behavioral eval coverage from the first guardian baseline into deeper intervention-quality, workflow, and cockpit-adjacent contracts
10. [ ] `human-world-model-v1`:
   deepen guardian-state quality from retrieval-plus-heuristics into a stronger explicit human/world model that can support consistently better intervention quality

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
- [x] guardian cockpit as the default browser shell, with the Phaser village kept as an explicit legacy mode
- [x] first native desktop-notification fallback path when browser delivery is unavailable but the daemon is connected
- [x] recent negative feedback on the same intervention type can now reduce interruption eagerness for similar future advisory nudges
- [x] 17 built-in tool capabilities exposed through the registry, with native and MCP-backed execution surfaces
- [x] first-class reusable workflow definitions loaded from defaults and workspace files, exposed through a workflows API, workflow metadata registry, and a dedicated `workflow_runner` specialist
- [x] first privileged-workflow hardening pass, including explicit workflow/tool execution-boundary metadata, richer approval behavior in tools/workflows APIs, and forced approval wrapping for approval-mode MCP workflow execution
- [x] first operator workflow-control layer in the settings surface, including workflow enable/disable, reload, draft-to-cockpit flow control, and artifact path round-tripping back into the command bar
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing across helper, scheduled, agent, delegation, and MCP-specialist paths
- [x] explicit guardian-state synthesis across chat, WebSocket, and strategist paths, combining observer context, salience/confidence signals, memory recall, session history, recent sessions, recent intervention feedback, and confidence into one structured downstream input
- [x] explicit intervention policy at the proactive delivery boundary, with first-class act, bundle, defer, request-approval, and stay-silent classifications plus salience-aware policy reasons
- [x] persisted guardian intervention records with delivery outcomes, native-notification acknowledgements, and explicit feedback capture exposed back through guardian state
- [x] runtime audit visibility across chat, session-bound helper and agent LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths
- [x] deterministic eval harness coverage for core runtime, audit, REST and WebSocket chat behavior, guardian-state synthesis, guardian feedback loop behavior, intervention policy behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP guardrail behavior, proactive flow behavior, delegated workflow behavior, workflow composition behavior, observer, storage, tool-boundary, vault repository, MCP test API, skills API, screen repository, and daily-briefing, activity-digest, plus evening-review degraded-input contracts
- [x] denser guardian cockpit evidence surfaces with pending approvals, recent outputs, selectable intervention/audit/trace rows, and an operations inspector that exposes linked details from the audit stream

## Recommended Reading Order

1. Read [Development Status](./STATUS.md) for the live shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read [08. Docs Contract](./08-docs-contract.md), [09. Benchmark Status](./09-benchmark-status.md), and [10. Superiority Delivery](./10-superiority-delivery.md) for the implementation-side mirrors of the research benchmark/program docs.
4. Read `01` through `07` for detailed per-workstream checklists.
5. Read the research docs for the benchmark and superiority target.
6. Treat `/legacy` docs as supporting history, not the live source of truth.
