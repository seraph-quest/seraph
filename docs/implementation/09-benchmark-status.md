---
title: 09. Benchmark Status
---

# 09. Benchmark Status

## Status On `develop`

- [x] The capability import program and benchmark refresh are now landed on `develop`.
- [ ] Seraph still cannot credibly claim broad benchmark superiority from implementation alone.

## Paired Research

- benchmark source of truth: [10. Competitive Benchmark](/research/competitive-benchmark)
- evidence rules: [09. Reference Systems And Evidence](/research/reference-systems-and-evidence)

## Purpose

This file is the implementation-side mirror of the competitive benchmark.

Research owns the comparison against OpenClaw, Hermes, and IronClaw.
This file owns three implementation questions:

1. what is already shipped on `develop` for each benchmark axis?
2. what is still missing before Seraph can credibly claim `Ahead` on that axis?
3. which implementation workstreams own that gap?

## Axis Status On `develop`

| Axis | Checklist | Shipped On `develop` | Biggest Missing Piece | Owning Workstreams |
|---|---|---|---|---|
| Operator visibility and legibility | `[ ]` | audit trails, visible tool streaming, session traces, workflow-run views, step records, replay/resume drafts, grouped Activity Ledger rows with runtime-path spend attribution, imported capability reach summaries, extension governance queues, browser mode and connector inventory, operations inspector, persisted layouts, active triage, evidence shortcuts, workflow step-focus rows with direct context/output handoff, branch-origin and failure-lineage debugger rows, family-history comparison plus output-comparison drafts, bundled workflow-family next-step planning drafts, keyboard-first inspect/approve/continue/redirect control, and thread-aware continue/open-thread controls | broader long-running workflow control and broader cross-surface command control beyond the new branch-debug, comparison, bundled family-planning, and triage layer | 06, 03, 07 |
| Longitudinal memory and human modeling | `[ ]` | guardian record, vector memory, consolidation, priorities, observer context, explicit guardian state, calibrated salience/confidence scoring, context packs, session search, and a structured world-model layer for focus, focus provenance, commitments, projects, recurring patterns, constraints, routines, memory signals, continuity threads, execution pressure, receptivity, judgment risks, plus cross-thread follow-through carryover and cross-source project arbitration on the live project | deeper adaptive world-model quality, richer multi-signal learning over time, and additive memory-provider extensibility without giving up the guardian-first canonical memory model | 05 |
| Intervention quality and timing | `[ ]` | strategist, proactive scheduler surfaces, explicit intervention policy, observer salience/confidence/interruption-cost scoring, calibrated high-salience timing overrides, persisted intervention outcomes, explicit user feedback capture, native-notification fallback, channel routing, messaging reach, a first multi-signal delivery/channel/escalation/timing/blocked-state learning layer with scoped live global/thread/project resolution, contradiction-aware guardian confidence downgrades when durable project recall conflicts with live observer context, and a first long-horizon pass that treats stale supporting recall plus stale execution pressure as contradictions against the live project anchor | deeper multi-signal learning from outcomes and better long-horizon intervention judgment across the broader reach surface | 05, 04 |
| Safe real-world execution | `[ ]` | approvals, policy modes, sandboxed shell and execute-code paths, audit logging, secret handling, explicit secret-ref containment, permission summaries and approval profiles for packaged capabilities, suspicious-context scanning, site-policy controls, browser mode boundaries, and rejection of underdeclared workflow or runtime metadata | stronger privileged-path isolation, connector credential hardening, authenticated-source boundary posture, and deeper execution-hardening boundaries | 01, 02 |
| Runtime reliability and eval rigor | `[ ]` | fallback chains, weighted routing policy, strict provider safeguard routing, first simulation-grade route planning with explicit budget steering, structured runtime audit, runtime-path spend attribution, and deterministic eval harness coverage across guardian flows, imported capability surfaces, workflow recovery, browser modes, and activity-ledger attribution | richer long-running integration evals across live providers, channels, browser transports, and production-like failure modes | 03 |
| Workflow composition and delegation | `[ ]` | specialists, MCP, skills, delegation foundations, execute-code and clarify/todo/session-search runtime primitives, first-class reusable workflows, starter packs, optional skill packs, skill registry flows, workflow runtimes, Browserbase/browser-relay packaging, direct artifact-to-workflow handoff, and thread-aware approval recovery | deeper operator-facing workflow debugging, richer authoring ergonomics, more opinionated runtime templates, and cleaner provider-neutral capability contracts for external sources and authenticated systems | 07, 02 |
| Dense interface efficiency | `[ ]` | guardian workspace with linked state, workflow runs, interventions, audit, trace, fixed composer, persisted layouts, keyboard layout switching, Windows control surface, searchable capability palette, denser operator terminal, active triage, evidence shortcuts, workflow step-focus rows, explicit branch-debug summaries, family-history comparison, direct step/output handoff actions, family-output reuse plus output-comparison drafts, bundled workflow-family next-step planning drafts, first keyboard-first inspect/approve/continue/redirect/workflow/output control, imported reach/governance surfaces, and a separate grouped Activity Ledger | stronger long-running workflow density and broader keyboard-first control beyond the current step-focus, evidence, branch-debug, family-history, and bundled family-planning surfaces | 06, 07 |
| Presence and reach across surfaces | `[ ]` | browser delivery, proactive queue/bundle delivery, native daemon, actionable desktop presence, browser-side native-notification controls, shared continuity snapshots, runtime route-health visibility, queued-bundle same-thread continuation, browser mode matrix, messaging connectors, automation triggers, node adapters, canvas outputs, and explicit channel routing | broader production-grade reach hardening beyond the current browser/native layer, stronger voice/mobile surfaces, and deeper continuity across the expanded transport set | 04, 06 |

## What This Means On `develop`

- [x] Seraph now has real shipped surfaces on every benchmark axis, including the imported capability families from the Hermes/OpenClaw program.
- [x] Guardian memory, proactive scaffolding, runtime legibility, and capability breadth are now materially stronger than a prototype-only baseline.
- [ ] Seraph is still not in a position to claim broad benchmark superiority from shipped implementation alone.
- [ ] The biggest implementation gaps now cluster around privileged execution hardening, denser operator/debug ergonomics, deeper intervention learning, richer world-model quality, production-grade hardening for the broadened reach surfaces, and cleaner provider-neutral capability/adapter seams.

## Acceptance Checklist

- [x] Every benchmark axis from research has an explicit implementation owner.
- [x] Every benchmark axis says what is shipped on `develop` and what is still missing.
- [x] The benchmark mirror has been refreshed after the five-wave capability import program.
- [ ] No axis should be called effectively “won” in implementation docs without shipped proof on `develop`.
- [x] The strategic roadmap and active GitHub execution layer still map back to the gaps named here.
