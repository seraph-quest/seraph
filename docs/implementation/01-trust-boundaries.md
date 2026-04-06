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
- [x] capability bootstrap and cockpit repair now keep privileged mutation on an explicit operator path by limiting automatic repair to low-risk local workflow or skill toggles while leaving policy lifts, external server enables, installs, and starter-pack activation manual
- [x] catalog installs and starter-pack activation now preflight lifecycle approvals before bundled MCP-backed capability expansion instead of letting approval-required installs surface only after mutation starts
- [x] generated doctor-plan and replay repair bundles no longer batch-apply multiple privileged mutations from one click; multi-step privileged fixes now require step-by-step operator execution
- [x] authenticated MCP-backed tools now preserve source-specific approval and audit context through wrapper layers, and workflow checkpoint/replay paths fail closed when a run crosses into an authenticated external-source boundary instead of treating it like generic MCP read access

## Working On Now

- [x] this workstream has now shipped both `execution-safety-hardening-v1` and `execution-safety-hardening-v2`
- [x] explicit secret-reference containment now blocks raw secret injection into non-injection-safe tools while leaving MCP and future explicit injection surfaces available
- [x] this workstream now also ships `execution-safety-hardening-v5`
- [x] manual MCP token setup now moves through vault-backed placeholders instead of raw config headers, and workflow approval/replay now preserves trust-boundary context instead of reusing stale approval assumptions
- [x] built-in delegation now separates generic memory planning from vault-backed secret management, with a dedicated privileged specialist plus deterministic eval coverage for secret-routing precedence and specialist tool isolation
- [x] this workstream now also ships `capability-bootstrap-autonomy-boundary-v1`
- [x] this workstream now also ships `catalog-install-lifecycle-approval-v1`
- [x] this workstream now also ships `privileged-repair-bundle-gating-v1`
- [x] this workstream now also ships `authenticated-source-boundary-hardening-v1`
- [x] this workstream now also ships `workflow-authenticated-source-drift-enforcement-v1`

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

### `capability-bootstrap-autonomy-boundary-v1`

- status: complete on `feat/privileged-autonomy-boundary-hardening-batch-h-v1`, intended for the aggregate Batch H PR for `#248`
- root cause addressed:
  - capability preflight recommendations for tool-policy elevation, MCP enablement, catalog install, and starter-pack activation had drifted into `autorepair_actions`, so `/api/capabilities/bootstrap` could mutate policy or expand capability reach directly from an advisory planning surface
  - the cockpit bootstrap path then auto-ran any returned `manual_actions`, which meant even actions intentionally left outside backend autorepair could still execute implicitly after a single repair click
- scope:
  - capability bootstrap now limits automatic repair to low-risk local workflow and skill toggles instead of treating policy lifts, external server enables, catalog installs, or starter-pack activation as safe autorepair
  - capability preflight and doctor plans still surface the broader repair sequence, but higher-risk steps now stay in explicit manual actions rather than crossing from planning into execution automatically
  - cockpit bootstrap now records and surfaces manual repair actions without auto-running them, so capability bootstrap preserves the operator boundary instead of silently replaying privileged mutations
- validation:
  - `python3 -m py_compile backend/src/api/capabilities.py backend/tests/test_capabilities_api.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_capabilities_api.py tests/test_eval_harness.py -q`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npm test -- --run src/components/cockpit/CockpitView.test.tsx`
- review pass:
  - direct review against bugs, regressions, and hallucinated assumptions found a second real boundary leak after the backend audit: the cockpit bootstrap flow was auto-running returned `manual_actions`, so high-risk repair steps could still execute implicitly after bootstrap stopped applying them server-side
  - fixed by keeping manual actions operator-visible and doctor-plan-accessible without auto-running them during bootstrap itself

### `catalog-install-lifecycle-approval-v1`

- status: complete on `feat/privileged-autonomy-boundary-hardening-batch-h-v1`, intended for the aggregate Batch H PR for `#248`
- root cause addressed:
  - direct catalog MCP installs already routed through lifecycle approval, but the shared catalog-install path still let starter-pack activation call `install_catalog_item_by_name()` directly, so bundled capability expansion could bypass the same approval envelope once it moved through capability bootstrap instead of the catalog endpoint
  - the first patch assumption also overgeneralized catalog approval as universal; review against the real install contract showed lifecycle approval is risk-based, not required for every low-risk bundled skill package
- scope:
  - catalog install now exposes one shared `require_catalog_install_approval()` seam and starter-pack activation prechecks all bundled install items against it before mutating runtime state
  - bundled MCP-backed capability expansion now requires the same extension-lifecycle approval path whether it starts from the catalog surface or a starter-pack activation path
  - low-risk bundled skill installs remain direct when their validated package carries no lifecycle-risk boundaries, so the approval contract stays narrow instead of turning every install into ceremony
