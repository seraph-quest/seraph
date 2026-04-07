---
slug: /status
title: Seraph Development Status
---

# Seraph Development Status

Seraph is an AI guardian that remembers, watches, and acts. This page is the fastest answer to what is real on `develop` right now.

## Legend

- `[x]` shipped on `develop`
- `[ ]` not fully shipped on `develop`
- in-flight branch work should be tracked in open PRs, not in this file

When this file is updated on an open feature branch, it reflects the intended post-merge `develop` state for that branch. Until merge, the open PR and its validation are the live integration truth.

## Current Snapshot

- [x] Seraph is usable today as a real guardian workspace with a browser cockpit, memory, screen awareness, proactive behavior, and a real action layer.
- [x] The live truth surface is now `docs/research/` plus `docs/implementation/`, while the GitHub Project, issues, and PRs carry active execution state.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability have strong foundations on `develop`.
- [x] The target product shape is now a power-user guardian workspace, not a village-first shell.
- [x] The guardian workspace is the only supported browser shell; the village/editor line is removed from the active repo path and should not be revived.
- [x] The workspace now exposes capability discovery, starter packs, workflow history, step records, typed artifact-to-workflow handoff, truthful checkpoint branch control, branch-family supervision with latest-branch continue/open-parent controls, parameterized replay, reload continuity, a searchable capability palette, capability preflight/autorepair, a separate Activity Ledger window, a denser operator terminal, live operator feed, saved runbook macros, active triage, evidence shortcuts, workflow step-focus rows with direct step/output handoff, branch-origin and failure-lineage debugger rows with best-continuation control, family-history comparison plus family-output reuse/output-comparison rows, denser ancestor/peer/failure-lineage follow-through actions, direct family-row checkpoint drill-in and family-row step-recovery controls on best-continuation and related family rows, direct triage failure-context and recovery controls, keyboard-first inspect or approve or continue or redirect or failure/recovery/workflow/output/best-continuation/comparison flows, and explicit continue/open-thread controls instead of leaving those as implicit operator knowledge.
- [x] Seraph now also has a first adapter-backed external evidence path: typed source adapters, normalized evidence bundles, executable public-web discovery/page/session reads, and explicit degraded managed-connector truth instead of leaving source routines to infer execution from inventory alone.
- [x] Seraph now also has a first connector-backed authenticated source-read path: when a managed connector is enabled, configured, and matched to a live MCP runtime, Seraph can normalize repository, work-item, and code-activity evidence through the same source-evidence contract instead of leaving authenticated providers permanently stuck in inventory-only degraded mode.
- [x] Seraph now also has reusable connector-first source review routines: `plan_source_review`, bundled daily/progress/goal-alignment starter packs and runbooks, mixed-source review plans that stay provider-neutral, and explicit degraded-step truth instead of forcing each external review flow into bespoke provider glue.
- [x] Seraph now also has synthesized cross-surface recovery state: observer continuity groups pending follow-through by thread, summarizes degraded reach and pending recovery in one payload, and drives cockpit presence plus desktop-shell recovery actions instead of leaving those surfaces to infer state from raw notifications, queued items, and route rows.
- [x] Seraph now also carries imported capability-family attention and typed source-adapter degradation through that same observer continuity path, so broader reach problems show up in cockpit presence, desktop-shell recovery, and active triage instead of hiding in separate operator inventories.
- [x] The same broader reach continuity contract now also feeds the threaded operator timeline and Activity Ledger, so typed source-adapter and imported-reach recovery remains visible outside the raw observer endpoint and cockpit shell.
- [x] Onboarding can now inspect an explicitly user-linked webpage during the current onboarding turn, so Seraph can derive profile or workspace context from a real source without widening onboarding into general browsing.
- [x] The workspace window system now uses flatter terminal-style chrome with close controls, a Windows visibility menu, and per-pane hide/show state instead of only static rounded dashboard cards.
- [x] The capability import program is now complete through all five waves, including Hermes-style runtime primitives, packaged reach surfaces, selective OpenClaw imports, operator-surface visibility, and deterministic proof for the imported capability families.
- [ ] No workstream is complete yet.
- [ ] Seraph is not yet the finished guardian product described in the research docs.

## Docs Contract

