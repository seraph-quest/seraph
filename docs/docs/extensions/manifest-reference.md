# Manifest Reference

Every extension package starts with `manifest.yaml`.

## Required fields

### `id`

Unique extension id.

Rules:

- lowercase letters, numbers, dots, hyphens, underscores
- must not start or end with punctuation

Example:

```yaml
id: seraph.research-pack
```

### `version`

Package version string. Must parse as a valid version.

Example:

```yaml
version: 2026.3.21
```

### `display_name`

Human-readable label.

### `kind`

Current supported values:

- `capability-pack`
- `connector-pack`

Authoring caveat:

- the manifest schema accepts both kinds today
- the shipped scaffold flow in this slice only creates `capability-pack`
- connector-pack install/authoring flows land in later connector slices

### `compatibility.seraph`

Version specifier for compatible Seraph runtimes.

Example:

```yaml
compatibility:
  seraph: ">=2026.3.19"
```

### `publisher.name`

Human-readable publisher name.

### `trust`

Current provenance values:

- `bundled`
- `local`
- `verified`

### `contributes`

Typed contribution lists keyed by contribution type.

At least one contribution is required.

## Permissions

Current manifest permissions are:

- `tools`
- `execution_boundaries`
- `network`
- `secrets`
- `env`

How Seraph uses them today:

- `tools`
  - must cover every tool required by packaged skills and workflows
  - missing tool declarations fail doctor validation and keep packaged skills/workflows out of the active runtime surface
- `execution_boundaries`
  - optional explicit boundary declaration for the package
  - if present, it must cover the derived boundaries of packaged skills/workflows/MCP connectors
  - if omitted, Seraph still derives runtime boundaries from the contributed tools
- `network`
  - must be `true` for networked connectors, observer sources, and any packaged skill/workflow that relies on networked tools such as `web_search`, `browse_webpage`, or MCP
- `secrets`
  - reserved allowlist for later secret-scope enforcement
  - include it only when the package intentionally needs named secret access semantics
- `env`
  - reserved allowlist for later environment-scope enforcement
  - include it only when the package intentionally depends on named environment variables

Approval behavior:

- packages that request high-risk execution boundaries such as `workspace_write`, `sandbox_execution`, `secret_*`, or `external_mcp` trigger approval on install/enable
- that lifecycle approval is bound to the exact package path and package-content digest Seraph validated, so changing the package revision requires a fresh approval
- high-risk runtime behavior is still enforced again at tool/workflow execution time by Seraph's core approval and policy systems

Example:

```yaml
permissions:
  tools:
    - read_file
    - write_file
  network: false
```

## Full example

```yaml
id: seraph.research-pack
version: 2026.3.21
display_name: Research Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/research-pack.md
  workflows:
    - workflows/research-pack.md
  runbooks:
    - runbooks/research-pack.yaml
permissions:
  tools:
    - read_file
```

For the meaning and canonical root of each contribution field, see [Contribution Types](./contribution-types.md).
