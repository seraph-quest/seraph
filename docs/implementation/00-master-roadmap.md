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

When these docs are updated on an open feature branch, they describe the intended post-merge `develop` state for that branch. Until merge, the open PR and its validation remain the integration truth.

## Docs Contract

- `docs/research/` defines target product shape, evidence rules, benchmark logic, and superiority program logic.
- `docs/implementation/STATUS.md` is the fastest shipped-state snapshot.
- this roadmap owns the live implementation queue and queue refresh rule.
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
| 02. Execution Plane | `[ ]` | Real tools, MCP, browser, shell, filesystem, goals, vault, web search, first-class reusable workflows, starter packs, threaded workflow history, step diagnostics, parameterized replay context, capability preflight/bootstrap, cockpit-native extension authoring, and first branch/resume workflow control are shipped; stronger execution safety and deeper visual workflow control are still left |
| 03. Runtime Reliability | `[ ]` | Fallback chains, routing rules, local runtime paths, weighted provider scoring, capability/cost/latency/task/budget safeguards, richer routing explainability, routing-summary audit visibility, and guardian-behavior runtime evals are shipped; simulation-grade policy planning and still broader eval depth are still left |
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, native daemon foundations, a first coherent desktop presence surface, unified browser/native continuity, and native action-card resume payloads are shipped; broader channel reach and deeper cross-surface continuity are still left |
| 05. Guardian Intelligence | `[ ]` | Guardian record, memory, goals, strategist, briefings, reviews, observer-driven state, observer salience/confidence scoring, explicit guardian state, corroboration-aware world-model fusion, continuity-thread memory signals, project timelines, obligations, collaborators, intervention policy, and learned timing/suppression/thread guidance are shipped foundations; stronger long-horizon learning loops are still left |
| 06. Embodied Interface | `[ ]` | The guardian cockpit is now the active browser shell, with a pane workspace, drag/resize plus grid snap, saved layout composition, session continuity restore, linked evidence, a searchable capability surface, a separate activity ledger window, richer live operator views, preflight/repair flows, and an extension studio shipped; deeper visual workflow debugging and denser keyboard-first control are still left |
| 07. Ecosystem And Delegation | `[ ]` | Skills, MCP, catalog/install surfaces, delegation foundations, reusable workflow composition, starter packs, capability discovery, threaded workflow history plus a separate activity ledger, parameterized runbooks, preflight/autorepair, bounded bootstrap, cockpit-native extension authoring, and first branch/resume control are shipped; stronger extension ergonomics, versioning, and clearer workflow visual control are still left |

## Progress Summary

- [x] Seraph is already a real local guardian prototype with observer, memory, goals, tools, approvals, MCP, and proactive scheduling.
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability are the strongest shipped foundations on `develop`.
- [x] The research tree now defines Seraph as a power-user guardian workspace, not a village-first product.
- [x] The guardian workspace is now the only supported interface contract; the village/editor line is removed from the active repo path and should not be revived.
- [x] Seraph now exposes a coherent capability surface for tools, skills, workflows, MCP servers, starter packs, workflow runs, reusable runbooks, preflight/autorepair actions, live operator logs, and active thread continuity from inside the cockpit itself.
- [x] Workflow runs, pending approvals, notifications, queued interventions, recent interventions, surfaced failures, and routing events now share explicit thread labels, continue drafts, open-thread links, and one threaded operator timeline instead of living as separate operator silos.
- [x] Seraph now also exposes a separate Activity Ledger window that answers what the agent did, why it did it, which thread it belonged to, and what spent LLM budget, instead of forcing the operator to reconstruct that from timeline, trace, and audit panes.
- [x] the Activity Ledger now groups request-scoped work into compact parent rows with emoji/icon scanability, child tool or routing rows, and completion summaries, giving the operator a Hermes-style action ledger without collapsing into raw terminal spam.
- [x] the workspace window system now uses flatter terminal-style chrome with close controls, visible resize grip, per-pane visibility state, and a top-level Windows menu instead of treating panes as fixed dashboard cards.
- [x] Workflow runs now expose step records, timestamps, duration, error summaries, retry-from-step drafts, richer fingerprints, blocked-skill repair guidance, workflow diagnostics, and cockpit-native debug/authoring handoff instead of only run-level replay.
- [x] The cockpit now includes a first extension authoring and validation studio for workflows, skills, and MCP config instead of forcing repo-level edits for every capability change.
- [x] Guardian state now carries memory signals, continuity threads, collaborators, recurring obligations, project timelines, corroboration sources, and learned timing, suppression, blocked-state, plus thread guidance instead of only first-pass focus and delivery bias.
- [x] Capability bootstrap now sequences safe autorepair/install actions for workflows, runbooks, and starter packs instead of leaving preflight and repair as separate manual operator steps.
- [x] Workflow runs now expose first branch/resume checkpoints, stored-load-error/debug surfaces, and resume drafts tied to existing inputs instead of only replay-from-start guidance.
- [x] Runtime routing now enforces capability, cost, latency, task-class, and budget safeguards with operator-readable audit details and live timeline summaries instead of only weighted scoring and cooldown rerouting.
- [ ] Seraph is still behind the strongest reference systems on capability marketplace depth, visual workflow branch debugging, stronger long-horizon intervention learning, broader native reach, and deeper execution hardening.
- [ ] No workstream is complete yet.