- [x] `docs/research/00-synthesis.md` defines what Seraph is trying to become.
- [x] `docs/research/10-competitive-benchmark.md` owns the comparative judgment.
- [x] `docs/research/11-superiority-program.md` owns the design-level superiority program.
- [x] this file owns the fastest shipped snapshot on `develop`.
- [x] `docs/implementation/00-master-roadmap.md` owns the strategic implementation program and completed-program record.
- [x] `docs/implementation/08-docs-contract.md`, `docs/implementation/09-benchmark-status.md`, and `docs/implementation/10-superiority-delivery.md` are the implementation-side mirrors of the research evidence/benchmark/program docs.
- [x] `docs/implementation/01` through `07` remain the workstream docs; `08` through `10` are meta mirrors, not extra workstreams.
- [x] the GitHub Project, issues, and PRs own active execution and review state.

## Current Focus On `develop`

- [x] The extension-platform transition and five-wave capability import program are now represented in the shipped state on `develop`.
- [x] Seraph now ships Hermes-style runtime primitives (`execute_code`, `delegate_task`, `clarify`, `todo`, `session_search`) plus packaged browser, messaging, automation, node, canvas, and workflow-runtime surfaces through the extension architecture.
- [x] The workspace now makes imported capability reach, extension governance, and runtime-path/capability-family spend attribution visible inside the operator surface instead of leaving the new breadth opaque.
- [x] The runtime now exposes provider-neutral source contracts and source inventory for public-web tools, managed authenticated connectors, and raw MCP gaps, so Seraph can compose source-aware routines without hardcoding one provider pipeline per use case.
- [x] The runtime now also exposes reusable source-review planning for daily review, progress review, and goal-alignment review, so source routines can stay connector-first and provider-neutral even when one preferred adapter cannot satisfy every review step.
- [x] Authenticated MCP-backed sources now keep their source context through runtime wrappers, so operator metadata, approval context, and workflow checkpoint gating can distinguish generic external MCP from authenticated external-source execution.
- [x] Guardian memory now exposes a deeper additive memory-provider surface: extension-backed provider inventory, lifecycle-managed provider config/toggle state, capability-state governance, additive retrieval, additive user/project modeling augmentation, stale-provider-evidence suppression, advisory post-canonical provider writeback, and explicit canonical-memory precedence when external providers are configured or unavailable.
- [x] The cockpit now exposes denser workflow-operating control through step-focus summaries, direct failed-step context handoff, direct output reuse and output-comparison drafts from workflow family rows, explicit branch-origin and failure-lineage debugging, family-history comparison, best-continuation controls, denser ancestor/peer/failure-lineage follow-through actions, direct family-row checkpoint drill-in, direct family-row retry/repair controls, bundled family next-step planning drafts, and active-triage workflow quick actions including failure-context reuse, direct best-continuation control, direct recovery controls, and keyboard-first best-continuation/comparison follow-through instead of leaving those actions buried in the inspector.
- [x] The cockpit desktop shell and presence pane now also surface continuity health, grouped thread follow-through, top recovery actions, and recommended focus across browser/native reach instead of only raw route rows and continuity item lists.
- [x] The cockpit desktop shell, presence pane, and active triage now also surface degraded typed source adapters and imported capability-family attention from the observer continuity contract, so broader reach issues are actionable from the same operator flow as route failures and queued follow-through.
- [x] Runtime Reliability now has deterministic proof for activity-ledger attribution, imported capability surfaces, and simulation-grade route-planning visibility in addition to the earlier guardian/runtime contracts.
- [x] The repo-wide strategic program is tracked in `docs/implementation/00-master-roadmap.md`, while active execution is tracked in the GitHub Project, issues, and PRs.
- [x] The next strategic focus is now post-import hardening: deeper execution isolation, denser operator/debug ergonomics, production-grade reach hardening, and stronger guardian learning on top of the expanded capability surface.

## Current Target Shape

