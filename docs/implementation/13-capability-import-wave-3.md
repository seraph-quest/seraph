# Capability Import Wave 3

## Scope

Wave 3 covers the Hermes packaged reach and capability-parity slices from the
capability import program:

17. `hermes-skill-registry-v1`
18. `hermes-optional-skill-packs-v1`
19. `hermes-mcp-toolset-bridge-v1`
20. `hermes-browserbase-connector-v1`
21. `hermes-browser-session-ops-v1`
22. `hermes-messaging-connectors-wave1-v1`
23. `hermes-vision-image-speech-v1`
24. `hermes-skill-authoring-loop-v1`

This wave is where the extension platform starts compounding into packaged
reach. It does not claim that every Hermes/OpenClaw surface is feature-complete
or remotely hosted already; the goal is to make skill packs, connector packs,
browser/provider inventory, and operator authoring feel like first-class
runtime surfaces instead of roadmap placeholders.

## Slice Log

### 17. hermes-skill-registry-v1

- status: complete
- intent:
  - turn the packaged catalog into the first real registry loop for optional
    skill-focused extension packs
  - surface install and update actions through the same operator and cockpit
    paths as the rest of the capability system
- implementation:
  - catalog extension packages are now discovered from
    `backend/src/defaults/catalog-extensions/` and surfaced alongside skill and
    MCP catalog entries
  - packaged extension entries now carry install/update metadata including
    `catalog_id`, trust, installed version, and contribution types
  - degraded catalog packages are now doctor-scanned before they are surfaced
    as installable, so broken packaged entries stop at the catalog layer rather
    than failing only at install time
  - install/update actions now route by stable `catalog_id` instead of display
    name for extension packs
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py tests/test_capabilities_api.py -q`
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`

### 18. hermes-optional-skill-packs-v1

- status: complete
- intent:
  - ship optional Hermes-style packaged skill bundles instead of only bundled
    defaults
  - prove that installable capability packs can bundle skills, context, and
    operator presets cleanly
- implementation:
  - added optional catalog extension packs:
    - `seraph.hermes-session-memory`
    - `seraph.hermes-research-ops`
  - these packs bundle real skills, context packs, and toolset presets so the
    extension registry and catalog expose optional operator-ready capability
    bundles instead of only base runtime content
  - cockpit and settings install surfaces now distinguish installed packs from
    updatable packs
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py tests/test_capabilities_api.py -q`
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`
  - `cd frontend && npm run build`

### 19. hermes-mcp-toolset-bridge-v1

- status: complete
- intent:
  - let packaged toolset presets bind to MCP server inventory cleanly, so
    operator-facing packs can advertise server-specific tool surfaces instead
    of opaque connector names
- implementation:
  - `toolset_presets` now support `include_mcp_servers`
  - MCP status payloads now surface server-linked toolset presets and concrete
    tool names
  - toolset permission evaluation now treats MCP-linked presets as
    `external_mcp` boundary consumers
  - doctor now reports broken MCP preset references when a preset targets
    missing server names
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py tests/test_capabilities_api.py -q`

### 20. hermes-browserbase-connector-v1

- status: complete
- intent:
  - package a Browserbase-style managed browser provider so browser-oriented
    packs feel first-class inside the extension platform
  - make the current execution mode explicit instead of overclaiming remote
    isolation before a real remote transport exists
- implementation:
  - added bundled connector pack `seraph.hermes-browserbase` with:
    - browser provider contribution
    - browser-oriented toolset preset
  - provider selection now surfaces Browserbase-style packaged inventory through
    the same connector registry as other extension-backed reach surfaces
  - the packaged Browserbase copy now explicitly describes the current staged
    local-fallback behavior instead of claiming live remote isolation
  - the public browser-session REST surface was deferred; the runtime remains
    tool-scoped until a later operator-surface wave adds a stronger ownership
    model
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_browser_session_tool.py tests/test_tools_api.py tests/test_catalog_api.py tests/test_app.py -q`

### 21. hermes-browser-session-ops-v1

- status: complete
- intent:
  - make browser sessions usable for real multi-step operator work with stable
    refs, snapshots, and provider-aware execution metadata
  - keep session handles scoped to the active Seraph thread instead of a
    process-global scratchpad
