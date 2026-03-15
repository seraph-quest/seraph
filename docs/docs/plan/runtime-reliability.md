---
sidebar_position: 3
title: Workstream 03 - Runtime Reliability
---

# Workstream 03: Runtime Reliability

## Goal

Make Seraph more resilient, observable, and predictable under real usage.

## Done

- [x] degraded-mode fallback in the token-aware context window when `tiktoken` cannot load offline
- [x] centralized provider-agnostic LLM runtime settings
- [x] direct LiteLLM fallback path
- [x] timeout-safe audit visibility into primary-vs-fallback LLM completion and agent-model behavior
- [x] fallback-capable `smolagents` model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for core guardian, tool, and observer/audit-runtime reliability contracts
- [x] lifecycle audit events for REST chat, WebSocket chat, and the full scheduler job surface
- [x] real tool execution audit events for call, result, and failure across agent transports
- [x] strategist tool calls and background helper flows now emit runtime audit coverage
- [x] MCP server connection lifecycle emits runtime audit coverage for connect, disconnect, auth-required, and failure states
- [x] observer context refresh and queued-bundle delivery emit background runtime audit coverage
- [x] proactive delivery-gate decisions emit runtime audit coverage for delivered, queued, and failed paths
- [x] observer daemon screen-context ingest emits runtime audit coverage for receive, persist success, and persist failure

## In Progress

- [ ] close the remaining runtime observability gaps outside the main agent, scheduler/helper flows, and current integration lifecycle coverage

## Left To Do

- [ ] broaden model and provider routing beyond the first shared fallback path
- [ ] deepen local-model-capable execution paths beyond API-base swapping
- [ ] add observability coverage across any remaining edge helpers and external integration paths beyond observer refresh, daemon ingest, proactive delivery gating, and current MCP lifecycle coverage
- [ ] expand eval coverage beyond the current runtime seam checks, including broader provider-routing and remaining edge-path contracts

## Done Means

- [ ] provider failure does not collapse the entire chat path
- [ ] a local or non-OpenRouter path is demonstrably possible
- [ ] key flows are observable and easier to debug
- [ ] the project has repeatable eval coverage for core behavior
