---
title: S1-B3 Runtime Reliability
---

# S1-B3: Runtime Reliability

## Intent

Make Seraph more resilient, observable, and predictable under real usage.

## Capabilities in scope

- model/provider routing and fallback
- at least one local-model-capable path
- richer observability for agent behavior and tool execution
- evaluation harness for core agent workflows
- clearer degraded-mode behavior when dependencies fail

## Current progress

This batch is just starting, but reliability hardening has begun.

Shipped in this batch so far:

- degraded-mode fallback in the token-aware context window when `tiktoken` cannot load offline

Still open inside this batch:

- model/provider routing and fallback beyond the single primary path
- clearer local-model-capable execution paths
- broader observability coverage beyond trust-boundary events
- a repeatable evaluation harness for core guardian and tool flows

## Non-goals

- exhaustive benchmark program across every model
- production-grade hosted observability platform
- fully automated eval-driven deployment gating

## Required architectural changes

- centralize model selection and fallback strategy
- standardize runtime event logging across critical paths
- define test/eval scenarios for guardian, tool, and proactive flows
- add explicit error-handling behavior for provider/tool outages

## Likely files/systems touched

- model configuration and agent factory paths
- scheduler and proactive jobs
- logging and evaluation utilities
- tool failure and timeout handling

## Acceptance criteria

- provider failure does not collapse the entire chat path
- a local or non-OpenRouter path is demonstrably possible
- key flows are observable and easier to debug
- the project has a repeatable evaluation harness for core behavior

## Dependencies on earlier batches

- can begin in parallel with [S1-B2 Execution Plane](./s1-b2-execution-plane)
- benefits from [S1-B1 Trust Boundaries](./s1-b1-trust-boundaries) defining clearer execution paths

## Open risks

- fallback logic can become inconsistent if added ad hoc
- observability can create noise if events are not scoped well
- local model support may underperform unless task routing is explicit
