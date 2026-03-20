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
| 02. Execution Plane | `[ ]` | Real tools, MCP, browser, shell, filesystem, goals, vault, web search, first-class reusable workflows, starter packs, threaded workflow history, parameterized replay context, and capability preflight hooks are shipped; stronger execution safety and deeper step-level workflow control are still left |
| 03. Runtime Reliability | `[ ]` | Fallback chains, routing rules, local runtime paths, weighted provider scoring, capability/cost/latency/task/budget safeguards, broad audit visibility, and guardian-behavior runtime evals are shipped; richer routing explainability and still broader eval depth are still left |
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, native daemon foundations, a first coherent desktop presence surface, unified browser/native continuity, and native action-card resume payloads are shipped; broader channel reach and deeper cross-surface continuity are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, goals, strategist, briefings, reviews, observer-driven state, observer salience/confidence scoring, explicit guardian state, structured world-model fusion, continuity-thread memory signals, project timelines, obligations, collaborators, intervention policy, and learned timing/suppression/thread guidance are shipped foundations; stronger learning loops are still left |
| 06. Embodied Interface | `[ ]` | The guardian cockpit is now the active browser shell, with a pane workspace, drag/resize plus grid snap, saved layout composition, session continuity restore, linked evidence, a searchable capability surface, a threaded operator timeline, preflight/repair flows, and denser operator-terminal control shipped; deeper workflow step-debugging density is still left |
| 07. Ecosystem And Delegation | `[ ]` | Skills, MCP, catalog/install surfaces, delegation foundations, reusable workflow composition, starter packs, capability discovery, threaded workflow/operator timelines, parameterized runbooks, preflight/autorepair, and repair flows are shipped; stronger extension ergonomics and clearer workflow step control are still left |

## Progress Summary

- [x] Seraph is already a real local guardian prototype with observer, memory, goals, tools, approvals, MCP, and proactive scheduling.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability are the strongest shipped foundations on `develop`.
- [x] The research tree now defines Seraph as a power-user guardian cockpit, not a village-first product.
- [x] The guardian cockpit is now the only supported interface contract; the village/editor line is removed from the active repo path and should not be revived.
- [x] Seraph now exposes a coherent capability surface for tools, skills, workflows, MCP servers, starter packs, workflow runs, reusable runbooks, preflight/autorepair actions, live operator logs, and active thread continuity from inside the cockpit itself.
- [x] Workflow runs, pending approvals, notifications, queued interventions, recent interventions, and failure events now share explicit thread labels, continue drafts, open-thread links, and one threaded operator timeline instead of living as separate operator silos.
- [x] Workflow runs now expose step records, retry-from-step drafts, richer fingerprints, blocked-skill repair guidance, and cockpit-native debug/authoring handoff instead of only run-level replay.
- [x] Guardian state now carries memory signals, continuity threads, collaborators, recurring obligations, project timelines, and learned timing, suppression, blocked-state, plus thread guidance instead of only first-pass focus and delivery bias.
- [x] Runtime routing now enforces capability, cost, latency, task-class, and budget safeguards with operator-readable audit details instead of only weighted scoring and cooldown rerouting.
- [ ] Seraph is still behind the strongest reference systems on deeper execution hardening, richer workflow step-debugging density, stronger long-horizon intervention learning, broader native reach, and fuller extension ergonomics.
- [ ] No workstream is complete yet.

## Completed 10-PR Batches

Completed batches stay visible instead of being deleted on queue refresh.

### Latest Completed 10-PR Batch

1. [x] `retire-village-and-editor-v1`:
   remove the dormant village shell, map editor, Phaser bridge, and game-facing docs so the repo, product story, public docs IA, and cockpit-only direction stop contradicting each other
2. [x] `execution-safety-hardening-v7`:
   harden threaded replay, operator-timeline mutations, capability autorepair, provider fallback escalation, and native continuation resume paths so the denser cockpit keeps clear privilege boundaries as leverage increases
3. [x] `workflow-step-debugging-and-recovery-v1`:
   deepen workflow history from run-level timelines into step-level diagnostics, checkpoint targeting, failed-step evidence, and safer retry-from-step recovery
4. [x] `cockpit-density-and-live-operator-views-v2`:
   tighten the operator timeline, workflow timeline, approvals, evidence, and command surfaces into a faster keyboard-first cockpit with better step/debug density
