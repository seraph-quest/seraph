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
| 02. Execution Plane | `[ ]` | Real tools, MCP, browser, shell, filesystem, goals, vault, web search, first-class reusable workflows, starter packs, and workflow-run replay context are shipped; stronger execution safety and richer workflow control are still left |
| 03. Runtime Reliability | `[ ]` | Fallback chains, routing rules, local runtime paths, provider scoring, broad audit visibility, and guardian-behavior runtime evals are shipped; richer provider policy and still broader eval depth are still left |
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, native daemon foundations, a first coherent desktop presence surface, unified browser/native continuity, and native action-card resume payloads are shipped; broader channel reach and deeper cross-surface continuity are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, goals, strategist, briefings, reviews, observer-driven state, observer salience/confidence scoring, explicit guardian state, structured world-model fusion, intervention policy, and feedback capture are shipped foundations; stronger learning loops are still left |
| 06. Embodied Interface | `[ ]` | The guardian cockpit is now the active browser shell, with a pane workspace, drag/resize plus grid snap, saved layout composition, session continuity restore, linked evidence, and keyboard workspace control shipped; denser workflow-operating surfaces are still left |
| 07. Ecosystem And Delegation | `[ ]` | Skills, MCP, catalog/install surfaces, delegation foundations, reusable workflow composition, starter packs, capability discovery, and first workflow history/replay surfaces are shipped; stronger extension ergonomics and clearer workflow control are still left |

## Progress Summary

- [x] Seraph is already a real local guardian prototype with observer, memory, goals, tools, approvals, MCP, and proactive scheduling.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability are the strongest shipped foundations on `develop`.
- [x] The research tree now defines Seraph as a power-user guardian cockpit, not a village-first product.
- [x] The guardian cockpit is now the active interface path, with the village retained only as a legacy fallback rather than a parallel active shell.
- [x] Seraph now exposes a first coherent capability surface for tools, skills, workflows, MCP servers, starter packs, workflow runs, and active thread continuity from inside the cockpit itself.
- [ ] Seraph is still behind the strongest reference systems on deeper execution hardening, richer workflow-operating density, stronger long-horizon intervention learning, broader native reach, and fuller cross-surface threading.
- [ ] No workstream is complete yet.

## Completed 10-PR Batches

Completed batches stay visible instead of being deleted on queue refresh.

### Latest Completed 10-PR Batch

1. [x] `capability-discovery-and-activation-v1`:
   make Seraph's shipped tools, skills, workflows, MCP servers, and policy-gated capabilities visible and operable from the cockpit, including clear blocked-state reasons and one-click activation paths
2. [x] `session-restore-and-thread-continuity-v1`:
   make reloads and reconnects restore the active thread predictably, clarify fresh-thread semantics, and preserve continuity across browser refreshes instead of feeling like a reset to empty state
3. [x] `execution-safety-hardening-v3`:
   tighten privileged execution isolation again around workflow replay, extension surfaces, artifact round-trips, and native-channel side effects before Seraph compounds more leverage
4. [x] `starter-skill-and-workflow-packs-v1`:
   ship clearer default starter packs so a fresh workspace feels capable immediately instead of relying on hidden bundled skills and workflows
5. [x] `workflow-history-and-replay-v1`:
   add deeper workflow history, rerun context, and artifact-linked replay so the cockpit can operate long-lived workflow chains instead of only recent runs
6. [x] `extension-debugging-and-recovery-v1`:
   deepen the cockpit operator surface into real debugging and recovery for skills, MCP servers, and blocked workflows instead of only status plus reload
7. [x] `world-model-memory-fusion-v3`:
   fuse observer state, goals, recent execution pressure, and memory recall into a stronger explicit world model that tracks durable projects and active constraints more accurately
8. [x] `guardian-learning-policy-v3`:
   make guardian learning shape salience thresholds, intervention phrasing, and escalation policy beyond the current delivery and channel bias layer
