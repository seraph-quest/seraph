# Capability Import Wave 4

## Scope

Wave 4 covers the selective OpenClaw import slices from the capability import
program:

25. `openclaw-browser-mode-matrix-v1`
26. `openclaw-channel-routing-bindings-v1`
27. `openclaw-webhook-poll-pubsub-v1`
28. `openclaw-node-device-adapters-v1`
29. `openclaw-canvas-output-v1`
30. `openclaw-workflow-engine-imports-v1`

This wave does not copy OpenClaw's raw plugin runtime. The goal is to import
the most valuable browser, routing, automation, adapter, and structured-output
ideas through Seraph's extension platform and guardian trust boundaries.

## Slice Log

### 25. openclaw-browser-mode-matrix-v1

- status: complete
- intent:
  - add an operator-visible browser mode matrix for local runtime, Browserbase,
    remote CDP, and browser-extension relay surfaces
  - make packaged browser-provider inventory visible through honest staged
    metadata instead of overclaiming remote execution before those transports
    land fully
- implementation:
  - mounted a public browser provider-inventory endpoint under
    `/api/browser/providers`
  - extended browser-provider selection and inventory reporting so packaged
    Browserbase, remote CDP, and extension-relay contributions surface through
    the same runtime path as local providers
  - added `browser_session(action="providers")` output for operator-side mode
    inspection
  - kept browser-session list/read/close management on the tool/runtime path;
    the public REST session-management surface remains deferred until a later
    wave can attach a stronger ownership model
  - added bundled catalog packs for:
    - `seraph.openclaw-remote-cdp`
    - `seraph.openclaw-extension-relay`
  - provider inventory now always includes the built-in `local-browser` lane so
    the matrix shows the real fallback/runtime path even when staged remote
    providers are installed
  - packaged provider copy explicitly describes staged `local_fallback`
    behavior until the remote transports are implemented end to end
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_browser_session_tool.py tests/test_browser_api.py tests/test_app.py -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_remote_cdp_pack_surfaces_staged_browser_provider_metadata tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_extension_relay_pack_surfaces_staged_browser_provider_metadata -q`

### 26. openclaw-channel-routing-bindings-v1

- status: complete
- intent:
  - replace hardcoded websocket-first proactive delivery with explicit,
    inspectable delivery bindings
  - let operators route live, alert, scheduled, and queued-bundle delivery
    across the active websocket and native-notification channel adapters
- implementation:
  - added persisted channel-routing bindings in extension runtime state with
    four explicit delivery classes:
    - `live_delivery`
    - `alert_delivery`
    - `scheduled_delivery`
    - `bundle_delivery`
  - added `/api/extensions/channel-routing` get/update endpoints so bindings
    are operator-visible and configurable without hand-editing state files
  - proactive delivery and queued-bundle delivery now obey those bindings
    instead of hardcoded websocket-first heuristics
  - routing responses include the current active channel adapters and active
    transports so the configured bindings are visible against live runtime
    availability
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extensions_api.py -k "channel_routing or channel_adapter" -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_delivery.py -k "channel_routing or native_channel_adapter" -q`

### 27. openclaw-webhook-poll-pubsub-v1

- status: complete
- intent:
  - import OpenClaw-style automation breadth without adding a second ad hoc
    scheduler/runtime model outside the extension platform
  - make webhook, poll, and pubsub triggers visible through an honest staged
    runtime inventory instead of pretending they already execute end to end
- implementation:
  - added `automation_triggers` as a typed connector contribution and exposed
    inventory at `/api/automation/triggers`
  - mounted signed webhook ingress at
    `/api/automation/webhooks/{trigger_name}` and normalized webhook trigger
    metadata so `endpoint` always reflects that real mounted path
  - runtime inventory now distinguishes:
    - `disabled`
    - `requires_config`
    - `armed_webhook`
    - `staged_runtime`
  - packaged the first OpenClaw-style trigger packs:
    - `seraph.openclaw-webhook-gateway`
    - `seraph.openclaw-poll-watch`
    - `seraph.openclaw-pubsub-relay`
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_automation_api.py -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py -k "webhook_gateway or poll_and_pubsub" -q`

### 28. openclaw-node-device-adapters-v1

- status: complete
- intent:
  - import OpenClaw’s node/device reach ideas through typed staged adapters
    instead of raw in-process plugins
  - surface honest readiness states for companion/device/canvas lanes without
    claiming a live runtime that does not exist yet
- implementation:
  - added `node_adapters` as a typed connector contribution and exposed
    inventory at `/api/nodes/adapters`
  - adapter inventory now distinguishes:
    - `disabled`
    - `requires_config`
    - `staged_link`
    - `staged_canvas`
  - default `requires_network` now follows adapter kind so non-canvas adapters
    do not silently present as offline-safe unless a package explicitly opts
    out
  - packaged the first OpenClaw-style node/device packs:
    - `seraph.openclaw-companion-node`
    - `seraph.openclaw-device-bridge`
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_nodes_api.py -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py -k "node_packs" -q`

