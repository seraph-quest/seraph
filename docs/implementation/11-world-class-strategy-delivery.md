---
title: 11. World-Class Strategy Delivery
---

# 11. World-Class Strategy Delivery

## Status On `develop`

- [ ] The world-class strategy is only partially translated into shipped implementation on `develop`.

## Paired Research

- design source of truth: [11. Superiority Program](/research/superiority-program)
- benchmark input: [10. Competitive Benchmark](/research/competitive-benchmark)
- canonical strategy source of truth: [17. Seraph World-Class Strategy](/research/seraph-world-class-strategy)
- M0 competitor truth: [18. Agent Competition Truth Table](/research/agent-competition-truth-table)
- M0 claim and wording gate: [19. Strategy Claim Ledger](/research/strategy-claim-ledger)
- synthesis context: [00. Research Synthesis](/research)

## Purpose

This file is the implementation-side mirror for the world-class Seraph strategy.

It translates strategy into delivery rules without time-boxed roadmaps. Seraph execution is milestone-based only: no quarters, no month targets, no date promises, and no countdown-style roadmap language.

Active execution remains in the GitHub Project, issues, and PRs. This doc only explains how implementation should be prioritized, proven, and bounded on `develop`.

M0 competitor truth lives in [18. Agent Competition Truth Table](/research/agent-competition-truth-table). Claim wording lives in [19. Strategy Claim Ledger](/research/strategy-claim-ledger). Implementation docs should not make new world-class, superiority, security, privacy, production-readiness, or ahead-of-competitor claims unless the wording is allowed by the claim ledger.

## Delivery Ownership

The GitHub Project owns active execution.

