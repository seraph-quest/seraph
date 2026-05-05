"""Node and device adapter inventory API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config.settings import settings
from src.extensions.node_adapters import NodeAdapterInventoryEntry, list_node_adapter_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import (
    clear_node_adapter_pairing_entry,
    connector_enabled_overrides,
    load_extension_state_payload,
    revoke_node_adapter_pairing_entry,
    save_extension_state_payload,
)

router = APIRouter()


class NodePairingMutationRequest(BaseModel):
    extension_id: str
    reference: str
    reason: str = Field(default="")


def _node_inventory(state_payload: dict[str, Any] | None = None) -> list[NodeAdapterInventoryEntry]:
    state_payload = state_payload if isinstance(state_payload, dict) else load_extension_state_payload()
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
    return inventory


def _adapter_payload(item: NodeAdapterInventoryEntry) -> dict[str, Any]:
    return {
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
        "pairing_state": item.pairing_state,
        "trust_state": item.trust_state,
        "paired": item.paired,
        "revoked": item.revoked,
        "pairing": dict(item.pairing),
        "safe_follow_up_ready": item.safe_follow_up_ready,
        "presence_contract": item.presence_contract,
        "reference": item.reference,
    }


def _find_adapter(
    inventory: list[NodeAdapterInventoryEntry],
    *,
    extension_id: str,
    reference: str,
) -> NodeAdapterInventoryEntry:
    for item in inventory:
        if item.extension_id == extension_id and item.reference == reference:
            return item
    raise HTTPException(status_code=404, detail="node adapter not found")


@router.get("/nodes/adapters")
async def list_node_adapters():
    inventory = _node_inventory()
    return {
        "adapters": [
            _adapter_payload(item)
            for item in inventory
        ]
    }


@router.get("/nodes/pairings")
async def list_node_pairings():
    inventory = _node_inventory()
    return {
        "pairings": [
            {
                "extension_id": item.extension_id,
                "reference": item.reference,
                "name": item.name,
                "adapter_kind": item.adapter_kind,
                "runtime_state": item.runtime_state,
                "pairing_state": item.pairing_state,
                "trust_state": item.trust_state,
                "paired": item.paired,
                "revoked": item.revoked,
                "safe_follow_up_ready": item.safe_follow_up_ready,
                "pairing": dict(item.pairing),
                "presence_contract": item.presence_contract,
            }
            for item in inventory
        ]
    }


@router.post("/nodes/pairings/revoke")
async def revoke_node_pairing(request: NodePairingMutationRequest):
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    revoke_node_adapter_pairing_entry(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
        reason=request.reason.strip(),
        revoked_at=datetime.now(timezone.utc).isoformat(),
    )
    save_extension_state_payload(state_payload)
    updated = _find_adapter(
        _node_inventory(state_payload),
        extension_id=adapter.extension_id,
        reference=adapter.reference,
    )
    return {"adapter": _adapter_payload(updated)}


@router.post("/nodes/pairings/clear")
async def clear_node_pairing(request: NodePairingMutationRequest):
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    removed = clear_node_adapter_pairing_entry(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
    )
    save_extension_state_payload(state_payload)
    updated = _find_adapter(
        _node_inventory(state_payload),
        extension_id=adapter.extension_id,
        reference=adapter.reference,
    )
    return {"cleared": removed, "adapter": _adapter_payload(updated)}
