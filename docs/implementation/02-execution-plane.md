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
- [x] built-in delegation now exposes a separate `vault_keeper` specialist for secret-management tools so generic memory delegation stops carrying direct vault read/write capability

## Working On Now

- [x] this workstream has now shipped both workflow-facing hardening slices through `execution-safety-hardening-v1` and `execution-safety-hardening-v2`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now also ships `workflow-timeline-and-approval-replay-v3`
- [x] this workstream now also ships `retire-village-and-editor-v1` and `execution-safety-hardening-v7` alongside richer workflow replay metadata, failed-step visibility, and retry-from-step control surfaces

## Still To Do On `develop`

- [ ] richer browser and workflow execution beyond the current tool-level operations
- [ ] richer long-running workflow supervision, deeper step-level history, and broader artifact chaining beyond the current cockpit workflow-run surface, typed artifact-input handoff, checkpoint branch controls, approval-aware timeline, and boundary-aware replay model
- [ ] broader external system leverage without weakening trust boundaries

## Current Slice Record

### `workflow-autonomy-and-artifact-control-v1`

- status: complete on `feat/workflow-autonomy-batch-g-v1`, pending inclusion in the aggregate Batch G PR for `#247`
- root cause addressed:
  - workflow recovery metadata already advertised retry-from-step and branch control, but later-step failures without reusable checkpoint state could still make the runs API behave as if that branch path was available
  - cockpit artifact chaining still assumed any workflow with a `file_path` input could consume any produced artifact, which made artifact-to-workflow handoff broad but not truthful
- scope:
  - workflow loader metadata now carries explicit `artifact_input` and `artifact_types` fields so workflow inputs can declare real artifact handoff expectations instead of relying on name-only conventions
  - workflow runtime and audit payloads now persist reusable checkpoint context for safe branch-from-step reuse, preserve structured step/failure context on hard workflow failures, and carry control lineage such as parent/root run identity and branch depth
  - the workflows API now exposes truthful checkpoint availability without letting unsupported later-step checkpoints 409 the entire runs list; unsupported checkpoints stay visible but non-resumable until a caller explicitly requests them
  - cockpit workflow and artifact inspectors now bind artifacts only into compatible workflow inputs, surface checkpoint-driven branch/retry actions from concrete checkpoint candidates, and expose attached approval decisions directly from workflow rows and inspectors
  - regression coverage now pins checkpoint reuse, hard-failure audit payload shape, unsupported-checkpoint handling, typed artifact draft binding, and cockpit artifact/branch controls
- local regression fixed before the slice stayed complete:
  - the first API pass still auto-selected the failed step as the default resume target even when that checkpoint had no reusable state, which made some degraded `GET /api/workflows/runs` responses fail with `409 Conflict`
  - fixed by skipping unsupported checkpoints for implicit/default resume selection while keeping explicit `/resume-plan` requests fail-closed for those same checkpoints
- validation:
  - `python3 -m py_compile backend/src/tools/audit.py backend/src/workflows/loader.py backend/src/workflows/manager.py backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q`
    - result: `60 passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npm test -- --run src/components/settings/workflowDraft.test.ts src/components/cockpit/CockpitView.test.tsx`
    - result: `51 passed`
  - `cd frontend && npm run build`
    - result: `passed`
- subagent review:
  - two focused review passes were started for bugs, regressions, and hallucinated assumptions after the runtime plus cockpit slice landed
  - both reviewer runs stalled before returning usable findings, so this slice record relies on the fixed API regression above plus direct diff verification and the targeted backend/frontend validation rather than claiming an unreturned clean review

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
