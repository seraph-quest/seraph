---
title: 09. Benchmark Status
---

# 09. Benchmark Status

## Status On `develop`

- [ ] The benchmark program is only partially implemented on `develop`.

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
| Operator visibility and legibility | `[ ]` | audit trails, visible tool streaming, session traces, guardian cockpit shell, linked evidence panes, operations inspector, and persisted cockpit layout presets | dedicated workflow-operating views plus even denser linked state, evidence, trace, artifact, and approval control surfaces | 06, 03, 07 |
| Longitudinal memory and human modeling | `[ ]` | soul, vector memory, consolidation, goals, observer context, explicit guardian state, first observer salience/confidence scoring, and a first calibrated aligned-work/high-salience pass | better world-model quality and richer multi-signal learning over time | 05 |
| Intervention quality and timing | `[ ]` | strategist, proactive scheduler surfaces, explicit intervention policy, observer salience/confidence/interruption-cost scoring, calibrated high-salience timing overrides, persisted intervention outcomes, explicit user feedback capture, native-notification fallback, and deterministic behavioral proof for calibrated deliver vs degraded-confidence defer behavior | deeper multi-signal learning from outcomes and better long-horizon intervention judgment | 05, 04 |
| Safe real-world execution | `[ ]` | approvals, policy modes, sandboxed shell path, audit logging, and secret handling | stronger privileged-path isolation and clearer execution-hardening boundaries | 01, 02 |
| Runtime reliability and eval rigor | `[ ]` | fallback chains, routing policy, structured runtime audit, and broad deterministic eval harness coverage | richer behavioral eval depth and broader provider-policy sophistication | 03 |
| Workflow composition and delegation | `[ ]` | specialists, MCP, skills, delegation foundations, and first-class reusable workflows | clearer operator-facing workflow control, artifact round-tripping, and easier extension ergonomics | 07, 02 |
| Dense interface efficiency | `[ ]` | guardian cockpit default with linked state, interventions, audit, trace, fixed composer, persisted `default` / `focus` / `review` layouts, keyboard layout switching, and legacy village fallback | stronger workflow-operating density, richer artifact/workflow views, and more flexible operator workspace control | 06, 07 |
| Presence and reach across surfaces | `[ ]` | browser delivery, proactive queue/bundle delivery, native daemon, a first desktop presence surface built on daemon status plus notification fallback, and browser-side controls for pending native notifications | broader channels and continuity beyond the first browser/native notification bridge | 04, 06 |

## What This Means On `develop`

- [x] Seraph already has real foundations on every benchmark axis.
- [x] Guardian memory, proactive scaffolding, and runtime legibility are stronger than a prototype-only baseline.
- [ ] Seraph is not yet in a position to claim broad benchmark superiority from shipped implementation alone.
- [ ] The biggest implementation gaps still cluster around workflow-operating density inside the cockpit, deeper intervention quality and world modeling, execution hardening, and stronger cross-surface continuity.

## Acceptance Checklist

- [x] Every benchmark axis from research has an explicit implementation owner.
- [x] Every benchmark axis says what is shipped on `develop` and what is still missing.
- [ ] No axis should be called effectively “won” in implementation docs without shipped proof on `develop`.
- [ ] The roadmap queue should continue to map directly back to the gaps named here.
