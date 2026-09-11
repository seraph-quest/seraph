"""Authenticated push-to-talk endpoints backed by the bounded audio worker."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Literal
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.guardian.audio_worker import (
    AudioConfirmationConflict,
    AudioUploadRequest,
    AudioWorkerError,
    default_audio_worker,
)
from src.security.trust_contract import AuthorityGrant


router = APIRouter()


class AudioIngressBody(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=256)
    audio_base64: str = Field(..., min_length=8, max_length=14_000_000)
    captured_at: datetime | None = None
    capture_consent_reference: str = Field(..., min_length=1, max_length=128)
    # Legacy clients may still send these fields, but the server deliberately
    # ignores them.  Only the durable grant registry can authorize a boundary.
    capture_consent_expires_at: datetime | None = None
    model_consent_reference: str | None = Field(default=None, min_length=1, max_length=128)
    model_consent_expires_at: datetime | None = None
    message_id: str | None = Field(default=None, max_length=256)
    attachment_id: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    requested_capability: str = Field(default="chat", min_length=1, max_length=32)


class TranscriptConfirmationBody(BaseModel):
    transcript: str = Field(..., min_length=1, max_length=20_000)
    expected_transcript_digest: str = Field(..., min_length=64, max_length=64)
    transcript_digest: str | None = Field(default=None, min_length=64, max_length=64)


class AudioConsentGrantBody(BaseModel):
    boundary: Literal["capture", "cloud_upload", "model"]


def _operator(request: Request) -> tuple[str, str, object]:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    owner = str(getattr(principal, "principal_id", "") or "").strip()
    operator_session_id = str(getattr(operator, "session_id", "") or "").strip()
    if (
        not operator
        or not principal
        or not getattr(principal, "authenticated", False)
        or getattr(principal, "revoked", False)
        or not owner
        or not operator_session_id
    ):
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    grants = getattr(principal, "grants", ())
    normalized_grants = {
        str(getattr(grant, "value", grant))
        for grant in grants
    }
    if AuthorityGrant.INGRESS.value not in normalized_grants:
        raise HTTPException(status_code=403, detail={"code": "audio_ingress_forbidden"})
    return owner, operator_session_id, operator


def _has_model_inference_grant(operator: object) -> bool:
    principal = getattr(operator, "principal", None)
    return AuthorityGrant.MODEL_INFERENCE.value in {
        str(getattr(grant, "value", grant))
        for grant in getattr(principal, "grants", ())
    }


def _decode_payload(encoded: str) -> bytes:
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_audio_base64"}) from exc
    if not payload:
        raise HTTPException(status_code=422, detail={"code": "empty_audio_payload"})
    return payload


def _error(exc: AudioWorkerError) -> HTTPException:
    status = 409 if exc.code in {
        "request_identity_conflict",
        "transcript_confirmation_stale",
        "canonical_message_identity_conflict",
        "canonical_attachment_identity_conflict",
        "audio_operator_session_mismatch",
        "confirmation_in_progress",
        "transcript_confirmation_digest_required",
        "model_inference_grant_revoked",
    } else 422
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def _operator_payload(snapshot, *, owner_principal_id: str, operator_session_id: str) -> dict:
    payload = snapshot.as_dict()
    review_text = default_audio_worker.review_transcript(
        snapshot.request_id,
        owner_principal_id=owner_principal_id,
        operator_session_id=operator_session_id,
    )
    if review_text is not None and snapshot.status == "transcript_ready":
        payload["transcript"] = {
            "text": review_text,
            "digest": snapshot.transcript_digest,
            "confirmed_digest": snapshot.confirmed_transcript_digest,
            "review_only": True,
        }
    return payload


async def _submit(body: AudioIngressBody, request: Request) -> dict:
    owner, operator_session_id, _ = _operator(request)
    captured_at = body.captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    captured_at = captured_at.astimezone(timezone.utc)
    model_consent = (
        SimpleNamespace(reference=body.model_consent_reference)
        if body.model_consent_reference
        else None
    )
    capture_consent = SimpleNamespace(reference=body.capture_consent_reference)
    try:
        snapshot = await default_audio_worker.submit(
            AudioUploadRequest(
                session_id=body.session_id,
                owner_principal_id=owner,
                operator_session_id=operator_session_id,
                audio_bytes=_decode_payload(body.audio_base64),
                captured_at=captured_at,
                capture_consent=capture_consent,
                model_consent=model_consent,
                message_id=body.message_id,
                attachment_id=body.attachment_id,
                request_id=body.request_id,
                requested_capability=body.requested_capability,
                model_inference_granted=_has_model_inference_grant(operator),
            ),
            process=True,
            authority_principal=getattr(operator, "principal", None),
        )
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_audio_request"}) from exc
    return _operator_payload(
        snapshot,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )

@router.post("/audio/ptt")
@router.post("/audio/ingress")
async def ingest_audio(body: AudioIngressBody, request: Request) -> dict:
    """Capture one bounded upload; model egress is available only by consent."""
    return await _submit(body, request)


@router.post("/audio/ptt/consent")
async def issue_audio_consent(body: AudioConsentGrantBody, request: Request) -> dict:
    owner, operator_session_id, operator = _operator(request)
    if body.boundary in {"cloud_upload", "model"} and not _has_model_inference_grant(operator):
        raise HTTPException(status_code=403, detail={"code": "audio_model_inference_forbidden"})
    try:
        grant = await default_audio_worker.issue_consent_grant(
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
            boundary=body.boundary,
            authority_principal=getattr(operator, "principal", None),
        )
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    return {
        "reference": grant.reference,
        "boundary": "cloud_upload" if body.boundary == "model" else body.boundary,
        "granted_at": grant.granted_at.isoformat(),
        "expires_at": grant.expires_at.isoformat(),
        "state": grant.state.value,
    }


@router.get("/audio/ptt/consent/{reference}")
async def read_audio_consent(reference: str, request: Request) -> dict:
    owner, operator_session_id, _ = _operator(request)
    try:
        grant = await default_audio_worker.read_consent_grant(
            reference,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
        )
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return {
        "reference": grant.reference,
        "state": grant.state.value,
        "granted_at": grant.granted_at.isoformat(),
        "expires_at": grant.expires_at.isoformat(),
    }


@router.post("/audio/ptt/consent/{reference}/revoke")
async def revoke_audio_consent(reference: str, request: Request) -> dict:
    owner, operator_session_id, _ = _operator(request)
    try:
        await default_audio_worker.revoke_consent_grant(
            reference,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
        )
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return {"reference": reference, "state": "revoked"}


async def _owned_job(request_id: str, request: Request, *, require_model: bool = False):
    owner, operator_session_id, _ = _operator(request)
    operator = getattr(request.state, "operator", None)
    if require_model and not _has_model_inference_grant(operator):
        raise HTTPException(status_code=403, detail={"code": "audio_model_inference_forbidden"})
    try:
        snapshot = await default_audio_worker._snapshot_by_request(request_id)
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    if snapshot.owner_principal_id != owner or snapshot.operator_session_id != operator_session_id:
        raise HTTPException(status_code=404, detail={"code": "audio_job_not_found"})
    return snapshot, owner, operator_session_id, operator


@router.get("/audio/ptt/{request_id}")
@router.get("/audio/ingress/{request_id}")
async def get_audio(request_id: str, request: Request) -> dict:
    snapshot, owner, operator_session_id, _ = await _owned_job(request_id, request)
    return _operator_payload(
        snapshot,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )


@router.post("/audio/ptt/{request_id}/process")
async def process_audio(request_id: str, request: Request) -> dict:
    snapshot, owner, operator_session_id, operator = await _owned_job(request_id, request, require_model=True)
    try:
        result = await default_audio_worker.process(
            snapshot.request_id,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
            authority_principal=getattr(operator, "principal", None),
        )
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    return _operator_payload(
        result,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )


@router.post("/audio/ptt/{request_id}/confirm")
async def confirm_audio(request_id: str, body: TranscriptConfirmationBody, request: Request) -> dict:
    _, owner, operator_session_id, operator = await _owned_job(request_id, request)
    try:
        snapshot = await default_audio_worker.confirm_transcript(
            request_id,
            body.transcript,
            expected_transcript_digest=body.expected_transcript_digest,
            transcript_digest=body.transcript_digest,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
        )
    except AudioConfirmationConflict as exc:
        raise _error(exc) from exc
    return _operator_payload(
        snapshot,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )


@router.post("/audio/ptt/{request_id}/cancel")
async def cancel_audio(request_id: str, request: Request) -> dict:
    _, owner, operator_session_id, operator = await _owned_job(request_id, request)
    try:
        snapshot = await default_audio_worker.cancel(
            request_id,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
        )
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    return _operator_payload(
        snapshot,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )
