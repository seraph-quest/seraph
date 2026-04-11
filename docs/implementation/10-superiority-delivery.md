---
title: 10. Superiority Delivery
---

# 10. Superiority Delivery

## Status On `develop`

- [ ] The superiority program is only partially translated into shipped implementation on `develop`.

## Paired Research

- design source of truth: [11. Superiority Program](/research/superiority-program)
- benchmark input: [10. Competitive Benchmark](/research/competitive-benchmark)
- synthesis context: [00. Research Synthesis](/research)

## Purpose

This file is the implementation-side mirror of the superiority program.

Research explains why Seraph should win and what “superior” means.
This file explains:

1. what parts of that program are already shipped on `develop`
2. what still needs to be built
3. which workstreams own each remaining gap
4. how active execution should be tracked without duplicating this translation layer

- [x] post-import hardening on `develop` now includes bounded capability bootstrap, risk-based catalog install approval reuse, and a step-by-step gate for generated privileged repair bundles instead of one-click mutation chains
- [x] benchmark proof on `develop` now also has an operator-visible suite/report layer, a dedicated guardian-memory benchmark with contradiction/forgetting diagnostics, a dedicated workflow-endurance benchmark with anticipatory-repair and backup-branch failure taxonomy, a dedicated trust-boundary benchmark with operator safety receipts and failure taxonomy, and explicit governed-improvement gate policy instead of leaving deterministic proof distributed across raw eval-scenario names

## Docs Contract

- [x] `docs/research/00-synthesis.md` defines the target product shape.
- [x] `docs/research/10-competitive-benchmark.md` owns the evidence-backed comparison.
- [x] `docs/research/11-superiority-program.md` owns the design-level program of work.
- [x] `docs/implementation/STATUS.md` owns the fastest shipped snapshot.
- [x] `docs/implementation/00-master-roadmap.md` owns the strategic implementation program and completed-program record.
- [x] the GitHub Project, issues, and PRs own live execution, review, and merge state.
- [x] This file owns the translation from benchmark/program gaps into implementation workstreams and delivery ownership.

## Translation On `develop`

### 1. Guardian state and human model

- [x] shipped foundations: soul, vector memory, goals, observer context, first observer salience/confidence/interruption-cost scoring, explicit guardian-state synthesis, a structured world-model layer for focus, focus provenance, commitments, active projects, active constraints, recurring patterns, active routines, collaborators, recurring obligations, project timelines, memory signals, continuity threads, recent execution pressure, receptivity, and judgment risks, plus learned communication guidance carried back into guardian state, first cross-source project arbitration when several projects compete, explicit project-ranking diagnostics plus stale-signal arbitration, explicit live-versus-procedural learning-conflict diagnostics, conservative ambiguity guardrails when anchor competition lines up with negative trends, first explicit user-model confidence plus preference-inference diagnostics for interruption or communication or thread or cadence posture, explicit user-model evidence facets plus action-posture/restraint-reason state, explicit intent-uncertainty guidance for clarify-versus-caution resolution, explicit judgment-proof lines for split evidence and ambiguous referents, an operator-visible guardian-state surface for confidence and explanation, first multi-day plus scheduled-review watchpoints for goal alignment, routines, and collaborators, a dedicated guardian-memory benchmark suite for reasoning-heavy retrieval, contradiction-aware ranking, selective forgetting, and operator-visible failure reporting, and a dedicated guardian user-model/restraint benchmark suite for continuity-aware user modeling, ambiguity-aware clarification, and restraint-before-action proof
- [ ] still missing: stronger human/world modeling quality, richer multi-signal learning, and deeper additive memory-provider follow-through beyond the new project/routine/collaborator/obligation-aware world-model, the explicit project-ranking and stale-signal arbitration layer, the explicit user-model substrate, the explicit action-posture/restraint contract, the explicit intent-uncertainty substrate, the first goal-alignment plus routine/collaborator watchpoint pass, the first provider-backed user/project augmentation pass, the new provider-neutral adapter model plus canonical-memory/provenance contract and guarded sync policy, the first usefulness-ranked provider diagnostics, query-matched canonical project hints for provider-backed user-model activation, the new explicit canonical-memory reconciliation diagnostics, the guarded post-canonical writeback pass that now also suppresses project-anchorless project-scoped mirrors, contradiction-aware confidence, the new guardian-memory benchmark proof, and first guidance layer
- owners: Workstream 05

