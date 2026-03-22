# Migration Guide

The extension platform is landing in stages.

Current reality:

- skills, workflows, runbooks, starter packs, and MCP surfaces still have transitional legacy loading paths
- the manifest, registry, doctor, layout, and scaffold seams now exist
- built-in declarative defaults now ship as the bundled `seraph.core-capabilities` package
- new authored skills/workflows now land in the managed workspace package at `workspace/extensions/workspace-capabilities/`
- bundled catalog skill installs now land as generated manifest-backed extension packages under `workspace/extensions/`

## What this means for contributors right now

If you are adding new reusable capability:

1. prefer creating a manifest-backed capability pack
2. keep the package inside the canonical layout
3. validate it with `validate_pack.py`
4. expect some runtime loading paths to remain transitional until the packaging slices finish

If you need a concrete reference while migrating, start from the shipped example
package in `examples/extensions/research-pack/`.

## Transitional compatibility

Current registry behavior still synthesizes legacy entries for:

- loose skills
- loose workflows
- loose runbooks
- loose starter packs
- runtime MCP state

That is temporary. The migration slices later in the roadmap remove those parallel paths once packaged loading fully replaces them. The important line today is:

- new content is package-backed
- old loose content is still readable

Until the last cleanup steps land, some runtime seams still read loose legacy content and the registry still surfaces those paths as transitional compatibility records. Under the normal manifest-backed startup and authoring path, those compatibility shims are no longer the primary source of truth.

## Recommended migration order for existing content

1. package loose skills
2. package loose workflows
3. package runbooks and starter packs
4. move built-in defaults onto bundled capability packs
5. then unify connector packaging and lifecycle

## What not to migrate into extensions

Do not try to package Seraph core safety/runtime systems as extensions:

- policy
- approvals
- audit/activity ownership
- secret handling
- sessions/threads
- workflow execution
- routing

Those remain core by design.
