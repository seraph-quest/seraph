"""Private typed audio authority and paired canonical writer helpers.

There is no audio execution table/queue here. WorkflowRunState owns execution;
AudioIngressJob contains the immutable capture and private review projection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import re

from sqlalchemy import update
from sqlmodel import select
from src.db.models import AudioIngressJob, AudioConsentGrant, OperatorSession, Session

KIND = "audio_transcription_v1"
AUTHORITY_KEY = "audio_native_admission"


@dataclass(frozen=True, slots=True)
class AudioTranscriptionResultV1:
    schema_version: str
    audio_job_id: str
    operation_id: str
    transcript: str
    transcript_digest: str
    effective_model_id: str
    effective_upstream_endpoint_tag: str
    provider_generation_id: str
    actual_cost_microusd: int
    contact_state: str

    def __post_init__(self):
        if (self.schema_version != "audio-transcription-result.v1" or self.contact_state != "settled"
            or type(self.actual_cost_microusd) is not int or not 0 <= self.actual_cost_microusd <= 1_000_000_000
            or not isinstance(self.transcript, str) or not self.transcript.strip() or len(self.transcript) > 20000
            or len(self.transcript.encode()) > 80000 or "\x00" in self.transcript
            or hashlib.sha256(self.transcript.encode()).hexdigest() != self.transcript_digest
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.provider_generation_id)):
            raise ValueError("audio_transcription_result_invalid")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value):
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # SQLite persisted UTC
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AudioNativeAdmissionBindingV1:
    schema_version: str
    audio_job_id: str
    workflow_job_id: str
    operation_id: str
    owner_principal_id: str
    original_root_id: str
    conversation_session_id: str
    capture_binding_digest: str
    capture_grant_ref: str
    model_grant_ref: str
    profile_hash: str
    endpoint_witness_ref: str
    policy_digest: str
    original_deadline_at: str
    audio_budget_microusd: int
    max_calls: int

    def __post_init__(self):
        if self.schema_version != "audio-native-admission.v1":
            raise ValueError("audio_native_schema_invalid")
        for name in ("capture_binding_digest", "profile_hash", "policy_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("audio_native_digest_invalid")
        for name in ("audio_job_id", "workflow_job_id", "operation_id", "owner_principal_id", "original_root_id", "conversation_session_id", "capture_grant_ref", "model_grant_ref", "endpoint_witness_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
                raise ValueError("audio_native_identifier_invalid")
        expected = "audio-transcription:" + hashlib.sha256(self.audio_job_id.encode()).hexdigest()[:40]
        if self.workflow_job_id != expected:
            raise ValueError("audio_native_job_identity_invalid")
        if type(self.audio_budget_microusd) is not int or not 1 <= self.audio_budget_microusd <= 1_000_000_000:
            raise ValueError("audio_native_budget_invalid")
        if type(self.max_calls) is not int or self.max_calls != 1:
            raise ValueError("audio_native_call_limit_invalid")
        value = datetime.fromisoformat(self.original_deadline_at.replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("audio_native_deadline_invalid")

    def payload(self):
        return asdict(self)

    @classmethod
    def restore(cls, value):
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("audio_native_binding_invalid")
        return cls(**value)


def validate_spec(spec, binding):
    from src.workflows.job_runtime import DurableJobAdmissionDenied
    if type(binding) is not AudioNativeAdmissionBindingV1:
        raise DurableJobAdmissionDenied("audio_native_binding_required")
    identity = spec.identity
    if (identity.job_kind != KIND or identity.owner_kind != "user"
        or identity.job_id != binding.workflow_job_id
        or identity.owner_principal_id != binding.owner_principal_id
        or identity.capability_version != "audio-transcription-v1"
        or identity.idempotency_scope != "audio-ingress"
        or spec.session_id != binding.conversation_session_id
        or spec.operator_session_id != binding.original_root_id
        or any(getattr(spec, k) is not None for k in ("goal_id", "goal_revision", "plan_revision", "parent_job_id", "source_task_id"))
        or spec.max_attempts != 1 or spec.resource_claims != ("remote_inference",)
        or spec.budget_microusd != binding.audio_budget_microusd
        or utc(spec.deadline_at) != utc(binding.original_deadline_at)
        or spec.declared_authority.get(AUTHORITY_KEY) != binding.payload()):
        raise DurableJobAdmissionDenied("audio_native_spec_mismatch")


async def current_capture(db, binding, *, require_current=True, physical=False):
    from src.workflows.job_runtime import DurableJobLeaseError
    row = await db.get(AudioIngressJob, binding.audio_job_id, populate_existing=True)
    if row is None or (row.owner_principal_id, row.operator_session_id, row.session_id, row.request_digest, row.capture_consent_reference, row.model_consent_reference) != (binding.owner_principal_id, binding.original_root_id, binding.conversation_session_id, binding.capture_binding_digest, binding.capture_grant_ref, binding.model_grant_ref):
        raise DurableJobLeaseError("audio_capture_binding_stale")
    if require_current:
        now = datetime.now(timezone.utc)
        root = await db.get(OperatorSession, binding.original_root_id, populate_existing=True)
        conversation = await db.get(Session, binding.conversation_session_id, populate_existing=True)
        if (root is None or root.principal_id != binding.owner_principal_id or root.revoked_at is not None or root.is_bearer_tombstone or root.replaced_by_id
            or utc(root.idle_expires_at) <= now or utc(root.absolute_expires_at) <= now
            or conversation is None or conversation.owner_principal_id != binding.owner_principal_id
            or utc(binding.original_deadline_at) <= now
            or utc(binding.original_deadline_at) > utc(row.raw_audio_retention_deadline)
            or utc(binding.original_deadline_at) > utc(root.absolute_expires_at)):
            raise DurableJobLeaseError("audio_original_authority_expired")
        for ref, boundary in ((binding.capture_grant_ref, "capture"), (binding.model_grant_ref, "cloud_upload")):
            grant = (await db.execute(select(AudioConsentGrant).where(AudioConsentGrant.reference == ref).with_for_update())).scalars().first()
            if (grant is None or grant.owner_principal_id != binding.owner_principal_id or grant.operator_session_id != binding.original_root_id or grant.boundary != boundary or grant.state != "active" or grant.revoked_at is not None or utc(grant.expires_at) < utc(binding.original_deadline_at) or utc(grant.granted_at) > utc(row.captured_at)):
                raise DurableJobLeaseError("audio_original_grant_invalid")
        from src.model_fabric.effective_policy import current_inference_policy
        from src.llm_runtime import _provider_profile
        from src.model_fabric.audio_contracts import audio_route_witness
        configured, policy = current_inference_policy()
        profile = _provider_profile("openrouter.audio")
        if policy != binding.policy_digest or profile is None or profile.contract_hash != binding.profile_hash:
            raise DurableJobLeaseError("audio_original_profile_changed")
        evidence, _ = await audio_route_witness(profile, proof_ref=binding.endpoint_witness_ref)
        if evidence.pricing.reserve_microusd > binding.audio_budget_microusd:
            raise DurableJobLeaseError("audio_original_budget_insufficient")
    if physical:
        from src.conversation.identity import validate_attachment_refs
        from src.guardian.audio_worker import AudioIngressWorker
        from pathlib import Path
        refs = validate_attachment_refs([json.loads(row.attachment_ref_json)], owner_principal_id=binding.owner_principal_id)
        if (len(refs) != 1 or refs[0].get("attachment_id") != row.attachment_id
            or refs[0].get("content_hash") != row.audio_payload_digest or refs[0].get("session_id") != row.session_id
            or refs[0].get("capture_consent_reference") != binding.capture_grant_ref
            or refs[0].get("model_consent_reference") != binding.model_grant_ref):
            raise DurableJobLeaseError("audio_attachment_binding_stale")
        if not row.raw_path:
            raise DurableJobLeaseError("audio_source_missing")
        # All ancestors and final bytes must still be physical local files.
        path = Path(row.raw_path)
        for parent in (*path.parents, path):
            if parent.is_symlink():
                raise DurableJobLeaseError("audio_source_symlink")
        raw = AudioIngressWorker._read_quarantine_bytes(path)
        if len(raw) != row.audio_size_bytes or hashlib.sha256(raw).hexdigest() != row.audio_payload_digest:
            raise DurableJobLeaseError("audio_source_digest_stale")
    return row


async def guard_admission(db, spec, binding):
    from src.workflows.job_runtime import DurableJobIdempotencyConflict, DurableJobLeaseError
    validate_spec(spec, binding)
    row = await current_capture(db, binding, physical=True)
    if spec.identity.idempotency_key != row.request_id:
        raise DurableJobIdempotencyConflict("audio_request_identity_changed")
    exact = canonical(binding.payload())
    grant = (await db.execute(select(AudioConsentGrant).where(AudioConsentGrant.reference == binding.model_grant_ref))).scalars().one()
    if row.workflow_job_id is not None:
        if row.workflow_job_id != binding.workflow_job_id or row.execution_binding_digest != digest(binding.payload()) or grant.audio_execution_binding_json != exact:
            raise DurableJobIdempotencyConflict("audio_execution_binding_changed")
        return row
    if row.admission_operation_id or row.transport_lease_id or row.status not in {"queued", "blocked", "awaiting_consent"} or grant.audio_execution_binding_json is not None:
        raise DurableJobLeaseError("audio_capture_not_fresh")
    changed = await db.execute(update(AudioConsentGrant).where(AudioConsentGrant.id == grant.id, AudioConsentGrant.audio_execution_binding_json.is_(None), AudioConsentGrant.state == "active").values(audio_execution_binding_json=exact).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobIdempotencyConflict("audio_grant_binding_conflict")
    return row


async def bind_capture(db, row, binding):
    from src.workflows.job_runtime import DurableJobIdempotencyConflict
    changed = await db.execute(update(AudioIngressJob).where(AudioIngressJob.id == row.id, AudioIngressJob.revision == row.revision, AudioIngressJob.workflow_job_id.is_(None)).values(workflow_job_id=binding.workflow_job_id, execution_binding_digest=digest(binding.payload()), revision=AudioIngressJob.revision + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobIdempotencyConflict("audio_pair_admission_conflict")


async def guard_audio_pair(db, run, *, require_current=True, physical=False):
    from src.workflows.job_runtime import DurableJobLeaseError
    if run.job_kind != KIND:
        return None, None
    authority = json.loads(run.declared_authority_json)
    binding = AudioNativeAdmissionBindingV1.restore(authority.get(AUTHORITY_KEY))
    row = await current_capture(db, binding, require_current=require_current, physical=physical)
    grant = (await db.execute(select(AudioConsentGrant).where(AudioConsentGrant.reference == binding.model_grant_ref))).scalars().one_or_none()
    if (run.run_identity != binding.workflow_job_id or run.owner_principal_id != binding.owner_principal_id or run.operator_session_id != binding.original_root_id or run.session_id != binding.conversation_session_id or run.goal_id is not None or run.max_attempts != 1
        or authority.get("budget_microusd") != binding.audio_budget_microusd
        or row.workflow_job_id != binding.workflow_job_id or row.execution_binding_digest != digest(binding.payload())
        or grant is None or grant.audio_execution_binding_json != canonical(binding.payload())):
        raise DurableJobLeaseError("audio_native_pair_corrupt")
    return binding, row


async def advance_audio_pair(db, run, audio, *, changes=None, task_capture_reservation=None):
    from src.workflows.job_runtime import DurableJobLeaseError
    if audio is None:
        return
    allowed = {"status", "transcript", "transcript_digest", "confirmed_transcript_digest", "result_digest", "error_code", "provider_status", "transport_status", "cleanup_status", "raw_path", "normalized_path", "admission_operation_id"}
    if task_capture_reservation is not None:
        from src.work_board.channel_capture import ChannelCaptureReservationV1
        if type(task_capture_reservation) is not ChannelCaptureReservationV1:
            raise DurableJobLeaseError("audio_task_capture_reservation_required")
        original = json.loads(audio.metadata_json or "{}")
        updated = dict(original, **{"channel_task_capture.v1": task_capture_reservation.model_dump(mode="json")})
        if changes != {"metadata_json": canonical(updated)}:
            raise DurableJobLeaseError("audio_task_capture_metadata_invalid")
        allowed.add("metadata_json")
    if set(changes or {}) - allowed:
        raise DurableJobLeaseError("audio_pair_changes_invalid")
    values = dict(changes or {}, revision=AudioIngressJob.revision + 1, updated_at=datetime.now(timezone.utc))
    changed = await db.execute(update(AudioIngressJob).where(AudioIngressJob.id == audio.id, AudioIngressJob.workflow_job_id == run.run_identity, AudioIngressJob.execution_binding_digest == audio.execution_binding_digest, AudioIngressJob.revision == audio.revision).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("audio_pair_revision_stale")


async def accounting_pair(db, run, *, require_current=True, physical=False, changes=None):
    from src.db.models import WorkflowRunState
    from src.workflows.job_runtime import DurableJobLeaseError
    binding, audio = await guard_audio_pair(db, run, require_current=require_current, physical=physical)
    if binding is None:
        return None
    await advance_audio_pair(db, run, audio, changes=changes)
    changed = await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == run.run_identity,
        WorkflowRunState.revision == run.revision, WorkflowRunState.fencing_token == run.fencing_token).values(
        revision=WorkflowRunState.revision + 1, updated_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("audio_accounting_pair_stale")
    return binding
