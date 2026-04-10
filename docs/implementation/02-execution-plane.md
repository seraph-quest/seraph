# Workstream 02: Execution Plane

## Status On `develop`

- [ ] Workstream 02 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [05. Execution Plane](/research/execution-plane)

## Shipped On `develop`

- [x] 17 built-in tool capabilities registered through the native tool registry
- [x] shell execution through the sandboxed shell tool
- [x] browser automation through the browser tool
- [x] filesystem read/write tool surface
- [x] soul and goals tool surface for agent self-context
- [x] vault tool surface for controlled secret storage and retrieval
- [x] web-search tool surface
- [x] dynamic MCP tool loading and runtime-managed MCP server configuration
- [x] visible tool execution in chat, WebSocket, onboarding, strategist, and specialist flows
- [x] first-class reusable workflows loaded from defaults and workspace files, with tool, skill, and MCP-aware gating
- [x] starter packs that bundle useful default skills and workflows into operator-invocable capability sets
- [x] explicit workflow metadata for policy modes, execution boundaries, approval behavior, and risk level exposed to operator-facing APIs
- [x] first operator-facing workflow controls for enable/disable, reload, and draft-to-cockpit steering
- [x] workflow loading now rejects underdeclared runtime step tools, and tool/workflow metadata now expose secret-reference acceptance explicitly for injection-safe paths
- [x] workflow execution audit now carries structured workflow-run details, artifact-path lineage, and degraded-step visibility for cockpit/operator views
- [x] workflow run history now exposes boundary-aware replay context, approval counts, risk level, step tools, and artifact lineage through the workflows API
- [x] workflow run history now also exposes pending-approval details, awaiting-approval state, replay guardrails, and thread-aware recovery metadata instead of only recent run summaries
- [x] workflow run history now also persists approval-context snapshots and blocks replay/resume when the workflow trust boundary changes instead of reusing stale approval assumptions
- [x] workflow runtime now persists reusable checkpoint context for safe branches, records structured failed-step payloads for later recovery, and keeps workflow-run listing truthful when a later checkpoint cannot actually be resumed
- [x] typed source-capability surfaces now also expose connector-backed authenticated mutation planning, executable bounded `work_items.write` plus `code_activity.write` routes, scoped approval and audit metadata, native mutation/report planning tools, and provider-neutral report publication workflows that can target work items or PR-native code-activity flows instead of leaving privileged write paths as implicit connector lore
- [x] cockpit workflow surfaces now derive branch-family supervision from persisted lineage, including child/peer branch inspection plus latest-branch continue/open-parent controls, instead of treating every workflow run as an isolated replay row
- [x] built-in delegation now exposes a separate `vault_keeper` specialist for secret-management tools so generic memory delegation stops carrying direct vault read/write capability
- [x] capability bootstrap now limits automatic repair to low-risk local workflow or skill toggles, leaving policy lifts and capability-surface expansion as explicit operator actions instead of workflow-planning side effects, and generated repair bundles no longer batch-run multiple privileged mutations from one cockpit click
- [x] provider-neutral source capability discovery now exposes typed public-web tools, managed authenticated connectors, and explicit raw-MCP gaps so execution planning can inspect real external-source seams before composing a routine
- [x] wrapped MCP tools now preserve authenticated source context through audit, approval, and secret-ref layers, so workflow approval context and operator tool metadata can distinguish generic external MCP from authenticated external-source execution at runtime
- [x] high-risk extension configure and source-save paths now bind approvals to materially changed config targets or exact requested source content, and pending approval/operator/activity surfaces carry structured approval scope, lifecycle boundary, and trust-context metadata instead of flattening privileged mutations into generic approval rows
- [x] sync helper runtime-audit logging now fails soft when no event loop is active and uses tracked background tasks when a loop exists, so audit persistence no longer destabilizes sync execution paths in CI teardown
- [x] tool and MCP metadata now expose explicit `secret_ref_fields`, wrapped MCP tools preserve only declared injection-safe secret-bearing fields, and connector-backed authenticated mutation execution now rejects undeclared payload keys instead of forwarding arbitrary action arguments to managed runtimes
- [x] operator control-plane synthesis now exposes workspace governance modes, role inventory, usage rollups, runtime posture, review receipts, and blocked-workflow or approval handoff state through one operator API instead of leaving team-control context split across settings, audit, extensions, continuity, and workflow surfaces
- [x] managed background processes now bind recovery visibility to the originating session, so list/read/stop recovery handles fail closed outside that session instead of leaving cross-session process recovery broadly discoverable
- [x] process-runtime tool metadata and approval receipts now expose explicit session-partition plus background-execution trust context, and `start_process` now requires confirmation even when global approval mode is off so persistent runtime work no longer inherits the generic high-risk tool path
- [x] the operator API now also exposes a workspace-level background-session substrate that joins session-owned managed processes, workflow branch-handoff bundles, and session continuity snippets instead of leaving long-running background work split between process handles, workflow rows, and ad hoc session recovery
- [x] the operator API now also exposes searchable engineering-memory bundles for repositories and pull requests, grouping workflow continuity, approval targets, audit receipts, artifact follow-through, and matched session snippets by shared reference instead of leaving repo context fragmented across threads and operator surfaces
- [x] the operator API now also exposes an explicit continuity graph that links sessions, workflows, approvals, artifacts, notifications, deferred guardian items, and interventions through one evidence-backed graph instead of forcing operators to infer those relationships from separate background-session, timeline, and observer surfaces