## Completed 10-PR Batches

Completed batches stay visible instead of being deleted on queue refresh.

### Latest Completed 10-PR Batch

1. [x] `capability-pack-autoinstall-and-bootstrap-v3`:
   turn capability bootstrap into a fuller install doctor that can stage bundled packs, persist repair plans, resolve more dependency chains, and make a fresh workspace feel capable with fewer manual recovery steps
2. [x] `extension-authoring-and-validation-studio-v1`:
   add schema-aware authoring, validation, diagnostics, and repair flows for user-authored workflows, skills, and MCP configs directly inside the cockpit
3. [x] `workflow-step-branching-and-resume-v1`:
   promote step recovery from repair hints into step-aware branch/resume checkpoints, lineage metadata, and safer branch-from-failure workflow control
4. [x] `cockpit-density-and-live-operator-views-v4`:
   deepen the cockpit into a more Hermes-like operator surface with denser live logs, better timeline composition, and stronger keyboard-first control over capabilities, workflows, and repairs
5. [x] `provider-policy-explainability-and-budgets-v3`:
   expose richer live provider-policy reasoning, budget guardrails, and "why not this model?" surfaces across the cockpit and threaded operator timeline
6. [x] `execution-safety-hardening-v9`:
   harden the new bootstrap, step repair, extension authoring, provider-budget escalation, and native continuation mutation paths before the growing operator surface compounds unsafe leverage
7. [x] `native-channel-expansion-v5`:
   broaden actionable native surfaces beyond the first thread-aware continuity layer with richer follow-up controls, notification repair cues, and stronger browser/native handoff
8. [x] `world-model-memory-fusion-v9`:
   deepen durable project state with stronger corroboration rules, richer cross-thread memory synthesis, and better linkage between execution evidence and ongoing world-model commitments
9. [x] `guardian-learning-policy-v9`:
   make learned guidance shape intervention sequencing, channel choice, blocked-state handling, and thread recovery more explicitly across browser, native, and workflow-triggered surfaces
10. [x] `guardian-behavioral-evals-v9`:
   add deterministic contracts for install-doctor flows, step branching/resume recovery, richer provider explainability, deeper thread continuity, and stronger learning-conditioned guardian behavior

### Previous Completed 10-PR Batch

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

## Current Extension Platform Transition Queue

This is the authoritative PR list for the implementation side.
For this architecture migration, the roadmap keeps the full multi-batch transition queue visible instead of truncating it to 10 items.

- every entry below is a numbered PR-sized slice
- the current active item is `#25 trusted-code-plugins-rfc-v1`
- this roadmap is the canonical queue for the transition; [Workstream 07](./07-ecosystem-and-leverage.md) summarizes the same program by phase and deliverable set rather than restating every item

1. [x] `extension-model-terminology-v1`:
   rename the misleading internal `plugins/` concept into clearer terms such as `native_tools`, `connector`, and `capability_pack` so the codebase and docs stop implying that Seraph already has a general arbitrary-code plugin runtime