### 2. Intervention quality and timing

- [x] shipped foundations: strategist, proactive scheduler surfaces, explicit intervention policy, queued bundles, salience-aware interruption gating, persisted intervention outcomes, explicit feedback capture, first coherent desktop presence plus native notification fallback, evidence-weighted live-versus-procedural guardian learning that can now quiet, accelerate, reroute, bias async-native escalation, and nudge timing, suppression, blocked-state, and grounded channel choices, scoped live learning resolution across global, thread, project, and thread-plus-project context, scoped procedural-memory routing by continuity thread and active project, calibrated high-salience timing overrides, inspectable learning diagnostics including conflict/override receipts between live outcomes and procedural guidance, explicit guardian-state intent-uncertainty guidance for clarify-versus-caution decisions, explicit action-posture plus restraint-reason surfaces for clarify-first judgment, and deterministic behavioral proof for calibrated deliver versus degraded-confidence defer outcomes at the delivery gate plus strategist-learning native-reroute continuity behavior and the named guardian user-model/restraint benchmark
- [ ] still missing: deeper multi-signal learning from outcomes and stronger long-horizon intervention judgment beyond the current evidence-weighted delivery/channel/escalation/timing/suppression/blocked-state adaptation layer plus the new stale-support, stale-execution, cross-thread follow-through, explicit user-model preference inference substrate, explicit action-posture/restraint contract, explicit intent-uncertainty outcome-learning loop, first multi-day plus scheduled-outcome watchpoint passes, the new inspectable live-learning diagnostics layer, and the first abstention-via-long-horizon policy pass for low-urgency and routine guidance
- owners: Workstream 05, Workstream 04

### 3. Reliability and legibility

- [x] shipped foundations: runtime routing, fallbacks, weighted scoring, strict required-capability plus cost/latency/task-class/budget safeguard routing, richer provider-planning comparison with capability-gap/live-feedback penalties, retained-primary versus planning-winner summaries, best-alternate route margins, structured audit visibility, deterministic eval harness coverage, a named guardian-memory benchmark suite with operator-visible failure reporting and CI-gated regression posture, a named workflow-endurance benchmark suite with anticipatory-repair plus backup-branch failure taxonomy, and a named trust-boundary benchmark suite with operator-visible safety receipts
- [ ] still missing: broader long-running and live-provider integration proof beyond the current weighted-plus-safeguard router, richer planning-comparison surfaces, and first live feedback/failure-risk layer
- owners: Workstream 03

### 4. Presence and reach

- [x] shipped foundations: browser delivery, WebSocket chat, native daemon ingest, a first desktop presence surface built on daemon status, pending native-notification state, a safe test-notification path, desktop-notification fallback, browser-side controls for pending native notifications, runtime route-health visibility for ready/fallback/unavailable states, a shared continuity snapshot for daemon state, deferred bundles, route reachability, pending native notifications, and recent interventions, queued-bundle same-thread continuation when the queue belongs to one session, synthesized continuity health/thread/recovery summaries, imported capability-family attention plus typed source-adapter degradation in that same continuity contract, explicit presence-surface ready-versus-attention continuity across messaging/connectors/adapters/observer definitions, inventory-backed browser-provider plus node-adapter continuity surfaces with selected/fallback and daemon/network prerequisite truth, that same broader continuity surfaced into the operator timeline and Activity Ledger, plus a first actionable cockpit desktop-shell surface for follow-up, dismiss, continue, open-thread, fallback inspection, and recovery drafting flows
- [ ] still missing: broader reach channels beyond the hardened browser/native layer, stronger voice/mobile surfaces, and deeper continuity across the expanded transport set
- owners: Workstream 04, Workstream 06

