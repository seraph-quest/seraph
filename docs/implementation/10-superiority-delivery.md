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

- [x] shipped foundations: soul, vector memory, goals, observer context, first observer salience/confidence/interruption-cost scoring, explicit guardian-state synthesis, a structured world-model layer for focus, commitments, active projects, active constraints, recurring patterns, active routines, collaborators, recurring obligations, project timelines, memory signals, continuity threads, recent execution pressure, and receptivity, plus learned communication guidance carried back into guardian state
- [ ] still missing: stronger human/world modeling quality and richer multi-signal learning beyond the new project/routine/collaborator/obligation-aware world-model and first guidance layer
- owners: Workstream 05

### 2. Intervention quality and timing

- [x] shipped foundations: strategist, proactive scheduler surfaces, explicit intervention policy, queued bundles, salience-aware interruption gating, persisted intervention outcomes, explicit feedback capture, first coherent desktop presence plus native notification fallback, a first multi-signal outcome-learning layer that can now quiet, accelerate, reroute, bias async-native escalation, and nudge phrasing, cadence, timing, suppression, blocked-state choices, plus thread preference, calibrated high-salience timing overrides, and deterministic behavioral proof for calibrated deliver versus degraded-confidence defer outcomes at the delivery gate plus strategist-learning native-reroute continuity behavior
- [ ] still missing: deeper multi-signal learning from outcomes and stronger long-horizon intervention judgment beyond the first delivery/channel/escalation plus phrasing/cadence/timing/suppression adaptation layer
- owners: Workstream 05, Workstream 04

### 3. Reliability and legibility

- [x] shipped foundations: runtime routing, fallbacks, weighted scoring, strict required-capability plus cost/latency/task-class/budget safeguard routing, structured audit visibility, and deterministic eval harness coverage
- [ ] still missing: broader behavioral eval depth and richer provider-policy explainability beyond the current weighted-plus-safeguard router
- owners: Workstream 03

### 4. Presence and reach

- [x] shipped foundations: browser delivery, WebSocket chat, native daemon ingest, a first desktop presence surface built on daemon status, pending native-notification state, a safe test-notification path, desktop-notification fallback, browser-side controls for pending native notifications, a shared continuity snapshot for daemon state, deferred bundles, pending native notifications, and recent interventions, plus a first actionable cockpit desktop-shell surface for follow-up, dismiss, continue, open-thread, and approval-adjacent native follow-up flows
- [ ] still missing: broader reach channels and continuity beyond the first browser/native continuity snapshot, operator timeline integration, and desktop-shell control layer
- owners: Workstream 04, Workstream 06

### 5. Operator cockpit

- [x] shipped foundations: distinct visual surface, current world UI, first guardian cockpit shell, fixed command bar, guardian-state/intervention/audit/trace panes, linked recent-output and pending-approval panes, dedicated workflow-run views, richer workflow inspector actions, artifact round-trip into the command bar, direct artifact-to-workflow draft handoff, persisted `default` / `focus` / `review` layouts, inspector visibility persistence, keyboard switching, per-layout save/reset composition, first browser-session continuity restore, a first actionable desktop shell, a first cockpit-native capability/operator surface for policy/extension/workflow state, a searchable capability palette, a threaded operator timeline, capability preflight/autorepair, and a denser operator terminal with recommendations, repair actions, installable items, live operator-feed status, and runbooks
- [ ] still missing: better step-level replay control, richer workflow debugging density, and more flexible workspace control beyond the first dedicated workflow and operator timeline layers
- owners: Workstream 06

### 6. Workflow leverage

- [x] shipped foundations: specialists, skills, MCP, delegation primitives, first-class reusable workflows, starter packs, a first operator-facing workflow-control layer with draft-to-cockpit steering, a first cockpit workflow-run/operator surface, first workflow-runs history with boundary-aware replay metadata, direct artifact-to-workflow draft handoff from cockpit inspectors, a cockpit-native operator surface for workflow availability plus extension/runtime visibility, a richer workflow timeline with approval recovery, thread links, replay guardrails, parameterized reruns, capability preflight/autorepair, and starter-pack repair guidance plus runbooks
- [ ] still missing: richer direct workflow control, deeper step-level workflow history, and easier extension ergonomics beyond that first cockpit timeline plus operator terminal
- owners: Workstream 07, Workstream 02

### 7. Execution hardening

- [x] shipped foundations: approvals, policy modes, secret redaction, sandbox path, audit logging, privileged workflow execution-boundary metadata, forced approval wrapping for high-risk/approval-mode workflow paths, explicit secret-ref containment to injection-safe surfaces, rejection of underdeclared workflow runtimes, workflow-run replay metadata that keeps risk/approval/boundary context visible at the operator surface, and stricter replay/native-resume guardrails around high-risk or secret-bearing workflow surfaces
- [ ] still missing: stronger privileged execution isolation and clearer hardening boundaries beyond that operator-aware replay/native-resume hardening pass
- owners: Workstream 01, Workstream 02

## Queue Ownership

- [x] The live rolling 10-PR queue stays in [00-master-roadmap.md](./00-master-roadmap.md).
- [x] This file should explain why the queue exists in its current order.
- [x] The previous 10-item queue is now fully shipped on this branch.
- [x] `execution-safety-hardening-v6`, `threaded-operator-timeline-v1`, `workflow-runbooks-and-parameterized-replay-v1`, `capability-preflight-and-autorepair-v1`, `provider-policy-safeguards-v3`, `native-channel-expansion-v3`, `world-model-memory-fusion-v6`, `guardian-learning-policy-v6`, `cockpit-density-and-live-operator-views-v1`, and `guardian-behavioral-evals-v6` now move into the latest completed batch in the roadmap.
- [x] The roadmap now refreshes to `execution-safety-hardening-v7` as the next active item because the latest batch made threaded replay, preflight repair, provider safeguards, and live operator control materially easier to use.
- [x] The new queue starts with execution hardening, step-level workflow recovery, broader capability bootstrap, and provider-policy explainability because those are now the highest-value trust and operability gaps after the current operator-timeline and preflight batch landed.
- [x] The refreshed queue keeps deeper native reach, world-model fusion, guardian learning, denser operator views, extension ergonomics, and broader behavioral proof in the top 10 because the new batch widened leverage and therefore raises the value of better judgment, stronger debugging, and proof.
- [ ] If benchmark research materially changes priority, update this file and the roadmap in the same PR.

## Acceptance Checklist

- [x] Every superiority-program area has explicit implementation ownership.
- [x] The docs now say where research truth ends and implementation truth begins.
- [x] The master roadmap, status page, and synthesis can point to this file instead of duplicating the whole translation layer.
- [ ] The implementation queue should continue to stay justified by the benchmark and superiority gaps named here.
