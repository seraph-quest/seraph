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
