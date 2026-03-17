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
| Operator visibility and legibility | `[ ]` | audit trails, visible tool streaming, session traces, and current settings/state surfaces | dense cockpit with linked state, evidence, traces, and approvals in one operator view | 06, 03 |
| Longitudinal memory and human modeling | `[ ]` | soul, vector memory, consolidation, goals, observer context, and explicit guardian state | better salience, confidence, and world-model quality over time | 05 |
| Intervention quality and timing | `[ ]` | strategist, proactive scheduler surfaces, explicit intervention policy, and native-notification fallback | learning from outcomes plus stronger salience/confidence-aware timing | 05, 04 |
| Safe real-world execution | `[ ]` | approvals, policy modes, sandboxed shell path, audit logging, and secret handling | stronger privileged-path isolation and clearer execution-hardening boundaries | 01, 02 |
| Runtime reliability and eval rigor | `[ ]` | fallback chains, routing policy, structured runtime audit, and broad deterministic eval harness coverage | richer behavioral eval depth and broader provider-policy sophistication | 03 |
| Workflow composition and delegation | `[ ]` | specialists, MCP, skills, and delegation foundations | first-class reusable workflows and clearer artifact round-tripping | 07, 02 |
| Dense interface efficiency | `[ ]` | current village UI, settings, and visible agent/tool activity | dense guardian cockpit as the primary operator surface | 06 |
| Presence and reach across surfaces | `[ ]` | browser delivery, proactive queue/bundle delivery, native daemon, and first desktop-notification fallback | richer desktop shell, stronger cross-surface continuity, and broader channels | 04, 06 |

## What This Means On `develop`

- [x] Seraph already has real foundations on every benchmark axis.
- [x] Guardian memory, proactive scaffolding, and runtime legibility are stronger than a prototype-only baseline.
- [ ] Seraph is not yet in a position to claim broad benchmark superiority from shipped implementation alone.
- [ ] The biggest implementation gaps still cluster around cockpit density, workflow composition, execution hardening, and richer native reach.

## Acceptance Checklist

- [x] Every benchmark axis from research has an explicit implementation owner.
- [x] Every benchmark axis says what is shipped on `develop` and what is still missing.
- [ ] No axis should be called effectively “won” in implementation docs without shipped proof on `develop`.
- [ ] The roadmap queue should continue to map directly back to the gaps named here.
