# Capability Import Wave 2

## Scope

Wave 2 covers the extension-capability-type slices from the capability import program:

10. `extension-toolset-presets-v1`
11. `extension-context-packs-v1`
12. `extension-automation-triggers-v1`
13. `extension-browser-providers-v1`
14. `extension-messaging-connectors-v1`
15. `extension-speech-profiles-v1`
16. `extension-node-adapters-v1`

This wave does not claim that Hermes or OpenClaw reach is fully live already.
It ships the typed manifest, registry, lifecycle, doctor, and authoring seams
those later imports depend on.

## Slice Log

### 10. extension-toolset-presets-v1

- status: complete
- intent:
  - add a packageable operator-facing preset surface for curated tool bundles
  - let packaged presets participate in permission and execution-boundary review
- implementation:
  - `toolset_presets` is now a first-class manifest contribution type with a
    canonical `presets/toolset/` root
  - the extension registry now surfaces typed metadata for toolset presets
  - doctor and permission evaluation now validate preset tool permissions plus
    derived execution-boundary and network requirements
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_registry.py tests/test_extension_doctor.py tests/test_extensions_api.py -q`

### 11. extension-context-packs-v1

- status: complete
- intent:
  - add reusable packaged context bundles for profile, prompt, guardian-memory,
    and related imported-capability defaults
- implementation:
  - `context_packs` is now a first-class manifest contribution type with a
    canonical `context/` root
  - scaffold, registry, and lifecycle payloads now treat context packs as typed
    package metadata instead of generic manifest entries
  - author tooling can now scaffold context-pack placeholders directly
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_scaffold.py tests/test_extension_registry.py tests/test_extensions_api.py -q`

### 12. extension-automation-triggers-v1

- status: complete
- intent:
  - add a packageable trigger contract for webhook, poll, and pub-sub style
    automation imports
- implementation:
  - `automation_triggers` is now a first-class manifest contribution type with
    a canonical `automation/` root
  - connector-style lifecycle payloads now expose trigger metadata, package
    config fields, configuration errors, and enable or disable state
  - package-level and connector-level enablement now route through the shared
    extension lifecycle instead of requiring a dedicated automation seam
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_scaffold.py tests/test_extensions_api.py -q`

### 13. extension-browser-providers-v1

- status: complete
- intent:
  - add a packageable browser-provider contract for later Browserbase, CDP, and
    relay imports
- implementation:
  - `browser_providers` is now a first-class manifest contribution type with a
    canonical `connectors/browser/` root
  - lifecycle payloads now expose provider kind, config fields, config state,
    and planned connector health through the shared extension API
  - scaffold and doctor flows now understand packaged browser-provider metadata
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_scaffold.py tests/test_extension_doctor.py tests/test_extensions_api.py -q`

### 14. extension-messaging-connectors-v1

- status: complete
- intent:
  - add a first-class messaging connector surface so later Telegram, Slack, and
    Discord imports land through the shared extension lifecycle
- implementation:
  - `messaging_connectors` is now a first-class manifest contribution type with
    a canonical `connectors/messaging/` root
  - runtime state can now carry typed connector config and enablement for
    packaged messaging connectors before concrete channel implementations land
  - registry and connector payloads now surface platform and config metadata for
    operator inspection
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_registry.py tests/test_extensions_api.py -q`

### 15. extension-speech-profiles-v1

- status: complete
- intent:
  - reserve a typed extension surface for later TTS, STT, wake-word, and
    talk-mode imports without forcing voice primitives into core
- implementation:
  - `speech_profiles` is now a first-class manifest contribution type with a
    canonical `speech/` root
  - scaffold and registry flows now expose typed metadata for packaged speech
    profiles
  - lifecycle payloads now treat speech profiles as passive typed contributions
    rather than opaque manifest entries
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_scaffold.py tests/test_extensions_api.py -q`

### 16. extension-node-adapters-v1

- status: complete
- intent:
  - add a packageable node or device adapter surface so later companion-node and
    embodied reach imports land through typed extensions instead of raw plugins