- implementation:
  - added bundled browser-session runtime primitives:
    - in-memory session store
    - `browser_session` native tool
    - browser-session operator skill and toolset preset
  - browser sessions are now scoped to the active runtime session, so one chat
    cannot list, read, or close another chat's browser captures through the
    tool path
  - browser capture failures no longer persist as successful sessions or
    snapshots; only successful `browse_webpage` results are stored
  - browser reads and listings now surface `execution_mode`, making
    `local_runtime` vs `local_fallback` visible in operator output
  - browser transport failures are normalized back to the `Error:` contract so
    capture validation is exact rather than brittle string-prefix guessing
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_browser_session_tool.py tests/test_tools_api.py tests/test_catalog_api.py tests/test_app.py -q`

### 22. hermes-messaging-connectors-wave1-v1

- status: complete
- intent:
  - ship the first Hermes-style messaging reach surfaces through typed connector
    packages rather than special-case runtime code
  - prove that messaging connectors install, configure, and enable through the
    extension lifecycle
- implementation:
  - added bundled messaging connector packs:
    - `seraph.hermes-telegram-relay`
    - `seraph.hermes-discord-relay`
    - `seraph.hermes-slack-relay`
  - each pack contributes a typed `messaging_connectors` manifest with config
    fields and delivery-mode metadata
  - catalog install/configure/enable flows now cover messaging connector packs,
    so the first channel wave uses the same extension lifecycle as the rest of
    the platform
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py -q`

### 23. hermes-vision-image-speech-v1

- status: complete
- intent:
  - import the highest-value Hermes multimodal packaging surfaces without
    pretending the full remote media runtime already exists
  - add speech and multimodal review scaffolding as installable capability
    packs
- implementation:
  - added bundled capability pack `seraph.hermes-speech-ops` with:
    - `speech_profiles`
    - a speech-oriented prompt pack
  - added bundled capability pack `seraph.hermes-multimodal-review` with:
    - `context_packs`
    - `prompt_packs`
    - `provider_presets`
  - the pack copy explicitly frames these as operator-ready scaffolding for the
    current runtime rather than claiming full voice or rich multimodal transport
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py tests/test_extensions_api.py tests/test_extension_scaffold.py -q`

### 24. hermes-skill-authoring-loop-v1

- status: complete
- intent:
  - let operators scaffold a skill-focused extension package directly from the
    workspace instead of hand-authoring every manifest and file
  - keep authoring constrained to safe workspace-local paths
- implementation:
  - added `POST /api/extensions/scaffold` for workspace-local extension package
    scaffolding
  - scaffold requests now:
    - sanitize package names
    - create under `WORKSPACE_DIR/extensions/`
    - validate the new package immediately
    - log extension lifecycle events for scaffold success and failure
  - Extension Studio now exposes a `Scaffold skill pack` flow that creates a
    packaged skill bundle, validates it, and opens it in the existing studio
    path/install loop
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extensions_api.py tests/test_extension_scaffold.py -q`
  - `cd frontend && npm test -- src/components/cockpit/CockpitView.test.tsx`

## Subagent Review

### Review 1

- reviewer: `Hooke`
- scope:
  - Wave 3 batch A (`17-19`): catalog-backed skill registry, optional packaged
    skill packs, and MCP-linked toolset presets
- findings:
  - installed extension packs with `update_available=true` were effectively not
    updatable from the main cockpit/settings surfaces because those views hid
    installed catalog items entirely
  - catalog extension packs were being surfaced directly from the registry
    snapshot without doctor status, so degraded/broken packages could appear
    installable and only fail later during install
  - extension-pack install actions still routed through display names instead of
    stable `catalog_id` values, which would collide if two packs shared a
    display label
  - `include_mcp_servers` had no validation, so a typo produced a dead preset
    with no warning
- resolution:
  - cockpit and settings surfaces now keep extension packs visible when they are
    updateable, and install/update actions are keyed by `catalog_id`
  - catalog extension entries now carry doctor/load-error status and degraded
    packs no longer receive install recommendations
  - capability recommendations now emit extension-pack install/update actions
    using `catalog_id`
  - doctor now validates `include_mcp_servers` against packaged/runtime MCP
    names and reports `missing_mcp_server_reference`

### Review 2

- reviewer: `Galileo`
- scope:
  - Wave 3 batch B (`20-21`): Browserbase-style packaged provider plus browser
    session runtime/tooling
