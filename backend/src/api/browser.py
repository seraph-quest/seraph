"""Browser provider inventory and live-control API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from config.settings import settings
from src.browser.sessions import browser_session_runtime
from src.extensions.browser_providers import list_browser_provider_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload
from src.tools.browser_session_tool import _resolve_browser_provider
from src.tools.browser_tool import browse_webpage

router = APIRouter()


class BrowserSessionOpenRequest(BaseModel):
    owner_session_id: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    provider: str = ""
    capture: Literal["extract", "html", "screenshot"] = "extract"


class BrowserSessionSnapshotRequest(BaseModel):
    owner_session_id: str = Field(..., min_length=1)
    capture: Literal["extract", "html", "screenshot"] = "extract"


class BrowserSessionControlRequest(BaseModel):
    owner_session_id: str = Field(..., min_length=1)
    action: Literal["quarantine", "recover", "reset_partition", "replay_snapshot", "close"]
    reason: str = ""
    acknowledge_degraded_fallback: bool = False


class BrowserComputerUseControlActionRequest(BrowserSessionControlRequest):
    session_id: str = Field(..., min_length=1)


def _capture_or_raise(url: str, capture: str) -> str:
    content = browse_webpage(url.strip(), action=capture)
    if str(content or "").startswith("Error:"):
        raise HTTPException(status_code=400, detail=content)
    return content


def _metadata_only_session_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    metadata = dict(payload)
    metadata.pop("content", None)
    return metadata


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
                "credential_surface": item.credential_surface,
                "cookie_scope": item.cookie_scope,
                "profile_persistence": item.profile_persistence,
                "owner_scope": item.owner_scope,
                "remote_transport": item.remote_transport,
                "fallback_policy": item.fallback_policy,
            }
            for item in inventory
        ]
    }


async def _browser_provider_payload() -> dict[str, object]:
    return await list_browser_providers()


@router.get("/browser/sessions")
async def list_browser_sessions(owner_session_id: str = Query(..., min_length=1)):
    return {
        "owner_session_id": owner_session_id,
        "sessions": browser_session_runtime.list_sessions(owner_session_id=owner_session_id),
    }


@router.post("/browser/sessions")
async def open_browser_session(request: BrowserSessionOpenRequest):
    provider_info, provider_error = _resolve_browser_provider(request.provider)
    if provider_error:
        raise HTTPException(status_code=400, detail=provider_error)
    content = _capture_or_raise(request.url, request.capture)
    payload = browser_session_runtime.open_session(
        owner_session_id=request.owner_session_id,
        url=request.url.strip(),
        provider_name=provider_info["provider_name"],
        provider_kind=provider_info["provider_kind"],
        execution_mode=provider_info["execution_mode"],
        capture=request.capture,
        content=content,
    )
    return {"session": _metadata_only_session_payload(payload)}


@router.get("/browser/sessions/{session_id}")
async def get_browser_session(session_id: str, owner_session_id: str = Query(..., min_length=1)):
    payload = browser_session_runtime.get_session(session_id, owner_session_id=owner_session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="browser_session_not_found")
    return {"session": _metadata_only_session_payload(payload)}


@router.post("/browser/sessions/{session_id}/snapshot")
async def snapshot_browser_session(session_id: str, request: BrowserSessionSnapshotRequest):
    existing = browser_session_runtime.get_session(session_id, owner_session_id=request.owner_session_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="browser_session_not_found")
    content = _capture_or_raise(str(existing["url"]), request.capture)
    payload = browser_session_runtime.snapshot_session(
        owner_session_id=request.owner_session_id,
        session_id=session_id,
        capture=request.capture,
        content=content,
    )
    if isinstance(payload, dict) and payload.get("error") == "session_quarantined":
        raise HTTPException(status_code=409, detail=payload)
    return {"session": payload}


@router.get("/browser/refs/{ref:path}")
async def read_browser_ref(ref: str, owner_session_id: str = Query(..., min_length=1)):
    payload = browser_session_runtime.read_ref(ref, owner_session_id=owner_session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="browser_ref_not_found")
    return {"ref": payload}


@router.post("/browser/sessions/{session_id}/control")
async def control_browser_session(session_id: str, request: BrowserSessionControlRequest):
    if request.action == "replay_snapshot":
        replay_state = browser_session_runtime.validate_replay_session(
            session_id,
            owner_session_id=request.owner_session_id,
            acknowledge_degraded_fallback=request.acknowledge_degraded_fallback,
        )
        if replay_state is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        if isinstance(replay_state, dict) and replay_state.get("error"):
            raise HTTPException(status_code=409, detail=replay_state)
        session = replay_state["session"]
        capture = str(session.get("latest_capture") or "extract")
        content = _capture_or_raise(str(session["url"]), capture)
        payload = browser_session_runtime.snapshot_session(
            owner_session_id=request.owner_session_id,
            session_id=session_id,
            capture=capture,
            content=content,
        )
        if isinstance(payload, dict) and payload.get("error") == "session_quarantined":
            raise HTTPException(status_code=409, detail=payload)
        result = browser_session_runtime.control_session(
            session_id,
            owner_session_id=request.owner_session_id,
            action=request.action,
            reason=request.reason,
            acknowledge_degraded_fallback=request.acknowledge_degraded_fallback,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(status_code=409, detail=result)
        refreshed = browser_session_runtime.get_session(
            session_id,
            owner_session_id=request.owner_session_id,
        )
        result["session"] = refreshed or payload
        return result

    result = browser_session_runtime.control_session(
        session_id,
        owner_session_id=request.owner_session_id,
        action=request.action,
        reason=request.reason,
        acknowledge_degraded_fallback=request.acknowledge_degraded_fallback,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="browser_session_not_found")
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.delete("/browser/sessions/{session_id}")
async def close_browser_session(session_id: str, owner_session_id: str = Query(..., min_length=1)):
    payload = browser_session_runtime.close_session(session_id, owner_session_id=owner_session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="browser_session_not_found")
    return {"session": payload}


@router.get("/operator/browser-computer-use-control")
async def browser_computer_use_control(owner_session_id: str = Query(..., min_length=1)):
    provider_payload = await _browser_provider_payload()
    return {
        "owner_session_id": owner_session_id,
        "providers": provider_payload["providers"],
        "sessions": browser_session_runtime.list_sessions(owner_session_id=owner_session_id),
        "blocked_claims": [
            "safe_browser_automation",
            "safe_autonomous_computer_use",
            "full_browser_parity",
            "arbitrary_credentialed_browsing_safety",
            "production_browser_automation_readiness",
        ],
    }


@router.post("/operator/browser-computer-use-control/actions")
async def browser_computer_use_control_action(request: BrowserComputerUseControlActionRequest):
    return await control_browser_session(request.session_id, request)
