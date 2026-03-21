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

## 6. Install/use later

This docs slice focuses on authoring and local validation.

The later lifecycle slices add:

- install
- enable/disable
- configure
- health/test
- update/remove

For the manifest fields themselves, see [Manifest Reference](./manifest-reference.md).
