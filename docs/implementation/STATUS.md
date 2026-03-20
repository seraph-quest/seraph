---
slug: /status
title: Seraph Development Status
---

# Seraph Development Status

## Legend

- `[x]` shipped on `develop`
- `[ ]` not fully shipped on `develop`
- in-flight branch work should be tracked in open PRs, not in this file

When this file is updated on an open feature branch, it reflects the intended post-merge `develop` state for that branch. Until merge, the open PR and its validation are the live integration truth.

## Current Snapshot

- [x] Seraph is usable today as a local guardian prototype with a real UI, memory, observer loop, and action layer.
- [x] The live planning surface is now `docs/research/` plus `docs/implementation/`.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability have strong foundations on `develop`.
- [x] The target product shape is now a power-user guardian cockpit, not a village-first shell.
- [x] The guardian cockpit is the only supported browser shell; the village/editor line is removed from the active repo path and should not be revived.
- [x] The cockpit now exposes capability discovery, starter packs, workflow history, step records, retry-from-step recovery, parameterized replay, reload continuity, a searchable capability palette, capability preflight/autorepair, a threaded operator timeline, a denser operator terminal, live operator feed, saved runbook macros, and explicit continue/open-thread controls instead of leaving those as implicit operator knowledge.
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

- [x] The latest delivery batch is now complete for the current roadmap horizon: capability bootstrap v3, extension studio v1, workflow branching/resume v1, cockpit density v4, provider explainability/budgets v3, execution hardening v9, native-channel expansion v5, world-model fusion v9, guardian-learning policy v9, and guardian behavioral evals v9 all landed together.
- [x] The roadmap has now refreshed to a new next-10 batch rather than leaving the just-shipped batch as future work.
- [x] Guardian Intelligence remains central inside the current batch, but it is no longer the only active workstream.
- [x] Runtime Reliability now has a strong baseline on `develop`, but it is not fully complete.
- [x] The repo-wide 10-PR horizon is tracked in `docs/implementation/00-master-roadmap.md`.
- [x] The next strategic focus is now `capability-marketplace-and-versioned-packs-v1`, because Seraph now exposes meaningful capability discovery, install-doctor bootstrap, extension authoring, and first branch/resume control, so the next biggest capability gain is safer versioned package distribution plus deeper studio and debugger depth.
- [x] `capability-pack-autoinstall-and-bootstrap-v3`, `extension-authoring-and-validation-studio-v1`, `workflow-step-branching-and-resume-v1`, `cockpit-density-and-live-operator-views-v4`, `provider-policy-explainability-and-budgets-v3`, `execution-safety-hardening-v9`, `native-channel-expansion-v5`, `world-model-memory-fusion-v9`, `guardian-learning-policy-v9`, and `guardian-behavioral-evals-v9` are now represented in the shipped state this branch is preparing to merge.
- [x] The published 10-PR horizon should be refreshed whenever landed PR count from that queue is divisible by 5.

## Current Target Shape

- [x] dense guardian cockpit as the primary operator surface
- [x] first clear capability discovery, activation, preflight, and repair for tools, skills, workflows, MCP surfaces, starter packs, installable catalog items, and runbooks from inside that cockpit
- [x] bounded capability bootstrap that can apply safe install or repair actions for workflows, runbooks, and starter packs directly from the operator surface
- [x] cockpit-native extension authoring and validation for workflows, skills, and MCP configs with diagnostics, save, and repair handoff
- [x] first browser reload and reconnect continuity for the active thread, with explicit fresh-thread semantics and background-activity badges
- [x] explicit cross-surface thread model that links approvals, workflow runs, notifications, queued interventions, and recent interventions back to browser threads
- [x] operator timeline routing summaries and native thread metadata that make live continuation state easier to inspect
- [x] typed longitudinal memory and explicit guardian state
- [x] policy-driven interventions with clear defer / bundle / act / request-approval decisions
- [x] non-browser presence through a first coherent desktop surface, notifications, native reach, and action-card continuation payloads
- [x] reusable workflow composition plus explicit feedback capture and future improvement loops
- [x] workflow diagnostics with stored load errors, step timestamps and durations, error summaries, and recovery hints
- [x] first branch/resume workflow control with checkpoint candidates, lineage metadata, resume drafts based on existing inputs, and safer approval-gated resume plans

## Shipped On `develop`

### Core guardian platform

