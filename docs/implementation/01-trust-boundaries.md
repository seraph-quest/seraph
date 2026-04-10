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
- [x] tool metadata and runtime secret-ref handling now fail closed to explicit field-level injection surfaces, and connector-backed authenticated mutation bundles now reject undeclared payload fields instead of passing arbitrary write arguments through to external runtimes
- [x] managed execution now also uses disposable worker roots outside the workspace for direct command and background-process runtime state, MCP secret-ref resolution now requires an explicit credential-egress allowlist, and delegated/workflow/operator trust receipts now preserve connector-egress plus branch-handoff trust partitions instead of flattening privileged execution back to a generic session boundary

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
- [x] this workstream now also ships the first Batch AT aggregate for field-scoped secret-reference injection, per-tool boundary narrowing, and allowlisted authenticated mutation payloads
- [x] this workstream now also ships the second Batch AT aggregate for session-bound managed-process recovery, so background-process listing, output reads, and stop controls fail closed outside the originating session instead of leaving cross-session recovery handles live
- [x] this workstream now also ships the first Batch BA aggregate for explicit background-process confirmation policy and session-partitioned process trust metadata, so `start_process` no longer inherits the generic high-risk approval path and process-runtime approvals/audit receipts now carry the narrower managed-process boundary contract
- [x] this workstream now also ships the second Batch BA aggregate for disposable worker runtime isolation, explicit credential-egress allowlists for secret-bearing MCP execution, and preserved trust-partition receipts across delegated, workflow, and background-handoff surfaces

## Still To Do On `develop`

- [ ] tighten isolation between planning, privileged execution, connector credential use, approval replay, and future workflow layers beyond the current metadata, disposable worker roots, credential-egress policy, and specialist-surface hardening passes
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

### `extension-source-mutation-boundary-enforcement-v1`

- status: complete on `feat/extension-source-boundary-hardening-batch-ad-v5`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - workspace extension source edits were writing directly into installed package files after validation, but they never re-entered the lifecycle approval seam
  - that meant a high-risk installed package could be materially rewritten after installation approval without any new destructive or privileged mutation approval, and the edited reference itself was not part of the approval identity
- scope:
  - source-save mutations now route through lifecycle approval before writing into installed workspace extension files
  - source-save approvals are target-scoped by edited reference, so approval for one high-risk file does not unlock a sibling workflow or other editable file in the same package
  - low-risk package editing remains direct, and broken-manifest repair still works because source-save approval falls back to the current extension payload rather than assuming the package is already fully healthy
- validation:
  - `python3 -m py_compile backend/src/api/extensions.py backend/tests/test_extensions_api.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_extensions_api.py -x -vv -k "high_risk_extension_source_save_requires_lifecycle_approval or high_risk_extension_source_save_requires_new_approval_if_package_changes or workspace_extension_source_save_updates_package_members or broken_workspace_manifest_can_be_loaded_and_repaired_via_source_api or high_risk_source_save_approval_is_scoped_to_each_target"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first target-scoping regression used packaged MCP source files and ran into unrelated packaged-MCP draft validation noise, which obscured the approval contract the slice was supposed to prove
  - fixed by pinning target-scoped source-save approval on a multi-workflow high-risk package instead, so the regression exercises the same save path without coupling to MCP-specific source semantics

### `extension-config-mutation-boundary-enforcement-v1`

- status: complete on `feat/extension-config-approval-hardening-batch-ad-v6`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - high-risk extension configure only re-entered lifecycle approval when a request carried a brand-new secret value, so materially different non-secret config changes on already-approved high-risk packages could still change runtime behavior without fresh approval
  - configure approvals were also package-scoped only, which meant the approval identity did not bind to either the requested config mutation or the current stored config subset that the mutation was changing
- scope:
  - high-risk configure now derives a normalized config-mutation approval context from recognized configurable targets and binds approval to both the requested config fragment and the current stored config fragment for those keys
  - redacted no-op reconfigures remain direct because replaying the stored config placeholder shape does not produce a new mutation context
  - unknown targets and purely invalid unmapped config entries still fall through to normal validation instead of consuming lifecycle approval for config paths the package does not actually declare
- validation:
  - `python3 -m py_compile backend/src/api/extensions.py backend/tests/test_extensions_api.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_extensions_api.py -x -vv -k "install_configure_and_toggle_wave2_contribution_surfaces"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first proof shape targeted a low-risk managed-connector package and therefore would not have exercised the hardened lifecycle path at all
  - fixed by pinning the regression on the already high-risk `wave2` pack, where changing only the non-secret `node_url` now requires fresh approval while a redacted no-op reconfigure stays direct

### `authenticated-source-audit-visibility-hardening-v1`