- implementation:
  - `node_adapters` is now a first-class manifest contribution type with a
    canonical `connectors/nodes/` root
  - lifecycle payloads now expose adapter kind, config fields, health summary,
    and enable or disable state through the shared connector API
  - connector-pack scaffolding can now generate node-adapter placeholders with
    package-derived network permissions where required
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_scaffold.py tests/test_extension_doctor.py tests/test_extensions_api.py -q`

## Shared Implementation Notes

- the manifest contract, package layout, registry enrichment, doctor, scaffold,
  and lifecycle API were all extended in this wave so the new contribution
  types behave like first-class extension surfaces instead of metadata sidecars
- `connector-pack` scaffolding is now real rather than deferred, which lets the
  authoring tools generate valid placeholder packages for `managed_connectors`,
  `automation_triggers`, `browser_providers`, `messaging_connectors`, and
  `node_adapters`
- Wave 2 deliberately stops at typed lifecycle support for imported reach
  surfaces; the concrete Hermes and OpenClaw integrations that consume those
  types still land in later waves

## Subagent Review

### Review 1

- reviewer: `Hooke`
- scope:
  - schema, layout, registry, scaffold, and docs for the new Wave 2
    contribution types
- findings:
  - non-UTF-8 `manifest.yaml` files could crash registry scanning instead of
    surfacing a structured load error
  - nested manifests under contribution roots could be misclassified as package
    manifests
  - duplicate extension-id precedence ignored the registry's configured
    manifest-root order and could prefer the wrong package source
  - docs overstated `observer_definitions` as connector-pack content even though
    that surface still belongs to capability-pack packaging rules
- resolution:
  - manifest loading now converts `UnicodeDecodeError` into a structured
    `ExtensionManifestError`
  - package-manifest detection now rejects manifests nested under contribution
    subtrees
  - duplicate extension resolution now respects the registry's actual
    manifest-root order first and only falls back to bundled/workspace heuristics
    afterward
  - authoring docs now clarify the distinction between capability-pack observer
    content and true connector-pack contribution types

### Review 2

- reviewer: `Galileo`
- scope:
  - full Wave 2 branch after lifecycle, doctor, and API support landed
- findings:
  - suspicious-context scanning for `context_packs` could still be bypassed by
    hiding hostile instructions inside fenced blocks
  - `connector-pack` scaffolding still accepted Wave 2 contribution sets that
    could never validate as connector packs because they contained no connector
    surfaces
  - extension docs still had pre-Wave-2 wording around scaffold scope and
    validation depth
- resolution:
  - doctor now scans fenced content for `context_packs` specifically, while
    preserving the earlier false-positive protections for the rest of the
    prompt-bearing surfaces
  - scaffold now rejects `connector-pack` creation requests that omit all true
    connector contribution types
  - extension overview and contribution-type docs now reflect the shipped Wave
    2 scaffold and validation surface instead of the pre-wave wording
  - a final follow-up spot-check was requested after these fixes landed; that
    re-review timed out without returning additional concrete findings before
    the wave was closed

## Wave 2 Exit Validation

- status: complete
- validation:
  - `python3 -m py_compile backend/src/extensions/capability_contributions.py backend/src/extensions/connector_health.py backend/src/extensions/doctor.py backend/src/extensions/lifecycle.py backend/src/extensions/manifest.py backend/src/extensions/layout.py backend/src/extensions/permissions.py backend/src/extensions/registry.py backend/src/extensions/scaffold.py`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_extension_manifest.py tests/test_extension_scaffold.py tests/test_extension_registry.py tests/test_extension_doctor.py tests/test_extensions_api.py -q`
  - `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - `cd frontend && npm test`
  - `cd frontend && npm run build`
  - `cd docs && npm run build`
  - `git diff --check`
- result:
  - focused Wave 2 backend suite: `104 passed`
  - backend full suite: `1123 passed`, `4` existing warnings
  - frontend full suite: `154 passed`
  - frontend build: passed
  - docs build: passed
  - diff hygiene: clean