2. [x] `extension-manifest-schema-v1`:
   add the first canonical extension manifest, schema validator, compatibility rules, and typed `contributes` contract so every later slice builds on one explicit package format instead of ad hoc files
3. [x] `extension-registry-and-loader-v1`:
   introduce one extension registry and loader abstraction that can enumerate manifests and typed contributions while preserving current skill, workflow, and MCP behavior during migration
4. [x] `extension-validation-and-doctor-v1`:
   add structured extension validation and doctor outputs for schema errors, missing references, compatibility failures, and permission mismatches so broken packs become diagnosable before install or execution
5. [x] `extension-package-layout-v1`:
   standardize the on-disk package structure for capability packs and connectors so one package can contribute skills, workflows, runbooks, starter packs, presets, and later connector definitions coherently
6. [x] `extension-scaffold-tools-v1`:
   ship local scaffolding and validation tools so adding a new capability pack does not require hand-authoring manifests and directory structure from scratch
7. [x] `extension-authoring-docs-v1`:
   publish first-class docs for creating capability packs, manifest fields, contribution types, validation, repair, and migration from the current loose-file model
8. [x] `example-capability-pack-v1`:
   add one canonical schema-valid example package that includes at least a skill, workflow, and runbook so docs, tests, and future contributors all share one golden reference before the migrated loaders become the default runtime path
9. [x] `capability-packaging-skills-v1`:
   migrate skill loading into manifest-backed capability packs with backward compatibility during the transition so skills become first-class extension contributions
10. [x] `capability-packaging-workflows-v1`:
   migrate workflow loading into manifest-backed capability packs with validated references and metadata so workflows stop living on a separate loading path
11. [x] `capability-packaging-runbooks-and-starter-packs-v1`:
   move runbooks and starter packs into the same manifest-backed architecture so higher-level reusable capability bundles stop being special-case inventory, with explicit runbook contributions and packaged starter packs now loaded through the extension registry during the coexistence window
12. [x] `bundled-capability-packs-v1`:
   convert Seraph’s shipped declarative defaults into real bundled capability packs so startup/runtime loading now prefers bundled skills, workflows, starter packs, and explicit runbooks from `backend/src/defaults/extensions/core-capabilities/` through the same manifest-root registry seam, with workspace packages taking precedence over bundled defaults during the coexistence window while install/bootstrap/catalog flows still finish their legacy-copy migration in later slices
13. [x] `extension-lifecycle-api-v1`:
   add one lifecycle API for install, validate, enable, disable, configure, inspect, and remove so UI and automation flows stop talking to per-surface install logic, with the backend now shipping `/api/extensions` list/inspect/validate/install/enable/disable/configure/remove endpoints while workspace lifecycle UI still lands in the next slice and the first `configure` step remains metadata-only until typed runtime config contracts arrive
14. [x] `extension-studio-manifest-awareness-v1`:
   make the extension studio package-aware so authors edit manifests and package-backed workflow/skill members together instead of forcing loose-file-only save paths, with `/api/extensions/{id}/source` now backing workspace package manifests and package-backed authoring while the studio sidebar groups manifests with their packaged members and still falls back to legacy loose-file paths where migration slices have not finished yet
15. [ ] `extension-lifecycle-ui-v1`:
   surface the unified extension lifecycle in the workspace so install, validation, health, enablement, configuration, and removal all happen through one operator path
16. [ ] `connector-manifest-and-health-v1`:
   define the connector package shape with auth/config metadata and health/test hooks so connectors stop being an architectural exception
17. [ ] `mcp-packaging-and-install-flow-v1`:
   move MCP server definitions into the extension package model and lifecycle so MCP becomes one connector type inside the platform instead of a separate world
18. [ ] `managed-connectors-v1`:
   add the curated non-MCP connector abstraction for first-party or high-trust integrations that need stronger UX, rollout, telemetry, and auth control than raw MCP
19. [ ] `observer-source-extensions-v1`:
   package observer sources as typed extensions where appropriate so input reach fits the same architecture as skills, workflows, and connectors
20. [ ] `channel-adapter-extensions-v1`:
   package output and delivery adapters as typed extensions where appropriate so reach surfaces stop being separate one-off integration paths