9. [x] `native-channel-expansion-v1`:
   expand proactive reach beyond browser plus desktop notifications into a broader but still policy-controlled native presence surface
10. [x] `cockpit-layout-composition-v2`:
   deepen operator workspace control with more flexible layout composition, stronger inspector linking, and better history-density inside the cockpit

### Previous Completed 10-PR Batch

1. [x] `execution-safety-hardening-v2`:
   tighten isolation, approval propagation, and secret or filesystem containment across shell, browser, workflow, and MCP execution paths before Seraph takes on more leverage
2. [x] `cockpit-workflow-views-v1`:
   add dedicated workflow-run, artifact-lineage, approval, and intervention views so the cockpit becomes a real operator console instead of a first generic shell
3. [x] `guardian-learning-loop-v2`:
   make intervention outcomes and explicit feedback change timing, channel choice, and escalation, not just interruption bias
4. [x] `cross-surface-continuity-v2`:
   unify browser state, daemon state, queued notifications, and recent interventions into one consistent continuity model
5. [x] `provider-policy-safeguards-v2`:
   add capability constraints, cost and latency guardrails, and stronger routing safety beyond the current weighted scoring layer
6. [x] `artifact-evidence-roundtrip-v2`:
   deepen round-tripping between workflow outputs, evidence panes, file artifacts, and the command surface
7. [x] `human-world-model-v2`:
   grow the first explicit working-state and commitments model into stronger project, pressure, and recent-execution understanding
8. [x] `native-desktop-shell-v2`:
   move from a presence card plus notifications to a more coherent desktop control shell with actionable recents and controls
9. [x] `extension-operator-surface-v1`:
   make skills, MCP servers, workflows, and policy state easier to operate and debug from one place
10. [x] `guardian-behavioral-evals-v3`:
   prove the next learning, workflow-density, and cross-surface behaviors with deeper end-to-end guardian contracts

### Earlier Completed 10-PR Batch

1. [x] `execution-safety-hardening-v1`:
   deepen privileged execution isolation, policy visibility, and hardening boundaries before Seraph expands more leverage on top of the current action layer
2. [x] `cockpit-linked-evidence-panels-v2`:
   make the guardian cockpit materially denser with linked evidence, trace, approval, and artifact panes so operator visibility becomes a real strength instead of just a first shell
3. [x] `workflow-control-and-artifact-roundtrips-v1`:
   turn shipped workflow composition into something easy to steer by adding operator-facing workflow control, approval visibility, and artifact round-tripping
4. [x] `guardian-outcome-learning-v1`:
   make stored intervention outcomes and explicit feedback change future guardian behavior instead of only being recorded
5. [x] `salience-calibration-v2`:
   improve interruption timing and proactive judgment by calibrating confidence, salience, and interruption cost beyond the first heuristic layer
6. [x] `saved-layouts-and-keyboard-control-v1`:
   make the cockpit feel like a real operator workspace with saved workspaces, stronger keyboard control, and denser navigation ergonomics
7. [x] `native-desktop-shell-v1`:
   move beyond browser-plus-daemon by shipping a more coherent native desktop presence around the existing observer and notification foundations
8. [x] `cross-surface-continuity-and-notification-controls`:
   connect ambient observation, proactive delivery, and deliberate interaction by exposing pending native notifications back into the browser and adding explicit browser-side notification controls
9. [x] `guardian-behavioral-evals-v2`:
   expand behavioral eval coverage from the first guardian baseline into deeper intervention-quality, workflow, and cockpit-adjacent contracts
10. [x] `human-world-model-v1`:
   deepen guardian-state quality from retrieval-plus-heuristics into a stronger explicit human/world model that can support consistently better intervention quality

## Current Rolling 10-PR List

This is the authoritative PR list for the implementation side.
It should always show the next 10 most valuable PRs, while the latest completed batch remains visible above.

- every entry below is a numbered PR-sized slice
- the current active item is `#1 execution-safety-hardening-v4`

