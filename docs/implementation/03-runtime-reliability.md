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
- [x] richer provider planning and route comparison now score candidate routes before execution, make budget steering explicit, carry per-target live feedback plus production-readiness and capability-gap state into route-comparison metadata around selection, and expose planning-winner versus retained-primary tradeoffs, alternate-route margins, and route-comparison summaries across runtime audit, operator timeline, and activity-ledger surfaces
- [x] operator control-plane surfaces now also synthesize runtime posture, extension health, continuity summaries, and review receipts alongside usage rollups, so deployment and governance legibility does not require reconstructing team state from raw runtime or audit rows
- [x] timeout-safe audit visibility into primary-vs-fallback completion and agent-model behavior
- [x] session-bound LLM runtime traces for helper and agent flows, including request-id visibility for routing and fallback decisions
- [x] fallback-capable model wrappers for chat, onboarding, strategist, and specialists
- [x] repeatable runtime eval harness for guardian, core chat behavior, observer refresh and delivery behavior, session consolidation behavior, tool/MCP policy guardrails, proactive flow behavior, delegated workflow behavior, workflow composition behavior, threaded workflow recovery, capability repair, observer, storage, and integration seam checks
- [x] runtime audit coverage across chat, WebSocket, scheduler jobs including daily-briefing, activity-digest, and evening-review degraded-input fallbacks, strategist helpers, proactive delivery transport, MCP lifecycle and manual test API paths, skills toggle/reload paths, observer lifecycle plus screen observation summary/cleanup boundaries, embedding, vector store, soul file, vault repository, filesystem, browser, sandbox, and web search paths
- [x] a first benchmark-proof reporting layer now groups the deterministic runtime harness into explicit benchmark suites for memory/workflow continuity, browser or desktop execution, planning or retrieval reporting, and governed improvement gates, with the same suite contract visible through the operator API and governed self-evolution receipts

## Working On Now

- [x] Runtime Reliability is no longer the repo-wide active focus after provider explainability and budgets v3 plus guardian behavioral evals v9 shipped
- [x] the previous runtime-focused slice sequence is fully shipped on `develop`
- [x] `provider-policy-safeguards-v3`, `provider-policy-explainability-and-budgets-v3`, and `guardian-behavioral-evals-v9` are now represented in the shipped batch, including richer routing reason surfaces, budget/task-class guardrails, and deeper deterministic proof for bootstrap plus branching behavior
- [x] `provider-policy-simulation-and-budget-planning-v1` is now represented in the shipped branch state, including candidate-route scoring, explicit budget steering, and cross-surface route legibility
- [ ] richer runtime work still remains on `develop`, but the remaining gap is now broader live-provider, long-running, and production-like eval depth rather than missing first-pass planning or route-comparison surfaces

## Still To Do On `develop`

- [ ] broaden live-provider and long-running integration proof beyond the shipped deterministic routing audit, operator/activity projection, benchmark-facing runtime planning details, and the current REST, WebSocket, observer refresh, delivery policy, strategist-learning continuity, consolidation, proactive, tool-policy guardrail, threaded workflow recovery, capability repair/bootstrap, delegated workflow, and workflow-composition behavioral contracts

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

### `provider-planning-benchmark-proof-and-live-integration-depth-v2`

- status: complete on `feat/provider-planning-batch-ay-v1`, pending inclusion in the aggregate Batch AY PR for `#366`
- root cause addressed:
  - the first planning pass could score routes, but it still hid why Seraph retained a healthy primary when a standby route won the raw planning score
  - operator/activity surfaces and the runtime audit still lacked a denser chosen-versus-alternate comparison contract that benchmark-facing proof could point to directly
  - the merged `develop` eval suite also had a stale guardian judgment detail contract that no longer matched the current conservative-ambiguity wording and live-vs-procedural-learning setup
- scope:
  - updated `backend/src/llm_runtime.py` so runtime routing now carries planning-priority components, capability-gap penalties, live-feedback penalties, planning winners, retained-primary or legacy-ordering policy mode, best-alternate margins, and route-comparison summaries alongside the selected route
  - extended `backend/src/api/operator.py` and `backend/src/api/activity.py` so operator and activity surfaces preserve the richer planning comparison metadata instead of collapsing back to only selected-model order
  - extended `backend/src/evals/harness.py` and the targeted runtime tests so deterministic proof now covers retained-primary versus planning-winner behavior, best-alternate margins, benchmark-facing comparison summaries, and the repaired guardian judgment detail contract
