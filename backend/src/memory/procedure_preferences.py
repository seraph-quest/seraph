"""Reviewed selection-only procedure memories; all writer checks are DB-only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select

from src.db import engine as db_engine
from src.db.models import Memory, MemoryProposal, MemoryProposalStatus, MemoryProposalDecisionEffect, MemoryTombstone, WorkBoardEvent
from src.memory.procedure_recommendations import PROPOSAL_SCHEMA, MAX_METADATA_BYTES, canonical, digest, bounded_json, assert_current_root, stage_procedure_bundle, _utc
from src.memory.procedure_recommendation_job import preference_scope, preview_text, recheck_bundle, pin_current_package, inspect_recommendation
from src.memory.repository import (
    memory_repository, _effect_mac_key, _m5_verified_source_binding,
    _m5_selection_binding_key_id, _m5_selection_binding_mac, _m5_selection_binding_matches,
)
from src.work_board.repository import BoardError, _begin_sqlite_immediate

ACTION_KIND = "procedure.preference_review.v1"


class ProcedurePreferenceActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    action: Literal["accept", "reject", "rollback"]
    expected_revision: int = Field(ge=1)
    expected_preview_text_digest: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]{64}$")
    expected_bundle_digest: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]{64}$")
    acknowledged_selection_only: StrictBool
    mutation_uuid: str = Field(min_length=36, max_length=36)
    reason: str = Field(default="", max_length=500)

    @field_validator("acknowledged_selection_only")
    @classmethod
    def acknowledged(cls, value):
        if value is not True:
            raise ValueError("Explicit selection-only acknowledgment is required")
        return value

    @field_validator("mutation_uuid")
    @classmethod
    def exact_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("mutation_uuid must be canonical")
        return value


def proposal_projection(row):
    # Pure fixed projection: generic capability discovery is not this schema.
    payload = {key: getattr(row, key) for key in (
        "proposal_id", "schema_version", "owner_principal_id", "owner_session_id", "source_task_id",
        "source_task_revision", "source_attempt_id", "source_attempt_fence", "workflow_run_id", "goal_id",
        "goal_revision", "preview_text", "preview_text_digest", "accepted_memory_id", "revision", "reason_code")}
    payload["status"] = getattr(row.status, "value", row.status)
    payload["scope"] = bounded_json(row.memory_scope_json, {})
    payload["expires_at"] = _utc(row.expires_at).isoformat() if row.expires_at else None
    provenance = bounded_json(row.provenance_json, {})
    outcomes = provenance.get("outcomes", [])
    from src.memory.procedure_recommendations import MANUAL_DISCLOSURE, QUALITY_DISCLOSURE
    payload.update({"outcomes": outcomes, "included_count": len(outcomes),
        "helpful_count": sum(item.get("verified") and item.get("feedback") == "helpful" for item in outcomes),
        "harmful_count": sum(item.get("feedback") == "harmful" for item in outcomes),
        "bundle_digest": row.evidence_digest, "manual_disclosure": MANUAL_DISCLOSURE,
        "quality_disclosure": QUALITY_DISCLOSURE, "evidence_population": "matching_manual_invocations_only",
        "quality_evidence": "unmeasured", "registered_capabilities": [], "allowed_decision_effects": []})
    return payload


async def inspect_preference(operator, proposal_id):
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
        if (row is None or row.schema_version != PROPOSAL_SCHEMA
            or (row.owner_principal_id, row.owner_session_id) != (operator.principal.principal_id, operator.session_id)):
            raise BoardError("procedure_proposal_owner_mismatch", "The review belongs to a different original Root")
        return proposal_projection(row)


async def apply_preference_action(operator, proposal_id, request: ProcedurePreferenceActionRequest):
    owner = operator.principal.principal_id
    request_digest = digest({"owner": owner, "root": operator.session_id, "proposal_id": proposal_id, **request.model_dump()})
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        replay = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.owner_principal_id == owner, WorkBoardEvent.owner_session_id == operator.session_id,
            WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))).scalar_one_or_none()
        row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
        if (row is None or row.schema_version != PROPOSAL_SCHEMA
            or (row.owner_principal_id, row.owner_session_id) != (owner, operator.session_id)):
            raise BoardError("procedure_proposal_owner_mismatch", "The exact review is unavailable")
        if replay is not None:
            if replay.kind != ACTION_KIND or replay.mutation_request_digest != request_digest:
                raise BoardError("procedure_request_conflict", "This request key already binds a different action")
            return {**proposal_projection(row), "idempotent_replay": True, "audit_event_id": replay.event_id}
        staged_revision = row.revision
        staged_proposal = digest({key: getattr(row, key) for key in (
            "memory_scope_json", "provenance_json", "preview_text", "preview_text_digest", "evidence_digest",
            "artifact_ref", "artifact_digest", "proposal_job_id", "readback_ref", "accepted_memory_id",
            "accepted_memory_content_digest", "acceptance_binding_digest")})
        scope = bounded_json(row.provenance_json, {}).get("scope", {})
        job_id = row.proposal_job_id
        candidate = row.preview_text or ""
    # Key loading and Vault-backed redaction are deliberately pre-writer.
    from src.memory.m5 import sanitize_m5_memory_text_async, _write_memory_action_audit
    signing_key = _effect_mac_key()
    text = await sanitize_m5_memory_text_async(candidate)
    if text != candidate or hashlib.sha256(text.encode()).hexdigest() != request.expected_preview_text_digest:
        raise BoardError("procedure_preview_changed", "Review the exact current preview again")
    if request.action == "rollback" and not request.reason.strip():
        raise BoardError("procedure_rollback_reason_required", "A rollback reason is required")
    bundle = None
    job_projection = None
    if request.action == "accept":
        bundle = await stage_procedure_bundle(operator, routine_id=scope["routine_id"], version=scope["version"],
            routine_revision=scope["routine_revision"], goal_id=scope["goal_id"], goal_revision=scope["goal_revision"])
        if (bundle.bundle_digest != request.expected_bundle_digest or bundle.projection()["status"] != "proposed"
            or text != preview_text(bundle)):
            raise BoardError("procedure_membership_changed", "The complete reviewed outcome set changed")
        job_projection = await inspect_recommendation(operator, scope["routine_id"], job_id)
        if job_projection.get("proposal_id") != proposal_id or job_projection.get("bundle_digest") != bundle.bundle_digest:
            raise BoardError("procedure_job_readback_invalid", "The exact recommendation producer readback changed")
    from contextlib import nullcontext
    with pin_current_package(bundle.scope) if bundle is not None else nullcontext():
        async with db_engine.get_session() as db:
            await _begin_sqlite_immediate(db)
            now = datetime.now(timezone.utc)
            await assert_current_root(db, operator, now=now)
            row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
            if (row is None or row.schema_version != PROPOSAL_SCHEMA
                or (row.owner_principal_id, row.owner_session_id) != (owner, operator.session_id)):
                raise BoardError("procedure_proposal_owner_mismatch", "The exact review is unavailable")
            replay = (await db.execute(select(WorkBoardEvent).where(
                WorkBoardEvent.owner_principal_id == owner, WorkBoardEvent.owner_session_id == operator.session_id,
                WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))).scalar_one_or_none()
            if replay is not None:
                if replay.kind != ACTION_KIND or replay.mutation_request_digest != request_digest:
                    raise BoardError("procedure_request_conflict", "This request key already binds a different action")
                return {**proposal_projection(row), "idempotent_replay": True, "audit_event_id": replay.event_id}
            current_proposal = digest({key: getattr(row, key) for key in (
                "memory_scope_json", "provenance_json", "preview_text", "preview_text_digest", "evidence_digest",
                "artifact_ref", "artifact_digest", "proposal_job_id", "readback_ref", "accepted_memory_id",
                "accepted_memory_content_digest", "acceptance_binding_digest")})
            if (row.revision != staged_revision or row.revision != request.expected_revision
                or current_proposal != staged_proposal or row.preview_text_digest != request.expected_preview_text_digest
                or row.evidence_digest != request.expected_bundle_digest):
                raise BoardError("procedure_review_stale", "The review changed during staging")
            if request.action == "accept":
                if row.status != MemoryProposalStatus.proposed or row.expires_at is None or _utc(row.expires_at) <= now:
                    raise BoardError("procedure_preview_expired", "Prepare and review a fresh recommendation")
                await recheck_bundle(db, operator, bundle)
                from src.db.models import WorkflowRunState
                run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
                if run is None or run.status != "succeeded" or run.revision != job_projection["job_revision"]:
                    raise BoardError("procedure_job_readback_invalid", "The recommendation producer changed during staging")
                memory_scope = preference_scope(bundle)
                if bounded_json(row.memory_scope_json) != memory_scope:
                    raise BoardError("procedure_scope_changed", "The exact signed procedure scope changed")
                source = _m5_verified_source_binding(row)
                if source is None:
                    raise BoardError("procedure_source_binding_invalid", "The original verified source is unavailable")
                content_digest = hashlib.sha256(text.encode()).hexdigest()
                effect = MemoryProposalDecisionEffect.require_operator_confirmation
                provenance = {"schema_version": "work_board_memory_provenance.v1", "proposal_id": proposal_id,
                    "owner_principal_id": owner, "owner_session_id": operator.session_id,
                    "source_context_digest": row.source_context_digest, "accepted_content_digest": content_digest,
                    "memory_kind": "pattern", "memory_scope": memory_scope,
                    "decision_effect": effect.value, "lifecycle_state": "active", "verified_source_binding": source,
                    "selection_binding_key_id": _m5_selection_binding_key_id(_signing_key=signing_key)}
                provenance["selection_binding_mac"] = _m5_selection_binding_mac(proposal_id=proposal_id,
                    accepted_content_digest=content_digest, owner_principal_id=owner, owner_session_id=operator.session_id,
                    source_context_digest=row.source_context_digest, source_binding=source, decision_effect=effect,
                    memory_scope=memory_scope, _signing_key=signing_key)
                metadata = canonical({"work_board_provenance": provenance})
                if len(metadata.encode()) + len(row.provenance_json.encode()) + len(row.memory_scope_json.encode()) > MAX_METADATA_BYTES:
                    raise BoardError("procedure_metadata_limit", "The complete signed preference exceeds its finite bound")
                memory = await memory_repository.create_m5_memory_in_session(db, content=text, kind=row.memory_kind,
                    source_session_id=operator.session_id, scope_key=digest({"scope": memory_scope, "proposal_id": proposal_id}),
                    metadata_json=metadata, confidence=0.5, proposal_id=proposal_id)
                row.accepted_memory_id = memory.id
                row.accepted_memory_content_digest = content_digest
                row.accepted_by_principal_id = owner
                row.accepted_by_session_id = operator.session_id
                row.accepted_at = now
                row.acceptance_binding_digest = request_digest
                row.decision_effect = effect
                row.status = MemoryProposalStatus.accepted
                row.reason_code = "procedure_preference_accepted"
            elif request.action == "rollback":
                if row.status != MemoryProposalStatus.accepted or not row.accepted_memory_id:
                    raise BoardError("procedure_preference_not_accepted", "Only an adopted preference can be rolled back")
                await memory_repository.rollback_m5_memory_in_session(db, memory_id=row.accepted_memory_id,
                    expected_content_digest=row.accepted_memory_content_digest, expected_proposal_id=proposal_id,
                    rollback_reason=request.reason.strip(), _signing_key=signing_key)
                row.status = MemoryProposalStatus.rolled_back
                row.rollback_by_principal_id = owner
                row.rollback_by_session_id = operator.session_id
                row.rollback_at = now
                row.rollback_reason = request.reason.strip()
                row.reason_code = "procedure_preference_rolled_back"
            else:
                if row.status != MemoryProposalStatus.proposed:
                    raise BoardError("procedure_preference_not_proposed", "Only a proposed preference can be rejected")
                row.status = MemoryProposalStatus.rejected
                row.rejected_by_principal_id = owner
                row.rejected_by_session_id = operator.session_id
                row.rejected_at = now
                row.reason_code = "procedure_preference_rejected"
            row.revision += 1
            row.updated_at = now
            db.add(row)
            event = WorkBoardEvent(owner_principal_id=owner, owner_session_id=operator.session_id,
                actor_principal_id=owner, actor_session_id=operator.session_id,
                task_id=row.source_task_id, kind=ACTION_KIND, mutation_idempotency_key=request.mutation_uuid,
                mutation_request_digest=request_digest, metadata_json=canonical({"schema": ACTION_KIND,
                    "proposal_id": proposal_id, "action": request.action, "bundle_digest": row.evidence_digest,
                    "proposal_revision": row.revision}))
            db.add(event)
            await _write_memory_action_audit(db, owner_principal_id=owner, owner_session_id=operator.session_id,
                proposal=row, action=request.action)
            await db.flush()
            return {**proposal_projection(row), "audit_event_id": event.event_id, "idempotent_replay": False}