- Current execution project: [Seraph Execution](https://github.com/orgs/seraph-quest/projects/1)
- GitHub Project items own priority, assignment, dependency state, PR links, review state, and completion.
- Issues own scoped problem statements, acceptance criteria, milestone labels, and proof requirements.
- PRs own integration truth until merged.
- Implementation docs own durable delivery rules and shipped/missing strategic status, not live task queues.
- Research docs own product strategy, milestone order, competitive frame, and acceptance standards.

If docs and Project state disagree, the Project is the execution source of truth and the docs should be corrected only through an explicit strategy or status update.

## Lead And Delegated Execution Operating Model

Seraph implementation should run as lead-directed, delegated execution.

- The lead owns milestone order, scope boundaries, acceptance criteria, priority calls, and final tradeoffs.
- Delegated agents own bounded issues or slices with explicit file limits, constraints, proof requirements, and PR outputs.
- Delegated agents must not rewrite strategy, change milestone order, broaden capability scope, or create time-bounded commitments without lead direction.
- Delegated execution should produce small, inspectable changes with named milestone impact and repeatable proof.
- When implementation reveals a strategy conflict, agents should surface it to the lead or Project rather than improvising a new roadmap in docs.

## Priority Model

Priority is a milestone and leverage model, not a calendar model.

- `P0`: must ship to defend the core capability OS moat, guardian moat, or trust boundary; if it is missing, broader leverage compounds the wrong thing.
- `P1`: major strategic leverage that sharpens capability composition, supervised workflow power, ecosystem discipline, or benchmark proof.
- `P2`: important product quality, control-surface, or reach expansion work that improves the system but should not outrank capability, guardian, or trust layers.
- `Guardrail Product Boundary Discipline`: the named non-negotiable guardrail that keeps the strategy coherent; safety, audit, boundary, and proof constraints must hold across all priorities and any item that does not strengthen a named pillar should be rejected, deferred, or reframed.

The corrected priority is capability-first. Guardian intelligence remains a moat and operating mode, but implementation should not treat "guardian capabilities" as the only top priority. Tools, workflows, connectors, automation, browser/computer use, memory, delegation, and supervised execution must become one coherent capability workspace.

## Milestone Stack

This is the delivery stack. It is intentionally not time-bounded.

- `M0. Competition truth and execution governance`: GitHub Project owns live execution state; docs own strategy, milestone definitions, acceptance rules, and primary-source competitive truth.
- `M1. Capability kernel and manifest contract`: one coherent map and contract for core tools, workflows, skills, MCP, connectors, automations, browser/computer-use surfaces, memory providers, runbooks, and extension-owned contributions.
- `M2. Execution supremacy`: terminal, process, browser/computer use, files, patching, artifacts, sandboxes, background sessions, and repair flows are excellent enough to compete with serious task agents.
- `M3. Trusted execution boundaries`: tool, workflow, browser, connector, secret, filesystem, process, delegation, and provider paths have explicit least-privilege boundaries and proof.
- `M4. Selective reach and channels`: native, messaging, browser, node, webhook, and external channels extend capability and continuity without fragmenting trust.
- `M5. Jobs, routines, workflows, and delegation`: long-running work supports branch, checkpoint, resume, compare, repair, handoff, background ownership, routines, and delegated execution.
- `M6. Memory superiority`: memory changes behavior through provenance, confidence, conflict handling, freshness, privacy boundaries, operator correction, and behavior-changing recall.
- `M7. Dense cockpit and activity ledger`: the operator can inspect capability state, execution history, approvals, routing, spend, artifacts, interventions, and recovery without source diving.
- `M8. Guardian brain over the capability substrate`: memory, world model, goals, salience, timing, and feedback shape capability choice, sequencing, restraint, and follow-through.
- `M9. Governed ecosystem`: external capability packs, managed connectors, versioning, compatibility, trust levels, and review flows can scale without turning Seraph into plugin soup.

## Initial GitHub Project Spine

These issues are the execution spine created for the milestone program. The Project remains the live source of truth for ownership, dependencies, status, and PR links.

- [#424 M0: Agent competition truth table and capability benchmark](https://github.com/seraph-quest/seraph/issues/424)
- [#425 M1: Capability kernel and manifest contract](https://github.com/seraph-quest/seraph/issues/425)
- [#427 M2: Execution supremacy across terminal browser files and sandboxes](https://github.com/seraph-quest/seraph/issues/427)
- [#428 M3: Secure capability host and trust-boundary enforcement](https://github.com/seraph-quest/seraph/issues/428)
- [#426 M4: Channels presence and device pairing](https://github.com/seraph-quest/seraph/issues/426)
- [#429 M5: Jobs routines workflows and delegation](https://github.com/seraph-quest/seraph/issues/429)
- [#433 M6: Memory superiority and behavior-changing recall](https://github.com/seraph-quest/seraph/issues/433)
- [#430 M7: Operator cockpit and activity control legibility](https://github.com/seraph-quest/seraph/issues/430)
- [#431 M8: Guardian brain over the capability substrate](https://github.com/seraph-quest/seraph/issues/431)
- [#432 M9: Ecosystem marketplace and verified capability packs](https://github.com/seraph-quest/seraph/issues/432)

The first execution wave underneath that spine is:

- [#436 P0: Strategy claim ledger and proof gate](https://github.com/seraph-quest/seraph/issues/436)
- [#434 P0: Atomic capability contract freeze](https://github.com/seraph-quest/seraph/issues/434)
- [#435 P0: IronClaw-class capability security parity gauntlet](https://github.com/seraph-quest/seraph/issues/435)
- [#437 P0: Guardian intervention quality benchmark](https://github.com/seraph-quest/seraph/issues/437)
- [#439 P1: Cockpit operator efficiency benchmark](https://github.com/seraph-quest/seraph/issues/439)
- [#438 P1: One excellent reach channel canary](https://github.com/seraph-quest/seraph/issues/438)
- [#441 P1: Memory provider quality gate](https://github.com/seraph-quest/seraph/issues/441)
- [#440 P1: Live workflow endurance canary](https://github.com/seraph-quest/seraph/issues/440)

## Current Next-Batch Order

The next implementation program should stay milestone-sized, proof-led, and branch-scoped. The Project remains the execution source of truth; this section records the strategy ordering and acceptance frame so future issues and PRs do not drift back into time-boxed or surface-area-first planning.

| Order | Batch | Primary milestone | Existing issue | Acceptance frame |
|---:|---|---|---|---|
| 1 | Board, proof, and P1 wave normalization | M0 | [#422](https://github.com/seraph-quest/seraph/issues/422) | active Project fields are complete, stale merged-item PR/review fields are corrected, #438-#441 remain tied to the parent execution wave, and proof/claim gates are explicit before implementation starts |
| 2 | Secure execution host v1 | M3 | new or parented under #422 | privileged execution, browser credential/cookie, workspace, provider, and connector paths fail closed with adversarial receipts; claim boundary remains stronger isolation foundations, not production secure-by-default execution |
| 3 | Live long-horizon eval replay v1 | M0 / cross-cutting proof | new or parented under #422 | live-ish fake providers, replay fixtures, failure taxonomy, CI receipts, and operator-visible proof cover memory, workflow, reach, security, and cockpit flows before stronger quality claims |
| 4 | Cockpit operator efficiency benchmark | M7 | [#439](https://github.com/seraph-quest/seraph/issues/439) | real operator tasks measure speed, clicks or keystrokes, error detectability, recovery, confidence, and receipts rather than only cockpit density |
| 5 | Memory provider quality gate | M6 / M9 | [#441](https://github.com/seraph-quest/seraph/issues/441) | provider evidence declares provenance, confidence, privacy, freshness, conflict handling, and suppression; noisy or stale evidence is blocked before guardian context |
| 6 | Workflow endurance canary | M5 | [#440](https://github.com/seraph-quest/seraph/issues/440) | multi-session interruption, checkpoint, branch, recovery, delegated ownership, artifact comparison, approval preservation, and audit trail are replayable without calling the workflow engine durable yet |
| 7 | Durable workflow engine v1 | M5 | new or parented under #422 | workflow state moves from audit-projected receipts toward a minimal durable state kernel with crash-safe resume, heartbeat or reactive triggers, retry, repair, and delegated artifact review lifecycle |
| 8 | One excellent reach channel canary | M4 | [#438](https://github.com/seraph-quest/seraph/issues/438) | one selected channel proves pairing, revocation, health, retry, thread continuity, memory/context continuity, approval handoff, degraded UI, and audit receipts with explicit anti-sprawl scope |
| 9 | Guardian world-model learning quality v2 | M8 | new or parented under #422 | multi-signal learning, stale/conflicting evidence arbitration, salience/confidence calibration, and false-positive/false-negative accounting are proven with live-ish intervention replay |
| 10 | Governed extension marketplace hardening foundations | M9 | new or parented under #422 | pack review/verification, compatibility semantics, supply-chain policy, provider trust downgrade handling, and authoring ergonomics improve production-oriented foundations without claiming production marketplace security |

Critic/contrarian gate for this ordering:

- accepted: trust and proof substrate must precede memory-provider breadth, reach expansion, and marketplace hardening
- accepted: #439 should benchmark real workflow and memory scenarios rather than stand alone as synthetic UI proof
- accepted: #440 can begin as an endurance canary, but durable-workflow claims wait for the durable state-machine batch
- accepted: #438 remains later in the stack unless the single channel, rejection criteria, and anti-sprawl scope are explicit
- accepted: capability/trust regression coverage should be folded into the M0/M3/proof batches so every capability class carries owner, permission, mutation, credential-egress, browser/session, audit, and operator-proof semantics

## M0 Competition Truth And Execution Governance

Implementation meaning: strategy, delivery, competitive evidence, and active execution must not drift.

- shipped foundations: paired research/implementation docs, GitHub issue/PR workflow, implementation status docs, and explicit docs contract language
- missing strategic gaps: consistent milestone labels across Project items, clearer Project fields for proof and ownership, tighter update rules when PRs change strategic status, maintained primary-source competitor matrix, and claim-ledger review discipline
- proof requirements: every active strategy item is represented in the GitHub Project with owner, milestone, acceptance criteria, linked PR or issue state, competitor gap where relevant, and allowed wording for strategic claims

M0 batch ownership:

- [#424 Agent competition truth table and capability benchmark](https://github.com/seraph-quest/seraph/issues/424) owns the primary-source competitor matrix and capability benchmark axes.
- [#436 Strategy claim ledger and proof gate](https://github.com/seraph-quest/seraph/issues/436) owns [19. Strategy Claim Ledger](/research/strategy-claim-ledger), the allowed wording/status model, and the review gate for unbacked superiority language.

## M1 Capability Kernel And Manifest Contract

Implementation meaning: Seraph needs one stable capability map and permission contract before capability breadth can scale safely.

- shipped foundations: native tools, skills, MCP, workflows, runbooks, starter packs, extension manifests, catalog extensions, managed connectors, and cockpit-visible capability surfaces
- missing strategic gaps: sharper taxonomy for core tools vs workflow tools vs MCP tools vs skills vs runbooks vs starter packs vs connectors vs extension-owned surfaces; clearer capability family ownership; one contract for permissions, provenance, mutation rights, health, compatibility, and trust level
- proof requirements: operator-visible inventory that shows source, trust level, owner, boundary, health, dependencies, declared permissions, and available actions for each capability class

M1 source-of-truth contract:

- scope: core tools, workflow tools, MCP tools, skills, workflows, runbooks, starter packs, automations, browser/computer-use surfaces, managed connectors, memory providers, source adapters, channel adapters, observer sources, and extension-owned contributions
- identity: every capability class needs a stable family, source, owner, package or core boundary, lifecycle state, and operator-facing label
- manifest contract: extension-owned capability must declare kind, contribution type, version, compatibility, publisher or origin, dependencies, declared permissions, trust level, health checks, and lifecycle controls before downstream milestones treat it as dependable
- permission contract: mutation rights, secret use, filesystem/process/browser/provider reach, connector egress, approval profile, and audit expectations must be explicit enough for M3 trust-boundary work to enforce or fail closed
- provenance contract: inventory and receipts should distinguish core-owned behavior, package-owned behavior, managed connector behavior, raw MCP exposure, external provider evidence, and transitional compatibility paths
- operator proof: cockpit/API surfaces should let an operator inspect why a capability exists, where it came from, what it can do, what blocks it, what repair or install action is available, and which receipt proves the state

Acceptance for #425/#434:

- the durable docs name M1 as the capability-kernel source of truth for M2, M3, and M9
- the accepted contract covers identity, manifest shape, permissions, provenance, mutation rights, health, compatibility, lifecycle state, and trust level
- acceptance/proof wording is evidence-grounded and does not imply M1 is complete from documentation alone
- future M2/M3/M9 work has a clear rule for when it must update M1 docs or reject a capability class as underdeclared

Proof language for the batch should cite concrete receipts, not broad strategy claims: affected manifests or APIs, capability inventory payloads, lifecycle/validation output, audit or Activity Ledger receipts, deterministic tests, benchmark suites, issue links, PR validation, and implementation-doc paths.

M2/M3/M9 dependency rules:

- M2 may build execution depth only on capability classes with declared owner, dependencies, health, available actions, and recovery/repair semantics.
- M3 may enforce trusted execution only on capability classes with declared permissions, mutation rights, approval behavior, audit expectations, provenance, and fail-closed behavior.
- M9 may grow ecosystem breadth only on extension-owned capability classes with manifest kind, contribution type, version, compatibility, publisher or origin, lifecycle state, package health, and review or verification posture.

## M2 Execution Supremacy

Implementation meaning: Seraph should be excellent at real work across terminal, process management, browser/computer use, file operations, patching, artifacts, sandboxes, background sessions, and repair.

- shipped foundations: capability discovery, policy-aware blocked reasons, preflight/autorepair, extension health, install/repair guidance, starter-pack activation, workflow diagnostics, and cockpit controls
- missing strategic gaps: broader repair coverage across connectors, browser/computer-use providers, delegated workers, automation triggers, external credentials, local/remote sandboxes, and artifact handoffs; clearer failure grouping by capability family
- proof requirements: repeatable execution and blocked-capability scenarios with inspectable diagnosis, safe repair action, audit receipt, and operator-visible recovery state

M2 batch execution rules:

- M2 is a milestone, not a time box or a series of small PR slices. The whole M2 completion claim must ship as one ready PR that closes #427 and #435 together.
- execution depth must land as capability contract surface area, runnable tool behavior, operator-visible receipts, and deterministic tests together
- file work needs first-class patch preview/apply behavior, not only raw file overwrite
- terminal, process, browser, HTTP, sandbox, filesystem, patch, and background-session surfaces should expose operation modes, session model, persistence, artifact contract, health, controls, and recovery actions
- M2 cannot claim parity or excellence if #435 trust-boundary checks are left as follow-up work; SSRF, DNS-resolution preflight for private addresses, redirect-to-internal, path traversal, secret egress, replay drift, delegation, and prompt/extension permission creep must stay in the acceptance frame
- previous failed PR tests are part of the batch acceptance burden when they affect the same milestone surfaces, especially deterministic benchmark and engineering-memory proof
- The implementation gate for this batch is `m2_execution_supremacy` plus the dedicated `/api/operator/m2-execution-benchmark` surface. If that suite or any prior failed shard regresses, M2 stays blocked.

## M3 Trusted Execution Boundaries

Implementation meaning: every capability path needs least privilege, approval behavior, auditability, and fail-closed semantics appropriate to its risk.

- shipped foundations: approvals, audit logging, secret redaction, secret-reference containment, sandboxed execution, background-process partitioning, disposable worker roots, credential-egress allowlists, managed connector boundary metadata, and replay drift blocking on privileged surfaces
- shipped in the M3 secure-host batch on this branch: secret references now fail closed across cross-session or expired refs and destination-host egress drift, generic workspace read and patch tools block secret-like files such as `.env`, credential, token, and private-key paths, workspace escape attempts fail closed on command/script/patch paths, foreground and background process execution receives an allowlisted environment instead of ambient host credentials, and the `secure_capability_host` benchmark plus `/api/operator/secure-capability-host-benchmark` exposes host-isolation strategy, browser cookie/session partition strategy, hostile-provider replay blocking, capability/trust regression matrix, and receipt-surface completeness receipts
- missing strategic gaps: broader host/container-grade isolation, complete browser credential and cookie isolation beyond the current per-run-context strategy, deeper live hostile-provider telemetry/replay across real providers, and stricter enforcement across future connector/browser/provider paths that do not yet carry destination-aware credential policies
- proof requirements: boundary-specific evals for browser, connector, delegation, background, provider fallback, filesystem, workspace escape, process, and workflow paths; operator-visible audit receipts; fail-closed behavior when a path cannot prove it stayed within its trust envelope
- claim boundary: this milestone proves deterministic secure-host choke points, claim-bounded host-isolation strategy, and operator-visible receipts; it does not claim full host/container isolation, secure-by-default production posture, or complete browser/provider credential isolation

TEEs are helpful, but they do not solve prompt injection or unsafe tool execution by themselves. Existing trust-drift blocking should be deepened and generalized across future delegated/background/browser/provider paths, not treated as a missing foundation.

## M4 Selective Reach And Channels

Implementation meaning: Seraph should expand beyond the browser only where the additional channel improves leverage, trust, or continuity.

- shipped foundations: browser-native continuity, desktop presence, messaging and connector surfaces, typed reach/adaptor inventory, native notifications, and channel-routing foundations
- shipped in the M4 benchmark-proof slice on this branch: the named `channels_presence_device_pairing` suite pins channel identity-boundary metadata, external-channel same-thread continuity, device pairing and revocation fail-closed receipts, channel mutation boundaries, and abuse/failure review visibility
- missing strategic gaps: live broad mobile and messaging transports remain future work, along with broader but selective non-browser reach, clearer operator control over active channels, production-grade pairing protocols, stronger continuity across high-value channels, and safer action cards for external surfaces
- proof requirements: each added reach surface should justify its trust cost, demonstrate a clear use case, preserve continuity rather than fragmenting it, and expose identity, pairing, revocation, mutation, and review boundaries before it can be treated as shipped reach

## M5 Jobs, Routines, Workflows, And Delegation

Implementation meaning: Seraph should operate long-running work as a supervised system, not as isolated run executions.

- shipped foundations: workflow history, step records, branch-family supervision, typed artifact handoff, checkpoint-truthful resume/branch control, workspace-level orchestration, live operator timelines, activity ledgers, and background-session continuity
- shipped in the M5 operating-layer slice on this branch: cron-style scheduled jobs now persist durable per-run receipts, lifecycle audit events cover create/update/pause/resume/delete/trigger/result, paused jobs record skipped no-fire receipts without executing actions, the cockpit has an operator-visible M5 work layer for jobs, routines, workflow projection, and delegation trust partitions, and the named `m5_jobs_routines_workflows_delegation` benchmark suite gates the payload, run-history, no-fire, delegation, and operator benchmark contracts
- shipped in the durable workflow-kernel v1 slice on this branch: workflow execution writes persisted run, step, safe checkpoint, and retry or repair state while redacting unsafe secret or authenticated checkpoint payloads; the repository and deterministic fixture proof cover heartbeat-receipt and delegated artifact-review lifecycle shapes; checkpoint resume tries durable state before audit projection; `/api/operator/durable-workflow-engine` and `durable_workflow_engine_v1` make the state-kernel proof operator-visible
- missing strategic gaps: live heartbeat and reactive trigger executors remain future work beyond receipt-only trigger proof, workflows are still not a production distributed workflow engine, delegated artifact-review rows are repository-supported but not yet emitted by normal workflow execution, and deeper step-level execution control plus richer multi-session repair remain open
- proof requirements: deterministic workflow-endurance and M5 operating-layer evals, durable run-history receipts for every scheduled trigger attempt, no-fire receipts for paused work, inspectable branch and recovery receipts, explicit delegation trust boundaries, and operator flows that can continue, compare, repair, delegate, or branch without reconstructing context manually

## M6 Memory Superiority

Implementation meaning: memory should change behavior and improve outcomes, not merely store more objects.

- shipped foundations: persistent guardian state, memory, observer synthesis, project and continuity modeling, learning signals, contradiction-aware world-model arbitration, watchpoints, and governed additive memory-provider foundations
- missing strategic gaps: deeper long-horizon learning loops, stronger project arbitration under sparse or conflicting evidence, more faithful reuse of continuity across sessions and threads, stricter provenance/conflict/freshness handling, and quality gates for external memory providers
- proof requirements: benchmark coverage for long-horizon recall, contradiction handling, stale-memory suppression, provider quality, and receipts showing memory changed capability choice, timing, intervention, or suppression appropriately

## M7 Dense Cockpit And Activity Ledger

Implementation meaning: the operator surface should stay obvious, dense, and fast enough that the user can understand what Seraph is doing without hunting across panes.

- shipped foundations: browser cockpit, command surfaces, activity ledger, operator terminal, workflow supervision views, live feed, active triage, approvals, traces, capability panes, team control-plane synthesis, background-session continuity, engineering-memory bundles, continuity graph, workflow operating layer, and the M7 command board that composes active work, trust boundaries, approvals, memory evidence, tool-call receipts, artifacts, recovery state, and fast controls into one operator surface
- shipped in the M7 cockpit-legibility slice on this branch: `/api/operator/m7-cockpit` now projects active work, approvals, trust boundaries, memory evidence, tool/audit receipts, artifacts, jobs, delegations, background sessions, channels/recovery, and fast controls with explicit control-mode labels for direct backend controls, routed or policy-gated controls, and operator-draft controls; `/api/operator/m7-cockpit-legibility-benchmark` and `m7_operator_cockpit_legibility` gate readable receipts, fast-control availability, handoff legibility, and trust-boundary clarity without claiming live usability superiority
- missing strategic gaps: deeper step-level execution control, broader direct mutation endpoints for pause/resume/revoke where appropriate, live multi-operator usability evidence, and broader cross-surface command control beyond the shipped M7 projection and command board
- proof requirements: cockpit tasks should be inspectable without source diving, common operator moves should stay keyboard-first, and the surface should make trust, capability, execution state, claim boundaries, and direct-versus-drafted control modes legible at a glance

## M8 Guardian Brain Over The Capability Substrate

Implementation meaning: guardian intelligence should improve which capabilities Seraph chooses, when it uses them, when it asks, when it waits, and how it follows through.

- shipped foundations: guardian strategy, project and continuity modeling, salience, watchpoints, intervention planning, operator feedback, and memory-backed context
- shipped in the M8 guardian-brain slice on this branch: `m8_guardian_intervention_quality` gates deterministic act/defer/bundle/clarify/request-approval/stay-silent scenarios over ambiguous evidence, stale memory, conflicting commitments, interruption cost, risky capability use, and no-action restraint; `/api/operator/m8-guardian-brain` also derives a live state-scoped guardian decision from the current guardian state before appending benchmark receipts, surfacing selected and rejected capability lanes, timing/usefulness/false-positive/false-negative/trust/recovery labels, approval-preserving high-risk paths, and operator correction hooks without claiming live superiority
- missing strategic gaps: live long-horizon human outcome studies, broader reach-surface intervention replay, and stronger adaptive learning beyond deterministic M8 receipts
- proof requirements: guardian intervention benchmarks with positive and negative cases, repeatable demos that show continuity surviving session churn, and receipts showing guardian state changed capability choice, timing, or suppression appropriately while preserving approval and trust boundaries

## Cross-Cutting Benchmark-Grade Proof

Implementation meaning: no strategic claim should be considered real unless the implementation, the benchmark, and the operator demo all agree.

- competitive frame: Hermes is strong on terminal ergonomics, background sessions, broad tools, MCP, browser work, memory, and channels; OpenClaw is strong on operator control UI, tool events, health/log/config, multi-agent composition, browser execution, sandboxing, and gateway security; IronClaw is strong on security-first execution, permissions, isolation, routines, hooks, extensions, and multi-surface control; adjacent agents such as Claude Code, Codex, Cursor, Devin-style workers, OpenHands, Aider, Goose, LangGraph-style builders, and browser/computer-use agents prove that the category is moving toward supervised capability composition
- shipped foundations: deterministic runtime evals, named benchmark suites for memory, workflow endurance, trust boundaries, computer use, M4 channel/device boundary proof, M9 governed-ecosystem foundations, governed improvement, and operator-readable receipts for several key execution seams
- missing strategic gaps: broader live-provider and real-world integration proof, stronger coverage for future delegated and browser paths, production marketplace security proof, and more repeatable proof surfaces for claims that would otherwise stay aspirational
- proof requirements: every major strategy item needs an explicit benchmark, a concrete operator-visible demo path, and a traceable implementation receipt on `develop`

## M9 Governed Ecosystem

Implementation meaning: extension capability should stay typed, governable, and easy to reason about, with clear core-vs-extension boundaries and a stable capability taxonomy.

- shipped foundations: skills, MCP, starter packs, extension lifecycle, package health, capability discovery, managed connectors, typed source adapters, catalog extensions, and cockpit-visible operator control over packaged capability surfaces
- shipped in the M9 governed-ecosystem proof slice on this branch: `m9_governed_ecosystem` gates deterministic manifest governance, lifecycle review gates, connector degradation truth, marketplace governance flow, diagnostics/update triage, and `/api/operator/m9-governed-ecosystem-benchmark` operator receipts with an explicit claim boundary: local deterministic governance proof, not competitor superiority or production marketplace security
- missing strategic gaps: stronger typed extension contracts across all extension kinds, broader managed-connector governance, deeper version and compatibility semantics, review/verification flows, production marketplace security, and a stable taxonomy that keeps core features separate from extension contributions
- proof requirements: manifest and lifecycle validation, compatibility and enable/disable receipts, connector and extension health proofs, marketplace/review receipts, and operator surfaces that show why a capability exists, where it came from, what trust level it carries, and which claim boundary applies

## Guardrail Product Boundary Discipline

- do not turn active execution into a doc-tracked queue; GitHub Project items, issues, and PRs own that state
- do not treat time as the organizing principle; milestones, priority, moat, proof, and trust boundary are the organizing principles
- do not ship a roadmap item without a named milestone, pillar, competitor or adjacent-agent gap, capability gap, moat effect, proof or eval plan, trust boundary, and operator surface
- do not introduce world-class, best, strongest, superior, secure, private, production-ready, complete, or ahead-of-competitor wording unless [19. Strategy Claim Ledger](/research/strategy-claim-ledger) allows that exact status and wording
- do not treat capability breadth as permission to become plugin soup; Seraph is a capability-first guardian agent OS/workspace with governed composition
- do not treat TEEs as a complete safety story; keep approvals, audit, sandboxing, and tool isolation as first-class constraints
- do not reclassify already-shipped trust-drift blocking as missing work; deepen and generalize the existing receipts across future delegated/background/browser/provider paths instead
- do not let extension growth blur the line between core platform behavior and extension-owned behavior

## Acceptance Rules For Future Roadmap Items

Future roadmap items should be accepted into the GitHub Project only when they clearly answer all of the following:

- milestone: which M0-M9 milestone does this advance?
- pillar: which strategic pillar does this belong to?
- gap: which competitor, adjacent-agent, or internal weakness does it close?
- capability: which capability class or composition path does it improve?
- moat: how does it strengthen Capability OS, Guardian Intelligence, Trusted Execution, or another durable advantage?
- proof: what benchmark, eval, or repeatable demo will prove it?
- trust boundary: what new or existing boundary does it touch?
- operator surface: where will an operator see, control, or verify it?
- ownership: who owns execution, review, and completion in the GitHub Project?
