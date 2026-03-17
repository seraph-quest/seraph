---
slug: /
title: Seraph Master Roadmap
---

# Seraph Master Roadmap

## Summary

Seraph now uses the same high-level documentation split as `maas`:

- `docs/research/` is the design and product thesis surface
- `docs/implementation/` is the shipped-state and delivery surface
- `docs/implementation/STATUS.md` is the live status ledger

This implementation tree is the canonical answer to three questions:

1. What is shipped on `develop`?
2. What is being hardened right now?
3. What is still left before Seraph is the product we want?

## Current Status

Read this roadmap together with [Development Status](./STATUS.md).

Legend for the checklist column:

- `[x]` shipped on `develop`
- `[ ]` not fully shipped on `develop`

| Workstream | Checklist | Notes |
|---|---|---|
| 01. Trust Boundaries | `[ ]` | Policy modes, approvals, audit logging, and secret redaction are shipped; deeper isolation and narrower privileged execution paths are still left |
| 02. Execution Plane | `[ ]` | Real tool execution, MCP, browser, shell, filesystem, goals, vault, and web search are shipped; richer workflow and external execution depth are still left |
| 03. Runtime Reliability | `[ ]` | Profile preferences, wildcard path rules, fallback chains, runtime-path overrides, local routing, broad runtime audit coverage including MCP test, skills API, and screen repository boundaries, and deterministic eval foundations are shipped; richer routing policy and broader eval depth are still left |
| 04. Presence And Reach | `[ ]` | Browser UI, WebSocket chat, proactive delivery, observer refresh, and native daemon foundations are shipped; richer native presence, notifications, and channels are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, goals, strategist, briefings, reviews, and observer-driven state are shipped foundations; stronger learning loops and intervention quality are still left |
| 06. Embodied UX | `[ ]` | Village UI, avatar casting, quest surfaces, and settings exist; the fuller life-OS shell and stronger ambient UX are still left |
| 07. Ecosystem And Leverage | `[ ]` | Skills, MCP, catalog/install surfaces, and delegation foundations are shipped; stronger workflow composition and extension ergonomics are still left |

## Delivery Order

1. Trust Boundaries
2. Execution Plane
3. Runtime Reliability
4. Presence And Reach
5. Guardian Intelligence
6. Embodied UX
7. Ecosystem And Leverage

## Stable Interfaces

- the browser and WebSocket chat surface
- the observer daemon ingest path
- `SKILL.md`-based skill loading
- runtime-path-based LLM routing and fallback settings
- runtime audit and eval harness contracts

## Current Shipped Slice On `develop`

- [x] local guardian stack with browser UI, backend APIs, WebSocket chat, scheduler, observer loop, and native macOS daemon
- [x] 17 built-in tool capabilities exposed through the registry, with native and MCP-backed execution surfaces
- [x] 9 scheduler jobs and 5 observer source boundaries wired into the current product
- [x] provider-agnostic LLM runtime with ordered fallback chains, health-aware rerouting, runtime-path profile preferences, wildcard path rules, runtime-path model overrides, runtime-path fallback overrides, and local-runtime routing
- [x] runtime audit visibility across chat, scheduler including evening-review degraded-input fallbacks, observer, screen observation summary/cleanup, proactive delivery transport, MCP lifecycle and manual test API flows, skills toggle/reload flows, embedding, vector store, soul file, filesystem, browser, sandbox, and web search paths
- [x] deterministic eval harness coverage for core runtime, audit, observer, storage, tool-boundary, MCP test API, skills API, screen repository, and evening-review degraded-input contracts

## Recommended Reading Order

1. Read [Development Status](./STATUS.md) for the live shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read `01` through `07` for detailed per-workstream checklists.
4. Read the `Research` section for the product and architecture target.
5. Treat `/legacy` docs as supporting history, not the live source of truth.