1. [ ] `execution-safety-hardening-v4`:
   harden replay, native action-card resume paths, approval recovery, secret-bearing workflow surfaces, and operator-triggered follow-on execution before the new cockpit leverage compounds further
2. [ ] `workflow-timeline-and-approval-replay-v2`:
   turn workflow history into a real operating timeline with approval recovery, artifact lineage, and deeper rerun context instead of only recent run cards
3. [ ] `capability-pack-install-and-recommendations-v1`:
   go beyond visibility by adding recommended packs, install guidance, and clearer enable-or-fix-next actions for tools, skills, workflows, and MCP capability bundles
4. [ ] `extension-debugging-and-recovery-v2`:
   add health diagnostics, blocked-step repair, and dependency recovery for skills, workflows, and MCP servers beyond the first inspector-facing recovery surface
5. [ ] `session-threading-across-surfaces-v2`:
   unify browser sessions, native notifications, workflow resumes, and audit traces into one explicit thread model instead of only restoring the last browser session
6. [ ] `world-model-memory-fusion-v4`:
   deepen the structured world model into durable projects, routines, constraints, and longer-lived execution context rather than the current first fusion layer
7. [ ] `guardian-learning-policy-v4`:
   make learning change phrasing, cadence, escalation, and bundle-versus-interrupt decisions beyond the new delivery and channel bias layer
8. [ ] `native-channel-expansion-v2`:
   extend the first action-card/native continuation model into broader yet still policy-controlled native channels
9. [ ] `cockpit-layout-composition-v3`:
   add richer pane composition, saved named workspaces, and denser multi-pane operating patterns beyond the first per-layout save and reset model
10. [ ] `guardian-behavioral-evals-v4`:
   prove capability activation, workflow replay, cross-surface thread continuity, and learned intervention policy with deeper behavioral contracts

## Queue Maintenance Rule

