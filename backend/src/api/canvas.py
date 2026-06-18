"""Structured canvas output inventory for operator-visible result surfaces."""

from __future__ import annotations

from fastapi import APIRouter

from config.settings import settings
from src.extensions.canvas_outputs import list_canvas_output_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace

router = APIRouter()


@router.get("/canvas/outputs")
async def list_canvas_outputs():
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    inventory = list_canvas_output_inventory(snapshot.list_contributions("canvas_outputs"))
    return {
        "outputs": [
            {
                "extension_id": item.extension_id,
                "name": item.name,
                "title": item.title,
                "description": item.description,
                "surface_kind": item.surface_kind,
                "sections": list(item.sections),
                "artifact_types": list(item.artifact_types),
                "preferred_panel": item.preferred_panel,
                "reference": item.reference,
            }
            for item in inventory
        ]
    }
