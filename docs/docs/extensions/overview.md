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
- later: connectors such as MCP packages and managed integrations

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

Current author tooling is intentionally narrower:

- scaffolding today targets `capability-pack`
- connector packaging and install UX are delivered in later slices
- current `new_pack.py --with ...` support covers:
  - `skills`
  - `workflows`
  - `runbooks`
  - `starter_packs`
  - `provider_presets`
  - `prompt_packs`
  - `scheduled_routines`

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
   Deferred. Not part of the current implementation path.

## Canonical layout

Each contribution type has a canonical package root:

- `skills/`
- `workflows/`
- `runbooks/`
- `starter-packs/`
- `presets/provider/`
- `prompts/`
- `routines/`
- `mcp/`
- `connectors/managed/`
- `observers/definitions/`
- `observers/connectors/`
- `channels/`
- `workspace/`

Current scaffolding focuses on the capability-pack surfaces that are already in scope for authoring.

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
- connector payloads get payload and network-permission checks when present
- the remaining canonical contribution types currently get manifest/layout/file
  validation first, with deeper semantic validators landing in later slices

For the concrete author workflow, continue with [Create A Capability Pack](./create-a-capability-pack.md).