5. [x] `capability-bootstrap-and-pack-install-v1`:
   move from first preflight/autorepair into broader bundled capability bootstrap, dependency install sequencing, and clearer pack-level recovery for skills, workflows, and MCP servers
6. [x] `provider-policy-explainability-and-budgets-v1`:
   expose operator-readable routing explanations, budget classes, task/risk-aware degrade paths, and better cross-surface visibility into why the runtime picked or rejected each target
7. [x] `extension-debugging-and-authoring-v1`:
   make third-party and user-authored skills, workflows, and MCP surfaces easier to validate, debug, recover, and operate from inside the cockpit
8. [x] `world-model-memory-fusion-v7`:
   deepen durable project state with collaborator timelines, recurring obligations, routines, execution-memory fusion, active blockers, next-up sequencing, and dominant-thread synthesis that can hold longer-running commitments more coherently
9. [x] `guardian-learning-policy-v7`:
   extend learning from the first suppression/timing/thread bias layer into stronger cooldown, escalation, and context-conditioned intervention policy adaptation with better quality gates
10. [x] `guardian-behavioral-evals-v7`:
   add deterministic contracts for workflow step recovery, broader capability bootstrap/autorepair, routing explainability and budgets, deeper learning, and richer native continuity

### Earlier Completed 10-PR Batch

1. [x] `execution-safety-hardening-v6`:
   harden approval replay, operator-triggered repair, runbook execution, and capability-install flows with stricter mutation guardrails, clearer privilege boundaries, and tighter secret-bearing path containment
2. [x] `threaded-operator-timeline-v1`:
   unify sessions, workflow runs, approvals, interventions, notifications, and audit failures into one first-class threaded operator timeline instead of spreading continuity across separate panes and APIs
3. [x] `workflow-runbooks-and-parameterized-replay-v1`:
   turn replay plus runbooks into safe parameterized reruns with checkpointed approval recovery, artifact input selection, and clearer "resume from here" semantics
4. [x] `capability-preflight-and-autorepair-v1`:
   add preflight checks that tell the operator exactly what will block a tool, workflow, or runbook before execution, plus one-click repair for missing tools, skills, MCP auth, and policy mismatches
5. [x] `provider-policy-safeguards-v3`:
   deepen runtime routing from weighted-plus-safeguard selection into explicit budget-aware, latency-aware, and task-class-aware policy with better degrade paths and operator-readable explanations
6. [x] `native-channel-expansion-v3`:
   move from desktop notification and action-card continuity to richer native continuation surfaces, including actionable recents, approval follow-up, and clearer browser/native routing arbitration
7. [x] `world-model-memory-fusion-v6`:
   grow the world model from projects, routines, and continuity threads into durable project timelines, recurring obligations, collaborators, and better pressure synthesis across goals, observer state, and execution history
8. [x] `guardian-learning-policy-v6`:
   make learning affect suppression windows, escalation cooldowns, bundle timing, thread or channel preference, and intervention framing by context instead of only first adaptive biases
9. [x] `cockpit-density-and-live-operator-views-v1`:
   turn the cockpit into a denser operator surface with live log streams, timeline-linked panes, better keyboard navigation, and tighter composition between terminal, history, approvals, and evidence
10. [x] `guardian-behavioral-evals-v6`:
   add deterministic contracts for threaded timelines, preflight or repair flows, richer runbook replay, provider-policy budgets, deeper world-model fusion, and new native-channel routing

### Previous Completed 10-PR Batch

1. [x] `execution-safety-hardening-v5`:
   tighten replay, approval-recovery, and operator-triggered repair surfaces so workflow continuation stays boundary-aware and actionable guidance only suggests real safe fixes
2. [x] `workflow-timeline-and-approval-replay-v3`:
   deepen workflow history into a real operator ledger with pending-approval timeline events, replay guardrails, thread labels, and explicit awaiting-approval state
3. [x] `session-threading-across-surfaces-v3`:
   bind approvals, workflow runs, notifications, queued interventions, and recent interventions back into explicit browser threads with continue and open-thread actions
4. [x] `capability-pack-autoinstall-and-policy-repair-v2`:
   move starter packs and blocked workflows from passive status into actionable repair guidance, including policy-fix recommendations and pack-aware recovery actions
5. [x] `operator-terminal-live-logs-and-runbooks-v2`:
   turn the operator terminal into a real control surface with live operator-feed events, saved runbook macros, and quick command reuse