- keep exactly 10 future PRs visible here
- keep the most recent completed 10-PR batch visible above with checkmarks
- do not delete the immediately previous completed batch until a later cleanup pass
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
- [x] guardian cockpit as the active browser shell, with the Phaser village kept only as an explicit legacy fallback path
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, desktop-notification fallback when browser delivery is unavailable, and a first actionable desktop control shell inside the cockpit
- [x] browser-side continuity controls for native notifications, including pending notification inspection, per-notification dismiss, bulk clear, cockpit-to-settings linkage for queued desktop state, and desktop-shell draft/continue actions over pending notifications, queued bundle items, and recent interventions
- [x] a unified continuity snapshot now ties daemon state, pending native notifications, deferred bundle items, and recent interventions into one browser-readable model across cockpit and settings surfaces
- [x] first capability-overview and starter-pack APIs now expose tools, skills, workflows, MCP servers, blocked-state reasons, and recommended starter bundles in one operator-readable shape
- [x] recent negative feedback on the same intervention type can now reduce interruption eagerness for similar future advisory nudges
- [x] aligned active-work signals now calibrate observer salience upward, and grounded high-salience nudges can cut through high interruption cost outside focus mode
- [x] the cockpit now supports a pane workspace with drag/resize, grid snap, packed `default` / `focus` / `review` layout presets, inspector visibility persistence, layout switching from both the header and keyboard shortcuts, plus per-layout save and reset behavior
- [x] 17 built-in tool capabilities exposed through the registry, with native and MCP-backed execution surfaces
- [x] first-class reusable workflow definitions loaded from defaults and workspace files, exposed through a workflows API, workflow metadata registry, and a dedicated `workflow_runner` specialist
- [x] starter packs now bundle default skills and workflows into obvious operator-invocable packages instead of leaving capability discovery entirely to disk inspection
- [x] first privileged-workflow hardening pass, including explicit workflow/tool execution-boundary metadata, richer approval behavior in tools/workflows APIs, and forced approval wrapping for approval-mode MCP workflow execution
- [x] second privileged-path hardening pass, including secret-ref containment to explicit injection-safe surfaces, workflow/operator metadata for secret-ref acceptance, and rejection of workflows whose runtime step tools are underdeclared
- [x] third workflow/operator hardening pass now carries workflow-run risk, approval, secret-ref, and execution-boundary context into replay/history surfaces so reruns remain boundary-aware
- [x] first operator workflow-control layer in the settings surface, including workflow enable/disable, reload, draft-to-cockpit flow control, and artifact path round-tripping back into the command bar
- [x] dedicated cockpit workflow-run views with richer workflow audit details, artifact-lineage linking, replay drafting, and workflow-specific inspector actions
- [x] cockpit artifacts and workflow outputs can now draft compatible follow-on workflows directly from the inspector instead of only seeding generic command-bar context
- [x] the cockpit now exposes a compact operator surface for workflow availability, starter packs, tools, skills, MCP server state, and live policy modes with direct reload and activation actions
- [x] the cockpit now restores the last active browser session on reload, tracks fresh-thread semantics explicitly, and marks background session activity instead of silently mixing threads
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing across helper, scheduled, agent, delegation, and MCP-specialist paths
- [x] first strict provider-policy safeguard layer for runtime paths, including required capability intents plus cost/latency guardrails that reroute only when compliant targets exist and otherwise fail open with explicit audit visibility
- [x] explicit guardian-state synthesis across chat, WebSocket, and strategist paths, combining observer context, salience/confidence signals, memory recall, session history, recent sessions, recent intervention feedback, and confidence into one structured downstream input
- [x] explicit guardian world model now also carries recent active projects, active constraints, recurring patterns, and recent execution pressure from workflow/tool outcomes, not only focus, commitments, and receptivity
- [x] explicit intervention policy at the proactive delivery boundary, with first-class act, bundle, defer, request-approval, and stay-silent classifications plus salience-aware policy reasons
- [x] persisted guardian intervention records with delivery outcomes, native-notification acknowledgements, and explicit feedback capture exposed back through guardian state
- [x] second outcome-learning layer now lets recent positive and acknowledged outcomes change direct-delivery timing, native-channel preference, and async-native escalation bias, not only interruption reduction
- [x] guardian behavioral proof now explicitly covers the calibrated high-salience deliver path versus degraded-confidence defer path at the proactive delivery gate
- [x] deeper guardian behavioral proof now also covers strategist tick learning its way into native-notification delivery and continuity-surface presence when high-signal learned nudges should bypass the browser
- [x] runtime audit visibility across chat, session-bound helper and agent LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths
- [x] deterministic eval harness coverage for core runtime, audit, REST and WebSocket chat behavior, guardian-state synthesis, guardian world-model behavior, guardian feedback loop behavior, calibrated salience/confidence delivery behavior, intervention policy behavior, observer refresh and delivery behavior, native desktop presence status plus the test-notification path, session consolidation behavior, tool/MCP guardrail behavior, proactive flow behavior, delegated workflow behavior, workflow composition behavior, observer, storage, tool-boundary, vault repository, MCP test API, skills API, screen repository, and daily-briefing, activity-digest, plus evening-review degraded-input contracts
- [x] denser guardian cockpit evidence surfaces with pending approvals, recent outputs, selectable intervention/audit/trace rows, an operations inspector that exposes linked details from the audit stream, and a pane workspace with packed layout presets, drag/resize, grid snap, and keyboard switching

## Recommended Reading Order

1. Read [Development Status](./STATUS.md) for the live shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read [08. Docs Contract](./08-docs-contract.md), [09. Benchmark Status](./09-benchmark-status.md), and [10. Superiority Delivery](./10-superiority-delivery.md) for the implementation-side mirrors of the research benchmark/program docs.
4. Read `01` through `07` for detailed per-workstream checklists.
5. Read the research docs for the benchmark and superiority target.
6. Treat `/legacy` docs as supporting history, not the live source of truth.
