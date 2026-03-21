# Validation And Doctor

Seraph uses two layers when checking an extension package:

1. Manifest and registry validation
2. Doctor diagnostics

## Validate locally

```bash
backend/.venv/bin/python scripts/extensions/validate_pack.py ./tmp/research-pack
```

## What validation catches

### Manifest / registry layer

- invalid manifest YAML
- invalid ids or versions
- invalid compatibility specifiers
- unsupported contribution types
- contribution paths outside the canonical layout
- nested manifests inside contribution folders being ignored as package roots
- resolved contribution paths escaping the package root

### Doctor layer

- missing referenced files
- unreadable files
- invalid skill files
- invalid workflow files
- tool-permission mismatches
- connector payload parse failures
- connector network-permission mismatches when the payload actually implies a network transport

Current semantic validation is intentionally narrower than the full canonical
contribution matrix:

- `skills` and `workflows` get parser-level content validation
- connector payloads get payload and network-permission checks
- `runbooks`, `starter_packs`, `provider_presets`, `prompt_packs`, and
  `scheduled_routines` are still validated primarily through manifest, layout,
  file-boundary, and existence checks

## Output shape

The validator emits JSON.

Top-level fields:

- `ok`
- `load_errors`
- `results`

Special case:

- if the target path is not an extension package at all, the tool exits non-zero
  with `{ "ok": false, "error": "..." }` instead of the normal report shape

Typical non-OK patterns:

- `load_errors` non-empty
  - package failed before the doctor layer
- `results[].issues` non-empty
  - package loaded, but content still needs repair

## Common errors

### `missing_reference`

The manifest points to a file that does not exist.

### `unreadable_contribution`

The file exists, but cannot be read as UTF-8 text.

### `invalid_skill`

The skill markdown/frontmatter is not parseable.

### `invalid_workflow`

The workflow markdown/frontmatter/steps are not parseable.

### `permission_mismatch`

The manifest’s declared permissions do not match what the contribution requires.

## Wrong path behavior

If you validate a directory that is not an extension package, the tool exits
non-zero and reports that no extension manifest was found under that path using
the `{ "ok": false, "error": ... }` shape above.
