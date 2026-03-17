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
- [x] first-class local runtime profile for helper, all current scheduled completion jobs, core agent, delegation, and connected MCP-specialist paths
- [x] timeout-safe audit visibility into primary-vs-fallback completion and agent-model behavior
- [x] session-bound LLM runtime traces for helper and agent flows, including request-id visibility for routing and fallback decisions
- [x] fallback-capable model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for guardian, core chat behavior, proactive flow behavior, delegated workflow behavior, observer, storage, and integration seam checks
- [x] runtime audit coverage across chat, WebSocket, scheduler jobs including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, strategist helpers, proactive delivery transport, MCP lifecycle and manual test API paths, skills toggle/reload paths, observer lifecycle plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths

## Working On Now

- [x] Runtime Reliability remains the repo-wide hardening track
- [x] `incident-trace-gap-closure` is the active PR from the numbered sequence below
- [x] the next repo-wide value sequence after this PR is prepared below so follow-on work is explicit before this queue lands
- [ ] close the remaining routing and eval gaps outside the already-covered seams

## Still To Do On `develop`

- [ ] deepen provider routing beyond profile preferences, path patterns, model overrides, ordered fallback chains, and cooldown rerouting with richer policy-aware selection
- [ ] expand eval coverage beyond the shipped REST, WebSocket, proactive, and delegated behavioral contracts into broader behavioral coverage

## Completed PR Sequence

This sequence is the finished Runtime Reliability execution order. A checked item can be in an open PR before it is shipped on `develop`.

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

This is the next ordered PR list after `incident-trace-gap-closure`. It mixes the remaining highest-value Runtime Reliability work with the next highest-leverage product-system gaps from Guardian Intelligence, Presence, and Ecosystem.

1. [ ] `provider-policy-scoring`:
   deepen provider routing with weighted policy scoring, explicit capability preferences, and clearer target ranking so runtime-path selection is stronger than simple preference chains and cooldown skips
2. [ ] `behavioral-evals-guardian-flows`:
   expand behavioral eval coverage beyond chat and scheduler seams into observer refresh, consolidation, proactive delivery, and policy-mode guardrails so broader guardian behavior is regression-tested
3. [ ] `guardian-state-synthesis`:
   build an explicit guardian-state layer that merges observer context, goals, memory, and recent sessions into one structured decision input instead of spreading that reasoning across separate call sites
4. [ ] `intervention-policy-v1`:
   improve proactive delivery quality by making intervene, defer, bundle, and stay-silent decisions depend on guardian state, confidence, and interruption cost instead of only the current heuristic gate
5. [ ] `native-presence-notifications`:
   add the first real non-browser presence path with native notifications and system-level reach so Seraph is not trapped inside the open web tab
6. [ ] `workflow-composition-v1`:
   add first-class multi-step workflow composition across specialists and tools so meaningful work can be expressed as reusable flows rather than isolated tool calls
7. [ ] `guardian-feedback-loop`:
   capture intervention outcomes and explicit or implicit feedback so Seraph can learn which actions were helpful instead of restarting from static heuristics every session

## Non-Goals

- pretending the runtime is done because the fallback baseline works
- live-provider eval dependence for every reliability check

## Acceptance Checklist

- [x] provider failure with configured fallbacks does not collapse the entire chat path
- [x] runtime paths can force distinct primary and fallback routing without changing the global baseline
- [x] dynamic runtime paths can inherit wildcard routing rules without losing exact-path control
- [x] a local or non-OpenRouter path is demonstrably possible across helper, all current scheduled completion jobs, core agent, delegation, and connected MCP-specialist flows
- [x] key flows are observable and easy to debug
- [ ] the project has broad repeatable eval coverage for core behavior beyond the shipped REST, WebSocket, proactive, and delegated behavioral contracts
