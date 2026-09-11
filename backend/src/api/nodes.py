"""Node and device adapter inventory API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import col, select

from config.settings import settings
from src.auth.service import AuthenticatedOperator
from src.db.engine import get_session
from src.db.models import PairedEdgeArtifact
from src.extensions.node_pairing import (
    PairingIngressStatus,
    apply_pairing_transition,
    ingest_pairing_request,
)
from src.extensions.node_adapters import NodeAdapterInventoryEntry, list_node_adapter_inventory
from src.extensions.paired_edge import (
    DEFAULT_NODE_EXTENSION_ID,
    DEFAULT_NODE_REFERENCE,
    artifact_metadata,
    authenticate_edge_request,
    content_hash,
    create_pairing,
    current_pairing,
    decode_content,
    pairing_entry_from_state,
    rotate_pairing,
    server_artifact_id,
    verify_pairing_credential,
)
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import (
    clear_node_adapter_pairing_entry,
    connector_enabled_overrides,
    ExtensionStateRevisionConflict,
    load_extension_state_payload,
    node_adapter_pairing_entry,
    revoke_node_adapter_pairing_entry,
    save_extension_state_payload,
    set_node_adapter_pairing_entry,
)

router = APIRouter()


class NodePairingMutationRequest(BaseModel):
    extension_id: str
    reference: str
    reason: str = Field(default="")
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    pairing_id: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=160)
    capability_scope: str = Field(default="media.ingest", min_length=1, max_length=128)
    data_purpose: str = Field(default="screen_capture", min_length=1, max_length=128)
    credential: str | None = Field(default=None, min_length=1, max_length=4096)
    expected_revision: int | None = Field(default=None, ge=0)


class EdgeIngressRequest(BaseModel):
    """Bound metadata and base64 bytes sent by a paired observation daemon."""

    extension_id: str = DEFAULT_NODE_EXTENSION_ID
    reference: str = DEFAULT_NODE_REFERENCE
    device_id: str = Field(min_length=1, max_length=128)
    pairing_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    captured_at: datetime
    content_hash: str
    media_type: str = "application/json"
    content_size: int = Field(ge=0)
    policy_version: str = "node-pairing-policy.v1"
    capability_scope: str = "media.ingest"
    data_purpose: str = "screen_capture"
    content_base64: str | None = None
    source_path: str | None = Field(default=None, max_length=4096)
    action_authority: bool = False
    app: str | None = Field(default=None, max_length=256)
    window_title: str | None = Field(default=None, max_length=2048)
    observation: dict[str, Any] | None = None
    spool_count: int = Field(default=0, ge=0)
    spool_bytes: int = Field(default=0, ge=0)
    spool_oldest_at: datetime | None = None
    recovery_state: str | None = Field(default=None, max_length=64)
    degraded_state: str | None = Field(default=None, max_length=160)


class EdgeHeartbeatRequest(EdgeIngressRequest):
    """Heartbeat uses the same authenticated monotonic envelope as upload."""

    content_base64: str | None = None
    media_type: str = "application/json"


def _operator_principal_id(request: Request) -> str:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    principal_id = getattr(principal, "principal_id", None)
    principal_type = getattr(getattr(principal, "principal_type", None), "value", getattr(principal, "principal_type", None))
    session_id = str(getattr(operator, "session_id", "") or "")
    principal_session_id = str(getattr(principal, "operator_session_id", "") or "")
    if (
        not isinstance(operator, AuthenticatedOperator)
        or not principal_id
        or principal_type != "operator"
        or not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or not session_id
        or principal_session_id != session_id
    ):
        raise HTTPException(status_code=401, detail={"code": "authenticated_operator_required"})
    return str(principal_id)


def _require_pairing_owner(
    state_payload: dict[str, Any],
    adapter: NodeAdapterInventoryEntry,
    owner_principal_id: str,
) -> None:
    entry, _ = current_pairing(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
    )
    stored_owner = str(entry.get("owner_principal_id") or "").strip()
    if not stored_owner or stored_owner != owner_principal_id:
        # Do not disclose whether another operator owns this pairing.
        raise HTTPException(status_code=404, detail={"code": "node_pairing_not_found"})


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
async def list_node_pairings(request: Request):
    _operator_principal_id(request)
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


@router.post("/nodes/pairings/pair")
async def pair_node(request: NodePairingMutationRequest, http_request: Request):
    """Create a pairing and return the raw credential exactly once."""

    owner_principal_id = _operator_principal_id(http_request)
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    device_id = request.device_id or f"device-{uuid4().hex[:16]}"
    pairing_id = request.pairing_id or f"pairing-{uuid4().hex[:16]}"
    expected_revision = (
        request.expected_revision
        if request.expected_revision is not None
        else int(state_payload.get("revision") or 0)
    )
    try:
        entry, credential, revision = await create_pairing(
            state_payload,
            extension_id=adapter.extension_id,
            reference=adapter.reference,
            name=adapter.name,
            device_id=device_id,
            pairing_id=pairing_id,
            owner_principal_id=owner_principal_id,
            label=request.label,
            capability_scope=request.capability_scope,
            data_purpose=request.data_purpose,
            expected_revision=expected_revision,
        )
    except ExtensionStateRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "expected_revision": exc.expected, "actual_revision": exc.actual},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": str(exc)}) from exc
    updated = _find_adapter(
        _node_inventory(state_payload),
        extension_id=adapter.extension_id,
        reference=adapter.reference,
    )
    return {
        "adapter": _adapter_payload(updated),
        "credential": credential,
        "credential_returned": True,
        "credential_ref": entry.get("credential_ref"),
        "revision": revision,
        "action_authority": False,
    }


@router.post("/nodes/pairings/rotate")
async def rotate_node_pairing(request: NodePairingMutationRequest, http_request: Request):
    """Rotate a credential under the persisted state revision fence."""

    owner_principal_id = _operator_principal_id(http_request)
    if not request.credential:
        raise HTTPException(status_code=400, detail={"code": "credential_required"})
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    _require_pairing_owner(state_payload, adapter, owner_principal_id)
    expected_revision = (
        request.expected_revision
        if request.expected_revision is not None
        else int(state_payload.get("revision") or 0)
    )
    try:
        entry, credential, revision = await rotate_pairing(
            state_payload,
            extension_id=adapter.extension_id,
            reference=adapter.reference,
            name=adapter.name,
            current_credential=request.credential,
            expected_revision=expected_revision,
        )
    except ExtensionStateRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "expected_revision": exc.expected, "actual_revision": exc.actual},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401 if str(exc) == "current_credential_invalid" else 400, detail={"code": str(exc)}) from exc
    updated = _find_adapter(
        _node_inventory(state_payload),
        extension_id=adapter.extension_id,
        reference=adapter.reference,
    )
    return {
        "adapter": _adapter_payload(updated),
        "credential": credential,
        "credential_returned": True,
        "credential_ref": entry.get("credential_ref"),
        "revision": revision,
        "action_authority": False,
    }


@router.post("/nodes/pairings/reconnect")
async def reconnect_node_pairing(request: NodePairingMutationRequest, http_request: Request):
    """Verify a pairing credential and return its current operator-visible state."""

    owner_principal_id = _operator_principal_id(http_request)
    if not request.credential:
        raise HTTPException(status_code=400, detail={"code": "credential_required"})
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    _require_pairing_owner(state_payload, adapter, owner_principal_id)
    try:
        entry, state = await verify_pairing_credential(
            state_payload,
            extension_id=adapter.extension_id,
            reference=adapter.reference,
            name=adapter.name,
            presented_credential=request.credential,
        )
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(
            status_code=503 if code == "credential_unavailable" else 401,
            detail={"code": code},
        ) from exc
    if state.lifecycle.value != "paired":
        raise HTTPException(status_code=403, detail={"code": f"pairing_{state.lifecycle.value}"})
    return {
        "reconnected": True,
        "adapter": _adapter_payload(adapter),
        "revision": int(state_payload.get("revision") or 0),
        "action_authority": False,
    }


@router.post("/nodes/pairings/expire")
async def expire_node_pairing(request: NodePairingMutationRequest, http_request: Request):
    """Expire a pairing through the same deterministic lifecycle contract."""

    owner_principal_id = _operator_principal_id(http_request)
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    _require_pairing_owner(state_payload, adapter, owner_principal_id)
    entry, state = current_pairing(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
    )
    transition = apply_pairing_transition(state, "expire")
    if not transition.accepted:
        raise HTTPException(status_code=409, detail={"code": transition.reason_code})
    updated_entry = pairing_entry_from_state(
        transition.state,
        base_entry=entry,
        credential_ref=str(entry.get("credential_ref") or "") or None,
        credential_scope=str(entry.get("credential_scope") or "") or None,
        owner_principal_id=str(entry.get("owner_principal_id") or "") or None,
    )
    set_node_adapter_pairing_entry(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
        pairing=updated_entry,
    )
    try:
        revision = save_extension_state_payload(
            state_payload,
            expected_revision=request.expected_revision
            if request.expected_revision is not None
            else int(state_payload.get("revision") or 0),
        )
    except ExtensionStateRevisionConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict", "actual_revision": exc.actual}) from exc
    updated = _find_adapter(_node_inventory(state_payload), extension_id=adapter.extension_id, reference=adapter.reference)
    return {"adapter": _adapter_payload(updated), "revision": revision, "action_authority": False}


@router.post("/nodes/pairings/revoke")
async def revoke_node_pairing(request: NodePairingMutationRequest, http_request: Request):
    owner_principal_id = _operator_principal_id(http_request)
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    _require_pairing_owner(state_payload, adapter, owner_principal_id)
    revoke_node_adapter_pairing_entry(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
        reason=request.reason.strip(),
        revoked_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        revision = save_extension_state_payload(
            state_payload,
            expected_revision=request.expected_revision
            if request.expected_revision is not None
            else int(state_payload.get("revision") or 0),
        )
    except ExtensionStateRevisionConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict", "actual_revision": exc.actual}) from exc
    updated = _find_adapter(
        _node_inventory(state_payload),
        extension_id=adapter.extension_id,
        reference=adapter.reference,
    )
    return {"adapter": _adapter_payload(updated), "revision": revision, "action_authority": False}


@router.post("/nodes/pairings/clear")
async def clear_node_pairing(request: NodePairingMutationRequest, http_request: Request):
    owner_principal_id = _operator_principal_id(http_request)
    state_payload = load_extension_state_payload()
    adapter = _find_adapter(
        _node_inventory(state_payload),
        extension_id=request.extension_id,
        reference=request.reference,
    )
    _require_pairing_owner(state_payload, adapter, owner_principal_id)
    removed = clear_node_adapter_pairing_entry(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
    )
    revision = save_extension_state_payload(
        state_payload,
        expected_revision=request.expected_revision
        if request.expected_revision is not None
        else int(state_payload.get("revision") or 0),
    )
    updated = _find_adapter(
        _node_inventory(state_payload),
        extension_id=adapter.extension_id,
        reference=adapter.reference,
    )
    return {"cleared": removed, "adapter": _adapter_payload(updated), "revision": revision}


def _edge_credential(request: Request) -> str:
    """Read a bearer credential without accepting a fingerprint as auth."""

    header = request.headers.get("authorization", "").strip()
    if header.lower().startswith("bearer "):
        value = header[7:].strip()
    else:
        value = request.headers.get("x-seraph-node-credential", "").strip()
    if not value:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    return value


def _edge_http_status(status: PairingIngressStatus) -> int:
    return {
        PairingIngressStatus.ACCEPTED: 201,
        PairingIngressStatus.DUPLICATE: 200,
        PairingIngressStatus.OUT_OF_ORDER: 409,
        PairingIngressStatus.EXPIRED: 410,
        PairingIngressStatus.REVOKED: 410,
        PairingIngressStatus.OVERSIZED: 413,
        PairingIngressStatus.BLOCKED: 403,
        PairingIngressStatus.RETRYABLE: 503,
    }[status]


def _edge_result(
    *,
    status: PairingIngressStatus,
    reason_code: str,
    request: EdgeIngressRequest,
    last_sequence: int | None = None,
    artifact: dict[str, Any] | None = None,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "schema": "seraph.paired_edge.ingress.v1",
        "status": status.value,
        "accepted": status is PairingIngressStatus.ACCEPTED,
        "retryable": status is PairingIngressStatus.RETRYABLE,
        "terminal": status is not PairingIngressStatus.RETRYABLE,
        "reason_code": reason_code,
        "device_id": request.device_id,
        "pairing_id": request.pairing_id,
        "request_id": request.request_id,
        "sequence": request.sequence,
        "last_sequence": last_sequence,
        "content_hash": request.content_hash,
        "content_size": request.content_size,
        "spool_count": request.spool_count,
        "spool_bytes": request.spool_bytes,
        "spool_oldest_at": request.spool_oldest_at.isoformat() if request.spool_oldest_at else None,
        "recovery_state": request.recovery_state,
        "degraded_state": request.degraded_state,
        "artifact": artifact,
        "action_authority": False,
    }
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = retry_after_seconds
    return JSONResponse(payload, status_code=_edge_http_status(status))


def _edge_observation_payload(body: EdgeIngressRequest, artifact: PairedEdgeArtifact) -> dict[str, Any]:
    """Build screen-observation metadata with a server-owned artifact handle."""

    raw = body.observation if isinstance(body.observation, dict) else {}
    details = raw.get("details")
    details_list = [item for item in details if isinstance(item, str)][:32] if isinstance(details, list) else []
    capture = {
        "provider": "paired_edge",
        "artifact_id": artifact.artifact_id,
        "readback_id": artifact.artifact_id,
        "content_hash": artifact.content_hash,
        "size_bytes": artifact.content_size,
        "server_owned": True,
        "source_path": None,
    }
    return {
        "app": body.app or str(raw.get("app") or ""),
        "window_title": body.window_title or str(raw.get("window_title") or ""),
        "activity": str(raw.get("activity") or "screen_capture"),
        "project": str(raw.get("project")) if raw.get("project") is not None else None,
        "summary": str(raw.get("summary")) if raw.get("summary") is not None else None,
        "details": details_list + ["capture_artifacts:" + json.dumps(capture, sort_keys=True, separators=(",", ":"))],
        "blocked": False,
        "capture_artifacts": capture,
    }


async def _edge_ingest(body: EdgeIngressRequest, request: Request, *, heartbeat: bool = False) -> Response:
    """Authenticate, validate, and durably accept one edge request."""

    presented_credential = _edge_credential(request)
    state_payload = load_extension_state_payload()
    try:
        adapter = _find_adapter(
            _node_inventory(state_payload),
            extension_id=body.extension_id,
            reference=body.reference,
        )
    except HTTPException:
        raise

    try:
        authenticated = await authenticate_edge_request(
            state_payload,
            extension_id=adapter.extension_id,
            reference=adapter.reference,
            name=adapter.name,
            device_id=body.device_id,
            pairing_id=body.pairing_id,
            request_id=body.request_id,
            sequence=body.sequence,
            captured_at=body.captured_at,
            content_hash=body.content_hash,
            media_type=body.media_type,
            content_size=body.content_size,
            capability_scope=body.capability_scope,
            data_purpose=body.data_purpose,
            policy_version=body.policy_version,
            presented_credential=presented_credential,
            source_path=body.source_path,
            action_authority=body.action_authority,
        )
    except ValueError as exc:
        code = str(exc)
        http_status = 503 if code in {"credential_unavailable", "credential_not_configured"} else 401
        raise HTTPException(status_code=http_status, detail={"code": code}) from exc

    # Authenticate before parsing or validating bytes so malformed requests
    # cannot use this endpoint as an unauthenticated content oracle.
    try:
        content = decode_content(body.content_base64)
    except ValueError as exc:
        return _edge_result(
            status=PairingIngressStatus.BLOCKED,
            reason_code=str(exc),
            request=body,
        )

    if heartbeat and content:
        return _edge_result(
            status=PairingIngressStatus.BLOCKED,
            reason_code="heartbeat_content_forbidden",
            request=body,
        )
    actual_hash = content_hash(content)
    if body.content_size != len(content):
        return _edge_result(
            status=PairingIngressStatus.BLOCKED,
            reason_code="content_size_mismatch",
            request=body,
        )
    if body.content_hash.strip().lower().removeprefix("sha256:") != actual_hash.removeprefix("sha256:"):
        return _edge_result(
            status=PairingIngressStatus.BLOCKED,
            reason_code="content_hash_mismatch",
            request=body,
        )

    outcome = ingest_pairing_request(
        authenticated.state,
        authenticated.request,
        authenticated.policy,
        now=datetime.now(timezone.utc),
    )
    if not outcome.result.accepted:
        existing_artifact = None
        if outcome.result.status is PairingIngressStatus.DUPLICATE:
            async with get_session() as db:
                existing_result = await db.execute(
                    select(PairedEdgeArtifact)
                    .where(col(PairedEdgeArtifact.extension_id) == body.extension_id)
                    .where(col(PairedEdgeArtifact.reference) == body.reference)
                    .where(col(PairedEdgeArtifact.pairing_id) == body.pairing_id)
                    .where(col(PairedEdgeArtifact.request_id) == body.request_id)
                )
                existing = existing_result.scalar_one_or_none()
                if existing is not None:
                    existing_artifact = artifact_metadata(existing)
        return _edge_result(
            status=outcome.result.status,
            reason_code=outcome.result.reason_code,
            request=body,
            last_sequence=outcome.result.last_sequence,
            artifact=existing_artifact,
            retry_after_seconds=2 if outcome.result.retryable else None,
        )

    artifact: PairedEdgeArtifact | None = None
    if not heartbeat:
        artifact = PairedEdgeArtifact(
            artifact_id=server_artifact_id(),
            extension_id=body.extension_id,
            reference=body.reference,
            device_id=body.device_id,
            pairing_id=body.pairing_id,
            request_id=body.request_id,
            owner_principal_id=authenticated.owner_principal_id,
            sequence=body.sequence,
            captured_at=body.captured_at,
            content_hash=actual_hash,
            media_type=body.media_type,
            content_size=len(content),
            content=content,
            app_name=body.app or str((body.observation or {}).get("app") or ""),
            window_title=body.window_title or str((body.observation or {}).get("window_title") or ""),
            observation_json=json.dumps(body.observation, sort_keys=True) if isinstance(body.observation, dict) else None,
        )
        try:
            async with get_session() as db:
                db.add(artifact)
                await db.flush()
        except Exception:
            # A duplicate request may race this process after the pure replay
            # check. Readback by request ID turns that race into idempotent
            # duplicate behavior for the daemon.
            async with get_session() as db:
                existing_result = await db.execute(
                    select(PairedEdgeArtifact)
                    .where(col(PairedEdgeArtifact.extension_id) == body.extension_id)
                    .where(col(PairedEdgeArtifact.reference) == body.reference)
                    .where(col(PairedEdgeArtifact.pairing_id) == body.pairing_id)
                    .where(col(PairedEdgeArtifact.request_id) == body.request_id)
                )
                existing = existing_result.scalar_one_or_none()
            if existing is not None:
                return _edge_result(
                    status=PairingIngressStatus.DUPLICATE,
                    reason_code="request_already_accepted",
                    request=body,
                    last_sequence=authenticated.state.last_sequence,
                    artifact=artifact_metadata(existing),
                )
            return _edge_result(
                status=PairingIngressStatus.RETRYABLE,
                reason_code="artifact_persistence_failed",
                request=body,
                last_sequence=authenticated.state.last_sequence,
                retry_after_seconds=2,
            )

    updated_entry = pairing_entry_from_state(
        outcome.state,
        base_entry=authenticated.entry,
        credential_ref=str(authenticated.entry.get("credential_ref") or "") or None,
        credential_scope=str(authenticated.entry.get("credential_scope") or "") or None,
        owner_principal_id=authenticated.owner_principal_id,
        policy=authenticated.policy,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    updated_entry.update(
        {
            "last_seen_at": now_iso,
            "last_ingest_at": now_iso,
            "last_transport_status": "accepted",
            "last_request_id": body.request_id,
            "last_capture_at": body.captured_at.isoformat(),
            "action_authority": False,
            "spool_count": body.spool_count,
            "spool_bytes": body.spool_bytes,
            "spool_oldest_at": body.spool_oldest_at.isoformat() if body.spool_oldest_at else None,
            "recovery_state": body.recovery_state,
            "degraded_state": body.degraded_state,
        }
    )
    set_node_adapter_pairing_entry(
        state_payload,
        extension_id=adapter.extension_id,
        reference=adapter.reference,
        name=adapter.name,
        pairing=updated_entry,
    )
    try:
        save_extension_state_payload(state_payload, expected_revision=authenticated.payload_revision)
    except ExtensionStateRevisionConflict:
        # The artifact is durable and request-id deduped. The next daemon retry
        # receives a duplicate; the operator can inspect the conflict state.
        return _edge_result(
            status=PairingIngressStatus.RETRYABLE,
            reason_code="pairing_state_revision_conflict",
            request=body,
            last_sequence=authenticated.state.last_sequence,
            artifact=artifact_metadata(artifact) if artifact is not None else None,
            retry_after_seconds=2,
        )

    if artifact is not None:
        try:
            observation = _edge_observation_payload(body, artifact)
            from src.observer.screen_repository import screen_observation_repo

            await screen_observation_repo.create(
                app_name=observation["app"],
                window_title=observation["window_title"],
                activity_type=observation["activity"],
                project=observation["project"],
                summary=observation["summary"],
                details=observation["details"],
                blocked=False,
                timestamp=body.captured_at,
            )
        except Exception:
            # Artifact acceptance remains durable even if the optional screen
            # projection is unavailable; readback still exposes the bytes.
            pass

    return _edge_result(
        status=PairingIngressStatus.ACCEPTED,
        reason_code="heartbeat_accepted" if heartbeat else "artifact_accepted",
        request=body,
        last_sequence=outcome.state.last_sequence,
        artifact=artifact_metadata(artifact) if artifact is not None else None,
    )


@router.post("/nodes/edge/heartbeat")
async def edge_heartbeat(body: EdgeHeartbeatRequest, request: Request):
    return await _edge_ingest(body, request, heartbeat=True)


@router.post("/nodes/edge/upload")
async def edge_upload(body: EdgeIngressRequest, request: Request):
    return await _edge_ingest(body, request, heartbeat=False)


@router.post("/nodes/edge/ingest")
async def edge_ingest(body: EdgeIngressRequest, request: Request):
    return await _edge_ingest(body, request, heartbeat=False)


async def _edge_artifact(artifact_id: str, *, owner_principal_id: str) -> PairedEdgeArtifact:
    async with get_session() as db:
        result = await db.execute(
            select(PairedEdgeArtifact)
            .where(col(PairedEdgeArtifact.artifact_id) == artifact_id)
            .where(col(PairedEdgeArtifact.owner_principal_id) == owner_principal_id)
        )
        artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="paired edge artifact not found")
    return artifact


@router.get("/nodes/edge/artifacts/{artifact_id}")
async def get_edge_artifact(artifact_id: str, request: Request):
    owner_principal_id = _operator_principal_id(request)
    artifact = await _edge_artifact(artifact_id, owner_principal_id=owner_principal_id)
    return artifact_metadata(artifact)


@router.get("/nodes/edge/artifacts/{artifact_id}/content")
async def get_edge_artifact_content(artifact_id: str, request: Request):
    owner_principal_id = _operator_principal_id(request)
    artifact = await _edge_artifact(artifact_id, owner_principal_id=owner_principal_id)
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={
            "X-Seraph-Artifact-Id": artifact.artifact_id,
            "X-Seraph-Content-Hash": artifact.content_hash,
        },
    )
