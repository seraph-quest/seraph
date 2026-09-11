"""Authenticated push-to-talk endpoints backed by the bounded audio worker."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.guardian.audio_ingress import AudioConsent, AudioConsentState
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
    capture_consent_expires_at: datetime
    model_consent_reference: str | None = Field(default=None, min_length=1, max_length=128)
    model_consent_expires_at: datetime | None = None
    message_id: str | None = Field(default=None, max_length=256)
    attachment_id: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    requested_capability: str = Field(default="chat", min_length=1, max_length=32)


class TranscriptConfirmationBody(BaseModel):
    transcript: str = Field(..., min_length=1, max_length=20_000)
    expected_transcript_digest: str | None = Field(default=None, min_length=64, max_length=64)
    transcript_digest: str | None = Field(default=None, min_length=64, max_length=64)


def _operator(request: Request) -> tuple[str, str, object]:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    owner = str(getattr(principal, "principal_id", "") or "").strip()
    if not operator or not owner:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    grants = getattr(principal, "grants", ())
    if AuthorityGrant.INGRESS not in grants:
        raise HTTPException(status_code=403, detail={"code": "audio_ingress_forbidden"})
    return owner, str(getattr(operator, "session_id", "") or "") or None, operator


def _decode_payload(encoded: str) -> bytes:
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_audio_base64"}) from exc
    if not payload:
        raise HTTPException(status_code=422, detail={"code": "empty_audio_payload"})
    return payload


def _consent(reference: str, expires_at: datetime, captured_at: datetime) -> AudioConsent:
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    captured_at = captured_at.astimezone(timezone.utc)
    return AudioConsent(
        reference=reference,
        state=AudioConsentState.ACTIVE,
        granted_at=captured_at - timedelta(seconds=1),
        expires_at=expires_at.astimezone(timezone.utc),
    )


def _error(exc: AudioWorkerError) -> HTTPException:
    status = 409 if exc.code in {"request_identity_conflict", "transcript_confirmation_stale", "canonical_message_identity_conflict"} else 422
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def _operator_payload(snapshot) -> dict:
    payload = snapshot.as_dict()
    if snapshot.transcript is not None:
        # Transcript text is returned only on this authenticated owner-bound
        # endpoint so the UI can edit it before the digest-bound confirmation.
        payload["transcript"]["text"] = snapshot.transcript
    return payload


async def _submit(body: AudioIngressBody, request: Request) -> dict:
    owner, operator_session_id, _ = _operator(request)
    captured_at = body.captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    captured_at = captured_at.astimezone(timezone.utc)
    if body.model_consent_reference and body.model_consent_expires_at:
        model_consent = _consent(body.model_consent_reference, body.model_consent_expires_at, captured_at)
    elif body.model_consent_reference or body.model_consent_expires_at:
        raise HTTPException(status_code=422, detail={"code": "model_consent_incomplete"})
    else:
        model_consent = None
    try:
        snapshot = await default_audio_worker.submit(
            AudioUploadRequest(
                session_id=body.session_id,
                owner_principal_id=owner,
                operator_session_id=operator_session_id,
                audio_bytes=_decode_payload(body.audio_base64),
                captured_at=captured_at,
                capture_consent=_consent(body.capture_consent_reference, body.capture_consent_expires_at, captured_at),
                model_consent=model_consent,
                message_id=body.message_id,
                attachment_id=body.attachment_id,
                request_id=body.request_id,
                requested_capability=body.requested_capability,
            ),
            process=True,
        )
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_audio_request"}) from exc
    return _operator_payload(snapshot)


@router.post("/audio/ptt")
@router.post("/audio/ingress")
async def ingest_audio(body: AudioIngressBody, request: Request) -> dict:
    """Capture one bounded upload; model egress is available only by consent."""
    return await _submit(body, request)


async def _owned_job(request_id: str, request: Request):
    owner, _, _ = _operator(request)
    try:
        snapshot = await default_audio_worker._snapshot_by_request(request_id)
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    if snapshot.owner_principal_id != owner:
        raise HTTPException(status_code=404, detail={"code": "audio_job_not_found"})
    return snapshot


@router.get("/audio/ptt/{request_id}")
@router.get("/audio/ingress/{request_id}")
async def get_audio(request_id: str, request: Request) -> dict:
    return _operator_payload(await _owned_job(request_id, request))


@router.post("/audio/ptt/{request_id}/process")
async def process_audio(request_id: str, request: Request) -> dict:
    snapshot = await _owned_job(request_id, request)
    try:
        result = await default_audio_worker.process(snapshot.request_id)
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    return _operator_payload(result)


@router.post("/audio/ptt/{request_id}/confirm")
async def confirm_audio(request_id: str, body: TranscriptConfirmationBody, request: Request) -> dict:
    await _owned_job(request_id, request)
    try:
        snapshot = await default_audio_worker.confirm_transcript(
            request_id,
            body.transcript,
            expected_transcript_digest=body.expected_transcript_digest,
            transcript_digest=body.transcript_digest,
        )
    except AudioConfirmationConflict as exc:
        raise _error(exc) from exc
    return _operator_payload(snapshot)


@router.post("/audio/ptt/{request_id}/cancel")
async def cancel_audio(request_id: str, request: Request) -> dict:
    await _owned_job(request_id, request)
    try:
        snapshot = await default_audio_worker.cancel(request_id)
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    return _operator_payload(snapshot)
