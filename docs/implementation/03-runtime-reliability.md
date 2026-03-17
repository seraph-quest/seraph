# Workstream 03: Runtime Reliability

## Status On `develop`

- [ ] Workstream 03 is only partially shipped on `develop`.

## Shipped On `develop`

- [x] degraded-mode fallback in the context window when `tiktoken` cannot load offline
- [x] centralized provider-agnostic LLM runtime settings
- [x] ordered fallback-chain routing across shared completion and agent-model paths
- [x] health-aware cooldown rerouting across shared completion and agent-model paths
- [x] runtime-path-specific profile preference chains across shared completion and agent-model paths
- [x] runtime-path-specific primary model overrides
- [x] runtime-path-specific fallback-chain overrides
- [x] wildcard runtime-path routing rules, with exact-path overrides taking precedence
- [x] first-class local runtime profile for helper, scheduler, core agent, delegation, and connected MCP-specialist paths
- [x] timeout-safe audit visibility into primary-vs-fallback completion and agent-model behavior
- [x] fallback-capable model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for guardian, core chat behavior, observer, storage, and integration seam checks
- [x] runtime audit coverage across chat, WebSocket, scheduler jobs including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, strategist helpers, proactive delivery transport, MCP lifecycle and manual test API paths, skills toggle/reload paths, observer lifecycle plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths

## Working On Now

- [x] Runtime Reliability remains the repo-wide hardening track
- [x] `behavioral-evals-core-chat` is the active PR from the numbered sequence below
- [ ] close the remaining routing, observability, and eval gaps outside the already-covered seams

## Still To Do On `develop`

- [ ] deepen provider routing beyond profile preferences, path patterns, model overrides, ordered fallback chains, and cooldown rerouting with richer policy-aware selection
- [ ] broaden local-model routing beyond the current helper, scheduler, core agent, delegation, and connected MCP-specialist paths into any remaining runtime paths where it makes sense
- [ ] add observability coverage across any remaining edge helpers and external integration paths beyond the proactive delivery transport, MCP management/test, skills state-management, and screen observation repository boundaries
- [ ] expand eval coverage beyond the shipped REST and WebSocket chat behavioral contracts into broader behavioral coverage, even with daily-briefing, activity-digest, and evening-review degraded-input auditing already covered

## Planned PR Sequence

This sequence is execution order for upcoming PRs. A checked item can be in an open PR before it is shipped on `develop`.

1. [x] `behavioral-evals-core-chat`:
   add behavioral eval contracts for REST chat and WebSocket chat, including fallback, timeout, approval, and audit expectations
2. [ ] `behavioral-evals-proactive-flows`:
   add behavioral evals for strategist tick, daily briefing, evening review, and activity digest with expected degraded behavior and delivery outcomes
3. [ ] `behavioral-evals-tool-heavy-flow`:
   add one delegated tool-heavy workflow contract covering routing, tool execution, audit, and degraded or failure handling
4. [ ] `provider-policy-capabilities`:
   add provider capability metadata and runtime-path policy intents such as `fast`, `cheap`, `reasoning`, and `local_first`
5. [ ] `provider-routing-decision-audit`:
   log structured routing decisions that explain the chosen target, rejected targets, and rejection reasons
6. [ ] `local-routing-gap-closure`:
   extend local routing only into the remaining runtime paths that still have clear product value after the eval and policy work lands
7. [ ] `incident-trace-gap-closure`:
   close the last real observability blind spots where a production incident still cannot be explained from one trace

## Non-Goals

- pretending the runtime is done because the fallback baseline works
- live-provider eval dependence for every reliability check

## Acceptance Checklist

- [x] provider failure with configured fallbacks does not collapse the entire chat path
- [x] runtime paths can force distinct primary and fallback routing without changing the global baseline
- [x] dynamic runtime paths can inherit wildcard routing rules without losing exact-path control
- [x] a local or non-OpenRouter path is demonstrably possible across helper, scheduler, core agent, delegation, and connected MCP-specialist flows
- [ ] key flows are observable and easy to debug
- [ ] the project has broad repeatable eval coverage for core behavior beyond the shipped REST and WebSocket chat contracts
