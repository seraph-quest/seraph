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
| `prompt_packs` | `prompts/` |
| `scheduled_routines` | `routines/` |
| `mcp_servers` | `mcp/` |
| `managed_connectors` | `connectors/managed/` |
| `observer_definitions` | `observers/definitions/` |
| `observer_connectors` | `observers/connectors/` |
| `channel_adapters` | `channels/` |
| `workspace_adapters` | `workspace/` |

## Notes by surface

Current author tooling does not scaffold every canonical contribution type yet.
Today `new_pack.py --with ...` supports:

- `skills`
- `workflows`
- `runbooks`
- `starter_packs`
- `provider_presets`
- `prompt_packs`
- `scheduled_routines`

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

### `prompt_packs`

- packaged prompt sets or prompt scaffolding assets

### `scheduled_routines`

- packaged routine definitions for later scheduling surfaces

### Connector and reach surfaces

These roots are now canonical in the layout contract, but some lifecycle and authoring paths land later:

- `mcp_servers`
- `managed_connectors`
- `observer_definitions`
- `observer_connectors`
- `channel_adapters`
- `workspace_adapters`

That means the on-disk location is already fixed, even where the full install/configure lifecycle is not yet shipped.

Current content-validation depth is also uneven by design:

- `skills` and `workflows` get parser-level validation today
- connector definitions can get payload and network-permission checks
- the remaining contribution types currently rely on manifest, layout, and file
  existence checks until later slices add deeper validators
