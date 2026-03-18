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
4. where the live 10-PR queue is maintained

## Docs Contract

- [x] `docs/research/00-synthesis.md` defines the target product shape.
- [x] `docs/research/10-competitive-benchmark.md` owns the evidence-backed comparison.
- [x] `docs/research/11-superiority-program.md` owns the design-level program of work.
- [x] `docs/implementation/STATUS.md` owns the fastest shipped snapshot.
- [x] `docs/implementation/00-master-roadmap.md` owns the rolling 10-PR queue and queue refresh rule.
- [x] This file owns the translation from benchmark/program gaps into implementation workstreams and delivery ownership.

## Translation On `develop`

### 1. Guardian state and human model

- [x] shipped foundations: soul, vector memory, goals, observer context, first observer salience/confidence/interruption-cost scoring, explicit guardian-state synthesis, a first explicit world-model layer for focus, commitments, pressure, and receptivity, and a first calibrated aligned-work/high-salience pass
- [ ] still missing: stronger human/world modeling quality and richer multi-signal learning beyond the first explicit world-model and calibration layers
- owners: Workstream 05

### 2. Intervention quality and timing

- [x] shipped foundations: strategist, proactive scheduler surfaces, explicit intervention policy, queued bundles, salience-aware interruption gating, persisted intervention outcomes, explicit feedback capture, first coherent desktop presence plus native notification fallback, a first multi-signal outcome-learning layer that can now quiet, accelerate, and reroute interventions, calibrated high-salience timing overrides, and deterministic behavioral proof for calibrated deliver versus degraded-confidence defer outcomes at the delivery gate
- [ ] still missing: deeper multi-signal learning from outcomes and stronger long-horizon intervention judgment beyond the first delivery/channel adaptation layer
- owners: Workstream 05, Workstream 04

### 3. Reliability and legibility

- [x] shipped foundations: runtime routing, fallbacks, scoring, structured audit visibility, and deterministic eval harness coverage
- [ ] still missing: broader behavioral eval depth and richer provider policy beyond the current weighted router
- owners: Workstream 03

### 4. Presence and reach

- [x] shipped foundations: browser delivery, WebSocket chat, native daemon ingest, a first desktop presence surface built on daemon status, pending native-notification state, a safe test-notification path, desktop-notification fallback, and browser-side controls for pending native notifications
- [ ] still missing: broader reach channels and continuity beyond the first browser/native notification bridge
- owners: Workstream 04, Workstream 06

### 5. Operator cockpit

- [x] shipped foundations: distinct visual surface, current world UI, first guardian cockpit shell, fixed command bar, guardian-state/intervention/audit/trace panes, linked recent-output and pending-approval panes, dedicated workflow-run views, richer workflow inspector actions, artifact round-trip into the command bar, persisted `default` / `focus` / `review` layouts, inspector visibility persistence, keyboard switching, and legacy village fallback
- [ ] still missing: deeper workflow history and more flexible workspace control beyond the first dedicated workflow-run layer
- owners: Workstream 06

### 6. Workflow leverage

- [x] shipped foundations: specialists, skills, MCP, delegation primitives, first-class reusable workflows, a first operator-facing workflow-control layer with draft-to-cockpit steering, and a first cockpit workflow-run/operator surface
- [ ] still missing: richer direct workflow control, deeper workflow history, stronger artifact round-tripping, and easier extension ergonomics
- owners: Workstream 07, Workstream 02

### 7. Execution hardening

- [x] shipped foundations: approvals, policy modes, secret redaction, sandbox path, audit logging, privileged workflow execution-boundary metadata, forced approval wrapping for high-risk/approval-mode workflow paths, explicit secret-ref containment to injection-safe surfaces, and rejection of underdeclared workflow runtimes
- [ ] still missing: stronger privileged execution isolation and clearer hardening boundaries beyond that first hardening pass
- owners: Workstream 01, Workstream 02

## Queue Ownership

- [x] The live rolling 10-PR queue stays in [00-master-roadmap.md](./00-master-roadmap.md).
- [x] This file should explain why the queue exists in its current order.
- [x] The current queue still starts with execution hardening because trust and privileged-path isolation remain the most dangerous gap until that whole batch lands on `develop`.
- [x] The previous 10-item queue is now exhausted on this branch, so the roadmap has been refreshed to the next highest-value 10-PR horizon.
- [x] `execution-safety-hardening-v2` is now the first shipped item from the refreshed queue on this branch.
- [x] `cockpit-workflow-views-v1` and `guardian-learning-loop-v2` are now the second and third shipped items from the refreshed queue on this branch.
- [x] The active queue lead now moves to `cross-surface-continuity-v2` because the cockpit/workflow and first multi-signal learning slices are now in place, and the next bottleneck shifts to continuity across browser and native surfaces.
- [x] The refreshed queue still keeps cross-surface continuity, provider safeguards, and native shell maturity in the top 10 because those are the remaining benchmark-facing leverage gaps outside guardian intelligence.
- [ ] If benchmark research materially changes priority, update this file and the roadmap in the same PR.

## Acceptance Checklist

- [x] Every superiority-program area has explicit implementation ownership.
- [x] The docs now say where research truth ends and implementation truth begins.
- [x] The master roadmap, status page, and synthesis can point to this file instead of duplicating the whole translation layer.
- [ ] The implementation queue should continue to stay justified by the benchmark and superiority gaps named here.