6. [x] `extension-debugging-and-recovery-v3`:
   expose clearer blocked-state repair paths for workflows, starter packs, skills, catalog items, and MCP surfaces directly inside the cockpit operator flow
7. [x] `native-channel-expansion-v2`:
   make native continuity more actionable through thread-aware continue and open-thread links plus safer browser-to-native follow-up control surfaces
8. [x] `world-model-memory-fusion-v5`:
   deepen guardian state with structured memory signals, continuity threads, and memory-degraded confidence handling rather than only plain-text recall
9. [x] `guardian-learning-policy-v5`:
   extend learning beyond phrasing and channel choice into timing and blocked-state policy bias that changes bundle-versus-act decisions
10. [x] `guardian-behavioral-evals-v5`:
   prove the new approval-threading, capability-repair, and learned blocked-state/timing behaviors through deterministic eval contracts

### Older Completed 10-PR Batch

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

### Archived Completed 10-PR Batch

1. [x] `execution-safety-hardening-v4`:
   harden replay, native action-card resume paths, approval recovery, secret-bearing workflow surfaces, and operator-triggered follow-on execution before the new cockpit leverage compounds further
2. [x] `workflow-timeline-and-approval-replay-v2`:
   turn workflow history into a real operating timeline with approval recovery, artifact lineage, and deeper rerun context instead of only recent run cards
3. [x] `capability-command-palette-v1`:
   add a Hermes-style searchable command palette for tools, skills, workflows, starter packs, MCP actions, and repair actions so capability activation becomes keyboard-first instead of pane-bound
4. [x] `capability-pack-install-and-recommendations-v1`:
   go beyond visibility by adding recommended packs, install guidance, and clearer enable-or-fix-next actions for tools, skills, workflows, and MCP capability bundles
5. [x] `capability-repair-and-install-flows-v1`:
   turn blocked capabilities into guided repair flows with direct install, enable, reconnect, or dependency-fix actions instead of only showing blocked reasons
6. [x] `extension-debugging-and-recovery-v2`:
   add health diagnostics, blocked-step repair, and dependency recovery for skills, workflows, and MCP servers beyond the first inspector-facing recovery surface
7. [x] `operator-terminal-and-runbooks-v1`:
   add a dense Hermes-like operator terminal for recent runs, failures, quick commands, and reusable runbooks instead of forcing operators through scattered panes and settings
8. [x] `session-threading-across-surfaces-v2`:
   unify browser sessions, native notifications, workflow resumes, and audit traces into one explicit thread model instead of only restoring the last browser session
9. [x] `world-model-memory-fusion-v4`:
   deepen the structured world model into durable projects, routines, constraints, and longer-lived execution context rather than the current first fusion layer
10. [x] `guardian-learning-policy-v4`:
   make learning change phrasing, cadence, escalation, and bundle-versus-interrupt decisions beyond the new delivery and channel bias layer

### Legacy Completed 10-PR Batch

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

### Historical Completed 10-PR Batch

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
- the current active item is `#1 execution-safety-hardening-v8`

1. [ ] `execution-safety-hardening-v8`:
   harden step-targeted workflow recovery, operator-triggered replay, pack install/repair mutations, provider budget escalation, and native continuation resumes so the denser cockpit keeps explicit privilege boundaries as leverage compounds
2. [ ] `workflow-step-debugging-and-recovery-v2`:
   deepen workflow history from the first step-record layer into richer failed-step evidence, checkpoint diagnostics, artifact deltas, and safer retry-from-step recovery with clearer operator guidance
3. [ ] `cockpit-density-and-live-operator-views-v3`:
   tighten the operator timeline, workflow timeline, approvals, evidence, and command surfaces into a faster keyboard-first cockpit with better live debugging density, fewer dead states, and stronger pane composition
4. [ ] `capability-bootstrap-and-pack-install-v2`:
   broaden bundled capability bootstrap with dependency install sequencing, safer first-run pack activation, missing-tool repair, and clearer pack-level recovery for skills, workflows, and MCP servers
5. [ ] `provider-policy-explainability-and-budgets-v2`:
   expose operator-readable routing explanations, budget classes, task/risk-aware degrade paths, and cross-surface visibility into why the runtime picked, skipped, or downgraded each target