- validation:
  - `python3 -m py_compile backend/src/api/catalog.py backend/src/api/capabilities.py backend/tests/test_catalog_api.py backend/tests/test_capabilities_api.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_catalog_api.py tests/test_capabilities_api.py -q`
- review pass:
  - direct review against bugs and hallucinated assumptions caught one bad generalization in the first test update: bundled catalog skill installs were treated as universally approval-gated even when the validated package had no lifecycle-risk boundaries
  - fixed by keeping approval enforcement risk-based and aligning the catalog tests with the actual permission-summary contract instead of broadening the boundary without evidence
  - PR review then found one real retry-loop bug in the shared starter-pack path: the first approval preflight was consuming approved lifecycle requests before any install happened, so a multi-item privileged pack could burn approval for item A, hit `approval_required` on item B, and then force the operator to re-approve item A on the next retry
  - fixed by making the starter-pack approval preflight non-consuming and consuming each lifecycle approval only at the corresponding install mutation point

### `privileged-repair-bundle-gating-v1`

- status: complete on `feat/privileged-autonomy-boundary-hardening-batch-h-v1`, intended for the aggregate Batch H PR for `#248`
- root cause addressed:
  - after bootstrap stopped auto-running manual actions, the cockpit still let generated doctor plans and replay repair bundles run multiple privileged mutations in one click, which collapsed inspection and execution back into one opaque operator gesture
- scope:
  - cockpit repair execution now distinguishes low-risk batchable actions from privileged mutations
  - low-risk local toggles and test actions can still run as a repair sequence, but generated bundles that mix or stack privilege-changing actions now stop at an explicit "step-by-step execution" boundary instead of chaining them automatically
  - single explicit privileged actions remain runnable so existing operator recovery paths still work when the user intentionally executes one concrete mutation
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npm test -- --run src/components/cockpit/CockpitView.test.tsx`
- review pass:
  - direct review against execution-envelope drift found that the operator surface was still treating a machine-generated privileged repair bundle as a safe batch operation even after backend bootstrap hardening
  - fixed by adding a cockpit-side batch gate so generated privileged repair plans cannot silently chain multiple mutations from one click

### `mcp-vault-credential-injection-v1`

- status: complete on `develop` via PR `#245`
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
  - PR review follow-up found two more real boundary gaps: sensitive-header validation still accepted mixed raw-secret plus placeholder values because it only searched for any `${...}` token, and missing-vault preflight still scanned raw header text before env substitution so env-backed vault placeholders could degrade into opaque downstream auth failures
  - root cause: the API guard was substring-based instead of requiring the credential-bearing portion of a sensitive header to be fully placeholder-backed, and `inspect_headers()` only looked for `${vault:...}` before env expansion
  - fix: sensitive headers now only accept fully placeholder-backed values such as `${ENV}`, `${vault:key}`, or `Bearer ${...}`, and header inspection now resolves env placeholders before vault-missing detection and credential-source classification
  - follow-up focused review found no remaining material issue after the placeholder-tightening and env-to-vault inspection fix landed

### `approval-replay-boundary-enforcement-v1`

- status: complete on `develop` via PR `#245`
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

### `authenticated-source-boundary-hardening-v1`

- status: complete on `feat/authenticated-source-boundary-hardening-batch-m-v1`, intended for the aggregate Batch M PR for `#260`
- root cause addressed:
  - authenticated external-source context was being attached only to raw MCP tools in `mcp_manager`, but the runtime immediately wrapped those tools for secret-ref handling, audit, and approval, so `/api/tools`, workflow approval context, and checkpoint gating could all regress back to generic `external_mcp`
  - that meant authenticated connector-backed MCP actions looked narrower in config than they actually were at execution time, and workflow checkpoint reuse could still treat authenticated-source runs as resumable safe branches
- scope:
  - tool policy helpers now unwrap wrapper chains when reading MCP source context, so authenticated source metadata survives audit/approval/secret-ref wrappers instead of disappearing after connect time
  - `/api/tools` now exposes the narrower `authenticated_external_source` execution boundary plus `authenticated_source=true` for MCP rows that actually cross that trust surface, without widening the operator contract for unrelated native or workflow rows
  - workflow approval context now records authenticated source systems and blocks checkpoint reuse whenever a workflow step depends on an authenticated external source, so replay/resume stays fail-closed for connector-backed runs
