"""Authenticated push-to-talk endpoints backed by the bounded audio worker."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Literal
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict

from src.guardian.audio_worker import (
    AudioConfirmationConflict,
    AudioUploadRequest,
    AudioWorkerError,
    default_audio_worker,
)
from src.security.trust_contract import AuthorityGrant
from src.work_board.contracts import GeneralTaskCreate


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
    model_config = ConfigDict(extra="forbid")
    boundary: Literal["capture", "cloud_upload", "model"]
    original_audio_selection: "OriginalAudioSelectionV1 | None" = None
    pre_capture_model_consent_reference: str | None = Field(default=None, min_length=1, max_length=128)
    expected_pre_capture_selection_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class OriginalAudioSelectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["select_one_original_audio_call"]
    conversation_session_id: str = Field(min_length=1, max_length=256)
    audio_budget_microusd: int = Field(ge=1, le=1_000_000_000)
    max_calls: int = Field(strict=True, ge=1, le=1)
    expected_audio_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    documentation_attestation_ref: str = Field(min_length=1, max_length=128)
    expected_documentation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AudioExecutionReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_calls: int = Field(strict=True, ge=1, le=1)
    expected_audio_revision: int = Field(ge=0)


class AudioExecutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_budget_microusd: int = Field(strict=True, ge=1, le=1_000_000_000)
    max_calls: int = Field(default=1, strict=True, ge=1, le=1)


class AudioTaskCaptureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_transcript_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    task: GeneralTaskCreate


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
        "audio_operator_session_invalid",
        "audio_operator_authority_required",
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
    owner, operator_session_id, operator = _operator(request)
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
            # Return the durable request identity before model processing so a
            # browser can poll, cancel, or retry an admitted job while the
            # bounded worker runs independently.
            process=False,
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
    if body.boundary in {"cloud_upload", "model"} and body.original_audio_selection is None:
        raise HTTPException(status_code=422, detail={"code": "audio_original_selection_required"})
    if body.original_audio_selection is not None:
        from src.model_fabric.repository import model_fabric_repository
        from src.model_fabric.audio_documentation import DocumentationError
        if body.boundary not in {"model", "cloud_upload"}:
            raise HTTPException(status_code=422, detail={"code":"audio_selection_boundary_invalid"})
        try:
            await model_fabric_repository.require_audio_execution_documentation(operator,
                body.original_audio_selection.documentation_attestation_ref,
                body.original_audio_selection.expected_documentation_digest)
        except DocumentationError as exc:
            # Missing exact facts cannot issue a model grant or preselection.
            raise HTTPException(status_code=exc.status, detail={"code":exc.code}) from None
    if body.pre_capture_model_consent_reference is not None or body.expected_pre_capture_selection_digest is not None:
        raise HTTPException(status_code=409, detail={"code":"audio_pre_capture_selection_unavailable"})
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
        revoked = await default_audio_worker.revoke_consent_grant(
            reference,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
        )
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    if revoked is not True:
        raise HTTPException(
            status_code=409,
            detail={"code": "audio_consent_revocation_unconfirmed"},
        )
    return {"reference": reference, "state": "revoked"}


async def _owned_job(request_id: str, request: Request, *, require_model: bool = False):
    owner, operator_session_id, _ = _operator(request)
    operator = getattr(request.state, "operator", None)
    if require_model and not _has_model_inference_grant(operator):
        raise HTTPException(status_code=403, detail={"code": "audio_model_inference_forbidden"})

    # Read the durable identity row before invoking the worker snapshot path.
    # ``_snapshot_by_request`` applies expiry/cleanup, so an owner mismatch must
    # be rejected while the lookup is still read-only.
    try:
        row = await default_audio_worker._job(request_id)
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "audio_job_not_found"})
    if row.owner_principal_id != owner or row.operator_session_id != operator_session_id:
        raise HTTPException(status_code=404, detail={"code": "audio_job_not_found"})

    try:
        snapshot = await default_audio_worker._snapshot_by_request(
            request_id,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
        )
    except AudioWorkerError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return snapshot, owner, operator_session_id, operator


@router.get("/audio/ptt/{request_id}")
@router.get("/audio/ingress/{request_id}")
async def get_audio(request_id: str, request: Request) -> dict:
    snapshot, owner, operator_session_id, operator = await _owned_job(request_id, request)
    payload = _operator_payload(
        snapshot,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )
    if snapshot.status == "confirmed" and snapshot.workflow_job_id:
        from src.api.work_board import _owner
        from src.db.engine import get_session
        from src.workflows.job_runtime import durable_job_repository as jobs, DurableJobError
        source_owner = _owner(operator)
        try:
            async with jobs.confirmed_audio_task_source_scope(owner=source_owner,
                request_id=request_id, message_id=snapshot.message_id,
                confirmed_digest=snapshot.confirmed_transcript_digest):
                async with get_session() as db:
                    _, message = await jobs.check_confirmed_audio_task_source(db, owner=source_owner,
                        request_id=request_id, message_id=snapshot.message_id,
                        confirmed_digest=snapshot.confirmed_transcript_digest)
                    payload["transcript"] = {"text": message.content,
                        "digest": snapshot.confirmed_transcript_digest,
                        "confirmed_digest": snapshot.confirmed_transcript_digest,
                        "review_only": True}
        except DurableJobError:
            raise HTTPException(status_code=409, detail={"code": "audio_confirmed_source_changed"})
    return payload


@router.post("/audio/ptt/{request_id}/process")
async def process_audio(request_id: str, request: Request, body: AudioExecutionBody | None = None) -> dict:
    snapshot, owner, operator_session_id, operator = await _owned_job(request_id, request, require_model=True)
    try:
        result = await default_audio_worker.process(
            snapshot.request_id,
            owner_principal_id=owner,
            operator_session_id=operator_session_id,
            authority_principal=getattr(operator, "principal", None),
            audio_budget_microusd=body.audio_budget_microusd if body else None,
        )
    except AudioWorkerError as exc:
        raise _error(exc) from exc
    return _operator_payload(
        result,
        owner_principal_id=owner,
        operator_session_id=operator_session_id,
    )


@router.post("/audio/ptt/{request_id}/execution-review")
async def review_original_audio_execution(request_id: str, body: AudioExecutionReviewBody, request: Request):
    snapshot, _, _, operator = await _owned_job(request_id, request, require_model=True)
    row = await default_audio_worker._job(snapshot.request_id)
    if row.revision != body.expected_audio_revision:
        raise HTTPException(status_code=409, detail={"code":"audio_revision_changed"})
    from src.model_fabric.repository import model_fabric_repository
    from src.model_fabric.audio_documentation import DocumentationError
    try:
        await model_fabric_repository.require_audio_execution_documentation(operator, None, None)
    except DocumentationError as exc:
        raise HTTPException(status_code=exc.status, detail={"code":exc.code}) from None


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


@router.post("/audio/ptt/{request_id}/task")
async def capture_audio_task(request_id: str, body: AudioTaskCaptureBody, request: Request) -> dict:
    """Explicit fresh Goal/C1 allowance after exact corrected confirmation."""
    from src.api.work_board import dispatcher, _safe_task_payload, _owner, _raise_board_error
    from src.db.engine import get_session
    from src.work_board.channel_capture import reserve_confirmed_audio_capture
    from src.work_board.repository import BoardError
    from src.workflows.job_runtime import durable_job_repository, DurableJobError
    snapshot, _owner_id, _root_id, operator = await _owned_job(request_id, request)
    if snapshot.status != "confirmed":
        raise HTTPException(status_code=409, detail={"code": "audio_confirmed_native_source_required"})
    service = dispatcher.general_tasks
    if service is None:
        raise HTTPException(status_code=503, detail={"code": "general_task_inactive"})
    if body.task.input.limits.max_cost_microusd <= 0 or body.task.input.limits.max_inference_calls <= 0:
        raise HTTPException(status_code=422, detail={"code": "channel_capture_positive_task_allowance_required"})
    try:
        async with get_session() as db:
            owner = _owner(operator)
            capture = await reserve_confirmed_audio_capture(db, owner, body.task, service=service,
                jobs=durable_job_repository, request_id=request_id, message_id=snapshot.message_id,
                confirmed_digest=body.confirmed_transcript_digest)
            mutation = await service.capture_intent(db, owner, body.task, capture=capture)
            return {"task": await _safe_task_payload(mutation.task, db=db),
                "idempotent_replay": mutation.idempotent_replay,
                "audio_budget_transferred": False, "audio_workflow_job_id": snapshot.workflow_job_id}
    except BoardError as exc:
        _raise_board_error(exc)
    except (DurableJobError, ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail={"code": "audio_task_capture_source_changed"}) from exc