6. [ ] `extension-debugging-and-authoring-v2`:
   make third-party and user-authored skills, workflows, and MCP surfaces easier to validate, debug, repair, and author directly from inside the cockpit
7. [ ] `native-channel-expansion-v4`:
   deepen non-browser reach beyond the first desktop-shell continuity layer with better native follow-up control, clearer browser/native arbitration, and broader actionable recents
8. [ ] `world-model-memory-fusion-v8`:
   deepen durable project state with collaborator timelines, recurring obligations, routines, execution-memory fusion, and stronger long-horizon pressure synthesis that can hold commitments more coherently over time
9. [ ] `guardian-learning-policy-v8`:
   extend learning from the first timing/suppression/thread layer into stronger cooldown, escalation, channel, and context-conditioned intervention adaptation with clearer quality gates
10. [ ] `guardian-behavioral-evals-v8`:
   add deterministic contracts for richer step-level workflow recovery, broader capability bootstrap/repair, routing explainability and budgets, deeper learning, and broader native continuity

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
- [x] guardian cockpit as the active and only supported browser shell
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, desktop-notification fallback when browser delivery is unavailable, and a first actionable desktop control shell inside the cockpit
- [x] browser-side continuity controls for native notifications, including pending notification inspection, per-notification dismiss, bulk clear, cockpit-to-settings linkage for queued desktop state, and desktop-shell draft/continue actions over pending notifications, queued bundle items, and recent interventions
- [x] a unified continuity snapshot now ties daemon state, pending native notifications, deferred bundle items, and recent interventions into one browser-readable model across cockpit and settings surfaces
- [x] first capability-overview and starter-pack APIs now expose tools, skills, workflows, MCP servers, blocked-state reasons, recommended starter bundles, installable catalog items, repair actions, runbook metadata, and preflight-ready action payloads in one operator-readable shape
- [x] capability preflight now explains whether workflows, runbooks, and starter packs are ready, what will block them, and which safe repair actions can be applied before execution
- [x] starter packs and blocked workflows now also publish policy-aware recommended actions so the operator surface can repair real blockers instead of suggesting no-op activations
- [x] recent negative feedback on the same intervention type can now reduce interruption eagerness for similar future advisory nudges
- [x] aligned active-work signals now calibrate observer salience upward, and grounded high-salience nudges can cut through high interruption cost outside focus mode
- [x] the cockpit now supports a pane workspace with drag/resize, grid snap, packed `default` / `focus` / `review` layout presets, inspector visibility persistence, layout switching from both the header and keyboard shortcuts, plus per-layout save and reset behavior
- [x] 17 built-in tool capabilities exposed through the registry, with native and MCP-backed execution surfaces
- [x] first-class reusable workflow definitions loaded from defaults and workspace files, exposed through a workflows API, workflow metadata registry, and a dedicated `workflow_runner` specialist
- [x] starter packs now bundle default skills and workflows into obvious operator-invocable packages instead of leaving capability discovery entirely to disk inspection
- [x] first privileged-workflow hardening pass, including explicit workflow/tool execution-boundary metadata, richer approval behavior in tools/workflows APIs, and forced approval wrapping for approval-mode MCP workflow execution
- [x] second privileged-path hardening pass, including secret-ref containment to explicit injection-safe surfaces, workflow/operator metadata for secret-ref acceptance, and rejection of workflows whose runtime step tools are underdeclared
- [x] third workflow/operator hardening pass now carries workflow-run risk, approval, secret-ref, and execution-boundary context into replay/history surfaces so reruns remain boundary-aware
- [x] workflow approvals now expose fingerprints, resume context, and thread labels so approval recovery can align cleanly with replay history and browser threads
- [x] first operator workflow-control layer in the settings surface, including workflow enable/disable, reload, draft-to-cockpit flow control, and artifact path round-tripping back into the command bar
- [x] dedicated cockpit workflow-run views with richer workflow audit details, artifact-lineage linking, replay drafting, and workflow-specific inspector actions
- [x] cockpit artifacts and workflow outputs can now draft compatible follow-on workflows directly from the inspector instead of only seeding generic command-bar context
- [x] the cockpit now exposes a compact operator surface for workflow availability, starter packs, tools, skills, MCP server state, and live policy modes with direct reload and activation actions
- [x] the cockpit now restores the last active browser session on reload, tracks fresh-thread semantics explicitly, and marks background session activity instead of silently mixing threads
- [x] the cockpit now exposes a searchable Hermes-style capability palette plus a denser operator terminal with recommendations, repair actions, installable catalog items, reusable runbooks, and preflight-aware workflow drafting
- [x] the cockpit now also includes live operator-feed status, a threaded operator timeline, saved runbook macros, approval-aware workflow timeline actions, replay repair actions, and explicit continue/open-thread controls across approvals, workflow runs, native notifications, queued interventions, and surfaced failures
- [x] workflow history now behaves like a true operator timeline with timeline events, approval-recovery copy, replay guardrails, parameterized replay drafts, and explicit thread metadata for opening or continuing the relevant thread
- [x] browser sessions, desktop notifications, queued interventions, recent interventions, and workflow runs now share explicit thread metadata instead of leaving continuity implicit
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing across helper, scheduled, agent, delegation, and MCP-specialist paths
- [x] stricter provider-policy safeguards now cover required capability intents plus cost, latency, task-class, and budget guardrails that reroute only when compliant targets exist and otherwise fail open with explicit audit visibility
- [x] explicit guardian-state synthesis across chat, WebSocket, and strategist paths, combining observer context, salience/confidence signals, memory recall, session history, recent sessions, recent intervention feedback, and confidence into one structured downstream input
- [x] guardian-state synthesis now also carries learned communication guidance, and the world model now includes active routines, collaborators, recurring obligations, and project timeline context alongside projects, constraints, and execution pressure
- [x] explicit guardian world model now also carries recent active projects, active constraints, recurring patterns, collaborators, recurring obligations, project timelines, and recent execution pressure from workflow/tool outcomes, not only focus, commitments, and receptivity
- [x] guardian state now carries structured memory signals, continuity threads, and memory-degraded confidence instead of only plain-text recall context
- [x] explicit intervention policy at the proactive delivery boundary, with first-class act, bundle, defer, request-approval, and stay-silent classifications plus salience-aware policy reasons
- [x] persisted guardian intervention records with delivery outcomes, native-notification acknowledgements, and explicit feedback capture exposed back through guardian state
- [x] second outcome-learning layer now lets recent positive and acknowledged outcomes change direct-delivery timing, native-channel preference, and async-native escalation bias, not only interruption reduction
- [x] guardian learning now also emits timing, suppression, blocked-state, and thread-preference policy bias that can change bundle-versus-act decisions for advisory nudges
- [x] guardian behavioral proof now explicitly covers the calibrated high-salience deliver path versus degraded-confidence defer path at the proactive delivery gate
- [x] deeper guardian behavioral proof now also covers strategist tick learning its way into native-notification delivery and continuity-surface presence when high-signal learned nudges should bypass the browser
- [x] runtime audit visibility across chat, session-bound helper and agent LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths
- [x] deterministic eval harness coverage for core runtime, audit, REST and WebSocket chat behavior, guardian-state synthesis, guardian world-model behavior, guardian feedback loop behavior, calibrated salience/confidence delivery behavior, intervention policy behavior, observer refresh and delivery behavior, native desktop presence status plus the test-notification path, session consolidation behavior, tool/MCP guardrail behavior, proactive flow behavior, delegated workflow behavior, workflow composition behavior, observer, storage, tool-boundary, vault repository, MCP test API, skills API, screen repository, and daily-briefing, activity-digest, plus evening-review degraded-input contracts
- [x] deterministic eval harness coverage now also proves workflow approval threading, capability preflight/repair behavior, provider safeguard routing, strategist-learning continuity, and learned blocked-state/timing policy outcomes
- [x] denser guardian cockpit evidence surfaces with pending approvals, recent outputs, selectable intervention/audit/trace rows, an operations inspector that exposes linked details from the audit stream, and a pane workspace with packed layout presets, drag/resize, grid snap, and keyboard switching

## Recommended Reading Order

1. Read [Development Status](./STATUS.md) for the live shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read [08. Docs Contract](./08-docs-contract.md), [09. Benchmark Status](./09-benchmark-status.md), and [10. Superiority Delivery](./10-superiority-delivery.md) for the implementation-side mirrors of the research benchmark/program docs.
4. Read `01` through `07` for detailed per-workstream checklists.
5. Read the research docs for the benchmark and superiority target.
6. Treat `/legacy` docs as supporting history, not the live source of truth.
