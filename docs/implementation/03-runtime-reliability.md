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
- [x] strict runtime-path provider safeguards for required capability intents plus cost, latency, task-class, and budget guardrails, with degrade-open behavior when no compliant target exists
- [x] first simulation-grade provider planning pass that scores candidate routes before execution, makes budget steering explicit, and exposes chosen-versus-rejected route details plus simulated route order across runtime audit, operator timeline, and activity-ledger surfaces
- [x] timeout-safe audit visibility into primary-vs-fallback completion and agent-model behavior
- [x] session-bound LLM runtime traces for helper and agent flows, including request-id visibility for routing and fallback decisions
- [x] fallback-capable model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for guardian, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, workflow composition behavior, threaded workflow recovery, capability repair, observer, storage, and integration seam checks
- [x] runtime audit coverage across chat, WebSocket, scheduler jobs including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, strategist helpers, proactive delivery transport, MCP lifecycle and manual test API paths, skills toggle/reload paths, observer lifecycle plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths

## Working On Now

- [x] Runtime Reliability is no longer the repo-wide active focus after provider explainability and budgets v3 plus guardian behavioral evals v9 shipped
- [x] the previous runtime-focused slice sequence is fully shipped on `develop`
- [x] `provider-policy-safeguards-v3`, `provider-policy-explainability-and-budgets-v3`, and `guardian-behavioral-evals-v9` are now represented in the shipped batch, including richer routing reason surfaces, budget/task-class guardrails, and deeper deterministic proof for bootstrap plus branching behavior
- [x] `provider-policy-simulation-and-budget-planning-v1` is now represented in the shipped branch state, including candidate-route scoring, explicit budget steering, and cross-surface route legibility
- [ ] richer runtime work still remains on `develop`, but the remaining gap is now broader long-running or production-like eval depth and stronger live-provider failure modeling rather than missing first-pass planning surfaces

## Still To Do On `develop`

- [ ] deepen provider selection policy beyond the first simulation-grade route scoring and explicit budget steering pass, especially with stronger live-provider health, cost, and production-like failure feedback
- [ ] expand eval coverage beyond the shipped REST, WebSocket, observer refresh, delivery policy, strategist-learning continuity, consolidation, proactive, tool-policy guardrail, threaded workflow recovery, capability repair/bootstrap, delegated workflow, and workflow-composition behavioral contracts

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

## Follow-On Slice Sequence Now Shipped

This follow-on Runtime Reliability slice sequence is now fully shipped on `develop`.
New runtime work should be activated through GitHub issues and the GitHub Project rather than a doc-owned task tracker.

1. [x] `provider-policy-scoring`:
   deepen provider routing with weighted policy scoring, explicit capability preferences, and clearer target ranking so runtime-path selection is stronger than simple preference chains and cooldown skips
2. [x] `behavioral-evals-guardian-flows`:
   expand behavioral eval coverage beyond chat and scheduler seams into observer refresh, consolidation, proactive delivery, and policy-mode guardrails so broader guardian behavior is regression-tested

## Current Slice Record

### `provider-policy-simulation-and-budget-planning-v1`

- status: complete on `feat/provider-policy-simulation-batch-f-v1`, pending inclusion in the aggregate Batch F PR for `#236`
- root cause addressed:
  - runtime routing already had weighted target scoring, fallback chains, and guardrails, but it still lacked a first-class route-planning surface
  - budget steering was still implicit in the final target order, and operators could not inspect why one plausible route beat another across audit, operator, and activity-ledger surfaces
- scope:
  - updated `backend/src/llm_runtime.py` so runtime routing now annotates candidate routes with `budget_steering_mode`, budget headroom, budget-preference score, and simulated route order before execution, while using that steering signal as a tie-break inside route selection
  - extended runtime decision details so audit records now expose `selected_route_score`, `selected_budget_preference_score`, `selected_budget_headroom`, and a `simulated_routes` lane that explains the chosen route against its alternatives
  - updated `backend/src/api/operator.py` and `backend/src/api/activity.py` so operator timeline rows and projected Activity Ledger `llm_call` rows expose the new route-planning and budget-steering details instead of dropping them at the API boundary
  - extended `backend/src/evals/harness.py` plus the runtime, operator, and activity API tests so deterministic proofs now cover budget-steered route choice, simulated-route visibility, and the projected route-planning metadata
- local regression fixed before the slice stayed complete:
  - the first activity-ledger patch still dropped the new routing fields because `_request_metadata_value(...)` only passed through non-empty strings and the final `llm_call` metadata projection copied only a narrower legacy subset of routing metadata
  - that meant `budget_steering_mode` and numeric route scores were present in runtime audit events but silently disappeared from the Activity Ledger projection
  - fixed by teaching `_request_metadata_value(...)` to preserve numeric routing metadata and widening the `llm_call` projection to carry the new budget-steering and route-score fields
- validation:
  - `python3 -m py_compile backend/src/llm_runtime.py backend/src/api/activity.py backend/src/api/operator.py backend/src/evals/harness.py backend/tests/test_llm_runtime.py backend/tests/test_activity_api.py backend/tests/test_operator_api.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_llm_runtime.py tests/test_activity_api.py tests/test_operator_api.py tests/test_eval_harness.py -q`
    - result: `89 passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios -q`
    - result: `1 passed`
- subagent review:
  - a focused subagent review was started for bugs, regressions, and hallucinated assumptions before the slice record was written
  - the first returned review response was not usable as a review artifact because it mixed the requested findings pass with out-of-scope operational mutations instead of returning a clean read-only findings list
  - the landed slice therefore relies on direct diff verification plus the targeted runtime validation above, and that direct review found no additional material bugs, regressions, or unsupported implementation claims in the shipped diff

## Non-Goals

- pretending the runtime is done because the fallback baseline works
- live-provider eval dependence for every reliability check

## Acceptance Checklist

- [x] provider failure with configured fallbacks does not collapse the entire chat path
- [x] runtime paths can force distinct primary and fallback routing without changing the global baseline
- [x] dynamic runtime paths can inherit wildcard routing rules without losing exact-path control
- [x] a local or non-OpenRouter path is demonstrably possible across helper, all current scheduled completion jobs, core agent, delegation, and connected MCP-specialist flows
- [x] key flows are observable and easy to debug
- [ ] the project has broad repeatable eval coverage for core guardian behavior beyond the shipped REST, WebSocket, observer refresh, delivery policy, consolidation, proactive, tool-policy guardrail, threaded workflow recovery, capability repair/bootstrap, delegated workflow, and workflow-composition behavioral contracts