### 29. openclaw-canvas-output-v1

- status: complete
- intent:
  - add richer structured output surfaces so workflows can publish operator
    results into typed boards instead of only returning plain text
  - keep those surfaces extension-backed and inspectable rather than hardcoded
- implementation:
  - added `canvas_outputs` as a passive typed contribution and exposed
    inventory at `/api/canvas/outputs`
  - bundled `seraph.openclaw-canvas-board`, which ships the
    `guardian-board` surface definition
  - workflow audit payloads now emit structured `canvas_output` data when a
    workflow resolves an output surface
  - the emitted sections now follow the registered canvas definition instead of
    hardcoded section labels
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_canvas_api.py -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py -k "canvas_pack" -q`

### 30. openclaw-workflow-engine-imports-v1

- status: complete
- intent:
  - selectively reinterpret OpenClaw runtime ideas such as OpenProse, Lobster,
    and LLM-task into Seraph’s workflow architecture instead of importing a
    second workflow engine wholesale
  - make runtime-profile requirements real execution constraints, not cosmetic
    metadata
- implementation:
  - added `workflow_runtimes` as a passive typed contribution and exposed
    inventory at `/api/workflows/runtimes`
  - workflows can now declare `runtime_profile` and optionally inherit their
    default `output_surface` from the selected runtime profile
  - workflow availability, active-workflow selection, and workflow-tool
    building now all respect missing runtime profiles and missing output
    surfaces
  - bundled `seraph.openclaw-workflow-runtimes`, which ships:
    - runtime profiles: `openprose`, `lobster`, `llm-task`
    - example workflows for each runtime family
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_workflow_runtimes_api.py tests/test_workflows.py -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py -k "workflow_runtimes_pack" -q`

## Subagent Review

### Review 1

- reviewer: `Hooke`
- scope:
  - Wave 4 slice `25` browser-mode inventory, API mounting, and packaged remote
    browser-provider metadata
- findings:
  - the first pass exposed list/get/close browser-session REST endpoints that
    trusted a caller-supplied `session_id`, which was weaker than the existing
    tool/runtime ownership model
  - the provider matrix hid the built-in local runtime once packaged providers
    existed, which made the browser-mode view misleading in the exact staged
    cases it was supposed to clarify
  - the Wave 4 docs and roadmap were not yet synced to the shipped slice state
- resolution:
  - the public browser API now exposes provider inventory only; session
    management remains tool-scoped until a later operator-surface wave adds a
    stronger ownership model
  - provider inventory now always includes `local-browser`, and the tool/API
    tests pin that visible fallback lane alongside staged remote providers
  - the Wave 4 implementation doc and roadmap are updated inline as slices land

### Review 2

- reviewer: `Hooke`
- scope:
  - Wave 4 batch A (`25-26`): browser mode inventory plus explicit channel
    routing and delivery bindings
- findings:
  - the channel-routing API and delivery runtime disagreed on the
    no-channel-adapter-contributions case: the operator surface reported no
    active transports while delivery still fell back to built-in websocket and
    native-notification lanes
  - the scheduled delivery lane was not pinned by any explicit routing test,
    which made slice `26` broader in docs than in tests
- resolution:
  - `/api/extensions/channel-routing` now mirrors the delivery runtime fallback
    and surfaces built-in websocket/native transports when no channel-adapter
    contributions are present
  - the delivery tests now include an explicit `scheduled_delivery` routing
    case, not just `live_delivery` and `bundle_delivery`

### Review 3

- reviewer: `Galileo`
- scope:
  - Wave 4 batch B (`27-28`): automation triggers and node/device adapters
- findings:
  - webhook `endpoint` metadata was being surfaced as if it were a free-form
    routing contract, even though the only mounted ingress path is
    `/api/automation/webhooks/{trigger_name}`
  - node adapters defaulted `requires_network` to `false`, which made URL-based
    adapters look offline-safe unless a pack author opted in explicitly
  - the `canvas_ready` node-adapter runtime state overclaimed a live runtime for
    an inventory-only slice
- resolution:
  - webhook trigger definitions now validate/canonicalize their endpoint to the
    mounted ingress path
  - node adapters now default `requires_network` by adapter kind, and the API
    tests pin the companion/canvas distinction
  - canvas adapters now surface `staged_canvas` instead of `canvas_ready`

### Review 4

- reviewer: `Leibniz`
- scope:
  - Wave 4 batch C (`29-30`): canvas outputs and workflow runtimes
- findings:
  - runtime profile / output-surface availability was only reflected in
    `list_workflows()` and did not actually gate `get_active_workflows()` or
    `build_workflow_tools()`
  - `default_output_surface` on workflow runtimes was dead metadata and was not
    applied anywhere in execution or availability
  - workflow canvas output payloads ignored the registered canvas surface
    metadata and hardcoded their own sections
  - the Wave 4 docs still stopped at slices `25-26`
