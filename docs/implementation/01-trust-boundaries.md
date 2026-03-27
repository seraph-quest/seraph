# Workstream 01: Trust Boundaries

## Status On `develop`

- [ ] Workstream 01 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [04. Trust And Governance](/research/trust-and-governance)

## Shipped On `develop`

- [x] tool policy modes for `safe`, `balanced`, and `full`
- [x] MCP access modes for `disabled`, `approval`, and `full`
- [x] approval gates for high-risk actions in chat and WebSocket flows
- [x] structured audit logging for tool calls, tool results, approvals, and runtime events
- [x] secret egress redaction for surfaced responses and errors
- [x] vault CRUD with audit visibility
- [x] session-scoped secret references for safer downstream tool usage
- [x] vault-backed MCP credential placeholders for manual server auth, with raw sensitive-header rejection in MCP management APIs and credential-source audit on connect/test paths
- [x] explicit execution-boundary metadata and approval behavior surfaced for tools and reusable workflows
- [x] forced approval wrapping for reusable workflows that cross high-risk or approval-mode MCP boundaries
- [x] approval records now preserve fingerprints, resume context, and thread labels so replay and resume surfaces can recover safely instead of guessing the target thread
- [x] reusable workflow approvals and workflow replay/resume now bind to explicit approval-context snapshots so stale approvals or replay plans fail closed when the privileged surface changes instead of trusting only tool name plus arguments
- [x] built-in delegation now keeps vault-backed secret operations on a dedicated `vault_keeper` specialist so generic memory delegation no longer inherits direct secret read/write tools, and secret-bearing tasks route to that privileged surface before generic memory cues can capture them

## Working On Now

- [x] this workstream has now shipped both `execution-safety-hardening-v1` and `execution-safety-hardening-v2`
- [x] explicit secret-reference containment now blocks raw secret injection into non-injection-safe tools while leaving MCP and future explicit injection surfaces available
- [x] this workstream now also ships `execution-safety-hardening-v5`
- [x] manual MCP token setup now moves through vault-backed placeholders instead of raw config headers, and workflow approval/replay now preserves trust-boundary context instead of reusing stale approval assumptions
- [x] built-in delegation now separates generic memory planning from vault-backed secret management, with a dedicated privileged specialist plus deterministic eval coverage for secret-routing precedence and specialist tool isolation

## Still To Do On `develop`

- [ ] tighten isolation between planning, privileged execution, connector credential use, approval replay, and future workflow layers beyond the current metadata, credential-placeholder, and specialist-surface hardening passes
- [ ] add deeper policy distinctions inside MCP and external execution paths
- [ ] keep trust UX strict without making approvals noisy or unusable

## Non-Goals

- a fake sense of safety based only on prompt instructions
- broadening high-risk execution before policy paths are clear

## Acceptance Checklist

- [x] privileged reusable workflows now expose an explicit policy path through approval behavior and execution-boundary metadata
- [x] high-risk actions are pauseable and resumable with audit visibility
- [x] secret references are now scoped to explicit injection-safe execution surfaces instead of resolving across the whole tool surface
- [ ] secret use is fully scoped and auditable end to end

## Current Slice Record

### `mcp-vault-credential-injection-v1`

- status: complete on `feat/trust-boundary-hardening-batch-e-v1`, pending inclusion in the aggregate `#235` PR
- scope:
  - manual MCP token updates now store bearer tokens in the vault and persist only `${vault:...}` placeholders in `mcp-servers.json`
  - MCP server validate and mutation APIs now reject raw sensitive credential headers so new manual connector auth paths stay on env or vault-backed placeholders
  - MCP connect and test paths now resolve env plus vault placeholders explicitly and emit credential-source audit details instead of silently treating all headers as the same trust surface
- validation:
  - `python3 -m py_compile backend/src/tools/mcp_manager.py backend/src/api/mcp.py backend/tests/test_mcp_manager.py backend/tests/test_mcp_api.py`
  - `git diff --check`
  - `cd backend && .venv/bin/python -m pytest tests/test_mcp_manager.py tests/test_mcp_api.py tests/test_tools_api.py -q`
  - `cd docs && npm run build`
