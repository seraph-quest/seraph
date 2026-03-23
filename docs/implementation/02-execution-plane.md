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

## Working On Now

- [x] this workstream has now shipped both workflow-facing hardening slices through `execution-safety-hardening-v1` and `execution-safety-hardening-v2`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now also ships `workflow-timeline-and-approval-replay-v3`
- [x] this workstream now also ships `retire-village-and-editor-v1` and `execution-safety-hardening-v7` alongside richer workflow replay metadata, failed-step visibility, and retry-from-step control surfaces

## Still To Do On `develop`

- [ ] richer browser and workflow execution beyond the current tool-level operations
- [ ] richer direct workflow execution, step-level visibility, artifact round-tripping, and workflow history on top of the cockpit workflow-run surface, approval-aware timeline, retry-from-step control, and boundary-aware replay model
- [ ] broader external system leverage without weakening trust boundaries

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