## Working On Now

- [x] this workstream has now shipped both workflow-facing hardening slices through `execution-safety-hardening-v1` and `execution-safety-hardening-v2`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now also ships `workflow-timeline-and-approval-replay-v3`
- [x] this workstream now also ships `retire-village-and-editor-v1` and `execution-safety-hardening-v7` alongside richer workflow replay metadata, failed-step visibility, and retry-from-step control surfaces
- [x] this workstream now also ships `adapter-first-capability-contracts-v1` for source-capability discovery and connector-first external evidence planning
- [x] this workstream now also ships `authenticated-source-boundary-hardening-v1` for wrapper-stable source context and fail-closed authenticated workflow checkpoints
- [x] this workstream now also ships the first `authenticated-mutation-and-boundary-explainability-v1` Batch AL aggregate for scoped extension mutation approvals, boundary-aware workflow surfaces, and safer sync runtime-audit behavior
- [x] this workstream now also ships `connector-backed-authenticated-mutation-boundaries-v1` for typed source-write planning, scoped connector mutation approvals, and CI stabilization around the heaviest backend/frontend suites
- [x] this workstream now also ships the first `adapter-backed-authenticated-operations-and-report-workflows-v1` Batch AM aggregate for executable connector-backed source actions, provider-neutral report publication planning, structured mutation execution audit, and heavier backend shard weighting for the approval/context suites
- [x] this workstream now also ships the first Batch AT aggregate for field-scoped secret-reference execution surfaces, explicit per-tool secret-ref metadata, and allowlisted authenticated mutation payload enforcement
- [x] this workstream now also ships the second Batch AT aggregate for session-bound managed-process recovery containment and deterministic eval proof for the process-runtime recovery boundary
- [x] this workstream now also ships the first Batch AU aggregate for operator control-plane governance, usage/runtime posture synthesis, and collaboration-safe handoff receipts across approvals, workflows, audit, continuity, and extension state
- [x] this workstream now also ships the first Batch AW aggregate for broader authenticated adapter operations, PR-native `code_activity.write` create or review actions, fixed-argument bounded external execution, and report-publication contract selection that stays truthful across work-item versus pull-request publication paths
- [x] this workstream now also ships the first Batch BA aggregate for explicit background-process confirmation policy, richer managed-process trust receipts, and deterministic eval proof that the process-runtime boundary stays both session-partitioned and operator-visible

## Still To Do On `develop`

- [ ] richer browser and workflow execution beyond the current tool-level operations
- [ ] deeper long-running workflow supervision and durable orchestration beyond the current cockpit workflow-run surface, typed artifact-input handoff, branch-family supervision, checkpoint branch controls, approval-aware timeline, and boundary-aware replay model
- [ ] broader external system leverage without weakening trust boundaries

