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

- [x] shipped foundations: soul, vector memory, goals, observer context, first observer salience/confidence/interruption-cost scoring, and explicit guardian-state synthesis
- [ ] still missing: stronger human/world modeling quality and better-calibrated salience/confidence beyond the first heuristic layer
- owners: Workstream 05

### 2. Intervention quality and timing

- [x] shipped foundations: strategist, proactive scheduler surfaces, explicit intervention policy, queued bundles, salience-aware interruption gating, persisted intervention outcomes, explicit feedback capture, and first native notification fallback
- [ ] still missing: closed-loop learning from outcomes and stronger timing quality beyond the first salience model
- owners: Workstream 05, Workstream 04

### 3. Reliability and legibility

- [x] shipped foundations: runtime routing, fallbacks, scoring, structured audit visibility, and deterministic eval harness coverage
- [ ] still missing: broader behavioral eval depth and richer provider policy beyond the current weighted router
- owners: Workstream 03

### 4. Presence and reach

- [x] shipped foundations: browser delivery, WebSocket chat, native daemon ingest, and first desktop-notification fallback path
- [ ] still missing: richer desktop shell, stronger non-browser continuity, and broader reach channels
- owners: Workstream 04, Workstream 06

### 5. Operator cockpit

- [x] shipped foundations: distinct visual surface, current world UI, first guardian cockpit shell, fixed command bar, guardian-state/intervention/audit/trace panes, linked recent-output and pending-approval panes, operations inspector details, and legacy village fallback
- [ ] still missing: stronger workflow-control density, saved-layout control surfaces, and broader keyboard/operator ergonomics
- owners: Workstream 06

### 6. Workflow leverage

- [x] shipped foundations: specialists, skills, MCP, delegation primitives, and first-class reusable workflows
- [ ] still missing: clearer operator-facing workflow control, stronger artifact round-tripping, and easier extension ergonomics
- owners: Workstream 07, Workstream 02

### 7. Execution hardening

- [x] shipped foundations: approvals, policy modes, secret redaction, sandbox path, audit logging, privileged workflow execution-boundary metadata, and forced approval wrapping for high-risk/approval-mode workflow paths
- [ ] still missing: stronger privileged execution isolation and clearer hardening boundaries beyond that first hardening pass
- owners: Workstream 01, Workstream 02

## Queue Ownership

- [x] The live rolling 10-PR queue stays in [00-master-roadmap.md](./00-master-roadmap.md).
- [x] This file should explain why the queue exists in its current order.
- [x] The current queue starts with execution hardening because trust and privileged-path isolation are the most dangerous remaining gap.
- [x] The next layer is cockpit depth plus workflow control because operator visibility and workflow leverage are where Seraph is still visibly behind the strongest reference systems.
- [x] The next layer after that is feedback-driven guardian learning and salience calibration because proactive quality is now bottlenecked more by judgment than by missing primitives.
- [x] The later items in the current queue deepen desktop presence, cross-surface continuity, and broader behavioral proof for the stronger guardian product shape.
- [ ] If benchmark research materially changes priority, update this file and the roadmap in the same PR.

## Acceptance Checklist

- [x] Every superiority-program area has explicit implementation ownership.
- [x] The docs now say where research truth ends and implementation truth begins.
- [x] The master roadmap, status page, and synthesis can point to this file instead of duplicating the whole translation layer.
- [ ] The implementation queue should continue to stay justified by the benchmark and superiority gaps named here.