- [x] dense guardian workspace as the primary operator surface
- [x] first clear capability discovery, activation, preflight, and repair for tools, skills, workflows, MCP surfaces, starter packs, installable catalog items, and runbooks from inside that cockpit
- [x] bounded capability bootstrap that can apply low-risk local enable or repair actions from the operator surface while leaving policy lifts, external server enables, installs, and starter-pack activation as explicit operator repair steps, with multi-step privileged repair bundles now gated behind step-by-step execution instead of one-click chaining
- [x] cockpit-native extension authoring and validation for workflows, skills, and MCP configs with diagnostics, save, and repair handoff
- [x] first browser reload and reconnect continuity for the active thread, with explicit fresh-thread semantics and background-activity badges
- [x] explicit cross-surface thread model that links approvals, workflow runs, notifications, queued interventions, and recent interventions back to browser threads
- [x] runtime reach snapshots now show whether browser websocket and native delivery are actually reachable, which route is falling back, and how queued/native continuity should resume instead of leaving reach health implicit
- [x] activity-ledger routing summaries, native thread metadata, and LLM spend attribution that make both live continuation state and day-scale budget use easier to inspect
- [x] packaged browser providers, messaging connectors, automation triggers, node adapters, canvas outputs, workflow runtimes, and channel-routing surfaces that stay visible and governed through the same extension lifecycle
- [x] typed longitudinal memory and explicit guardian state
- [x] additive external memory-provider retrieval, user/project augmentation, and provider governance inventory that can augment recall and live modeling without replacing canonical guardian memory
- [x] policy-driven interventions with clear defer / bundle / act / request-approval decisions
- [x] non-browser presence through a first coherent desktop surface, notifications, native reach, and action-card continuation payloads
- [x] reusable workflow composition plus explicit feedback capture and future improvement loops
- [x] workflow diagnostics with stored load errors, step timestamps and durations, error summaries, and recovery hints
- [x] first branch/resume workflow control with checkpoint candidates, lineage metadata, persisted reusable checkpoint state, truthful unsupported-checkpoint fallback, branch-family supervision in the cockpit, resume drafts based on existing inputs, and safer approval-gated resume plans

## Shipped On `develop`

### Core guardian platform

- [x] browser-based guardian workspace as the only supported browser shell
- [x] FastAPI backend with chat, WebSocket, goals, tools, observer, settings, audit, approvals, vault, skills, and MCP APIs
- [x] native macOS observer daemon for screen/window ingest
- [x] persistent guardian record, vector memory, sessions, and goal storage

### Trust and control

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP policy modes for `disabled`, `approval`, and `full`
- [x] approval-gated high-risk actions in chat and WebSocket flows
- [x] explicit execution-boundary metadata and approval behavior surfaced for tools and reusable workflows
- [x] structured audit logging for approval, tool, and runtime events
- [x] secret redaction and scoped secret-reference handling
- [x] secret-reference resolution now stays limited to explicit injection-safe surfaces instead of resolving into arbitrary tool calls
- [x] delegation now keeps generic memory handling separate from vault-backed secret management, with explicit secret/vault routing into a dedicated privileged specialist and deterministic eval coverage for that boundary
- [x] authenticated MCP-backed tools now carry source-aware boundary context through approval/audit wrapping, and workflow checkpoint reuse blocks authenticated external-source runs instead of treating them like generic MCP replay

### Execution and integrations

