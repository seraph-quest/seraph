---
sidebar_position: 3
title: Workstream 03 - Runtime Reliability
---

# Workstream 03: Runtime Reliability

## Status On `develop`

- [ ] Workstream 03 is only partially shipped on `develop`.

## Goal

Make Seraph more resilient, observable, and predictable under real usage.

## Shipped On `develop`

- [x] degraded-mode fallback in the token-aware context window when `tiktoken` cannot load offline
- [x] centralized provider-agnostic LLM runtime settings
- [x] direct LiteLLM fallback path
- [x] ordered fallback-chain routing across shared completion and agent-model paths
- [x] health-aware cooldown rerouting across shared completion and agent-model paths
- [x] runtime-path-specific primary model overrides across shared completion and agent-model paths
- [x] runtime-path-specific ordered fallback-chain overrides across shared completion and agent-model paths
- [x] first-class local runtime profile for bounded helper flows, scheduled completion-based jobs, core agent model factories, delegation paths, and connected MCP specialists
- [x] timeout-safe audit visibility into primary-vs-fallback LLM completion and agent-model behavior
- [x] fallback-capable `smolagents` model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for core guardian, tool, MCP specialist, and observer/audit-runtime reliability contracts
- [x] lifecycle audit events for REST chat, WebSocket chat, and the full scheduler job surface
- [x] real tool execution audit events for call, result, and failure across agent transports
- [x] strategist tool calls and background helper flows, including context-window summarization, now emit runtime audit coverage
- [x] MCP server connection lifecycle emits runtime audit coverage for connect, disconnect, auth-required, and failure states
- [x] sandbox, browser, and web-search tool boundaries emit runtime integration coverage for success, blocked, timeout, empty-result, and failure paths
- [x] observer calendar, git, goal, and time source boundaries emit runtime integration coverage for unavailable, empty-result, success, and failure paths
- [x] observer context refresh and queued-bundle delivery emit background runtime audit coverage
- [x] proactive delivery-gate decisions emit runtime audit coverage for delivered, queued, and failed paths
- [x] observer daemon screen-context ingest emits runtime audit coverage for receive, persist success, and persist failure

## Working On Now

- [x] Runtime Reliability remains the current repo-wide hardening track
- [ ] close the remaining runtime observability gaps outside the main agent, scheduler/helper flows, current integration lifecycle coverage, and observer surfaces already instrumented

## Still To Do On `develop`

- [ ] deepen provider routing beyond the current explicit runtime-path primary and fallback overrides, ordered fallback, and cooldown rerouting with richer policy-aware selection
- [ ] broaden local-model routing beyond the current helper, scheduled completion, core agent-model, delegation, and connected MCP specialist paths into any remaining runtime paths where it makes sense
- [ ] add observability coverage across any remaining edge helpers and external integration paths beyond observer refresh, calendar/git/goal/time sources, daemon ingest, proactive delivery gating, current MCP lifecycle coverage, and the browser/sandbox/web-search tool boundaries
- [ ] expand eval coverage beyond the current runtime seam checks, including broader provider-routing and remaining edge-path contracts

## Acceptance Checklist

- [x] provider failure with configured fallbacks does not collapse the entire chat path
- [x] a local or non-OpenRouter path is demonstrably possible across helper, scheduled completion, core agent, delegation, and connected MCP specialist flows
- [x] runtime paths can force distinct primary and fallback routing without changing the global runtime baseline
- [ ] key flows are observable and easier to debug
- [ ] the project has repeatable eval coverage for core behavior
