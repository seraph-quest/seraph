"""Node and device adapter inventory API."""

from __future__ import annotations

from fastapi import APIRouter

from config.settings import settings
from src.extensions.node_adapters import list_node_adapter_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

router = APIRouter()


@router.get("/nodes/adapters")
async def list_node_adapters():
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    inventory = list_node_adapter_inventory(
        snapshot.list_contributions("node_adapters"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None),
    )
    return {
        "adapters": [
            {
                "extension_id": item.extension_id,
                "name": item.name,
                "adapter_kind": item.adapter_kind,
                "description": item.description,
                "enabled": item.enabled,
                "configured": item.configured,
                "config_keys": list(item.config_keys),
                "capabilities": list(item.capabilities),
                "requires_network": item.requires_network,
                "requires_daemon": item.requires_daemon,
                "runtime_state": item.runtime_state,
                "reference": item.reference,
            }
            for item in inventory
        ]
    }
