"""Browser provider inventory and live-control API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import settings
from src.agent.session import session_manager
from src.approval.runtime import get_current_session_id, reset_runtime_context, set_runtime_context
from src.api.capabilities import _require_authenticated_capability_operator
from src.api.chat import _begin_rest_revocation_watch, _end_rest_revocation_watch, _ensure_rest_authorized
from src.auth.cancellation import RuntimeRevokedError, assert_runtime_not_revoked
from src.auth.service import auth_enabled, bind_operator_principal
from src.browser.sessions import browser_session_runtime
from src.extensions.browser_providers import list_browser_provider_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload
from src.observer.manager import context_manager
from src.tools.browser_session_tool import _resolve_browser_provider
from src.tools.browser_tool import browse_webpage, redact_browser_error

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


async def _capture_or_raise(url: str, capture: str) -> str:
    assert_runtime_not_revoked()
    content = await asyncio.to_thread(browse_webpage, url.strip(), action=capture)
    if str(content or "").startswith("Error:"):
        raise HTTPException(status_code=400, detail=redact_browser_error(content))
    return content


def _metadata_only_session_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    metadata = dict(payload)
    metadata.pop("content", None)
    return metadata


async def _bind_browser_operator(request: Request, owner_session_id: str | None):
    """Bind browser authority to the authenticated operator's conversation.

    The authentication cookie session is the revocation handle for the
    operator. Chat and WebSocket ingress then bind that operator principal to
    the server-created conversation session, which is the owner persisted by
    the browser runtime and sent by the cockpit. Browser routes use that same
    conversation identity; the caller-supplied value is never authority on its
    own. If an execution context is already active, the request owner must
    match it so a nested request cannot switch to another browser owner.

    The current operator authentication model has one authenticated operator
    principal, so the middleware-bound principal is the server-side relation
    for every conversation session it creates. A future multi-operator owner
    relation must be added at the session boundary before that model changes.
    """
    operator = _require_authenticated_capability_operator(request)
    if owner_session_id is not None and not str(owner_session_id).strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "browser_owner_session_required"},
        )
    requested_session_id = str(owner_session_id or "").strip()
    active_runtime_session_id = get_current_session_id()
    if (
        active_runtime_session_id
        and requested_session_id
        and requested_session_id != active_runtime_session_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "browser_owner_session_mismatch"},
        )
    canonical_owner_session_id = requested_session_id or active_runtime_session_id or None
    if canonical_owner_session_id and not active_runtime_session_id and auth_enabled():
        conversation = await session_manager.get(canonical_owner_session_id)
        if (
            conversation is None
            or conversation.owner_principal_id != operator.principal.principal_id
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "browser_owner_session_forbidden"},
            )
    context_session_id = canonical_owner_session_id or operator.session_id
    tokens = set_runtime_context(
        context_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, context_session_id),
    )
    return canonical_owner_session_id, tokens


async def _bind_browser_read_operator(request: Request, owner_session_id: str | None):
    """Keep read routes on the same canonical authority path as mutators."""

    return await _bind_browser_operator(request, owner_session_id)


@asynccontextmanager
async def _browser_request_authority(
    request: Request,
    owner_session_id: str | None,
) -> AsyncIterator[str | None]:
    """Authorize, watch, and clean up one browser request."""

    bound_owner_session_id, tokens = await _bind_browser_operator(request, owner_session_id)
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        assert_runtime_not_revoked()
        yield bound_owner_session_id
        await _ensure_rest_authorized(request, revocation_scope)
        assert_runtime_not_revoked()
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


def _browser_provider_inventory_payload() -> dict[str, object]:
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


@router.get("/browser/providers")
async def list_browser_providers(
    http_request: Request,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id):
        payload = _browser_provider_inventory_payload()
        return payload


async def _browser_provider_payload() -> dict[str, object]:
    return _browser_provider_inventory_payload()


@router.get("/browser/sessions")
async def list_browser_sessions(
    http_request: Request,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id) as bound_owner_session_id:
        sessions = browser_session_runtime.list_sessions(owner_session_id=bound_owner_session_id)
        journal = browser_session_runtime.list_journal(owner_session_id=bound_owner_session_id)
        return {
            "owner_session_id": bound_owner_session_id,
            "sessions": sessions,
            "journal": journal,
        }


@router.post("/browser/sessions")
async def open_browser_session(request: BrowserSessionOpenRequest, http_request: Request):
    async with _browser_request_authority(http_request, request.owner_session_id) as owner_session_id:
        provider_info, provider_error = _resolve_browser_provider(request.provider)
        if provider_error:
            raise HTTPException(status_code=400, detail=provider_error)
        assert_runtime_not_revoked()
        content = await _capture_or_raise(request.url, request.capture)
        assert_runtime_not_revoked()
        payload = browser_session_runtime.open_session(
            owner_session_id=owner_session_id,
            url=request.url.strip(),
            provider_name=provider_info["provider_name"],
            provider_kind=provider_info["provider_kind"],
            execution_mode=provider_info["execution_mode"],
            capture=request.capture,
            content=content,
        )
        return {"session": _metadata_only_session_payload(payload)}


@router.get("/browser/sessions/{session_id}")
async def get_browser_session(
    session_id: str,
    http_request: Request,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id) as bound_owner_session_id:
        payload = browser_session_runtime.get_session(
            session_id,
            owner_session_id=bound_owner_session_id,
        )
        if payload is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        metadata = _metadata_only_session_payload(payload)
        return {"session": metadata}


@router.post("/browser/sessions/{session_id}/snapshot")
async def snapshot_browser_session(
    session_id: str,
    request: BrowserSessionSnapshotRequest,
    http_request: Request,
):
    async with _browser_request_authority(http_request, request.owner_session_id) as owner_session_id:
        session = browser_session_runtime.get_session(
            session_id,
            owner_session_id=owner_session_id,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        if session.get("replayable") is False:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "session_replay_unavailable_after_reload",
                    "session": session,
                },
            )
        capture_url = browser_session_runtime.get_session_capture_url(
            session_id,
            owner_session_id=owner_session_id,
        )
        if capture_url is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        assert_runtime_not_revoked()
        content = await _capture_or_raise(capture_url, request.capture)
        assert_runtime_not_revoked()
        payload = browser_session_runtime.snapshot_session(
            owner_session_id=owner_session_id,
            session_id=session_id,
            capture=request.capture,
            content=content,
        )
        if isinstance(payload, dict) and payload.get("error") == "session_quarantined":
            raise HTTPException(status_code=409, detail=payload)
        return {"session": _metadata_only_session_payload(payload)}


@router.get("/browser/sessions/{session_id}/journal")
async def get_browser_session_journal(
    session_id: str,
    http_request: Request,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id) as bound_owner_session_id:
        if browser_session_runtime.get_session(
            session_id,
            owner_session_id=bound_owner_session_id,
        ) is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        journal = browser_session_runtime.list_journal(
            owner_session_id=bound_owner_session_id,
            session_id=session_id,
        )
        return {
            "owner_session_id": bound_owner_session_id,
            "session_id": session_id,
            "journal": journal,
        }


@router.get("/browser/refs/{ref:path}")
async def read_browser_ref(
    ref: str,
    http_request: Request,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id) as bound_owner_session_id:
        payload = browser_session_runtime.read_ref(ref, owner_session_id=bound_owner_session_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="browser_ref_not_found")
        return {"ref": payload}


@router.post("/browser/sessions/{session_id}/control")
async def control_browser_session(
    session_id: str,
    request: BrowserSessionControlRequest,
    http_request: Request,
):
    async with _browser_request_authority(http_request, request.owner_session_id) as owner_session_id:
        if request.action == "replay_snapshot":
            replay_state = browser_session_runtime.validate_replay_session(
                session_id,
                owner_session_id=owner_session_id,
                acknowledge_degraded_fallback=request.acknowledge_degraded_fallback,
            )
            if replay_state is None:
                raise HTTPException(status_code=404, detail="browser_session_not_found")
            if isinstance(replay_state, dict) and replay_state.get("error"):
                raise HTTPException(status_code=409, detail=replay_state)
            session = replay_state["session"]
            capture = str(session.get("latest_capture") or "extract")
            assert_runtime_not_revoked()
            capture_url = browser_session_runtime.get_session_capture_url(
                session_id,
                owner_session_id=owner_session_id,
            )
            if capture_url is None:
                raise HTTPException(status_code=404, detail="browser_session_not_found")
            assert_runtime_not_revoked()
            content = await _capture_or_raise(capture_url, capture)
            assert_runtime_not_revoked()
            payload = browser_session_runtime.snapshot_session(
                owner_session_id=owner_session_id,
                session_id=session_id,
                capture=capture,
                content=content,
            )
            if isinstance(payload, dict) and payload.get("error") == "session_quarantined":
                raise HTTPException(status_code=409, detail=payload)
            assert_runtime_not_revoked()
            result = browser_session_runtime.control_session(
                session_id,
                owner_session_id=owner_session_id,
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
                owner_session_id=owner_session_id,
            )
            result["session"] = refreshed or payload
            return result

        assert_runtime_not_revoked()
        result = browser_session_runtime.control_session(
            session_id,
            owner_session_id=owner_session_id,
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
async def close_browser_session(
    http_request: Request,
    session_id: str,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id) as owner_session_id:
        payload = browser_session_runtime.close_session(session_id, owner_session_id=owner_session_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="browser_session_not_found")
        return {"session": payload}


@router.get("/operator/browser-computer-use-control")
async def browser_computer_use_control(
    http_request: Request,
    owner_session_id: str = Query(..., min_length=1),
):
    async with _browser_request_authority(http_request, owner_session_id) as bound_owner_session_id:
        provider_payload = await _browser_provider_payload()
        sessions = browser_session_runtime.list_sessions(owner_session_id=bound_owner_session_id)
        journal = browser_session_runtime.list_journal(owner_session_id=bound_owner_session_id)
        return {
            "owner_session_id": bound_owner_session_id,
            "providers": provider_payload["providers"],
            "sessions": sessions,
            "journal": journal,
            "blocked_claims": [
                "safe_browser_automation",
                "safe_autonomous_computer_use",
                "full_browser_parity",
                "arbitrary_credentialed_browsing_safety",
                "production_browser_automation_readiness",
            ],
        }


@router.post("/operator/browser-computer-use-control/actions")
async def browser_computer_use_control_action(
    request: BrowserComputerUseControlActionRequest,
    http_request: Request,
):
    return await control_browser_session(request.session_id, request, http_request)