- [x] 17 built-in tool capabilities in the registry
- [x] first capability-overview API that aggregates tools, skills, workflows, MCP servers, blocked-state reasons, and starter packs for one cockpit-readable surface
- [x] capability-overview now also exposes installable catalog items, repair/install actions, recommendations, reusable runbooks, policy-aware starter-pack repair guidance, and machine-readable preflight/autorepair metadata for cockpit/operator use
- [x] shell execution via sandboxed tool path
- [x] browser automation foundation
- [x] filesystem, guardian-record, goals, vault, and web-search tool foundations
- [x] MCP server management and runtime-managed server configuration, with manual bearer-token updates now landing in vault-backed placeholders instead of raw config headers and MCP connect/test audit surfacing the credential source
- [x] execute-code, clarify, todo, session-search, and first-class delegation runtime primitives for deeper Hermes-style operator work
- [x] visible tool execution streaming in chat and agent flows
- [x] first-class reusable workflows loaded from defaults and workspace files, exposed through a workflows API and `workflow_runner` specialist
- [x] starter packs that bundle default skills and workflows into directly activatable operator-facing packages
- [x] skill registry flows, optional skill packs, packaged browser providers, messaging connectors, automation triggers, node adapters, canvas outputs, workflow runtimes, and channel-routing-managed reach through the extension platform
- [x] forced approval wrapping for high-risk and approval-mode MCP workflow paths
- [x] first operator workflow-control layer with workflow list/toggle/reload plus draft-to-cockpit support
- [x] workflow loader/runtime metadata now derive from actual step tools and reject underdeclared workflow definitions
- [x] workflow audit now surfaces structured workflow-run details for cockpit/operator views, including artifact-path lineage and degraded-step visibility
- [x] workflow history endpoint now exposes run arguments, risk level, execution boundaries, approval counts, secret-ref acceptance, and artifact lineage for replay and operator inspection
- [x] workflow history endpoint now also exposes timeline events, replay guardrails, parameterized replay drafts, approval-recovery messaging, pending-approval details, and explicit thread metadata for replay/open-thread control
- [x] workflow approvals and workflow history now persist approval-context snapshots plus context-aware run fingerprints, so replay/resume and approval reuse fail closed when the privileged workflow surface changes
- [x] workflow runtime and history now persist reusable checkpoint context for safe branches, record structured failed-step payloads, and surface checkpoint availability truthfully so the runs API stops advertising retry paths the runtime cannot actually resume
- [x] cockpit workflow supervision now derives parent/peer/child branch families from persisted lineage, so operators can inspect branch families and continue the latest branch directly from the workflow surface instead of reconstructing lineage manually
- [x] operator timeline API now unifies workflow runs, approvals, notifications, queued insights, recent interventions, and surfaced failures into one threaded live operator feed
- [x] activity ledger API now projects workflow runs, approvals, guardian events, audit activity, tool steps, and attributed LLM call spend into one separate accountability feed for the browser workspace
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
- [x] first simulation-grade provider planning pass that scores candidate routes before execution, makes budget steering explicit, and surfaces route scores plus simulated-route explanations through runtime audit, operator timeline, and activity-ledger views
- [x] runtime audit visibility across chat, WebSocket, session-bound helper LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallback paths, strategist, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, observer plus screen observation summary/cleanup boundaries, embedding, vector store, guardian-record file, vault repository, filesystem, browser, sandbox, and web search flows
- [x] deterministic runtime eval harness for fallback, routing, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, workflow composition behavior, storage, observer, and integration seam contracts, including vault repository, the MCP test API, skills API, screen repository boundaries, and daily-briefing, activity-digest, plus evening-review degraded-input audit behavior
- [x] deterministic runtime eval coverage for activity-ledger attribution and imported capability surfaces, so runtime-path spend, packaged reach visibility, and extension-governance surfaces stay pinned on `develop`
- [x] deterministic runtime eval coverage now also pins cross-surface continuity through observer, operator, and Activity Ledger together, and backend CI now runs isolated per-file shard execution instead of one shard-wide pytest process.

### Guardian intelligence and proactive behavior

- [x] guardian-record-backed persistent identity
- [x] vector memory retrieval and consolidation
- [x] hierarchical goals and progress APIs
- [x] explicit guardian-state synthesis for chat, WebSocket, and strategist paths
- [x] guardian world model now includes active projects, active constraints, recurring patterns, active routines, collaborators, recurring obligations, project timelines, memory signals, continuity threads, recent execution pressure from degraded workflow/tool outcomes, focus provenance, and judgment risks, not only focus, commitments, open loops or pressure, alignment, and receptivity
- [x] observer salience, confidence, and interruption-cost scoring for observer refresh, guardian state, and proactive policy
- [x] explicit intervention-policy decisions for proactive delivery, including act / bundle / defer / request-approval / stay-silent classifications
- [x] persisted guardian intervention outcome tracking plus explicit feedback capture, including notification acknowledgement and feedback API flows
- [x] evidence-weighted guardian learning that now resolves the strongest live guidance across global, thread, project, and thread-plus-project scopes before arbitrating with durable procedural memory at policy time, can prefer direct delivery or reduce interruptions from weighted delivery evidence, strengthens timing/channel/blocked-state lessons from actual routing outcomes, and emits grounded channel, escalation, timing, suppression, and blocked-state guidance back into guardian state and intervention policy while leaving phrasing/cadence/thread neutral until runtime records those variants explicitly
- [x] long-horizon guardian learning now also treats stale collaborator/obligation/timeline recall and stale execution pressure as contradiction sources against the live project anchor, promotes execution pressure into blocker/open-loop synthesis, and lowers receptivity further when those conflicts line up with negative intervention trends
- [x] guardian world-model synthesis now also arbitrates between competing projects across observer, recent-session, memory, and execution evidence, carries richer canonical project labels forward, and exposes project-anchor ambiguity or drift risk when cross-source evidence stays split
- [x] guardian world-model synthesis now also carries live-project cross-thread commitments forward from recent sessions and turns matching execution setbacks into explicit follow-through risk on the same live project instead of leaving that linkage trapped in separate recent-session and audit surfaces
- [x] second-layer salience calibration that promotes aligned active-work signals and allows grounded high-salience nudges to cut through generic high-interruption bundling outside focus mode
- [x] deterministic guardian behavioral proof that grounded high-salience observer state can still deliver through high interruption cost while degraded observer confidence defers before transport
- [x] deterministic guardian behavioral proof that strategist tick can use learned direct/native-delivery bias and still surface the resulting intervention through continuity state
- [x] strategist agent and strategist scheduler tick
- [x] daily briefing, evening review, activity digest, and weekly review surfaces
- [x] observer refresh across time, calendar, git, goals, and screen context
- [x] proactive delivery gating and queued-bundle behavior
- [x] first coherent desktop presence surface with daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, native-notification fallback delivery when browser sockets are unavailable but the daemon is connected, browser-side inspect/dismiss controls for queued desktop notifications, runtime route-health visibility for ready/fallback/unavailable delivery, a unified continuity snapshot for daemon state, queued bundle items, route reachability, and recent interventions, and an actionable cockpit desktop-shell card for follow-up, dismiss, continue, and fallback inspection flows
- [x] queued native bundles now preserve same-thread resume when every deferred item shares one session, and queued continuity keeps the stored `session_id` even when the matching intervention is no longer in the recent-intervention window