- subagent review:
  - `Galileo` found one real regression: MCP validation and test endpoints could still raise a `500` if vault-backed credential resolution failed before endpoint-level error handling ran
  - root cause: `validate` and `test` were calling credential resolution too early; validation should inspect placeholders without reading secret values, and test should degrade credential-resolution failures into an operator-visible auth/config result instead of crashing
  - fix: validation now uses non-secret-bearing header inspection, and the test endpoint converts credential-resolution failures into `auth_required` with explicit `credential_resolution_failed` audit detail
  - no remaining material issue was found after the credential-resolution fix and the per-header credential-source inspection path landed

### `approval-replay-boundary-enforcement-v1`

- status: complete on `feat/trust-boundary-hardening-batch-e-v1`, pending inclusion in the aggregate `#235` PR
- scope:
  - workflow approvals now fingerprint an explicit approval context instead of only workflow tool name plus arguments, so changed privileged workflow surfaces cannot silently consume earlier approvals
  - workflow audit and workflow-run history now persist approval-context snapshots and context-aware run fingerprints, so pending-approval projection and replay reasoning stay tied to the privileged surface that actually ran
  - workflow replay and resume now fail closed with `approval_context_changed` when the recorded trust boundary no longer matches the current workflow surface
- validation:
  - `python3 -m py_compile backend/src/approval/repository.py backend/src/tools/audit.py backend/src/tools/approval.py backend/src/workflows/manager.py backend/src/api/workflows.py backend/src/evals/harness.py backend/tests/test_approval_tools.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_approval_tools.py tests/test_workflows.py tests/test_activity_api.py tests/test_operator_api.py tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios -q`
- subagent review:
  - `Zeno` found one real regression risk: approval-context comparison was treating ordered `step_tools` and `execution_boundaries` lists as identity, which would have blocked replay or approval reuse on harmless list-order drift
  - root cause: approval-context fingerprints and mismatch checks were comparing raw list order instead of canonical trust-boundary sets
  - fix: workflow approval-context generation and workflow-run approval-context normalization now canonicalize those lists before hashing or comparison, and a regression test pins that behavior
  - validation also caught stale eval drift from the first Batch E slice: the MCP test API harness still mocked the old unresolved-var path instead of `resolve_headers()`, so the eval contract was updated to match the shipped endpoint behavior before closing the slice

### `planner-secret-surface-isolation-v1`

- status: complete on `feat/trust-boundary-hardening-batch-e-v1`, pending inclusion in the aggregate `#235` PR
- scope:
  - built-in specialist routing now splits generic guardian-record handling from vault-backed secret management by moving `store_secret`, `get_secret`, `get_secret_ref`, `list_secrets`, and `delete_secret` onto a dedicated `vault_keeper`
  - explicit delegation aliases and auto-routing now treat `vault`, `secret`, `credential`, and `api key` work as a privileged vault surface instead of letting generic memory delegation inherit those tools
  - deterministic eval-harness coverage now pins the specialist tool split and the secret-routing precedence so future delegation refactors do not silently reopen secret-bearing planning paths
- validation:
  - `python3 -m py_compile backend/src/agent/specialists.py backend/src/tools/delegate_task_tool.py backend/src/agent/factory.py backend/src/evals/harness.py backend/tests/test_specialists.py backend/tests/test_delegate_task_tool.py backend/tests/test_delegation.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_specialists.py tests/test_delegate_task_tool.py tests/test_delegation.py -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -k "build_all_specialists or workflow_runner" -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py::test_main_lists_available_scenarios tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details -q`
- review pass:
  - the first cut still had a real trust-boundary regression risk: `delegate_task` checked generic memory keywords before vault keywords, so a prompt like `Remember this password` would have routed to `memory_keeper` instead of the privileged vault surface
  - root cause: the delegation matcher gave generic `memory` and `remember` cues higher precedence than secret-bearing cues, which let planning-style phrasing capture tasks that should stay on the secret-management boundary
  - fix: vault routing now takes precedence over generic memory cues, with regression tests plus a dedicated eval-harness scenario pinning both the precedence rule and the specialist tool split
