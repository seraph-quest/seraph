# Workstream 03: Runtime Reliability

## Status On `develop`

- [ ] Workstream 03 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [03. Runtime And Reliability](/research/runtime-and-reliability)

## Shipped On `develop`

- [x] degraded-mode fallback in the context window when `tiktoken` cannot load offline
- [x] centralized provider-agnostic LLM runtime settings
- [x] ordered fallback-chain routing across shared completion and agent-model paths
- [x] health-aware cooldown rerouting across shared completion and agent-model paths
- [x] runtime-path-specific profile preference chains across shared completion and agent-model paths
- [x] runtime-path-specific primary model overrides
- [x] runtime-path-specific fallback-chain overrides
- [x] wildcard runtime-path routing rules, with exact-path overrides taking precedence
- [x] first-class local runtime profile for helper, all current scheduled completion jobs, core agent, delegation, and connected MCP-specialist paths
- [x] strict runtime-path provider safeguards for required capability intents plus cost/latency guardrails, with degrade-open behavior when no compliant target exists
- [x] timeout-safe audit visibility into primary-vs-fallback completion and agent-model behavior
- [x] session-bound LLM runtime traces for helper and agent flows, including request-id visibility for routing and fallback decisions
- [x] fallback-capable model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for guardian, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, workflow composition behavior, observer, storage, and integration seam checks
- [x] runtime audit coverage across chat, WebSocket, scheduler jobs including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, strategist helpers, proactive delivery transport, MCP lifecycle and manual test API paths, skills toggle/reload paths, observer lifecycle plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths

## Working On Now

- [x] Runtime Reliability is no longer the repo-wide active focus after the first guardian behavior baseline shipped
- [x] the previous runtime-focused queue is fully shipped on `develop`
- [ ] broader guardian behavioral eval depth remains in the refreshed repo-wide 10-PR horizon through `guardian-behavioral-evals-v3`
- [ ] richer provider policy still remains to do on `develop`, but the new safeguard layer means the remaining work is now deeper ranking/explainability rather than first-pass hard requirements and tier guardrails

## Still To Do On `develop`

- [ ] deepen provider selection policy beyond the shipped weighted scoring, required capability safeguards, tier guardrails, path patterns, explicit overrides, ordered fallbacks, and cooldown rerouting
- [ ] expand eval coverage beyond the shipped REST, WebSocket, observer refresh, delivery policy, consolidation, proactive, tool-policy guardrail, delegated workflow, and workflow-composition behavioral contracts

## Completed PR Sequence

This sequence is the finished Runtime Reliability execution order on `develop`.

1. [x] `behavioral-evals-core-chat`:
   add behavioral eval contracts for REST chat and WebSocket chat, including fallback, timeout, approval, and audit expectations
2. [x] `behavioral-evals-proactive-flows`:
   add behavioral evals for strategist tick, daily briefing, evening review, and activity digest with expected degraded behavior and delivery outcomes
3. [x] `behavioral-evals-tool-heavy-flow`:
   add one delegated tool-heavy workflow contract covering routing, tool execution, audit, and degraded or failure handling
4. [x] `provider-policy-capabilities`:
   add provider capability metadata and runtime-path policy intents such as `fast`, `cheap`, `reasoning`, and `local_first`
5. [x] `provider-routing-decision-audit`:
   log structured routing decisions that explain the chosen target, rejected targets, and rejection reasons
6. [x] `local-routing-gap-closure`:
   verify the remaining worthwhile local-routing surface across onboarding, strategist, and all scheduled completion jobs so the queue does not stay open on assumed gaps
7. [x] `incident-trace-gap-closure`:
   bind session-aware helper and agent LLM runtime events into the same audit trace so target choice, reroutes, and fallback outcomes can be explained for one session incident

## Next Most Valuable PR Sequence

This is the next ordered Runtime Reliability slice after the completed incident-trace queue. The repo-wide cross-workstream queue lives in `00-master-roadmap.md`.

1. [x] `provider-policy-scoring`:
   deepen provider routing with weighted policy scoring, explicit capability preferences, and clearer target ranking so runtime-path selection is stronger than simple preference chains and cooldown skips
2. [x] `behavioral-evals-guardian-flows`:
   expand behavioral eval coverage beyond chat and scheduler seams into observer refresh, consolidation, proactive delivery, and policy-mode guardrails so broader guardian behavior is regression-tested

## Non-Goals

- pretending the runtime is done because the fallback baseline works
- live-provider eval dependence for every reliability check

## Acceptance Checklist

- [x] provider failure with configured fallbacks does not collapse the entire chat path
- [x] runtime paths can force distinct primary and fallback routing without changing the global baseline
- [x] dynamic runtime paths can inherit wildcard routing rules without losing exact-path control
- [x] a local or non-OpenRouter path is demonstrably possible across helper, all current scheduled completion jobs, core agent, delegation, and connected MCP-specialist flows
- [x] key flows are observable and easy to debug
- [ ] the project has broad repeatable eval coverage for core guardian behavior beyond the shipped REST, WebSocket, observer refresh, delivery policy, consolidation, proactive, tool-policy guardrail, delegated workflow, and workflow-composition behavioral contracts