21. [x] `extension-permissions-and-approvals-v1`:
   map extension-declared permissions cleanly into policy, approval, and execution behavior so packages cannot bypass Seraph’s trust boundaries
22. [x] `extension-audit-and-activity-v1`:
   make extension install, update, enable, disable, health, and execution visible in Activity Ledger and audit so operators can explain what changed and why
23. [x] `extension-versioning-and-update-flow-v1`:
   add version-aware updates, compatibility checks, and bundled-vs-user-installed semantics so packages can evolve without hidden drift, with validation now returning lifecycle plans for install vs update vs workspace override, the lifecycle API shipping a dedicated update path, and packaged MCP connectors refreshing their runtime config cleanly during workspace package upgrades
24. [x] `legacy-loader-cleanup-v1`:
   demote the old loose-file authoring/install paths so the new extension platform is now the supported primary write path, with new skill/workflow saves landing in the managed `workspace/extensions/workspace-capabilities/` package, bundled catalog skill installs landing as manifest-backed packages under `workspace/extensions/`, and old loose loaders remaining read-only transitional compatibility
25. [ ] `trusted-code-plugins-rfc-v1`:
   explicitly decide whether privileged code plugins are needed at all, with a gated RFC rather than silently drifting into an arbitrary-code plugin runtime

## Queue Maintenance Rule

- keep the full active queue for architecture-transition programs visible here until the transition is complete
- keep the most recent completed 10-PR batch visible above with checkmarks
- do not delete the immediately previous completed batch until a later cleanup pass
- keep landed slices in the active queue marked `[x]` until a full 10-slice completed batch is ready to move into the completed-batches section
- when 5 slices from the published queue have landed, rerank only the remaining open items while leaving the landed items in place
- when 10 slices from the published queue have landed, move that completed set into the completed-batches history and renumber the remaining active queue
- rerank earlier if new evidence from `docs/research/` materially changes the priority order
- each internal slice must close with a subagent review pass against bugs, missing tests, design drift, and hallucinated assumptions before it is marked complete or rolled into a final GitHub PR
- the result of that subagent review must be recorded in the eventual GitHub PR `Validation` section before any slice is marked complete in these docs

## Delivery Order

1. Trust Boundaries
2. Execution Plane
3. Runtime Reliability
4. Presence And Reach
5. Guardian Intelligence
6. Embodied Interface
7. Ecosystem And Delegation

Implementation docs `08` through `10` are supporting mirror layers for this roadmap, not additional workstreams.

## Stable Interfaces Outside This Transition

- the browser and WebSocket chat surface
- the observer daemon ingest path
- runtime-path-based LLM routing and fallback settings
- runtime audit and eval harness contracts

## Transitional Interfaces Slated For Migration

- `SKILL.md`-based skill loading
- loose workflow loading from the current workspace file layout
- MCP server configuration and server-management APIs as they exist before connector manifests and packaged install flows land

## Current Shipped Slice On `develop`

