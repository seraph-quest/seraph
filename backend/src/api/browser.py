"""Browser provider inventory API."""

from __future__ import annotations

from fastapi import APIRouter

from config.settings import settings
from src.extensions.browser_providers import list_browser_provider_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

router = APIRouter()


@router.get("/browser/providers")
async def list_browser_providers():
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    inventory = list_browser_provider_inventory(
        snapshot.list_contributions("browser_providers"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None),
    )
    return {
        "providers": [
            {
                "extension_id": item.extension_id,
                "name": item.name,
                "provider_kind": item.provider_kind,
                "description": item.description,
                "enabled": item.enabled,
                "configured": item.configured,
                "selected": item.selected,
                "execution_mode": item.execution_mode,
                "runtime_state": item.runtime_state,
                "config_keys": list(item.config_keys),
                "requires_network": item.requires_network,
                "requires_daemon": item.requires_daemon,
                "capabilities": list(item.capabilities),
                "reference": item.reference,
            }
            for item in inventory
        ]
    }
