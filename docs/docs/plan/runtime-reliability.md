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
- [x] timeout-safe audit visibility into primary-vs-fallback LLM completion behavior
- [x] fallback-capable `smolagents` model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for core guardian and tool reliability contracts
- [x] lifecycle audit events for REST chat, WebSocket chat, and scheduled proactive jobs

## In Progress

- [ ] broaden observability beyond the first direct LLM events and first chat/proactive-job lifecycle coverage

## Left To Do

- [ ] broaden model and provider routing beyond the first shared fallback path
- [ ] deepen local-model-capable execution paths beyond API-base swapping
- [ ] add observability coverage across more tool and runtime paths
- [ ] expand eval coverage beyond the first core scenarios

## Done Means

- [ ] provider failure does not collapse the entire chat path
- [ ] a local or non-OpenRouter path is demonstrably possible
- [ ] key flows are observable and easier to debug
- [ ] the project has repeatable eval coverage for core behavior