- [x] local guardian stack with browser UI, backend APIs, WebSocket chat, scheduler, observer loop, and native macOS daemon
- [x] guardian cockpit as the active and only supported browser shell
- [x] first coherent desktop presence surface built on daemon status, capture-mode visibility, pending native-notification state, a safe test-notification path, desktop-notification fallback when browser delivery is unavailable, and a first actionable desktop control shell inside the cockpit
- [x] browser-side continuity controls for native notifications, including pending notification inspection, per-notification dismiss, bulk clear, cockpit-to-settings linkage for queued desktop state, and desktop-shell draft/continue actions over pending notifications, queued bundle items, and recent interventions
- [x] a unified continuity snapshot now ties daemon state, pending native notifications, deferred bundle items, and recent interventions into one browser-readable model across cockpit and settings surfaces
- [x] first capability-overview and starter-pack APIs now expose tools, skills, workflows, MCP servers, blocked-state reasons, recommended starter bundles, installable catalog items, repair actions, runbook metadata, and preflight-ready action payloads in one operator-readable shape
- [x] capability preflight now explains whether workflows, runbooks, and starter packs are ready, what will block them, and which safe repair actions can be applied before execution
- [x] capability bootstrap can now apply bounded safe install or repair actions for workflows, runbooks, and starter packs instead of leaving preflight as a separate manual operator step
- [x] starter packs and blocked workflows now also publish policy-aware recommended actions so the operator surface can repair real blockers instead of suggesting no-op activations
- [x] workflow diagnostics now expose stored load errors, richer step timestamps and duration, error summaries, and recovery hints so broken definitions and failed runs are easier to debug from the cockpit
- [x] recent negative feedback on the same intervention type can now reduce interruption eagerness for similar future advisory nudges
- [x] aligned active-work signals now calibrate observer salience upward, and grounded high-salience nudges can cut through high interruption cost outside focus mode
- [x] the cockpit now supports a pane workspace with drag/resize, grid snap, packed `default` / `focus` / `review` layout presets, inspector visibility persistence, layout switching from both the header and keyboard shortcuts, plus per-layout save and reset behavior
- [x] the pane workspace now also supports per-pane hide/show controls, a dedicated Windows menu, and flatter Godel-style window framing instead of the earlier rounded-card shell
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
- [x] the cockpit now also includes live operator-feed status, a separate Activity Ledger window, saved runbook macros, approval-aware workflow timeline actions, replay repair actions, and explicit continue/open-thread controls across approvals, workflow runs, native notifications, queued interventions, surfaced failures, and LLM spend attribution
- [x] the activity ledger now exposes routing summaries, selected reason codes, policy-score context, rejected targets, native thread-source or continuation metadata, and per-call LLM tokens/cost instead of only coarse event summaries
- [x] workflow history now behaves like a true operator timeline with timeline events, approval-recovery copy, replay guardrails, parameterized replay drafts, and explicit thread metadata for opening or continuing the relevant thread
- [x] browser sessions, desktop notifications, queued interventions, recent interventions, and workflow runs now share explicit thread metadata instead of leaving continuity implicit
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing across helper, scheduled, agent, delegation, and MCP-specialist paths
- [x] stricter provider-policy safeguards now cover required capability intents plus cost, latency, task-class, and budget guardrails that reroute only when compliant targets exist and otherwise fail open with explicit audit visibility
- [x] explicit guardian-state synthesis across chat, WebSocket, and strategist paths, combining observer context, salience/confidence signals, memory recall, session history, recent sessions, recent intervention feedback, and confidence into one structured downstream input
- [x] guardian-state synthesis now also carries learned communication guidance, and the world model now includes active routines, collaborators, recurring obligations, and project timeline context alongside projects, constraints, and execution pressure
- [x] guardian-state synthesis now also groups memories into category-aware buckets, requires corroboration from multiple sources before calling the world model grounded, and feeds learned blocked-state guidance back into intervention receptivity
- [x] explicit guardian world model now also carries recent active projects, active constraints, recurring patterns, collaborators, recurring obligations, project timelines, and recent execution pressure from workflow/tool outcomes, not only focus, commitments, and receptivity
- [x] guardian state now carries structured memory signals, continuity threads, and memory-degraded confidence instead of only plain-text recall context
- [x] explicit intervention policy at the proactive delivery boundary, with first-class act, bundle, defer, request-approval, and stay-silent classifications plus salience-aware policy reasons
- [x] persisted guardian intervention records with delivery outcomes, native-notification acknowledgements, and explicit feedback capture exposed back through guardian state
- [x] second outcome-learning layer now lets recent positive and acknowledged outcomes change direct-delivery timing, native-channel preference, and async-native escalation bias, not only interruption reduction
- [x] guardian learning now also emits timing, suppression, blocked-state, and thread-preference policy bias that can change bundle-versus-act decisions for advisory nudges
- [x] guardian behavioral proof now explicitly covers the calibrated high-salience deliver path versus degraded-confidence defer path at the proactive delivery gate
- [x] deeper guardian behavioral proof now also covers strategist tick learning its way into native-notification delivery and continuity-surface presence when high-signal learned nudges should bypass the browser
- [x] runtime audit visibility across chat, session-bound helper and agent LLM traces, scheduler including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, guardian-record file, vault repository, filesystem, browser, sandbox, and web search paths
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