- findings:
  - browser sessions were stored in a process-global map with no thread
    ownership, so one session could enumerate, read, or close another
    session's captures through the runtime
  - browser capture failures and site-policy blocks were being stored as
    successful browser sessions or snapshots because `browser_session`
    persisted raw `browse_webpage` error strings
  - packaged Browserbase copy and browser-session guidance overclaimed isolated
    remote behavior even though the shipped runtime still executes through
    local fallback
- resolution:
  - browser sessions are now owned by the active runtime session, and the
    browser-session tool scopes list/read/close/snapshot operations to that
    owner session only
  - browser capture failures now return immediately and no longer create or
    append browser-session state
  - Browserbase/browser-ops packaging now explicitly documents the current
    local-fallback execution mode, and the public REST browser-session surface
    was deferred until a later operator-surface wave can provide a stronger
    ownership model

### Review 3

- reviewer: `Kierkegaard`
- scope:
  - Wave 3 branch after the browser/session, connector-redaction, provider
    preset, and scaffold fixes landed
- findings:
  - no new correctness findings
- note:
  - `backend/src/api/browser.py` remains intentionally unmounted until a later
    operator-surface wave introduces a stronger ownership model for any public
    browser-session API

### Review 4

- reviewer: `Leibniz`
- scope:
  - Wave 3 docs and test consistency across the full branch
- findings:
  - Wave 3 roadmap items were still unchecked even though the branch had landed
    slices `17-24`
  - the Wave 3 implementation doc still described later slices as only
    "implemented locally"
  - a stale backend test still expected the old install-time approval behavior
    for the synthetic Wave 2 capability pack
  - shipped browser, messaging, and multimodal packs were not yet pinned by
    end-to-end install-path tests, and `prompt_packs` were still effectively
    unpinned runtime metadata
- resolution:
  - the capability-pack approval expectation now matches the current permission
    model: secret-bearing capability packs require approval, while the no-secret
    managed GitHub test pack installs directly
  - the master roadmap now marks Wave 3 complete and aligns slice `23` with the
    shipped multimodal scaffolding scope
  - real bundled browserbase, browser-ops, Discord, Slack, speech, and
    multimodal packs now have install-path coverage
  - `prompt_packs` are now first-class runtime metadata surfaced by the
    registry and extension payloads

### Review 5

- reviewer: `Hooke`
- scope:
  - final Wave 3 follow-up after the shipped-pack coverage pass
- findings:
  - secret-bearing packs were install-gated, but configure operations that
    stored new secret values were still bypassing the lifecycle approval gate
  - the roadmap still drifted from the shipped implementation state
- resolution:
  - configure operations now require lifecycle approval when the request
    supplies new password-backed secret values, while redacted no-op
    reconfigure passes keep working without repeated approval prompts
  - the roadmap now mirrors the completed Wave 3 state directly

### Review 6

- reviewer: `Hooke`
- scope:
  - final re-review after the configure-approval and roadmap-sync fixes landed
- findings:
  - no new correctness findings

## Final Validation

- focused backend Wave 3 suite:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py tests/test_extensions_api.py tests/test_extension_scaffold.py tests/test_app.py tests/test_browser_session_tool.py tests/test_tools_api.py tests/test_extension_registry.py -q`
  - result: `123 passed`
- focused regressions:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_browserbase_pack_surfaces_browser_provider_metadata tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_browser_ops_pack_surfaces_browser_skills_and_toolset tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_additional_messaging_connector_packs tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_speech_pack_surfaces_speech_profile_metadata tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_multimodal_pack_surfaces_provider_preset_metadata tests/test_extension_registry.py -q`
  - result: `25 passed`
- secret-approval follow-up:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_messaging_connector_pack_can_be_configured_and_enabled tests/test_catalog_api.py::TestCatalogAPI::test_install_catalog_additional_messaging_connector_packs tests/test_extensions_api.py::test_install_configure_and_toggle_wave2_contribution_surfaces tests/test_extensions_api.py::test_install_and_configure_workspace_managed_connector_extension -q`
  - result: `5 passed`
- backend full suite:
  - `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - result: `1154 passed`, `4 warnings`
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
  - result: clean

## Current Validation State

- Wave 3 is complete across slices `17-24`.
- Batch A, Batch B, and Batch C are all landed on the branch and covered by
  focused backend validation, full backend/frontend validation, and docs/build
  checks.
- The branch is ready for the Wave 3 PR.