- status: complete on `feat/authenticated-source-audit-hardening-batch-ad-v8`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - authenticated-source and approval context were only guaranteed in audit events when a tool supplied custom MCP audit payloads, so wrapper-composed tools could lose source provenance and privilege context from default `tool_call`, `tool_result`, or `tool_failed` events
  - that weakened the operator trail exactly where Batch AD is trying to make privileged paths easier to inspect and explain
- scope:
  - the audit wrapper now enriches default and custom audit payloads with wrapper-visible source context and approval context whenever they are available
  - authenticated MCP tools keep their credential/source provenance visible even when they rely on default audit summaries instead of custom payload hooks
  - the enrichment remains additive and fail-open, so existing custom audit payloads still win for summaries and bespoke fields
- validation:
  - `python3 -m py_compile backend/src/tools/audit.py backend/tests/test_tool_audit.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_tool_audit.py -q -k "secret_ref_wrapper_preserves_authenticated_mcp_failure_audit_payload or audited_tool_defaults_include_authenticated_source_context"`
  - `git diff --check`
- review pass:
  - the real risk here was not missing audit entirely, but silent loss of authenticated-source provenance whenever a wrapper chain fell back to the generic audit path
  - fixed by enriching audit details centrally in the audit wrapper instead of requiring every privileged tool surface to remember its own source-context plumbing

### `workflow-replay-delegation-boundary-enforcement-v1`

- status: complete on `feat/workflow-replay-delegation-boundary-hardening-batch-ad-v10`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - workflow replay and resume projection normalized risk, secret, and authenticated-source fields, but it still ignored delegated-specialist routing fields when comparing the recorded trust boundary to the current one
  - the underlying workflow runtime also restored checkpoint state without re-checking the parent run's approval context, so direct resume could bypass the API-side `approval_context_changed` guard and reuse state across delegated-boundary drift
- scope:
  - approval-context normalization for workflow replay now includes `delegated_specialists` and `delegation_target_unresolved`, so delegated routing drift is treated as a real trust-boundary change instead of noise
  - checkpoint restore now compares the parent run's normalized approval context to the current workflow approval context before reusing any saved step state, and it fails closed when the boundary changed
  - reordering of delegated specialists or authenticated source-system metadata remains non-material and does not trigger false drift
- validation:
  - `python3 -m py_compile backend/src/workflows/manager.py backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q -k "resume_rejects_when_delegation_boundary_changes or detects_delegated_specialist_context_drift or ignores_delegated_specialist_reordering or approval_context_list_reordering or approval_context_changes or authenticated_source_context_drift or authenticated_source_system_reordering or failure_payload_keeps_checkpoint"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first proof pass only blocked the direct resume seam, which would still have left replay projection blind to delegated-specialist drift
  - while pinning the fix, I also caught two real test regressions of my own: one existing workflow failure-payload method was accidentally dedented during the edit, and one older approval-context reordering assertion was overwritten with the wrong replay expectation
  - both were corrected before publish, and the expanded targeted suite now covers the old stable cases alongside the new delegated-boundary drift cases

### `workflow-legacy-replay-boundary-enforcement-v1`

- status: complete on `feat/workflow-legacy-replay-boundary-hardening-batch-ad-v11`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - legacy workflow runs can still carry reusable checkpoint context without any recorded approval context at all, and direct resume trusted those payloads as long as the workflow name matched
  - the API replay projection had the same blind spot for protected medium-risk surfaces like authenticated external sources, where replay could still look valid even though the run predates trust-boundary tracking
- scope:
  - privileged workflow surfaces now require tracked approval lineage before replay or resume is considered safe, including authenticated sources, delegated specialist routes, unresolved delegation, secret-bearing boundaries, external MCP, and other high-risk surfaces
  - direct checkpoint restore now fails closed when a parent run predates trust-boundary tracking for the current protected workflow surface
  - workflow run projection now reports `approval_context_missing` for those legacy protected runs so replay/resume UI and API paths surface the same fresh-run requirement
- validation:
  - `python3 -m py_compile backend/src/workflows/manager.py backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q -k "approval_context_is_missing_for_authenticated_surface or resume_rejects_legacy_checkpoint_for_authenticated_surface or approval_context_changes or authenticated_source_context_drift or authenticated_source_system_reordering or delegated_specialist_context_drift or delegated_specialist_reordering or approval_context_list_reordering"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first implementation pass had a real syntax regression in the replay recovery-message branch because I duplicated the nested conditional while adding the new `approval_context_missing` case
  - after fixing that, I reran the targeted seam set to make sure the older authenticated-source and delegated-stability cases still behaved the same while only the legacy protected runs started failing closed