### 5. Operator cockpit

- [x] shipped foundations: distinct visual surface, current world UI, first guardian cockpit shell, fixed command bar, guardian-state/intervention/audit/trace panes, linked recent-output and pending-approval panes, dedicated workflow-run views, richer workflow inspector actions, artifact round-trip into the command bar, declared-type artifact-to-workflow handoff, verified artifact source-run plus related-output follow-through with explicit unresolved-state fallback, direct artifact source-run open/continue or source-failure reuse plus related-output comparison shortcuts across evidence/output/inspector panes, checkpoint-truthful branch actions, branch-family supervision with latest-branch continue/open-parent controls, direct workflow-attached approve or deny controls, persisted `default` / `focus` / `review` layouts, inspector visibility persistence, keyboard switching, per-layout save/reset composition, first browser-session continuity restore, a first actionable desktop shell, a first cockpit-native capability/operator surface for policy/extension/workflow state, a searchable capability palette, a threaded operator timeline, capability preflight/autorepair, a denser operator terminal with recommendations, repair actions, installable items, live operator-feed status, runbooks, active triage for approvals, workflow branches, queued guardian items, and degraded reach, evidence shortcuts for approval context, trace, and artifact lineage, a first team control-plane lane for governance/usage/runtime/handoff synthesis, a workspace-level background-session substrate that joins managed processes with branch handoff and session continuity snippets, searchable repository or pull-request engineering-memory bundles over workflow or approval or audit continuity plus session-search matches, an explicit continuity graph across sessions/workflows/approvals/artifacts/notifications/deferred guardian items, a unified background-continuity supervision lane that composes those operator continuity surfaces into one handoff view, a long-running workflow operating layer with queue-state supervision, denser repair paths, anticipatory repair and backup-branch controls, workflow-endurance benchmark surfacing, latest-branch open/compare control, related-output debugger context, and explicit long-horizon eval proof, and keyboard-first inspect/approve/continue/open-thread/redirect/workflow/artifact follow-through
- [ ] still missing: deeper step-level execution control and more flexible cross-surface workspace control beyond the shipped step records, branch-family supervision, workspace-level multi-session workflow orchestration plus long-run state capsules and queue-state recovery supervision, anticipatory repair or backup-branch guidance, background-session substrate, artifact source/family follow-through, explicit output/checkpoint/lineage workflow history rows, checkpoint branch controls, triage and evidence shortcuts, and first dedicated workflow and operator timeline layers
- owners: Workstream 06

### 6. Workflow leverage

- [x] shipped foundations: specialists, skills, MCP, delegation primitives, first-class reusable workflows, starter packs, a first operator-facing workflow-control layer with draft-to-cockpit steering, a first cockpit workflow-run/operator surface, first workflow-runs history with boundary-aware replay metadata, declared-type artifact-to-workflow handoff from cockpit inspectors, verified artifact source-run plus related-output follow-through in the cockpit with explicit unresolved-state fallback, truthful checkpoint branch controls backed by persisted runtime state, branch-family workflow supervision with parent/peer/child inspection, a cockpit-native operator surface for workflow availability plus extension/runtime visibility, a richer workflow timeline with approval recovery, thread links, replay guardrails, parameterized reruns, capability preflight/autorepair, starter-pack repair guidance plus runbooks, operator-readable marketplace-flow composition across starter packs, extension packs, and packaged runbooks, a first adapter-backed source-evidence runtime with normalized evidence bundles across public-web contracts, a first connector-backed authenticated source-read bridge for provider-neutral repository/work-item/code-activity evidence, a first connector-backed authenticated source-action path with scoped approval and audit metadata, `plan_source_report` publication planning, reusable connector-first source-review/report routines with provider-neutral daily/progress/goal-alignment planning, bounded PR-native `code_activity.write` create or review actions with fixed-argument guardrails, searchable engineering-memory bundles over repository or pull-request continuity, an explicit continuity graph across sessions/workflows/approvals/artifacts/notifications/deferred guardian items, a governed self-evolution loop for declarative skills/runbooks/starter packs/prompt packs that stays eval-gated and PR-review-oriented instead of mutating capability assets in place while now persisting explicit change-summary and review-risk receipts into the saved proposal and PR draft, and a long-running workflow operating layer with queue-state orchestration, dense recovery guidance, anticipatory repair drafts, backup-branch candidates, condensation-fidelity reporting, a named workflow-endurance benchmark suite, latest-branch/output debugger context, and explicit workflow operating-layer eval proof
- [ ] still missing: richer direct step-level workflow control, deeper workflow history, broader marketplace depth, easier extension ergonomics, and broader authenticated connector execution beyond the shipped repository/work-item/code-activity read bridge plus bounded work-item and pull-request activity write paths, the shipped step records, branch-family supervision, workspace-level workflow orchestration plus long-run state capsules and queue-state recovery guidance, anticipatory repair or backup-branch guidance, artifact source/family follow-through, checkpoint branch controls, first cockpit timeline, operator terminal, plus the newer package-health/governance surfaces
- owners: Workstream 07, Workstream 02

