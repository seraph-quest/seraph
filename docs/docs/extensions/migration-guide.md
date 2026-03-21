# Migration Guide

The extension platform is landing in stages.

Current reality:

- skills, workflows, runbooks, starter packs, and MCP surfaces still have transitional legacy loading paths
- the manifest, registry, doctor, layout, and scaffold seams now exist
- built-in declarative defaults now ship as the bundled `seraph.core-capabilities` package, but install/bootstrap/catalog and lifecycle paths still have transitional legacy behavior

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

That is temporary. The migration slices later in the roadmap remove those parallel paths once packaged loading fully replaces them. Until then, some catalog flows still copy bundled skill files into the workspace as transitional compatibility shims, and some bootstrap flows still reinitialize managers against bundled manifest roots to make packaged workflows available. Under the normal manifest-backed startup path those compatibility shims are not the primary source of truth; they exist only to keep legacy install/bootstrap flows working until the lifecycle migration slices land.

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
