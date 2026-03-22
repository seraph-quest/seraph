# Create A Capability Pack

This is the fastest way to add new reusable capability under the extension platform.

## 1. Scaffold the package

Run the local scaffold tool from the repo root:

```bash
backend/.venv/bin/python scripts/extensions/new_pack.py \
  ./tmp/research-pack \
  --id seraph.research-pack \
  --name "Research Pack" \
  --with skills \
  --with workflows \
  --with runbooks
```

What this creates:

- `manifest.yaml`
- one valid starter skill
- one valid starter workflow
- one starter runbook

If you want a canonical in-repo reference, compare your package against:

- `examples/extensions/research-pack/`

Current scope:

- the scaffold targets `capability-pack`
- connector scaffolding is intentionally deferred to later connector slices
- current `--with` support is limited to:
  - `skills`
  - `workflows`
  - `runbooks`
  - `starter_packs`
  - `provider_presets`
  - `prompt_packs`
  - `scheduled_routines`

## 2. Inspect the generated layout

Example package:

```text
research-pack/
├─ manifest.yaml
├─ skills/
│  └─ research-pack.md
├─ workflows/
│  └─ research-pack.md
└─ runbooks/
   └─ research-pack.yaml
```

The same structure now exists in-repo at `examples/extensions/research-pack/`.

## 3. Edit the package contents

Typical next steps:

- update the skill instructions in `skills/*.md`
- update the workflow steps in `workflows/*.md`
- adjust the runbook metadata in `runbooks/*.yaml`
- add or remove contribution files, then keep `manifest.yaml` in sync

The shipped example pack keeps the scaffolded runbook placeholder convention:

- `runbooks/research-pack.yaml` uses `workflow: research-pack`
- that matches the shared file slug used by the example workflow file

The generated workflow is intentionally minimal but valid:

- it declares the `read_file` tool
- it includes one starter step
- it passes the current doctor/registry validation seam

## 4. Validate the package

```bash
backend/.venv/bin/python scripts/extensions/validate_pack.py ./tmp/research-pack
```

The validator checks:

- manifest parsing
- compatibility rules
- canonical package layout
- package-boundary resolution
- doctor diagnostics such as:
  - missing files
  - unreadable files
  - invalid skills
  - invalid workflows
  - permission mismatches

Current semantic validation is intentionally narrower than the full canonical
contribution matrix:

- `skills` and `workflows` get parser-level validation
- connector payloads get payload and network-permission checks when present
- `runbooks`, `starter_packs`, `provider_presets`, `prompt_packs`, and
  `scheduled_routines` currently get manifest/layout/file-boundary validation,
  but not deep semantic validation yet

## 5. Iterate until validation is clean

A clean package returns `ok: true`.

If validation fails, fix the reported files and run the validator again.

## 6. Install/use through the lifecycle API

This docs slice focuses on authoring and local validation.

The backend lifecycle API now supports:

- list
- inspect
- validate
- install
- update
- enable/disable
- configure (metadata-only in this slice)
- remove

Current gap:

- workspace UI adoption, runtime-consumed typed config, and richer health/test flows still land in later lifecycle slices

Current backend endpoints:

- `GET /api/extensions`
- `GET /api/extensions/{extension_id}`
- `GET /api/extensions/{extension_id}/source`
- `POST /api/extensions/{extension_id}/source`
- `POST /api/extensions/validate`
- `POST /api/extensions/install`
- `POST /api/extensions/update`
- `POST /api/extensions/{extension_id}/enable`
- `POST /api/extensions/{extension_id}/disable`
- `POST /api/extensions/{extension_id}/configure`
- `DELETE /api/extensions/{extension_id}`
- `GET /api/extensions/{extension_id}/connectors`
- `POST /api/extensions/{extension_id}/connectors/test`
- `POST /api/extensions/{extension_id}/connectors/enabled`

Install/update rules:

- validate a package path first
- if validation reports `recommended_action: install`, use `POST /api/extensions/install`
- if validation reports `recommended_action: update`, use `POST /api/extensions/update`
- if validation reports a bundled `workspace_override`, install the package; Seraph will keep the bundled package intact and place the override under the workspace extension root

High-risk package note:

- packages that declare or derive high-risk boundaries such as `workspace_write`, `sandbox_execution`, `secret_*`, or `external_mcp` now pause behind the normal approval system on install, update, and enable
- that approval is tied to the validated package path and content digest, so changing the package after approval forces a new approval
- low-risk declarative packages install and enable directly

Current Studio support:

- workspace-installed packages can now be opened in Extension Studio with their `manifest.yaml`
  and package-backed workflow/skill members grouped together
- manifest, packaged workflow, and packaged skill edits save back through
  `/api/extensions/{extension_id}/source`
- packaged MCP connectors now test and toggle through `/api/extensions/{extension_id}/connectors/*`
  instead of raw `/api/mcp`
- packaged managed connectors now also use `/api/extensions/{extension_id}/connectors/*`
  for health/test and enable/disable, while their operator-supplied config lives
  in extension runtime state rather than the package manifest
- the standalone MCP config editor remains for manual servers only; packaged MCP definitions are
  read-only in Extension Studio until package-backed MCP source editing lands

Managed workspace authoring note:

- when Seraph saves a new skill or workflow from the live workspace, it now writes
  into the managed `workspace/extensions/workspace-capabilities/` package instead
  of creating new loose files under `workspace/skills/` or `workspace/workflows/`
- those loose folders are now transitional read-compatibility only

For the manifest fields themselves, see [Manifest Reference](./manifest-reference.md).