### Current interface surface

- [x] workspace-first browser guardian shell with session rail, guardian-state panel, workflow-run views, interventions feed, audit surface, trace view, pending approvals, recent outputs, operations inspector, artifact round-trip into the command bar, a fixed composer, and live send fallback
- [x] cockpit workflow and artifact inspectors can now draft compatible follow-on workflows directly from existing artifact paths by declared artifact-input type instead of only inserting generic file-context commands
- [x] cockpit workflow controls now show checkpoint-state visibility plus real branch/retry actions driven by persisted checkpoint candidates instead of generic retry copy
- [x] workflow rows and inspectors can now carry attached approval decisions directly, so operators can approve, deny, continue, or open the relevant thread from the workflow surface instead of jumping to a separate approval pane first
- [x] workflow rows and inspectors now also surface branch-family supervision with parent/peer/child run inspection plus latest-branch continue/open-parent actions instead of leaving branch lineage buried in raw metadata
- [x] grid-snapped draggable panes plus packed persisted `default` / `focus` / `review` layouts with keyboard switching, per-layout save, and per-layout reset now define the main cockpit workspace
- [x] the pane workspace now also supports per-pane close/hide controls, a dedicated Windows menu for visibility and focus, and flatter Godel-style window chrome
- [x] the cockpit now includes a first desktop-shell rail for pending native notifications, queued bundle items, and recent interventions with direct follow-up, continue, and dismiss controls
- [x] cockpit desktop-shell and settings surfaces now also expose shared route-health summaries and continuation metadata for queued/native delivery instead of reconstructing fallback state per surface
- [x] the cockpit now includes a first operator surface for tool/MCP policy state, workflow availability, tools, skills, starter packs, and MCP server visibility with direct reload and activation controls
- [x] the cockpit now also includes a searchable capability palette plus a denser operator terminal for recommendations, repair actions, installable items, reusable runbooks, capability preflight, live operator-feed status, and saved runbook macros
- [x] the workspace now includes a separate Activity Ledger window that links workflow runs, approvals, queued continuity, recent interventions, surfaced failures, tool steps, and attributed LLM calls back to one browser thread model
- [x] activity ledger rows now group request-scoped work into compact parent rows with emoji/icon scanability, child tool or routing rows, and completion summaries so operators can skim what Seraph did without opening raw trace panes
- [x] the operator terminal now also surfaces imported capability reach and extension-governance state, while the activity ledger now shows top runtime-path and capability-family spend buckets for attributed LLM use
- [x] the operator terminal now also surfaces active triage for approvals, workflow branch families, queued guardian items, and degraded reach plus evidence shortcuts for approval context, artifact lineage, and recent trace, with keyboard-first inspect, approve, continue, open-thread, and redirect control over the highest-priority items
- [x] the cockpit now restores the last active session on reload, preserves explicit fresh-thread semantics, and marks background thread activity instead of silently resetting to an empty conversation
- [x] larger more readable settings and priorities overlays now support the guardian workspace directly
- [x] capability state, workflow history, the activity ledger, and live status are now visible in the current cockpit surface
- [x] settings and management surfaces for tools, MCP, and system state
- [x] macOS daemon-backed desktop presence card plus browser-side inspect/dismiss controls for native notifications and notification fallback for non-browser proactive reach

