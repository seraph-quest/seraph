---
title: 10. Competitive Benchmark
---

# 10. Competitive Benchmark

## Purpose

This file records the current evidence-backed comparison between Seraph on `develop` and the reference systems named in this program: OpenClaw, Hermes, and IronClaw.

The goal is not to win every category on paper. The goal is to identify exactly where Seraph is ahead, behind, or still unknown, then tie that to implementation work.

Implementation mirror:

- `docs/implementation/09-benchmark-status.md` owns the shipped-on-`develop` translation of these axes

## How To Read This

- `Ahead` means Seraph’s currently shipped repo surface is stronger on the reviewed evidence.
- `Behind` means the competitor’s currently documented product/runtime surface is stronger.
- `At Par` means the shipped surfaces are comparable enough that Seraph does not have a clear advantage.
- `Unknown` means the reviewed evidence was insufficient for a confident call.

## Axis Matrix

| Axis | OpenClaw | Hermes | IronClaw |
|---|---|---|---|
| Operator visibility | `Behind` | `At Par` | `Behind` |
| Longitudinal memory | `At Par` | `Ahead` | `At Par` |
| Intervention quality | `Unknown` | `Ahead` | `Ahead` |
| Safe real-world execution | `Behind` | `Behind` | `Behind` |
| Runtime reliability / eval rigor | `Unknown` | `Ahead` | `Unknown` |
| Workflow composition | `Behind` | `Behind` | `Behind` |
| Dense interface efficiency | `Behind` | `Behind` | `Behind` |
| Presence / reach | `Behind` | `Behind` | `Behind` |

## OpenClaw

### Current Read

Seraph is currently behind OpenClaw on operator console density, workflow leverage breadth, safe execution breadth, and cross-channel reach. It is roughly at par on longitudinal memory. Intervention quality and eval rigor remain unclear from the reviewed official OpenClaw materials.

### Why

- OpenClaw’s official Control UI exposes chat, tool-event cards, sessions, cron, skills, health, logs, and config in one operator surface.
- OpenClaw documents first-class multi-agent composition and explicit workflow primitives through sub-agents and OpenProse.
- Seraph now ships first-class reusable workflows, but OpenClaw still appears ahead on composition breadth and operator control around those workflows.
- OpenClaw documents broader safety controls around sandboxing, approvals, browser execution, and gateway security than Seraph has shipped on `develop`.
- Seraph’s current strengths relative to OpenClaw remain its guardian-specific memory, observer loop, proactive scaffolding, and deterministic runtime eval harness, but the reviewed OpenClaw sources do not give enough evidence to score intervention quality or eval rigor decisively.

### Sources

- [OpenClaw Control UI](https://docs.openclaw.ai/web/control-ui)
- [OpenClaw architecture](https://docs.openclaw.ai/concepts/architecture)
- [OpenClaw agent runtime](https://docs.openclaw.ai/concepts/agent)
- [OpenClaw memory](https://docs.openclaw.ai/concepts/memory)
- [OpenClaw browser](https://docs.openclaw.ai/tools/browser), [OpenClaw sandboxing](https://docs.openclaw.ai/gateway/sandboxing), and [OpenClaw security](https://docs.openclaw.ai/gateway/security)
- [OpenClaw multi-agent composition](https://docs.openclaw.ai/concepts/multi-agent) and [OpenProse](https://docs.openclaw.ai/prose)

## Hermes

### Current Read

Seraph is currently ahead of Hermes on longitudinal memory, guardian-style intervention scaffolding, and runtime eval rigor. Hermes is ahead on workflow composition, dense terminal efficiency, cross-channel reach, and likely safer real-world execution surfaces.

### Why

- Hermes ships a strong TUI with persistent status, slash-command grammar, interrupt-and-redirect, and strong terminal ergonomics.
- Hermes ships broader workflow surfaces today through tools, skills, background sessions, messaging channels, cron, and code execution; Seraph now has first-class reusable workflows but not yet the same operator-facing workflow density.
- Hermes memory is intentionally bounded and file-based, while Seraph already ships soul, vector memory, consolidation, goals, and observer-driven state.
- Seraph documents a deterministic runtime eval harness and broader guardian-specific proactive scaffolding than the official Hermes materials show.

### Sources

- [Hermes CLI](https://hermes-agent.nousresearch.com/docs/user-guide/cli/)
- [Hermes memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)
- [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/), [Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/), [Hermes browser](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser/), and [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security/)
- [Hermes homepage](https://hermes-agent.nousresearch.com/)

## IronClaw

### Current Read

Seraph is currently ahead of IronClaw on guardian-style intervention scaffolding. IronClaw is ahead on safe execution, workflow composition, dense operator surfaces, and multi-channel reach. Longitudinal memory looks roughly at par. Runtime reliability/eval rigor remains unclear on the reviewed official materials.

### Why

- IronClaw documents a security-first execution model with capability permissions, isolation layers, and protected secret handling.
- IronClaw documents both TUI and dashboard-style operator surfaces with logs, jobs, routines, and extensions.
- IronClaw documents broader composition through MCP, WASM tools/channels, hooks, and routines; Seraph now has first-class reusable workflows but remains behind on breadth and operator control.
- Seraph currently has stronger guardian-specific strategist, briefing, review, and proactive delivery scaffolding than the official IronClaw materials show.

### Sources

- [IronClaw official site](https://www.ironclaw.com/)
- [IronClaw official README](https://github.com/nearai/ironclaw/blob/staging/README.md)
- [IronClaw feature parity](https://github.com/nearai/ironclaw/blob/staging/FEATURE_PARITY.md)

## What This Means

The benchmark is clear enough to set priorities:

- Seraph’s strongest relative moat is still guardian-specific memory plus intervention scaffolding.
- Seraph’s biggest gaps are now operator cockpit quality, workflow control ergonomics, native reach, and execution hardening.
- The next product push should therefore combine guardian-state and intervention improvements with an interface and execution program, not just more runtime seam work.