- validation:
  - `python3 -m py_compile backend/src/api/tools.py backend/src/tools/policy.py backend/src/workflows/manager.py backend/tests/test_mcp_manager.py backend/tests/test_tools_api.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_mcp_manager.py tests/test_tools_api.py tests/test_workflows.py -q`
  - `cd docs && npm run build`
- review pass:
  - `Copernicus` found two real live-path gaps after the first implementation pass: `SecretRefResolvingTool` still dropped `get_approval_context`, so authenticated MCP tools lost the narrower boundary before forced approval wrapped them, and it also dropped `get_audit_failure_payload`, so authenticated-source attribution disappeared on failure even when call/result payloads were source-aware
  - fixed by forwarding both hooks through the secret-ref wrapper and adding regressions that pin authenticated approval context persistence plus authenticated MCP failure audit payloads through the real wrapped execution path

### `workflow-authenticated-source-drift-enforcement-v1`

- status: complete on `feat/execution-isolation-batch-ad-v1`, intended for the first Batch AD PR for `#299`
- root cause addressed:
  - workflow run projection was normalizing approval context down to risk, boundaries, secret-ref acceptance, and step tools, which silently dropped authenticated-source flags and source-system provenance from recorded runs
  - the current workflow surface was also derived from static workflow metadata, which cannot see the live wrapped MCP tool surface, so replay and resume drift checks could miss a change from generic external access to authenticated external-source execution
- scope:
  - workflow approval-context normalization now preserves authenticated-source truth and normalized source-system provenance when those fields are actually present, while staying backward-compatible for older runs that never recorded them
  - workflow run projection now derives current workflow approval context from runtime-built workflow tools before falling back to static metadata, so authenticated MCP/source wrappers participate in replay and resume blocking
  - replay and resume blocking now catches authenticated-source drift deterministically instead of collapsing both sides back to the same generic boundary summary
- validation:
  - `python3 -m py_compile backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q -k "approval_context or authenticated_source_system"`
- review pass:
  - direct review against bugs and regressions exposed one real backward-compatibility problem in the first pass: adding default `authenticated_source=false` and empty `source_systems=[]` into normalized approval context changed legacy workflow fingerprints for runs that never carried source metadata
  - fixed by only persisting authenticated-source fields in normalized approval context when they are materially present, which keeps older approval fingerprints stable while still surfacing real authenticated-source drift

### `workflow-delegation-boundary-enforcement-v1`

- status: complete on `feat/delegated-workflow-boundary-hardening-batch-ad-v2`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - reusable workflows that used `delegate_task` only advertised the generic `delegation` surface in workflow metadata and runtime approval context, even when the delegated specialist actually crossed vault or authenticated external-source boundaries
  - that made delegated workflows look safer than the real execution path, which weakened safe/balanced exposure decisions and let checkpoint/replay policy reason about an underspecified trust surface
- scope:
  - delegated workflow approval context now resolves the selected specialist when the route is explicit or renderable from runtime inputs, and merges the delegated specialist's real risk, boundary, and authenticated-source signals into the workflow approval snapshot
  - workflow metadata now derives policy modes, risk, execution boundaries, and secret-ref handling from that richer approval context instead of trusting only the direct step tool list
  - workflows with unresolved dynamic delegation targets now fail closed as `full` / `high` and block checkpoint reuse instead of staying exposed as generic low-risk delegation
- validation:
  - `python3 -m py_compile backend/src/tools/delegate_task_tool.py backend/src/workflows/manager.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q -k "delegated_vault_routes or authenticated_delegated_source or delegate_target_is_dynamic or authenticated_source_context_drift or authenticated_source_system_reordering or approval_context_marks_authenticated_mcp_sources"`
- review pass:
  - direct review against bugs and regressions found two real implementation problems in the first pass: static vault delegation was still reading a low-risk surface because the specialist graph exposed tools as a dict and the new boundary walk treated it like a list, and the first vault regression overclaimed `accepts_secret_refs` even though the real fail-closed signal is the delegated secret-injection boundary
  - fixed by normalizing specialist tool collections before walking delegated boundaries and by pinning the actual checkpoint-blocking contract instead of inventing a broader secret-ref claim

### `extension-removal-boundary-enforcement-v1`

- status: complete on `feat/extension-removal-boundary-hardening-batch-ad-v3`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - install, update, enable, and secret-bearing configure already route through extension lifecycle approval, but direct removal still mutated the workspace package and runtime state immediately
  - that meant a high-risk extension could be torn down without the same package-digest-bound approval envelope that already protects the rest of the lifecycle mutation surface
