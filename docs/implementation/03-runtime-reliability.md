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
- [x] repeatable runtime eval harness for guardian, observer, storage, and integration seam checks
- [x] runtime audit coverage across chat, WebSocket, scheduler jobs, strategist helpers, proactive delivery transport, MCP lifecycle and manual test API paths, observer lifecycle, embedding, vector store, soul file, filesystem, browser, sandbox, and web search paths

## Working On Now

- [x] Runtime Reliability remains the repo-wide hardening track
- [ ] close the remaining routing, observability, and eval gaps outside the already-covered seams

## Still To Do On `develop`

- [ ] deepen provider routing beyond profile preferences, path patterns, model overrides, ordered fallback chains, and cooldown rerouting with richer policy-aware selection
- [ ] broaden local-model routing beyond the current helper, scheduler, core agent, delegation, and connected MCP-specialist paths into any remaining runtime paths where it makes sense
- [ ] add observability coverage across any remaining edge helpers and external integration paths beyond the proactive delivery transport and MCP management/test boundaries
- [ ] expand eval coverage beyond deterministic seam checks into broader behavioral contracts

## Non-Goals

- pretending the runtime is done because the fallback baseline works
- live-provider eval dependence for every reliability check

## Acceptance Checklist

- [x] provider failure with configured fallbacks does not collapse the entire chat path
- [x] runtime paths can force distinct primary and fallback routing without changing the global baseline
- [x] dynamic runtime paths can inherit wildcard routing rules without losing exact-path control
- [x] a local or non-OpenRouter path is demonstrably possible across helper, scheduler, core agent, delegation, and connected MCP-specialist flows
- [ ] key flows are observable and easy to debug
- [ ] the project has broad repeatable eval coverage for core behavior