## Current Slice Record

### `workflow-autonomy-supervision-and-artifact-control-v1`

- status: complete on `develop` via PR `#251`
- root cause addressed:
  - workflow recovery metadata already advertised retry-from-step and branch control, but later-step failures without reusable checkpoint state could still make the runs API behave as if that branch path was available
  - cockpit artifact chaining still assumed any workflow with a `file_path` input could consume any produced artifact, which made artifact-to-workflow handoff broad but not truthful
  - workflow run lineage already carried parent/root branch metadata, but the cockpit still presented runs as isolated rows, which meant operators could not inspect parent/peer/child branches or continue the latest branch directly from the workflow surface
- scope:
  - workflow loader metadata now carries explicit `artifact_input` and `artifact_types` fields so workflow inputs can declare real artifact handoff expectations instead of relying on name-only conventions
  - workflow runtime and audit payloads now persist reusable checkpoint context for safe branch-from-step reuse, preserve structured step/failure context on hard workflow failures, and carry control lineage such as parent/root run identity and branch depth
  - the workflows API now exposes truthful checkpoint availability without letting unsupported later-step checkpoints 409 the entire runs list; unsupported checkpoints stay visible but non-resumable until a caller explicitly requests them
  - cockpit workflow and artifact inspectors now bind artifacts only into compatible workflow inputs, surface checkpoint-driven branch/retry actions from concrete checkpoint candidates, expose attached approval decisions directly from workflow rows and inspectors, and derive workflow branch families from lineage so operators can open parent runs, inspect peer/child branches, and continue the latest branch without leaving the workflow surface
  - regression coverage now pins checkpoint reuse, hard-failure audit payload shape, unsupported-checkpoint handling, typed artifact draft binding, and cockpit artifact/branch-family supervision controls
- local regression fixed before the slice stayed complete:
  - the first API pass still auto-selected the failed step as the default resume target even when that checkpoint had no reusable state, which made some degraded `GET /api/workflows/runs` responses fail with `409 Conflict`
  - fixed by skipping unsupported checkpoints for implicit/default resume selection while keeping explicit `/resume-plan` requests fail-closed for those same checkpoints
- PR review follow-up fixed before merge:
  - workflow parent-run checkpoint lookup still treated `session_id + tool_name + fingerprint` as unique, which meant two runs with identical arguments could restore checkpoint state from the wrong sibling run
  - workflow audit-call fingerprinting still hashed raw control inputs while execution hashed normalized controls, which could split `tool_call` and `tool_result` events for semantically identical runs
  - fixed by carrying a unique `call_event_id` discriminator from the audit call into workflow result/failure payloads, using that discriminator in workflow run identity and checkpoint restore lookup, and fingerprinting audit-call payloads from the same normalized inputs that execution uses
- validation:
  - `python3 -m py_compile backend/src/tools/audit.py backend/src/workflows/loader.py backend/src/workflows/manager.py backend/src/workflows/run_identity.py backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q`
    - result: `64 passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npm test -- --run src/components/settings/workflowDraft.test.ts src/components/cockpit/CockpitView.test.tsx`
    - result: `52 passed`
  - `cd frontend && npm run build`
    - result: `passed`
- subagent review:
  - two focused review passes were started for bugs, regressions, and hallucinated assumptions after the runtime plus cockpit slice landed
  - both reviewer runs stalled before returning usable findings, and the follow-up branch-family supervision slice used the same direct diff verification plus targeted backend/frontend validation instead of claiming an unreturned clean review

## Non-Goals

- adding tools just to increase the count
- unbounded process execution with weak policy control

## Interface Checklist

- [x] native tools are auto-discoverable through the registry
- [x] MCP tools can be added and removed without code changes
- [x] tool execution is visible to the user

## Acceptance Checklist

- [x] Seraph can browse, search, read/write local files, inspect goals, and use the shell
- [x] Seraph can use connected MCP servers in the current runtime
- [x] Seraph can execute richer cross-tool workflows than it could before the reusable workflow runtime
- [x] Seraph can expose workflow replay and safety context back to the operator instead of treating runs as opaque