### `workflow-surface-boundary-truthfulness-v1`

- status: complete on `feat/workflow-surface-boundary-hardening-batch-ad-v12`, intended for the next Batch AD PR for `#299`
- root cause addressed:
  - the workflows runs API already marked trust-boundary drift correctly with `approval_context_changed` and `approval_context_missing`, but it still serialized checkpoint candidates and a concrete `resume_plan` for those same blocked runs
  - that left operator surfaces with stale branch/retry metadata even though `/api/workflows/runs/{run_identity}/resume-plan` would fail closed, which made the API contract less truthful than the runtime boundary
- scope:
- workflow run projection now clears `resume_from_step`, `resume_checkpoint_label`, `checkpoint_candidates`, and `resume_plan` whenever replay is blocked because the trust boundary changed or because the run predates tracked lineage for the current privileged surface
- operator timeline and activity ledger now re-sanitize blocked workflow runs too, so stale replay drafts, checkpoint metadata, and retry actions do not leak back in through downstream surfaces if upstream run payloads ever regress
  - the same fail-closed rule now applies to both completed workflow runs reconstructed from audit events and still-pending runs reconstructed from call state, so the operator surface does not advertise stale continuation paths on either side
  - blocked trust-boundary runs also stop surfacing approval-style thread continuation prompts, so activity and cockpit layers fall back to the recovery message instead of implying that approval alone can unblock the run
  - blocked trust-boundary runs now also drop stale replay repair and step-recovery actions, so policy-lift or repair suggestions only appear when the run is actually blocked by availability or repair state rather than by privilege drift
  - delegated and direct authenticated-source workflow approval context now carries credential-source provenance too, so replay and resume checks can detect credential-route drift rather than only server-name drift
  - delegated workflow approval context now also records delegated tool inventory, so replay and resume checks can detect a widened specialist tool surface even when the specialist name stayed the same
  - pending approvals, repair guidance, and other non-boundary replay blocks still keep their existing branch metadata where that metadata is part of the intended operator contract
- validation:
  - `python3 -m py_compile backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q -k "approval_context_changes or authenticated_source_context_drift or delegated_specialist_context_drift or approval_context_is_missing_for_authenticated_surface or approval_context_list_reordering or authenticated_source_system_reordering or delegated_specialist_reordering"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the real issue here was not backend resume enforcement, which was already fail-closed, but surface drift: blocked runs still looked resumable because branch metadata was generated before the trust-boundary stop was applied consistently across the serialized run shape
  - fixed by moving the trust-boundary decision up into the run projection itself and pinning both mismatch and legacy-missing-context cases in the workflow suite

### `authenticated-mutation-and-boundary-explainability-v1`

- status: complete on `feat/execution-hardening-batch-al-v1`, intended for the next Batch AL PR for `#342`
- root cause addressed:
  - high-risk extension mutation approvals now fingerprint requested config or source changes correctly, but operator-facing approval surfaces still exposed only a thin pending-approval shell instead of the exact target, lifecycle boundary, or trust context being approved
  - sync runtime audit helpers also tried to persist through `asyncio.run(...)` when no event loop was active, which made aiosqlite worker threads race a closed loop in CI and turned a real runtime-audit best-effort path into a backend test failure
  - workflow run projection already computed structured trust-boundary payloads, but downstream operator and activity surfaces only partially surfaced that structure and older tests still did not pin the new metadata shape
- scope:
  - high-risk extension configure approvals now bind to materially changed config targets only, and high-risk source-save approvals now bind to the exact requested content hash plus current content hash instead of only package drift
  - pending approval surfaces across `/api/approvals/pending`, `/api/operator/timeline`, and `/api/activity/ledger` now carry structured `approval_scope`, lifecycle-approval state, extension action/package metadata, and approval-context trust data instead of flattening everything into just `tool_name` and `risk_level`
  - workflow run surfaces now expose a structured `trust_boundary` payload through workflow, operator, and activity APIs so boundary drift is legible as first-class metadata rather than only an implicit replay block reason
  - sync runtime-audit helpers now fail soft when no event loop is active and use tracked background tasks when a loop exists, so audit persistence no longer destabilizes sync helper paths like context-window summarization
  - deterministic regression coverage now pins the scoped extension approval payloads, the workflow/operator/activity trust-boundary surface, the approval explainability surface, and the no-loop runtime-audit path