- [x] browser-based guardian cockpit as the only supported browser shell
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
- [x] first capability-overview API that aggregates tools, skills, workflows, MCP servers, blocked-state reasons, and starter packs for one cockpit-readable surface
- [x] capability-overview now also exposes installable catalog items, repair/install actions, recommendations, reusable runbooks, policy-aware starter-pack repair guidance, and machine-readable preflight/autorepair metadata for cockpit/operator use
- [x] shell execution via sandboxed tool path
- [x] browser automation foundation
- [x] filesystem, soul, goals, vault, and web-search tool foundations
- [x] MCP server management and runtime-managed server configuration
- [x] visible tool execution streaming in chat and agent flows
- [x] first-class reusable workflows loaded from defaults and workspace files, exposed through a workflows API and `workflow_runner` specialist
- [x] starter packs that bundle default skills and workflows into directly activatable operator-facing packages
- [x] forced approval wrapping for high-risk and approval-mode MCP workflow paths
- [x] first operator workflow-control layer with workflow list/toggle/reload plus draft-to-cockpit support
- [x] workflow loader/runtime metadata now derive from actual step tools and reject underdeclared workflow definitions
- [x] workflow audit now surfaces structured workflow-run details for cockpit/operator views, including artifact-path lineage and degraded-step visibility
- [x] workflow history endpoint now exposes run arguments, risk level, execution boundaries, approval counts, secret-ref acceptance, and artifact lineage for replay and operator inspection
- [x] workflow history endpoint now also exposes timeline events, replay guardrails, parameterized replay drafts, approval-recovery messaging, pending-approval details, and explicit thread metadata for replay/open-thread control
- [x] operator timeline API now unifies workflow runs, approvals, notifications, queued insights, recent interventions, and surfaced failures into one threaded live operator feed
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
- [x] strict runtime-path provider safeguards for required capability intents plus cost, latency, task-class, and budget guardrails, with explicit degrade-open audit semantics when no compliant target exists
- [x] runtime audit visibility across chat, WebSocket, session-bound helper LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallback paths, strategist, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, observer plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search flows
- [x] deterministic runtime eval harness for fallback, routing, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, workflow composition behavior, storage, observer, and integration seam contracts, including vault repository, the MCP test API, skills API, screen repository boundaries, and daily-briefing, activity-digest, plus evening-review degraded-input audit behavior

### Guardian intelligence and proactive behavior

- [x] soul-backed persistent identity
- [x] vector memory retrieval and consolidation
- [x] hierarchical goals and progress APIs
- [x] explicit guardian-state synthesis for chat, WebSocket, and strategist paths
- [x] guardian world model now includes active projects, active constraints, recurring patterns, active routines, collaborators, recurring obligations, project timelines, memory signals, continuity threads, and recent execution pressure from degraded workflow/tool outcomes, not only focus, commitments, open loops or pressure, alignment, and receptivity
- [x] observer salience, confidence, and interruption-cost scoring for observer refresh, guardian state, and proactive policy
- [x] explicit intervention-policy decisions for proactive delivery, including act / bundle / defer / request-approval / stay-silent classifications
- [x] persisted guardian intervention outcome tracking plus explicit feedback capture, including notification acknowledgement and feedback API flows
- [x] first multi-signal guardian learning loop that can reduce interruption eagerness after negative outcomes, prefer direct delivery, native reroute, and async-native escalation after repeated positive/acknowledged outcomes, and now also emit phrasing, cadence, timing, suppression, blocked-state, and thread guidance back into guardian state and intervention policy
- [x] second-layer salience calibration that promotes aligned active-work signals and allows grounded high-salience nudges to cut through generic high-interruption bundling outside focus mode
- [x] deterministic guardian behavioral proof that grounded high-salience observer state can still deliver through high interruption cost while degraded observer confidence defers before transport
- [x] deterministic guardian behavioral proof that strategist tick can use learned direct/native-delivery bias and still surface the resulting intervention through continuity state
- [x] strategist agent and strategist scheduler tick
- [x] daily briefing, evening review, activity digest, and weekly review surfaces
- [x] observer refresh across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle behavior
- [x] first coherent desktop presence surface with daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, native-notification fallback delivery when browser sockets are unavailable but the daemon is connected, browser-side inspect/dismiss controls for queued desktop notifications, a unified continuity snapshot for daemon state, queued bundle items, and recent interventions, and an actionable cockpit desktop-shell card for follow-up, dismiss, and continue flows

### Current interface surface

