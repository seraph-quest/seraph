"""Automation trigger inventory and webhook ingress."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from config.settings import settings
from src.audit.runtime import log_integration_event
from src.extensions.automation import list_automation_trigger_inventory, select_automation_trigger
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

router = APIRouter()


def _automation_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    inventory = list_automation_trigger_inventory(
        snapshot.list_contributions("automation_triggers"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None),
    )
    return state_payload, [
        {
            "extension_id": item.extension_id,
            "name": item.name,
            "trigger_type": item.trigger_type,
            "description": item.description,
            "enabled": item.enabled,
            "configured": item.configured,
            "config_keys": list(item.config_keys),
            "schedule": item.schedule,
            "endpoint": item.endpoint,
            "topic": item.topic,
            "capabilities": list(item.capabilities),
            "requires_network": item.requires_network,
            "runtime_state": item.runtime_state,
            "reference": item.reference,
        }
        for item in inventory
    ]


@router.get("/automation/triggers")
async def list_automation_triggers():
    _state_payload, inventory = _automation_snapshot()
    return {"triggers": inventory}


@router.post("/automation/webhooks/{trigger_name}")
async def receive_automation_webhook(
    trigger_name: str,
    request: Request,
    x_seraph_signature: str | None = Header(default=None),
):
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    trigger = select_automation_trigger(
        snapshot.list_contributions("automation_triggers"),
        trigger_name=trigger_name,
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None),
    )
    if trigger is None or not trigger.enabled or not trigger.configured or trigger.trigger_type != "webhook":
        raise HTTPException(status_code=404, detail=f"Webhook trigger '{trigger_name}' is not armed")

    config_secret: str | None = None
    if isinstance(state_by_id, dict):
        extension_entry = state_by_id.get(trigger.extension_id)
        if isinstance(extension_entry, dict):
            config = extension_entry.get("config")
            if isinstance(config, dict):
                trigger_config = config.get("automation_triggers")
                if isinstance(trigger_config, dict):
                    named_config = trigger_config.get(trigger.name)
                    if isinstance(named_config, dict):
                        candidate_secret = named_config.get("signing_secret")
                        if isinstance(candidate_secret, str) and candidate_secret.strip():
                            config_secret = candidate_secret
    if config_secret is not None and x_seraph_signature != config_secret:
        raise HTTPException(status_code=403, detail="Webhook signature rejected")

    try:
        payload = await request.json()
    except Exception:
        payload = {"raw_body": (await request.body()).decode("utf-8", errors="replace")}

    await log_integration_event(
        integration_type="automation_trigger",
        name=trigger.name,
        outcome="accepted",
        details={
            "trigger_type": trigger.trigger_type,
            "extension_id": trigger.extension_id,
            "capabilities": list(trigger.capabilities),
            "payload_keys": (
                sorted(str(key) for key in payload.keys())
                if isinstance(payload, dict)
                else None
            ),
        },
    )
    return {
        "status": "accepted",
        "trigger": {
            "name": trigger.name,
            "trigger_type": trigger.trigger_type,
            "extension_id": trigger.extension_id,
            "runtime_state": trigger.runtime_state,
        },
        "payload": payload,
    }
