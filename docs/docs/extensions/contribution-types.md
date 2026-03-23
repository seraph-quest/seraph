# Contribution Types

Each manifest contribution list maps to a canonical on-disk root.

## Current canonical roots

| Contribution type | Canonical root |
| --- | --- |
| `skills` | `skills/` |
| `workflows` | `workflows/` |
| `runbooks` | `runbooks/` |
| `starter_packs` | `starter-packs/` |
| `provider_presets` | `presets/provider/` |
| `toolset_presets` | `presets/toolset/` |
| `prompt_packs` | `prompts/` |
| `context_packs` | `context/` |
| `scheduled_routines` | `routines/` |
| `mcp_servers` | `mcp/` |
| `managed_connectors` | `connectors/managed/` |
| `automation_triggers` | `automation/` |
| `browser_providers` | `connectors/browser/` |
| `messaging_connectors` | `connectors/messaging/` |
| `observer_definitions` | `observers/definitions/` |
| `observer_connectors` | `observers/connectors/` |
| `channel_adapters` | `channels/` |
| `speech_profiles` | `speech/` |
| `node_adapters` | `connectors/nodes/` |
| `workspace_adapters` | `workspace/` |

## Notes by surface

Current author tooling does not scaffold every canonical contribution type yet.
Today `new_pack.py --with ...` supports:

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

### `skills`

- markdown with YAML frontmatter
- validated by the skill parser

### `workflows`

- markdown with YAML frontmatter
- validated by the workflow parser
- current doctor checks declared tool permissions against workflow requirements

### `runbooks`

- structured helper content for reusable operator procedures

### `starter_packs`

- packaged starter bundles for turning on related capability surfaces together

### `provider_presets`

- packaged provider/model routing defaults

### `toolset_presets`

- packaged operator-facing tool bundles and policy-aware capability presets

### `context_packs`

- packaged guardian context bundles such as prompt instructions, profile fields, and memory tags

### `prompt_packs`

- packaged prompt sets or prompt scaffolding assets

### `scheduled_routines`

- packaged routine definitions for later scheduling surfaces

### Connector and reach surfaces

These roots are now canonical in the layout contract. Runtime depth still
varies by surface:

- `mcp_servers`
- `managed_connectors`
- `automation_triggers`
- `browser_providers`
- `messaging_connectors`
- `observer_definitions`
- `observer_connectors`
- `channel_adapters`
- `node_adapters`
- `workspace_adapters`
- `speech_profiles`

That means the on-disk location is fixed for every surface, while runtime
adoption still ranges from passive typed metadata up through full
connector-aware lifecycle handling.

Kind note:

- `observer_definitions` are shared-lifecycle reach surfaces, but they are still treated as capability-pack content rather than connector-pack-only surfaces
- connector-pack manifests must currently use true connector contributions such as `mcp_servers`, `managed_connectors`, `automation_triggers`, `browser_providers`, `messaging_connectors`, `channel_adapters`, or `node_adapters`

Current content-validation depth is also uneven by design:

- `skills` and `workflows` get parser-level validation today
- connector and reach definitions can get payload and network-permission checks
- the remaining contribution types currently rely on manifest, layout, and file
  existence checks until later slices add deeper validators

Managed connector note:

- `managed_connectors` now participate in the shared extension lifecycle
- packaged managed connectors ship disabled until configured unless the definition explicitly sets `enabled: true`
- operator-supplied config now lives in extension runtime state rather than the package manifest itself
- enable/disable now goes through the shared connector toggle endpoint rather than raw connector-specific APIs

Observer definition note:

- `observer_definitions` now participate in the shared extension lifecycle
- connector-level and package-level enable/disable both write lifecycle state overrides keyed by extension id and contribution reference
- the observer runtime selector now honors those overrides directly, so disabling a higher-priority observer source yields to another enabled packaged definition of the same `source_type`, and disabling every packaged definition for that `source_type` removes it from active refresh entirely

Channel adapter note:

- `channel_adapters` now participate in the shared extension lifecycle
- connector-level and package-level enable/disable both write lifecycle state overrides keyed by extension id and contribution reference
- the delivery transport selector now honors those overrides directly, so disabling a higher-priority adapter yields to another enabled packaged adapter for the same transport, and disabling every packaged adapter for that transport removes it from active delivery entirely