- validation:
  - `python3 -m py_compile backend/src/approval/surfaces.py backend/src/api/approvals.py backend/src/api/activity.py backend/src/api/operator.py backend/src/api/extensions.py backend/src/api/workflows.py backend/src/audit/runtime.py backend/src/evals/harness.py backend/tests/test_approvals_api.py backend/tests/test_activity_api.py backend/tests/test_context_window.py backend/tests/test_eval_harness.py backend/tests/test_extensions_api.py backend/tests/test_operator_api.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_context_window.py tests/test_approvals_api.py -q`
  - `cd backend && .venv/bin/python -m pytest tests/test_extensions_api.py -q -k "source_save or wave2_contribution_surfaces"`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py tests/test_operator_api.py tests/test_activity_api.py -q -k "approval_context_changed or approval_context_missing or boundary_is_blocked or aggregates_llm_calls_budget_and_threaded_actions or aggregates_threaded_workflows_notifications_and_repairs"`
  - `cd backend && .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "approval_explainability_surface_behavior or workflow_boundary_blocked_surface_behavior or context_window_summary_audit or test_main_lists_available_scenarios"`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first extension-approval explainability pass over-reported unchanged redacted config groups, which would have made the new `approval_scope` surface less truthful than the actual fingerprinted boundary
  - the first targeted proof for source-save scoping also reused a packaged MCP source file path and picked up unrelated draft-validation behavior, so the regression was moved to a multi-workflow high-risk package where the approval contract itself was the only variable
  - the live CI review also surfaced a separate real backend failure: sync runtime-audit helpers were creating closed-loop teardown noise through `asyncio.run(...)`; the fix now skips persisted runtime audit when no loop exists instead of pretending the path is safe

### `connector-backed-authenticated-mutation-boundaries-v1`

- status: complete on `feat/execution-hardening-batch-al-v2`, intended for the next Batch AL PR for `#342`
- root cause addressed:
  - Seraph already exposed typed authenticated source-read planning, but connector-backed write paths still degraded into generic unavailable operations without a first-class mutation boundary, which left approvals, audit, and operator guidance too implicit for privileged source writes
  - the growing backend matrix also still let a few heavy suites dominate hosted-runner time when they stalled, and frontend CI was still depending on worker concurrency that did not match the stable local contract
- scope:
  - typed managed-connector write contracts now surface explicit mutation planning metadata, including whether the operation mutates state, whether approval is required, the scoped approval boundary to request, and the audit category to record
  - `/api/capabilities/source-mutation-plan` now returns structured connector-backed mutation scope for authenticated source writes, and the native `plan_source_mutation` tool exposes the same boundary to operator-facing planning flows without pretending the write is executable when no runtime route exists
  - source-capability rendering now distinguishes mutating operations from read/evidence paths and makes approval-required write surfaces explicit instead of flattening them into generic capability rows
  - deterministic regression coverage now pins connector-backed write planning, scoped approval payloads for ready and degraded routes, the native mutation-plan tool contract, and the heavier CI shard/frontend stability contract
  - backend shard execution now applies per-file timeouts and file-level splits for the long-tail delivery, observer, and workflow suites, while frontend CI runs the canonical test script with a single worker so hosted runs match the stable local path
- validation:
  - `python3 -m py_compile backend/src/extensions/source_operations.py backend/src/api/capabilities.py backend/src/tools/source_mutation_tool.py backend/src/tools/source_capabilities_tool.py backend/src/native_tools/registry.py backend/src/evals/harness.py backend/scripts/run_backend_test_shard.py backend/tests/test_source_operations.py backend/tests/test_source_capabilities.py backend/tests/test_run_backend_test_shard.py backend/tests/test_eval_harness.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_source_operations.py tests/test_source_capabilities.py tests/test_run_backend_test_shard.py -q`
  - `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test .venv/bin/python -m pytest tests/test_eval_harness.py -q -k "source_mutation_boundary_behavior or source_adapter_evidence_behavior or source_review_routine_behavior or test_main_lists_available_scenarios"`
  - `cd frontend && npm test -- --maxWorkers=1`
  - `cd docs && npm run build`
  - `git diff --check`
- review pass:
  - the first connector-mutation proof shape reused a low-risk managed connector fixture, which would not have exercised the high-risk approval boundary at all; the regression was moved onto a real bound write route with explicit approval metadata
  - degraded connector write routes initially dropped their mutation and approval metadata for `requires_config`, `disabled`, and `no_runtime_adapter`, which made the new planner and capability rendering understate privileged write scope on broken authenticated connectors; the shipped path now preserves mutation metadata for those degraded states and pins the real inventory builder contract
  - the first mutation-plan output also hardcoded generic scope and audit values instead of reading the route metadata it had just attached, so the planner would have silently mis-scoped future write routes; the shipped path now derives approval-scope and audit-event values from the selected operation metadata
  - the live CI review showed the practical failure mode was still runner stability rather than a clean product assertion, so the batch ships heavier shard splitting plus single-worker frontend CI instead of claiming a phantom logic bug

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