### 7. Execution hardening

- [x] shipped foundations: approvals, policy modes, secret redaction, sandbox path, audit logging, privileged workflow execution-boundary metadata, forced approval wrapping for high-risk/approval-mode workflow paths, explicit secret-ref containment to injection-safe surfaces, field-scoped secret-ref injection metadata for native and MCP tool surfaces, rejection of underdeclared workflow runtimes, workflow-run replay metadata that keeps risk/approval/boundary context visible at the operator surface, stricter replay/native-resume guardrails around high-risk or secret-bearing workflow surfaces, approval-context-bound workflow replay/resume plus stale-approval blocking when the privileged surface changes, scoped high-risk extension configure/source-save approvals with explicit mutation targets, operator-visible approval scope plus trust-context metadata across approval/activity/operator surfaces, connector-backed authenticated source-mutation planning with scoped approval plus audit payloads for typed write contracts, allowlisted authenticated mutation payload enforcement during execution, safer sync runtime-audit fallback behavior for no-loop helper paths, vault-backed MCP credential storage plus credential-source audit for manual connector auth paths, a dedicated `vault_keeper` delegation surface so generic memory planning no longer inherits direct secret-management tools, explicit managed-process session/background trust receipts plus always-confirm `start_process` approval for persistent runtime work, disposable worker runtime roots outside the workspace for direct command and background-process execution, explicit credential-egress allowlists for secret-bearing MCP execution, delegated/workflow/operator trust receipts that preserve connector-egress plus branch-handoff partitions, and a named `trust_boundary_and_safety_receipts` benchmark lane with operator-visible safety receipts
- [ ] still missing: stronger host/container-grade privileged execution isolation, deeper connector credential enforcement beyond hostname allowlists, and broader trust-partition enforcement for future delegated/background execution beyond the new operator-aware replay/native-resume hardening plus field-scoped secret-ref, mutation-allowlist, disposable worker roots, credential-egress allowlists, session-partitioned managed-process trust receipts, background-session continuity substrate, and always-confirm background process start layer
- owners: Workstream 01, Workstream 02

## Execution Contract

- [x] Active execution state lives in the GitHub Project, issues, and PRs.
- [x] The roadmap preserves completed implementation programs and strategic gap framing instead of active kanban state.
- [x] This file explains why the remaining work belongs to the workstreams named above.
- [ ] If benchmark research materially changes implementation priority, update this file and the roadmap in the same PR, then refresh the affected issues or project items.

## Acceptance Checklist

- [x] Every superiority-program area has explicit implementation ownership.
- [x] The docs now say where research truth ends and implementation truth begins.
- [x] The master roadmap, status page, and synthesis can point to this file instead of duplicating the whole translation layer.
- [ ] The implementation strategy should continue to stay justified by the benchmark and superiority gaps named here.
