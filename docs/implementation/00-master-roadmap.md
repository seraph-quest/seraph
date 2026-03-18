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
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, native daemon foundations, and a first coherent desktop presence surface are shipped; broader channel reach and stronger cross-surface continuity are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, goals, strategist, briefings, reviews, observer-driven state, observer salience/confidence scoring, explicit guardian state, intervention policy, and feedback capture are shipped foundations; stronger learning loops are still left |
| 06. Embodied Interface | `[ ]` | A first guardian cockpit shell with linked evidence, saved layouts, keyboard workspace control, and legacy village fallback is shipped, but denser workflow-operating surfaces are still left |
| 07. Ecosystem And Delegation | `[ ]` | Skills, MCP, catalog/install surfaces, delegation foundations, and reusable workflow composition are shipped; stronger extension ergonomics and clearer workflow control are still left |

## Progress Summary

- [x] Seraph is already a real local guardian prototype with observer, memory, goals, tools, approvals, MCP, and proactive scheduling.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability are the strongest shipped foundations on `develop`.
- [x] The research tree now defines Seraph as a power-user guardian cockpit, not a village-first product.
- [x] The first guardian cockpit shell is now shipped, with the village retained as a legacy mode rather than the default workflow.
- [ ] Seraph is still behind the strongest reference systems on workflow-operating density, deeper salience/intervention quality, native reach, deeper execution hardening, and operator workflow control.
- [ ] No workstream is complete yet.

## Rolling 10-PR List

This is the authoritative PR list for the implementation side.
It should always show the next 10 most valuable PRs, and a checked item may remain visible until the next scheduled refresh.

- every entry below is a numbered PR-sized slice
- the current active item is `#1 execution-safety-hardening-v2`

1. [ ] `execution-safety-hardening-v2`:
   tighten isolation, approval propagation, and secret or filesystem containment across shell, browser, workflow, and MCP execution paths before Seraph takes on more leverage
2. [ ] `cockpit-workflow-views-v1`:
   add dedicated workflow-run, artifact-lineage, approval, and intervention views so the cockpit becomes a real operator console instead of a first generic shell
3. [ ] `guardian-learning-loop-v2`:
   make intervention outcomes and explicit feedback change timing, channel choice, and escalation, not just interruption bias
4. [ ] `cross-surface-continuity-v2`:
   unify browser state, daemon state, queued notifications, and recent interventions into one consistent continuity model
5. [ ] `provider-policy-safeguards-v2`:
   add capability constraints, cost and latency guardrails, and stronger routing safety beyond the current weighted scoring layer
6. [ ] `artifact-evidence-roundtrip-v2`:
   deepen round-tripping between workflow outputs, evidence panes, file artifacts, and the command surface
7. [ ] `human-world-model-v2`:
   grow the first explicit working-state and commitments model into stronger project, pressure, and recent-execution understanding
8. [ ] `native-desktop-shell-v2`:
   move from a presence card plus notifications to a more coherent desktop control shell with actionable recents and controls
9. [ ] `extension-operator-surface-v1`:
   make skills, MCP servers, workflows, and policy state easier to operate and debug from one place
10. [ ] `guardian-behavioral-evals-v3`:
   prove the next learning, workflow-density, and cross-surface behaviors with deeper end-to-end guardian contracts

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
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, and desktop-notification fallback when browser delivery is unavailable
- [x] browser-side continuity controls for native notifications, including pending notification inspection, per-notification dismiss, bulk clear, and cockpit-to-settings linkage for queued desktop state
- [x] recent negative feedback on the same intervention type can now reduce interruption eagerness for similar future advisory nudges
- [x] aligned active-work signals now calibrate observer salience upward, and grounded high-salience nudges can cut through high interruption cost outside focus mode
- [x] the cockpit now supports persisted `default` / `focus` / `review` workspace presets, inspector visibility persistence, and layout switching from both the header and keyboard shortcuts
- [x] 17 built-in tool capabilities exposed through the registry, with native and MCP-backed execution surfaces
- [x] first-class reusable workflow definitions loaded from defaults and workspace files, exposed through a workflows API, workflow metadata registry, and a dedicated `workflow_runner` specialist
- [x] first privileged-workflow hardening pass, including explicit workflow/tool execution-boundary metadata, richer approval behavior in tools/workflows APIs, and forced approval wrapping for approval-mode MCP workflow execution
- [x] first operator workflow-control layer in the settings surface, including workflow enable/disable, reload, draft-to-cockpit flow control, and artifact path round-tripping back into the command bar
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing across helper, scheduled, agent, delegation, and MCP-specialist paths
- [x] explicit guardian-state synthesis across chat, WebSocket, and strategist paths, combining observer context, salience/confidence signals, memory recall, session history, recent sessions, recent intervention feedback, and confidence into one structured downstream input
- [x] first explicit human/world model layered into guardian state, with current focus, active commitments, open loops or pressure, focus alignment, and intervention receptivity
- [x] explicit intervention policy at the proactive delivery boundary, with first-class act, bundle, defer, request-approval, and stay-silent classifications plus salience-aware policy reasons
- [x] persisted guardian intervention records with delivery outcomes, native-notification acknowledgements, and explicit feedback capture exposed back through guardian state
- [x] guardian behavioral proof now explicitly covers the calibrated high-salience deliver path versus degraded-confidence defer path at the proactive delivery gate
- [x] runtime audit visibility across chat, session-bound helper and agent LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths
- [x] deterministic eval harness coverage for core runtime, audit, REST and WebSocket chat behavior, guardian-state synthesis, guardian world-model behavior, guardian feedback loop behavior, calibrated salience/confidence delivery behavior, intervention policy behavior, observer refresh and delivery behavior, native desktop presence status plus the test-notification path, session consolidation behavior, tool/MCP guardrail behavior, proactive flow behavior, delegated workflow behavior, workflow composition behavior, observer, storage, tool-boundary, vault repository, MCP test API, skills API, screen repository, and daily-briefing, activity-digest, plus evening-review degraded-input contracts
- [x] denser guardian cockpit evidence surfaces with pending approvals, recent outputs, selectable intervention/audit/trace rows, an operations inspector that exposes linked details from the audit stream, and persisted layout presets with keyboard switching

## Recommended Reading Order

1. Read [Development Status](./STATUS.md) for the live shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read [08. Docs Contract](./08-docs-contract.md), [09. Benchmark Status](./09-benchmark-status.md), and [10. Superiority Delivery](./10-superiority-delivery.md) for the implementation-side mirrors of the research benchmark/program docs.
4. Read `01` through `07` for detailed per-workstream checklists.
5. Read the research docs for the benchmark and superiority target.
6. Treat `/legacy` docs as supporting history, not the live source of truth.