- local regression fixed before the slice stayed complete:
  - `tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details` on `develop` still asserted an older conservative-ambiguity phrase and no longer injected the live-versus-procedural timing conflict needed for the learning-conflict diagnostics it expected
  - fixed by making the guardian judgment eval scenario deterministically patch scoped live learning plus recent scoped feedback and by checking the ambiguity guardrail via behavior-relevant wording instead of one stale sentence literal
- validation:
  - `python3 -m py_compile backend/src/llm_runtime.py backend/src/api/operator.py backend/src/api/activity.py backend/src/evals/harness.py backend/tests/test_llm_runtime.py backend/tests/test_operator_api.py backend/tests/test_activity_api.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_operator_api.py tests/test_activity_api.py tests/test_eval_harness.py -q -k "routing_metadata or provider_routing_decision_audit or llm_cost_summary_uses_routing_metadata"`
    - result: `passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_llm_runtime.py -q -k "logs_routing_decision or weighted_policy_scores or guardrail_compliant_target or lower_budget_compliant_route or intent_match_ahead_of_budget_steering"`
    - result: `passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "guardian_judgment_behavior or provider_routing_decision_audit or test_run_runtime_evals_passes_group_4"`
    - result: `1 passed`

### `runtime-planning-provider-feedback-and-live-eval-depth-v2`

- status: complete on `feat/runtime-planning-batch-ak-v1`, pending inclusion in the aggregate Batch AK PR for `#307`
- root cause addressed:
  - the first simulation-grade router could score candidates and expose budget steering, but it still treated live provider failures too shallowly once a target left immediate cooldown
  - route explanations and downstream operator/activity surfaces still lacked a denser production-readiness or failure-risk summary, and the heaviest runtime eval seam was still brittle enough to burn an entire backend shard
- scope:
  - updated `backend/src/llm_runtime.py` so runtime routing now carries per-target live feedback, recent failure classification, production-readiness state, failure-risk scoring, and rejected-target summaries into candidate ranking and route explanations
  - extended `backend/src/api/operator.py` and `backend/src/api/activity.py` so operator and activity surfaces preserve `selected_live_feedback`, `selected_failure_risk_score`, `selected_production_readiness`, `route_explanation`, and `rejected_target_summaries`
  - extended `backend/src/evals/harness.py` so the runtime audit eval now proves the richer failure-risk, readiness, and route-explanation contract instead of only selected model order
  - updated `backend/scripts/run_backend_test_shard.py` to split the heavy `tests/test_eval_harness.py` runtime-eval seam into isolated shard invocations so one long-running scenario cannot consume the whole backend shard budget
- local regression fixed before the slice stayed complete:
  - the first shard-runner regression test still asserted the pre-split command shape for the remaining eval-harness invocation and failed locally once the specialized invocation path landed
  - fixed by pinning the post-split invocation shape in `backend/tests/test_run_backend_test_shard.py` and rerunning the shard-runner suite
- PR review follow-up fixed on the same batch branch:
  - stale provider failures were not decaying out of live routing state, so a target could stay `degraded` after the recent-feedback window had actually expired
  - fixed by expiring recent failure and success counts inside `_feedback_snapshot(...)` and pinning the decay behavior in `backend/tests/test_llm_runtime.py`
- validation:
  - `python3 -m py_compile backend/src/llm_runtime.py backend/src/api/operator.py backend/src/api/activity.py backend/src/evals/harness.py backend/tests/test_llm_runtime.py backend/tests/test_operator_api.py backend/tests/test_activity_api.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_llm_runtime.py -q -k "live_feedback or logs_routing_decision or reroutes_away_from_unhealthy_primary"`
    - result: `4 passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_operator_api.py tests/test_activity_api.py -q -k "routing_metadata or capability_family"`
    - result: `3 passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "provider_routing_decision_audit or test_main_lists_available_scenarios"`
    - result: `1 passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_run_backend_test_shard.py tests/test_backend_test_shards.py -q`
    - result: `15 passed`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test uv run python -m src.evals.harness --scenario provider_routing_decision_audit --indent 0`
    - result: `passed`
- subagent review:
  - focused read-only subagent review was run on the runtime/eval seam and on the API/shard-runner seam
  - both review passes ended with no remaining material findings
  - the only material review finding was the stale-feedback decay bug above, which was fixed on the same branch before PR open

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
- PR review follow-up fixed on the same batch branch:
  - the first route-planning sort key let budget steering outrank plain intent matching when `runtime_policy_scores` was unset, because budget preference was applied before `capability_priority`
  - that could route a lower-budget but less-capable model ahead of the target that actually matched the requested runtime intents
  - fixed by restoring capability-priority ordering ahead of budget steering and adding a regression test that pins intent-first behavior when budget steering is active without explicit runtime score weights
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