- resolution:
  - active-workflow selection and workflow-tool building now both respect
    runtime-profile and output-surface availability
  - runtime profiles can now default an output surface into workflows when the
    workflow itself does not set one
  - canvas output payloads now derive their title/sections from the registered
    canvas definition
  - this document now records all six Wave 4 slices and the batch-level review
    trail

### Review 5

- reviewer: `Hooke`
- scope:
  - Wave 4 batch A re-review after the channel-routing fallback and scheduled
    delivery fixes
- findings:
  - no findings
- resolution:
  - none required

### Review 6

- reviewer: `Galileo`
- scope:
  - Wave 4 batch B re-review after endpoint canonicalization and node-adapter
    readiness fixes
- findings:
  - no findings
- resolution:
  - none required

### Review 7

- reviewer: `Galileo`
- scope:
  - Wave 4 automation webhook ingress after the branch-wide follow-up pass
- findings:
  - webhook triggers without an explicit `signing_secret` config field still
    surfaced as `armed_webhook`, which left `/api/automation/webhooks/{name}`
    unsigned whenever a pack author omitted the field
- resolution:
  - webhook trigger definitions now inject a required `signing_secret`
    password field even when a pack omits it, so unsigned webhook packs stay in
    `requires_config` and the ingress route never exposes a public unsigned
    POST lane

### Review 8

- reviewer: `Zeno`
- scope:
  - Wave 4 automation duplicate-name handling after the initial registry
    conflict pass
- findings:
  - duplicate webhook trigger names still resolved to a live winner in runtime
    selection, so a public webhook path could execute even while the duplicate
    was flagged as invalid in `/api/extensions`
- resolution:
  - automation-trigger duplicates now use an all-definitions conflict mode, so
    every colliding webhook contribution becomes invalid and the runtime
    inventory and ingress route both refuse the duplicated name

### Review 9

- reviewer: `Leibniz`
- scope:
  - Wave 4 duplicate-name handling and channel-routing regression coverage
- findings:
  - duplicate-name handling was still inconsistent across browser providers,
    workflow runtimes, and canvas outputs even though those surfaces are all
    consumed by `name`
  - `alert_delivery` still lacked an explicit routing regression despite being
    a first-class channel lane
- resolution:
  - registry conflict handling now covers browser providers, workflow runtimes,
    and canvas outputs, and the runtime/API inventories skip conflicted loser
    entries
  - delivery tests now pin the `alert_delivery` routing path explicitly

### Review 10

- reviewer: `Epicurus`
- scope:
  - Wave 4 workflow runtime and canvas metadata after the duplicate-name
    hardening pass
- findings:
  - runtime and canvas metadata could still drift by snapshot display ordering
    instead of extension priority because the name-keyed maps were not yet
    skipping conflicted duplicate contributions everywhere
- resolution:
  - workflow runtime and canvas map building now skip registry-conflicted
    contributions in both the workflow manager and the draft-validation API,
    and regressions pin the higher-priority winner behavior

### Review 11

- reviewer: Schrodinger
- scope:
  - full Wave 4 diff after the webhook, duplicate-name, and alert-routing fixes
- findings:
  - none
- resolution:
  - no additional changes were required before final validation

### Review 12

- reviewer: Darwin
- scope:
  - PR `#226` follow-up fixes for workflow step-tool gating and registry
    conflict-order metadata enrichment
- findings:
  - none
- resolution:
  - no additional changes were required after the follow-up patch

## Wave Validation

- focused Wave 4 follow-up suite:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest --no-cov tests/test_automation_api.py tests/test_extensions_api.py tests/test_browser_api.py tests/test_delivery.py tests/test_workflows.py tests/test_workflow_runtimes_api.py tests/test_canvas_api.py -q`
  - result: `146 passed`
- targeted Wave 4 backend matrix:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_browser_session_tool.py tests/test_browser_api.py tests/test_extensions_api.py tests/test_delivery.py tests/test_automation_api.py tests/test_nodes_api.py tests/test_canvas_api.py tests/test_workflow_runtimes_api.py tests/test_workflows.py tests/test_catalog_api.py tests/test_app.py -q`
  - result: `187 passed`
- PR review follow-up slice:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_workflows.py tests/test_extension_registry.py -q`
  - result: `71 passed`
- PR review follow-up matrix:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extensions_api.py tests/test_workflow_runtimes_api.py tests/test_workflows.py tests/test_extension_registry.py -q`
  - result: `125 passed`
- backend full suite:
  - `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - result: `1192 passed, 4 warnings`
- frontend full suite:
  - `cd frontend && npm test`
  - result: `156 passed`
- frontend build:
  - `cd frontend && npm run build`
  - result: passed
- docs build:
  - `cd docs && npm run build`
  - result: passed
- diff hygiene:
  - `git diff --check`
  - result: passed

## Current Validation State

- Wave 4 is complete across slices `25-30`.
- The late automation and duplicate-name findings are fixed and pinned by
  focused backend regressions.
- Final validation passed, including the full backend suite and final
  subagent re-review.
- Wave 4 is ready to publish as a single PR.