- [x] cockpit-first browser guardian shell with session rail, guardian-state panel, workflow-run views, interventions feed, audit surface, trace view, pending approvals, recent outputs, operations inspector, artifact round-trip into the command bar, a fixed composer, and live send fallback
- [x] cockpit workflow and artifact inspectors can now draft compatible follow-on workflows directly from existing artifact paths instead of only inserting generic file-context commands
- [x] grid-snapped draggable panes plus packed persisted `default` / `focus` / `review` layouts with keyboard switching, per-layout save, and per-layout reset now define the main cockpit workspace
- [x] the cockpit now includes a first desktop-shell rail for pending native notifications, queued bundle items, and recent interventions with direct follow-up, continue, and dismiss controls
- [x] the cockpit now includes a first operator surface for tool/MCP policy state, workflow availability, tools, skills, starter packs, and MCP server visibility with direct reload and activation controls
- [x] the cockpit now also includes a searchable capability palette plus a denser operator terminal for recommendations, repair actions, installable items, reusable runbooks, capability preflight, live operator-feed status, and saved runbook macros
- [x] the cockpit now includes a threaded operator timeline that links workflow runs, approvals, queued continuity, recent interventions, and surfaced failures back to one browser thread model
- [x] the cockpit now restores the last active session on reload, preserves explicit fresh-thread semantics, and marks background thread activity instead of silently resetting to an empty conversation
- [x] larger more readable settings and goals overlays now support the cockpit-first shell directly
- [x] capability state, workflow history, operator timelines, and live status are now visible in the current cockpit surface
- [x] settings and management surfaces for tools, MCP, and system state
- [x] macOS daemon-backed desktop presence card plus browser-side inspect/dismiss controls for native notifications and notification fallback for non-browser proactive reach

### Ecosystem foundations

- [x] `SKILL.md` support and runtime skill loading
- [x] MCP-powered extension surface
- [x] recursive delegation foundations behind a flag
- [x] reusable workflow runtime with tool, skill, specialist, and MCP-aware gating

## Still To Do On `develop`

### Runtime and execution

- [ ] richer provider selection policy beyond the shipped weighted scoring, required capability safeguards, tier guardrails, path patterns, explicit overrides, ordered fallbacks, and cooldown rerouting
- [ ] broader eval coverage beyond the shipped REST, WebSocket, observer refresh, delivery policy, salience/confidence delivery, strategist-learning continuity, consolidation, proactive, tool/MCP guardrail, delegated workflow, and workflow-composition behavioral contracts
- [ ] stronger execution isolation and privileged-path hardening beyond the first workflow/tool boundary pass
- [ ] richer capability installation, recommendation, and recovery beyond the new starter-pack repair guidance, catalog-install, runbook preflight, bounded bootstrap flow, and first cockpit-native extension studio

### Guardian intelligence

- [ ] stronger learning and feedback loops beyond the first multi-signal delivery/channel/timing/suppression/thread layer
- [ ] deeper guardian world modeling, learning loops, and stronger intervention quality beyond the new project/routine/collaborator/obligation-aware world-model layer
- [ ] stronger salience calibration and confidence quality beyond the first aligned-work/high-salience pass

### Interface and presence

- [ ] richer cockpit density and broader keyboard/operator control beyond the first dedicated workflow-run shell
- [ ] richer cross-surface continuity and broader non-browser presence beyond the new continuity snapshot, action-card continuation model, and first actionable desktop-shell/browser-native control layer
- [ ] stronger explicit threading between ambient observation, workflow runs, native notifications, approvals, and deliberate interaction beyond the new shared thread metadata and continue/open-thread layer

### Workflow and leverage

- [ ] deeper operator-facing workflow control and workflow history beyond the new workflow-runs API, replay guardrails, timeline events, and cockpit workflow timeline
- [ ] stronger extension ergonomics around reusable capabilities and workflows beyond the new cockpit operator surface, starter packs, repair flows, and runbooks

## Practical Summary

- [x] Seraph already has a serious local guardian core: memory, observer loop, strategy, tools, approvals, runtime audit, and deterministic evals.
- [x] The strongest current moat is guardian-oriented state plus proactive scaffolding, not the UI.
- [ ] The biggest gaps against the reference systems are versioned capability distribution, deeper extension-studio ergonomics, visual workflow branch debugging, deeper execution hardening, stronger intervention learning beyond the new world-model plus timing/suppression/thread layer, and broader native reach.
- [ ] The next major step is to deepen the new cockpit shell into a denser, more legible, more stateful guardian workspace without losing the existing trust and memory foundations.

## Workstream View

- [ ] Workstream 01: Trust Boundaries is only partially complete
- [ ] Workstream 02: Execution Plane is only partially complete
- [ ] Workstream 03: Runtime Reliability is only partially complete
- [ ] Workstream 04: Presence And Reach is only partially complete
- [ ] Workstream 05: Guardian Intelligence is only partially complete
- [ ] Workstream 06: Embodied Interface is only partially complete
- [ ] Workstream 07: Ecosystem And Delegation is only partially complete