- scope:
  - extension removal now reuses the existing lifecycle approval seam before destructive workspace-package teardown
  - removal approvals bind to the current installed package digest, so a changed package must be re-approved before the delete path can consume the destructive mutation
  - low-risk workspace removals remain direct because the lifecycle approval profile is still driven by the extension's real declared boundaries instead of turning every uninstall into ceremony
- validation:
  - `python3 -m py_compile backend/src/api/extensions.py backend/tests/test_extensions_api.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_extensions_api.py -q -k "remove_high_risk_extension_requires_approval or remove_high_risk_extension_requires_new_approval_if_package_changes or install_high_risk_extension_requires_new_approval_if_package_changes or install_and_enable_high_risk_extension_require_approval"`
- review pass:
  - direct review against regressions found the real risk was not broad uninstall approval in general, but the missing destructive boundary specifically for already-approved high-risk extensions; the fix keeps low-risk removals direct and only tightens the high-risk mutation seam

### `extension-disable-and-connector-target-boundary-enforcement-v1`

- status: complete on `feat/extension-config-boundary-hardening-batch-ad-v4`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - high-risk extension disable still bypassed lifecycle approval even after install, update, enable, and remove were hardened, which let privileged or safety-relevant packages be silently deactivated
  - packaged connector approval was also extension-scoped rather than target-scoped, so approval for one high-risk connector could be reused for a sibling connector inside the same pack when their boundary profile matched
  - degraded packages could lose derived contribution permission profiles entirely, which made the disable path fail open because the preview no longer advertised the lifecycle approval boundary that the manifest still declared
- scope:
  - high-risk extension disable now routes through the same lifecycle approval seam as the rest of the mutation surface instead of remaining an ungated teardown path
  - packaged connector enable and disable approvals now fingerprint the specific connector target reference, name, and type, so sibling connectors cannot reuse each other's lifecycle approvals
  - lifecycle approval now falls back to declared manifest permissions when a degraded package loses its derived approval profile, keeping disable fail-closed under workflow or contribution validation drift
- validation:
  - `python3 -m py_compile backend/src/api/extensions.py backend/tests/test_extensions_api.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_extensions_api.py -x -vv -k "install_and_enable_high_risk_extension_require_approval or disable_high_risk_extension_requires_new_approval_if_package_changes or extension_connector_enable_endpoint_controls_packaged_mcp_runtime or connector_lifecycle_approval_is_scoped_to_each_packaged_connector_target or install_toggle_and_remove_workspace_connector_extension or enable_rejects_degraded_extension_with_invalid_workflow_before_approval"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first implementation pass exposed a real fail-open regression: once a high-risk package became degraded, its projected `approval_profile` dropped out and disable reverted to `200` without approval
  - fixed by deriving a fallback lifecycle approval profile from declared manifest permissions when the live preview no longer carries one, which keeps the disable seam hard without widening low-risk packages that still declare no lifecycle boundaries

### `planner-secret-surface-isolation-v1`

- status: complete on `develop` via PR `#245`
- scope:
  - built-in specialist routing now splits generic guardian-record handling from vault-backed secret management by moving `store_secret`, `get_secret`, `get_secret_ref`, `list_secrets`, and `delete_secret` onto a dedicated `vault_keeper`
  - explicit delegation aliases and auto-routing now treat `vault`, `secret`, `credential`, and `api key` work as a privileged vault surface instead of letting generic memory delegation inherit those tools
  - deterministic eval-harness coverage now pins the specialist tool split and the secret-routing precedence so future delegation refactors do not silently reopen secret-bearing planning paths
- validation:
  - `python3 -m py_compile backend/src/agent/specialists.py backend/src/tools/delegate_task_tool.py backend/src/agent/factory.py backend/src/evals/harness.py backend/tests/test_specialists.py backend/tests/test_delegate_task_tool.py backend/tests/test_delegation.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_specialists.py tests/test_delegate_task_tool.py tests/test_delegation.py -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -k "build_all_specialists or workflow_runner" -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py::test_main_lists_available_scenarios tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details -q`
- subagent review:
  - the first review pressure exposed a real trust-boundary regression risk: `delegate_task` still checked generic memory keywords before vault keywords, so a prompt like `Remember this password` would have routed to `memory_keeper` instead of the privileged vault surface
  - root cause: the delegation matcher gave generic `memory` and `remember` cues higher precedence than secret-bearing cues, which let planning-style phrasing capture tasks that should stay on the secret-management boundary
  - fix: vault routing now takes precedence over generic memory cues, with regression tests plus a dedicated eval-harness scenario pinning both the precedence rule and the specialist tool split
  - follow-up `Pauli` review found no remaining material issue after the precedence fix landed