### Ecosystem foundations

- [x] `SKILL.md` support and runtime skill loading
- [x] MCP-powered extension surface
- [x] recursive delegation foundations behind a flag
- [x] reusable workflow runtime with tool, skill, specialist, and MCP-aware gating

## Still To Do On `develop`

### Runtime and execution

- [ ] deeper provider planning beyond the first simulation-grade route scoring and explicit budget steering pass, especially with stronger production-like failure modeling and live-provider feedback
- [ ] broader live-provider and long-running integration eval coverage beyond the shipped deterministic REST, WebSocket, observer, delivery, activity-ledger, imported-capability, tool/MCP guardrail, delegated workflow, and workflow-composition contracts
- [ ] stronger execution isolation and privileged-path hardening beyond the current workflow/tool, browser-mode, and connector-boundary pass
- [ ] richer capability installation, recommendation, and recovery beyond the shipped catalog/install, runbook preflight, bounded bootstrap flow, extension studio, and imported-reach governance surfaces

### Guardian intelligence

- [ ] stronger learning and feedback loops beyond the current evidence-weighted delivery/channel/timing/blocked-state/suppression layer and first live-versus-durable procedural arbitration pass
- [ ] deeper guardian world modeling, learning loops, and stronger intervention quality beyond the new project/routine/collaborator/obligation-aware world-model layer, the first additive provider-backed user/project augmentation pass, and the first focus-provenance plus contradiction-aware confidence pass
- [ ] stronger salience calibration and confidence quality beyond the first aligned-work/high-salience pass

### Interface and presence

- [ ] richer cockpit density and broader keyboard/operator control beyond the current workflow, activity-ledger, imported-reach, governance, active-triage, evidence-shortcut, and first keyboard-first command surfaces
- [ ] richer cross-surface continuity and broader non-browser presence beyond the current desktop presence shell, runtime route-health snapshot, channel routing, messaging connectors, browser mode matrix, automation triggers, node adapters, and canvas outputs
- [ ] stronger explicit threading between ambient observation, workflow runs, native notifications, approvals, and deliberate interaction beyond the new shared thread metadata and continue/open-thread layer

### Workflow and leverage

- [ ] deeper operator-facing workflow control and workflow history beyond the new workflow-runs API, typed artifact handoff, checkpoint branch controls, replay guardrails, timeline events, and cockpit workflow timeline
- [ ] stronger extension ergonomics around reusable capabilities and workflows beyond the new cockpit operator surface, starter packs, repair flows, and runbooks

## Practical Summary

- [x] Seraph already has a serious guardian core: memory, observer loop, strategy, tools, approvals, runtime audit, and deterministic evals.
- [x] The strongest current moat is guardian-oriented state plus proactive scaffolding, not the UI.
- [ ] The biggest gaps against the reference systems are now deeper execution hardening, denser step-level workflow/operator debugging beyond the new triage and evidence-control layer, stronger intervention learning beyond the current world-model plus evidence-weighted timing/channel/blocked-state/suppression layer, and production-grade hardening for the broadened reach surface.
- [ ] The next major step is to harden and generalize the imported capability surface around atomic capability contracts, adapter-backed authenticated source access, and stronger execution boundaries without losing the existing guardian trust and memory foundations.

## Workstream View

- [ ] Workstream 01: Trust Boundaries is only partially complete
- [ ] Workstream 02: Execution Plane is only partially complete
- [ ] Workstream 03: Runtime Reliability is only partially complete
- [ ] Workstream 04: Presence And Reach is only partially complete
- [ ] Workstream 05: Guardian Intelligence is only partially complete
- [ ] Workstream 06: Embodied Interface is only partially complete
- [ ] Workstream 07: Ecosystem And Delegation is only partially complete
