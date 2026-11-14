"""Private typed audio authority and paired canonical writer helpers.

There is no audio execution table/queue here. WorkflowRunState owns execution;
AudioIngressJob contains the immutable capture and private review projection.

#1179 (2026-11-14) retired voice input. Nothing admits or executes an
``audio_transcription_v1`` run any more (admission returns
``audio_route_retired``). What remains here only reads and fences historical
runs, so generic accounting/recovery and Work-board origin checks can still
settle or display them. A physical source read is never possible again.
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
        # The retired audio worker owned the quarantine bytes; a historical
        # run can never re-read them or reach provider contact.
        raise DurableJobLeaseError("audio_route_retired")
    return row


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


async def advance_audio_pair(db, run, audio, *, changes=None):
    from src.workflows.job_runtime import DurableJobLeaseError
    if audio is None:
        return
    allowed = {"status", "transcript", "transcript_digest", "confirmed_transcript_digest", "result_digest", "error_code", "provider_status", "transport_status", "cleanup_status", "raw_path", "normalized_path", "admission_operation_id"}
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
