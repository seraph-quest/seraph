---
sidebar_position: 2
title: Master Plan
---

# Seraph Master Plan

## Summary

Seraph now uses one live planning structure only:

- one master plan file
- one live status file
- one file per workstream

The old `sections / seasons / batches` split is not the live planning surface anymore. If an old roadmap file still exists in the repo, treat it as legacy reference material.

## Current Status

This roadmap should be read alongside [Development Status](./status-report).

Legend for the checklist column:

- `[x]` fully shipped on `develop`
- `[ ]` not fully shipped on `develop`

| Workstream | Checklist | Notes |
|---|---|---|
| 01. Trust Boundaries | `[ ]` | Policy modes, approvals, audit logging, secret redaction, and scoped secret references are shipped; deeper execution isolation and narrower secret-use paths are still left |
| 02. Execution Plane | `[ ]` | Shell, browser, MCP, discovery, and visible tool execution foundations are shipped; richer process, browser, and workflow execution are still left |
| 03. Runtime Reliability | `[ ]` | Ordered fallbacks, runtime-path primary and fallback overrides, local routing across helper/agent/delegation/MCP-specialist paths, runtime audit visibility including embedding, vector-store, soul-file, and filesystem boundaries, and deterministic eval foundations are shipped; richer provider selection, remaining edge coverage, and broader evals are still left |
| 04. Presence And Reach | `[ ]` | Browser, WebSocket, proactive delivery, and the native observer daemon are shipped foundations; desktop presence, notifications, channels, and cross-surface continuity are still left |
| 05. Guardian Intelligence | `[ ]` | Soul, memory, strategist, goals, daily briefing, and evening review foundations are shipped; the deeper adaptive guardian layer is still left |
| 06. Embodied UX | `[ ]` | Village UI, quest log, avatar, ambient indicators, and settings surfaces are shipped; the fuller life-OS shell is still left |
| 07. Ecosystem And Leverage | `[ ]` | Skills, MCP, and delegation foundations are shipped; stronger workflow leverage and extension ergonomics are still left |

## Delivery Order

1. Trust Boundaries
2. Execution Plane
3. Runtime Reliability
4. Presence And Reach
5. Guardian Intelligence
6. Embodied UX
7. Ecosystem And Leverage

## Current Shipped Slice On `develop`

- [x] policy-controlled tool and MCP access with approval gates, audit logging, secret redaction, and scoped secret references
- [x] shell, browser, vault, filesystem, goals, and web-search tool foundations with live tool execution in chat
- [x] ordered LLM fallbacks, health-aware rerouting, runtime-path primary and fallback overrides, local helper, agent, delegation, and MCP-specialist runtime paths, and broad runtime audit coverage
- [x] deterministic runtime eval harness coverage for fallback routing, local routing, embedding/vector-store/soul-file/filesystem boundaries, browser/sandbox/web-search tool boundaries, observer boundaries, and audit seams
- [x] browser UI, WebSocket chat, proactive delivery, and a native observer daemon
- [x] soul, memory, goals, strategist, daily briefing, and evening review foundations
- [x] skills, MCP integration, and recursive delegation foundations

## Workstreams

### 01. [Trust Boundaries](../plan/trust-boundaries)

- [ ] not fully shipped on `develop`
- focus: make Seraph safer and more governable before expanding autonomy further

### 02. [Execution Plane](../plan/execution-plane)

- [ ] not fully shipped on `develop`
- focus: make Seraph better at doing real work, not just reasoning about it

### 03. [Runtime Reliability](../plan/runtime-reliability)

- [ ] not fully shipped on `develop`
- focus: routing, fallbacks, observability, evals, and degraded-mode behavior

### 04. [Presence And Reach](../plan/presence-and-reach)

- [ ] not fully shipped on `develop`
- focus: make Seraph reachable outside a browser tab

### 05. [Guardian Intelligence](../plan/guardian-intelligence)

- [ ] not fully shipped on `develop`
- focus: move from retrieval + heuristics toward richer human understanding and adaptation

### 06. [Embodied UX](../plan/embodied-ux)

- [ ] not fully shipped on `develop`
- focus: make Seraph feel alive, legible, and motivating without becoming gimmicky

### 07. [Ecosystem And Leverage](../plan/ecosystem-and-leverage)

- [ ] not fully shipped on `develop`
- focus: compound Seraph through reusable extensions without losing product clarity

## Recommended Reading Order

1. Read [Development Status](./status-report) for the current shipped vs unfinished view.
2. Read this file for workstream ordering and current scope.
3. Read the workstream files under `Plan` for detailed shipped work, active focus, and remaining work.
4. Treat the legacy phase docs as background material only.
