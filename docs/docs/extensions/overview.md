# Extension Platform Overview

Seraph’s extension platform is the typed packaging layer for reusable capability.

Use it to ship:

- skills
- workflows
- runbooks
- starter packs
- provider presets
- prompt packs
- scheduled routines
- connector packs such as MCP packages and managed integrations

The extension platform is deliberately **not** a general arbitrary-code plugin runtime.

Seraph keeps these systems core-owned:

- policy
- approvals
- audit and activity
- secret handling
- session and thread state
- workflow execution
- capability preflight and repair
- model routing

That split is intentional. Extensions contribute capability inside Seraph’s trust boundaries; they do not replace those boundaries.

## Package kinds

The current manifest contract supports two package kinds:

- `capability-pack`
- `connector-pack`

Current author tooling supports both package kinds:

- scaffolding now supports `capability-pack` and `connector-pack`
- current `new_pack.py --with ...` support covers:
  - `skills`
  - `workflows`
  - `runbooks`
  - `starter_packs`
  - `provider_presets`
  - `toolset_presets`
  - `prompt_packs`
  - `context_packs`
  - `scheduled_routines`
  - `managed_connectors`
  - `automation_triggers`
  - `browser_providers`
  - `messaging_connectors`
  - `speech_profiles`
  - `node_adapters`

## Trust model

Current manifest provenance values are:

- `bundled`
- `local`
- `verified`

These are package provenance markers, not a replacement for Seraph’s architectural trust tiers.

At a higher level, Seraph treats extension work in three buckets:

1. Safe declarative extensions
   Skills, workflows, runbooks, packs, presets, and similar file-based contributions.
2. Connector extensions
   MCP packages and managed integrations.
3. Trusted code plugins
   Explicitly out of scope for the current architecture. See the
   [Trusted Code Plugins RFC](/research/trusted-code-plugins-rfc).

## Canonical layout

Each contribution type has a canonical package root:

- `skills/`
- `workflows/`
- `runbooks/`
- `starter-packs/`
- `presets/provider/`
- `prompts/`
- `context/`
- `routines/`
- `mcp/`
- `connectors/managed/`
- `automation/`
- `connectors/browser/`
- `connectors/messaging/`
- `observers/definitions/`
- `observers/connectors/`
- `channels/`
- `speech/`
- `connectors/nodes/`
- `workspace/`

Current scaffolding focuses on the capability-pack surfaces that are already in scope for authoring.

Seraph now also ships one canonical in-repo example package:

- `examples/extensions/research-pack/`

## Managed workspace authoring

New authored capability content now lands in a managed workspace package:

- `workspace/extensions/workspace-capabilities/`

That package is now the primary write target for:

- saved skill drafts
- saved workflow drafts
- capability-draft workflow saves

Bundled catalog skill installs now also land as manifest-backed extension
packages under `workspace/extensions/`, but not inside the managed
`workspace-capabilities` authoring package.

The old loose workspace folders remain readable only as transitional
compatibility inputs while existing content is migrated forward.

## Current local tools

Create a new capability pack:

```bash
backend/.venv/bin/python scripts/extensions/new_pack.py \
  ./tmp/my-pack \
  --id seraph.my-pack \
  --name "My Pack" \
  --with skills \
  --with workflows \
  --with runbooks
```

Validate a package:

```bash
backend/.venv/bin/python scripts/extensions/validate_pack.py ./tmp/my-pack
```

Current validation depth:

- `skills` and `workflows` get semantic parser checks
- `toolset_presets` get tool and execution-boundary permission checks
- connector payloads get payload and network-permission checks when present
- the remaining canonical contribution types currently get manifest/layout/file
  validation first, with deeper semantic validators landing in later slices

Current connector-runtime visibility:

- `GET /api/extensions/{id}/connectors` lists packaged connector contributions with a normalized health contract
- `POST /api/extensions/{id}/connectors/test` now routes packaged MCP connectors through a live runtime test path and returns normalized connector status for the other shipped connector surfaces
- `POST /api/extensions/{id}/connectors/enabled` now owns packaged MCP enable and disable changes, while raw `/api/mcp` update/remove/test/token flows reject extension-managed servers so package-owned connectors stay inside the extension lifecycle
- the standalone MCP config editor is now a manual-server path only; packaged MCP definitions stay read-only in Extension Studio until package-backed MCP source editing lands
- packaged managed connectors now use the same lifecycle state model: they ship disabled until configured, keep operator-supplied config in extension runtime state, support connector-level enable/disable through `POST /api/extensions/{id}/connectors/enabled`, and participate in package-level enable/disable through the normal extension lifecycle endpoints
- packaged `automation_triggers`, `browser_providers`, `messaging_connectors`, and `node_adapters` now also show up through the shared lifecycle and connector payload paths as planned connector surfaces, including package-bound config fields, config validation, enable/disable state, and normalized health/status summaries even before their concrete runtime integrations land in later waves
- packaged `toolset_presets`, `context_packs`, and `speech_profiles` now also appear as typed contribution metadata in extension payloads instead of generic manifest entries, so packages can carry operator-visible presets and future reach metadata through the shared registry today
- packaged observer definitions now also use shared lifecycle state: connector-level and package-level enable/disable both feed the observer runtime selector, disabling a higher-priority observer lets the next enabled packaged definition of the same `source_type` take over, disabling every packaged definition for that `source_type` leaves the selector empty instead of silently reviving hardcoded fallbacks, and observer health now distinguishes `active`, `disabled`, `invalid`, and `overridden`
- packaged channel adapters now use the same lifecycle state path: connector-level and package-level enable/disable both feed the delivery transport selector, disabling a higher-priority transport lets the next enabled packaged adapter take over, disabling every packaged adapter for that transport leaves delivery with no active adapter instead of reviving hardcoded fallbacks, and channel health now distinguishes `active`, `degraded`, `disabled`, `invalid`, and `overridden`
- the first shipped health contract now covers packaged MCP connectors, managed connectors, observer definitions, and channel adapters, with connector-level enable/disable available across every runtime-backed connector surface

For the concrete author workflow, continue with [Create A Capability Pack](./create-a-capability-pack.md).

## Lifecycle visibility

Extension lifecycle actions now land on Seraph's existing audit and activity seams.

That includes:

- validation attempts
- installs
- updates
- enable/disable actions
- configuration changes
- source saves
- removal

High-risk lifecycle actions still pause behind approvals when required, and failed
extension actions now show up with the same lifecycle context in the Activity Ledger.

## Versioning and update semantics

Manifest-backed packages now expose a lifecycle plan during validation.

That plan tells Seraph whether a package path represents:

- a new install
- a workspace-package update
- an up-to-date package
- a bundled-package override that should install into workspace scope

Current behavior:

- workspace-installed packages validate as `update` when the candidate package changes
- bundled packages validate as `install` when the candidate should become a workspace override
- package updates preserve existing runtime-enabled state for packaged MCP connectors while refreshing package-defined metadata such as URL, description, and auth hints
