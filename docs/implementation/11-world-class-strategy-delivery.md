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
- synthesis context: [00. Research Synthesis](/research)

## Purpose

This file is the implementation-side mirror for the world-class Seraph strategy.

It translates strategy into delivery rules without time-boxed roadmaps.

Active execution remains in the GitHub Project, issues, and PRs. This doc only explains how implementation should be prioritized, proven, and bounded on `develop`.

## Priority Model

Priority is a leverage model, not a calendar model.

- `P0`: must ship to defend the core moat or the core trust boundary; if it is missing, broader leverage compounds the wrong thing.
- `P1`: major strategic leverage that sharpens the moat, broadens power-user value, or removes a meaningful execution bottleneck.
- `P2`: important product quality, control-surface, or reach expansion work that improves the system but should not outrank the moat or trust layers.
- `Guardrail Product Boundary Discipline`: the named non-negotiable guardrail that keeps the strategy coherent; safety, audit, boundary, and proof constraints must hold across all priorities and any item that does not strengthen a named pillar should be rejected, deferred, or reframed.

## P0 Guardian Intelligence Moat

Implementation meaning: Seraph should keep becoming better at durable judgment, project continuity, selective attention, and long-horizon operator support than a generic assistant can be.

- shipped foundations: persistent guardian state, memory, observer synthesis, project and continuity modeling, learning signals, contradiction-aware world-model arbitration, watchpoints, and the first governed additive memory-provider lane
- missing strategic gaps: deeper long-horizon learning loops, stronger project arbitration under sparse or conflicting evidence, more faithful reuse of continuity across sessions and threads, and stronger additive memory-provider follow-through without replacing canonical guardian memory
- proof requirements: benchmark coverage for long-horizon recall and judgment, repeatable demos that show continuity surviving session churn, and explicit receipts that live evidence can override stale procedural bias when it should

## P0 Trusted Intelligence and Trusted Execution

Implementation meaning: Seraph should infer locally first, move into an attested TEE lane when the task deserves it, fall back to redacted cloud execution only when needed, and keep every tool/action path isolated from unnecessary privilege.

- shipped foundations: approvals, audit logging, secret redaction, secret-reference containment, sandboxed execution, background-process partitioning, disposable worker roots, credential-egress allowlists, managed connector boundary metadata, and replay drift blocking on privileged surfaces
- missing strategic gaps: broader host-grade isolation, stricter enforcement across future delegated/background/browser/provider paths, deeper credential-egress discipline for connector and browser work, and clearer tool/action separation for the routes that can mutate state
- proof requirements: boundary-specific evals for browser, connector, delegation, background, and provider fallback paths; operator-visible audit receipts; and fail-closed behavior when a path cannot prove it stayed within its trust envelope

TEEs are helpful, but they do not solve prompt injection or unsafe tool execution by themselves.

Existing trust-drift blocking should be deepened and generalized across future delegated/background/browser/provider paths, not treated as a missing foundation.

## P1 Capabilities And Extension Discipline

Implementation meaning: extension capability should stay typed, governable, and easy to reason about, with clear core-vs-extension boundaries and a stable capability taxonomy.

- shipped foundations: skills, MCP, starter packs, extension lifecycle, package health, capability discovery, managed connectors, typed source adapters, and cockpit-visible operator control over packaged capability surfaces
- missing strategic gaps: stronger typed extension contracts across all extension kinds, more explicit managed-connector governance, clearer version and compatibility semantics, and a stable taxonomy that keeps core features separate from extension contributions
- proof requirements: manifest and lifecycle validation, compatibility and enable/disable receipts, connector and extension health proofs, and operator surfaces that show why a capability exists, where it came from, and what trust level it carries

## P1 Supervised Workflow Operating Layer

Implementation meaning: Seraph should operate long-running work as a supervised system, not as isolated run executions.

- shipped foundations: workflow history, step records, branch-family supervision, typed artifact handoff, checkpoint-truthful resume/branch control, workspace-level orchestration, live operator timelines, activity ledgers, and background-session continuity
- missing strategic gaps: deeper step-level control, richer multi-session supervision, more complete lineage and checkpoint control, and stronger recovery/repair surfaces for long-running work
- proof requirements: deterministic workflow-endurance and supervision evals, inspectable branch and recovery receipts, and operator flows that can continue, compare, repair, or branch without reconstructing context manually

## P1 Benchmark And Proof System

Implementation meaning: no strategic claim should be considered real unless the implementation, the benchmark, and the operator demo all agree.

- shipped foundations: deterministic runtime evals, named benchmark suites for memory, workflow endurance, trust boundaries, computer use, and governed improvement, plus operator-readable receipts for several key execution seams
- missing strategic gaps: broader live-provider and real-world integration proof, stronger coverage for future delegated and browser paths, and more repeatable proof surfaces for claims that would otherwise stay aspirational
- proof requirements: every major strategy item needs an explicit benchmark, a concrete operator-visible demo path, and a traceable implementation receipt on `develop`

## P2 Cockpit Legibility And Control

Implementation meaning: the operator surface should stay obvious, dense, and fast enough that the user can understand what Seraph is doing without hunting across panes.

- shipped foundations: browser cockpit, command surfaces, activity ledger, operator terminal, workflow supervision views, live feed, and active triage
- missing strategic gaps: denser control over complex flows, better cross-surface explanation of system state, and more direct actions for recovery, comparison, and follow-through
- proof requirements: cockpit tasks should be inspectable without source diving, common operator moves should stay keyboard-first, and the surface should make trust and execution state legible at a glance

## P2 Selective Reach

Implementation meaning: Seraph should expand beyond the browser only where the additional channel improves leverage, trust, or continuity.

- shipped foundations: browser-native continuity, desktop presence, messaging and connector surfaces, and typed reach/adaptor inventory
- missing strategic gaps: broader but selective non-browser reach, clearer operator control over which channels are active, and stronger continuity across the channels that matter most
- proof requirements: each added reach surface should justify its trust cost, demonstrate a clear use case, and preserve continuity rather than fragmenting it

## Guardrail Product Boundary Discipline

- do not turn active execution into a doc-tracked queue; GitHub Project items, issues, and PRs own that state
- do not treat time as the organizing principle; priority, moat, proof, and trust boundary are the organizing principles
- do not ship a roadmap item without a named pillar, a competitor or capability gap, a moat effect, a proof or eval plan, a trust boundary, and an operator surface
- do not treat TEEs as a complete safety story; keep approvals, audit, sandboxing, and tool isolation as first-class constraints
- do not reclassify already-shipped trust-drift blocking as missing work; deepen and generalize the existing receipts across future delegated/background/browser/provider paths instead
- do not let extension growth blur the line between core platform behavior and extension-owned behavior

## Acceptance Rules For Future Roadmap Items

Future roadmap items should be accepted only when they clearly answer all of the following:

- pillar: which strategic pillar does this belong to?
- gap: which competitor gap or internal weakness does it close?
- moat: how does it strengthen Guardian Intelligence, Trusted Execution, or another durable advantage?
- proof: what benchmark, eval, or repeatable demo will prove it?
- trust boundary: what new or existing boundary does it touch?
- operator surface: where will an operator see, control, or verify it?
