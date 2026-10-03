"""Finite, provider-free procedure recommendation through the native job ledger."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.auth.service import AuthenticatedOperator
from src.db import engine as db_engine
from src.db.models import MemoryKind, MemoryProposal, MemoryProposalStatus, WorkflowRunState
from src.extensions.capability_pack import CapabilityPackLifecycle, capability_pack_digest
from src.memory.procedure_recommendations import (
    PROPOSAL_SCHEMA, SCOPE_SCHEMA, MAX_METADATA_BYTES, PreparedProcedureBundle,
    _BUNDLE_SEAL, _utc, assert_current_root, assert_membership_unchanged,
    bounded_json, canonical, digest, read_private_proof, resolve_scope, stage_procedure_bundle,
)
from src.work_board.repository import BoardError
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository

CAPABILITY_ID = "memory.procedure-recommendation.v1"
JOB_KIND = "work.procedure-recommendation.v1"
OUTPUT_KIND = "procedure_recommendation_output"


@contextmanager
def pin_current_package(scope):
    """Existing nonblocking shared lifecycle pin, acquired outside SQL."""
    from src.workflows.routines import _routine_pack_id, _routine_pack_root
    lifecycle = CapabilityPackLifecycle()
    with lifecycle._state_lock(shared=True):
        state = lifecycle._load()
        pack_id = _routine_pack_id(scope.routine_id, scope.version)
        pointer = state.get("active", {}).get(pack_id)
        if (not isinstance(pointer, dict) or pointer.get("status") != "active"
            or pointer.get("digest") != scope.package_digest
            or not lifecycle._pointer_binding_valid(state, pack_id, pointer)):
            raise BoardError("procedure_package_stale", "The reviewed active package changed during staging")
        lifecycle._require_pointer_identity(pointer, owner_principal_id=scope.owner_principal_id,
            session_id=scope.owner_session_id)
        if capability_pack_digest(_routine_pack_root(scope.routine_id, scope.version)) != scope.package_digest:
            raise BoardError("procedure_package_stale", "The exact reviewed package bytes changed")
        yield


class ProcedureRecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=128)
    expected_goal_revision: int = Field(ge=1)
    request_uuid: str = Field(min_length=36, max_length=36)

    @field_validator("request_uuid")
    @classmethod
    def exact_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("request_uuid must be canonical")
        return value


class ProcedureRecommendationCancelRequest(ProcedureRecommendationRequest):
    expected_job_revision: int = Field(ge=1)
    expected_fencing_token: int = Field(ge=0)


async def cancel_recommendation(operator, routine_id, job_id, request: ProcedureRecommendationCancelRequest):
    """Server-owned pure current-Root/Goal callback in the existing cancel CAS."""
    owner = operator.principal.principal_id
    body_digest = digest({"owner": owner, "root": operator.session_id, "routine_id": routine_id,
        "job_id": job_id, **request.model_dump()})
    staged_fingerprint = None
    staged_authority = None
    staged_lease_owner = None
    async def current(db, run):
        await assert_current_root(db, operator)
        await resolve_scope(db, operator, routine_id=routine_id, version=request.version,
            routine_revision=request.expected_routine_revision, goal_id=request.goal_id,
            goal_revision=request.expected_goal_revision)
        authority = bounded_json(run.declared_authority_json, {})
        if (run.job_kind != JOB_KIND or run.operator_session_id != operator.session_id
            or run.owner_principal_id != owner or run.fencing_token != request.expected_fencing_token
            or authority.get("routine_id") != routine_id or authority.get("routine_version") != request.version
            or run.run_fingerprint != staged_fingerprint or run.declared_authority_json != staged_authority
            or run.goal_id != request.goal_id or run.goal_revision != request.expected_goal_revision):
            raise BoardError("procedure_cancel_binding_invalid", "Cancel only the exact current owned recommendation")
        if run.status == "cancelled":
            if run.result_digest != digest({"memory_status": "no_learning", "cancel_request_digest": body_digest}):
                raise BoardError("procedure_request_conflict", "The closed cancellation binds another exact request")
        elif run.revision != request.expected_job_revision or run.status not in {"accepted", "queued", "running", "blocked"}:
            raise BoardError("procedure_cancel_stale", "Inspect the exact current job revision before cancellation")
        elif run.lease_owner != staged_lease_owner or (run.status == "running"
            and run.lease_owner != f"procedure-recommendation:{job_id}"):
            raise BoardError("procedure_cancel_binding_invalid", "The exact recommendation lease changed")
    # Initial inspection admits no cancellation; final authority is rechecked
    # in the repository's immediate writer below, including exact replay.
    async with db_engine.get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
        if run is None:
            raise BoardError("procedure_job_owner_mismatch", "The exact owned recommendation is unavailable")
        staged_fingerprint, staged_authority = run.run_fingerprint, run.declared_authority_json
        staged_lease_owner = run.lease_owner
        await current(db, run)
    await durable_job_repository.cancel_job(job_id, owner=staged_lease_owner,
        fencing_token=request.expected_fencing_token if staged_lease_owner else None, expected_revision=request.expected_job_revision,
        cancellation_authority_check=current, result={"memory_status": "no_learning", "cancel_request_digest": body_digest})
    return await inspect_recommendation(operator, routine_id, job_id)


def preview_text(bundle: PreparedProcedureBundle) -> str:
    scope = bundle.scope
    return (f"Suggest reviewed public-browser-check procedure {scope.routine_id} version {scope.version} "
        f"for this exact Goal. Selection only; fresh execution permissions are always required. "
        f"Review bundle: {bundle.bundle_digest}. " + bundle.projection()["manual_disclosure"] + " "
        + bundle.projection()["quality_disclosure"])


def preference_scope(bundle: PreparedProcedureBundle) -> dict:
    membership = bounded_json(bundle.membership_json)
    return {"schema_version": SCOPE_SCHEMA, **asdict(bundle.scope),
        "source_context_digest": digest(asdict(bundle.scope)),
        "membership_digest": membership["membership_digest"], "bundle_digest": bundle.bundle_digest,
        "source_task_ids": membership["task_ids"]}


async def recheck_bundle(db, operator, bundle):
    if not isinstance(bundle, PreparedProcedureBundle) or bundle.seal is not _BUNDLE_SEAL:
        raise BoardError("procedure_proof_invalid", "A server-staged native proof is required")
    current = await resolve_scope(db, operator, routine_id=bundle.scope.routine_id,
        version=bundle.scope.version, routine_revision=bundle.scope.routine_revision,
        goal_id=bundle.scope.goal_id, goal_revision=bundle.scope.goal_revision)
    if current != bundle.scope:
        raise BoardError("procedure_version_stale", "The exact reviewed procedure changed")
    await assert_membership_unchanged(db, current, bounded_json(bundle.membership_json))


async def inspect_recommendation(operator: AuthenticatedOperator, routine_id: str, job_id: str) -> dict:
    """Historical inspection admits no job, feedback, adoption or invocation."""
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
        authority = bounded_json(run.declared_authority_json, {}) if run else {}
        if (run is None or run.job_kind != JOB_KIND or run.owner_principal_id != operator.principal.principal_id
            or run.operator_session_id != operator.session_id or authority.get("routine_id") != routine_id):
            raise BoardError("procedure_job_owner_mismatch", "The recommendation belongs to a different original Root or procedure")
        artifacts = bounded_json(run.artifact_receipts_json, [])
        effects = bounded_json(run.effect_receipts_json, [])
        matching = [item for item in artifacts if item.get("artifact_type") == OUTPUT_KIND]
        result = {"job_id": job_id, "job_status": run.status, "job_revision": run.revision,
            "fencing_token": run.fencing_token, "status": run.status,
            "capability_id": CAPABILITY_ID, "native_job_kind": JOB_KIND,
            "typed_input_schema": "procedure-recommendation-request.v1", "typed_output_schema": "procedure-recommendation-output.v1",
            "permissions": authority.get("permissions", []), "limits": authority.get("limits", {}),
            "priority": run.priority, "max_attempts": run.max_attempts, "budget_microusd": 0,
            "memory_status": "no_learning", "reason_code": run.error or run.status,
            "deadline_at": _utc(run.deadline_at).isoformat() if run.deadline_at else None}
        if run.status == "succeeded":
            if len(matching) != 1:
                raise BoardError("procedure_job_output_missing", "The native recommendation has no unique output")
            artifact = matching[0]
            positive = [item for item in effects if item.get("effect_type") == OUTPUT_KIND
                and item.get("receipt_kind") == "readback" and item.get("effect_id") == f"procedure-recommendation:{job_id}"
                and item.get("readback_id") == f"procedure-recommendation-readback:{job_id}"
                and item.get("target_path") == artifact["file_path"] and item.get("content_sha256") == artifact["content_sha256"]
                and item.get("status") == "succeeded" and item.get("fencing_token") == run.fencing_token
                and item.get("details", {}).get("verified") is True]
            if len(positive) != 1:
                raise BoardError("procedure_job_readback_missing", "The exact native output has no protected readback")
            # The writer has ended before the bounded physical read below.
            path, sha = artifact["file_path"], artifact["content_sha256"]
        else:
            return result
    output = bounded_json(read_private_proof(path, sha).decode())
    if output.get("job_id") != job_id or output.get("scope", {}).get("routine_id") != routine_id:
        raise BoardError("procedure_job_output_invalid", "The exact native output binding changed")
    if positive[0].get("target_digest") != output.get("bundle_digest"):
        raise BoardError("procedure_job_readback_invalid", "The protected readback names a different exact output bundle")
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        current = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one()
        if (current.revision != result["job_revision"] or current.owner_principal_id != operator.principal.principal_id
            or current.operator_session_id != operator.session_id or current.artifact_receipts_json != run.artifact_receipts_json
            or current.effect_receipts_json != run.effect_receipts_json):
            raise BoardError("procedure_job_readback_changed", "The canonical producer changed during physical inspection")
    result.update(output)
    return result


async def find_recommendation(operator, routine_id, request: ProcedureRecommendationRequest):
    """Resolve only the retained exact request; GET never prepares a job."""
    owner = operator.principal.principal_id
    binding = {"owner_principal_id": owner, "owner_session_id": operator.session_id,
        "routine_id": routine_id, **request.model_dump()}
    job_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-recommendation:{owner}:{operator.session_id}:{request.request_uuid}"))
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
        if row is None:
            return {"found": False, "job": None}
        if row.run_fingerprint != digest(binding):
            raise BoardError("procedure_request_conflict", "This exact request identity binds different parameters")
    return {"found": True, "job": await inspect_recommendation(operator, routine_id, job_id)}


async def prepare_recommendation(operator: AuthenticatedOperator, routine_id: str,
                                 request: ProcedureRecommendationRequest) -> dict:
    owner = operator.principal.principal_id
    binding = {"owner_principal_id": owner, "owner_session_id": operator.session_id,
        "routine_id": routine_id, **request.model_dump()}
    job_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-recommendation:{owner}:{operator.session_id}:{request.request_uuid}"))
    current = await durable_job_repository.get_job(job_id)
    if current is not None:
        if current.get("run_fingerprint") != digest(binding):
            raise BoardError("procedure_request_conflict", "This request identity already binds different review parameters")
        return await inspect_recommendation(operator, routine_id, job_id)
    async with db_engine.get_session() as db:
        root = await assert_current_root(db, operator)
        scope = await resolve_scope(db, operator, routine_id=routine_id, version=request.version,
            routine_revision=request.expected_routine_revision, goal_id=request.goal_id,
            goal_revision=request.expected_goal_revision)
        deadline = min(datetime.now(timezone.utc) + timedelta(seconds=120),
            _utc(root.idle_expires_at), _utc(root.absolute_expires_at))
    authority = {"principal": owner, "session_id": operator.session_id, "owner_kind": "user",
        "operator_session_id": operator.session_id, "goal_id": scope.goal_id, "goal_revision": scope.goal_revision,
        "routine_id": routine_id, "routine_version": scope.version, "routine_revision": scope.routine_revision,
        "plan_digest": scope.plan_digest, "package_digest": scope.package_digest,
        "capability_id": CAPABILITY_ID, "permissions": ["local_procedure_outcome_read", "local_recommendation_artifact_write"],
        "budget_microusd": 0, "limits": {"max_tasks": 20, "max_feedback_events": 100,
            "max_output_bytes": MAX_METADATA_BYTES, "max_runtime_seconds": 120},
        "finite_authority": {"expires_at": deadline.isoformat()}}
    job = await durable_job_repository.admit_job(DurableJobSpec(
        identity=DurableJobIdentity(job_id, "user", owner, JOB_KIND, CAPABILITY_ID,
            "procedure-recommendation", request.request_uuid), inputs=binding,
        session_id=operator.session_id, operator_session_id=operator.session_id,
        goal_id=scope.goal_id, goal_revision=scope.goal_revision, priority=50,
        declared_authority=authority, deadline_at=deadline, max_attempts=1,
        max_outstanding_jobs=1, budget_microusd=0, run_fingerprint=digest(binding)))
    if job.get("receipt", {}).get("status") == "deduped":
        return await inspect_recommendation(operator, routine_id, job_id)
    job = await durable_job_repository.queue_job(job_id, expected_revision=job["revision"])
    lease_owner = f"procedure-recommendation:{job_id}"
    job = await durable_job_repository.claim_job(job_id, owner=lease_owner, lease_seconds=120,
        expected_revision=job["revision"])
    fence = job["lease"]["fencing_token"]
    try:
        async with asyncio.timeout(max(0.001, (deadline - datetime.now(timezone.utc)).total_seconds())):
            # Serialize normal package publication while physical proof is
            # staged and then handed to pure canonical writer callbacks.
            bundle = await stage_procedure_bundle(operator, routine_id=routine_id, version=scope.version,
                routine_revision=scope.routine_revision, goal_id=scope.goal_id, goal_revision=scope.goal_revision)
            if (await durable_job_repository.get_job(job_id))["status"] == "cancelled":
                return await inspect_recommendation(operator, routine_id, job_id)
            with pin_current_package(scope):
                output = {**bundle.projection(), "job_id": job_id, "proposal_id": None}
                proposal_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-preference:{owner}:{operator.session_id}:{bundle.bundle_digest}"))
                if output["status"] == "proposed":
                    output["proposal_id"] = proposal_id
                raw = canonical(output).encode()
                if len(raw) > MAX_METADATA_BYTES:
                    raise BoardError("procedure_metadata_limit", "The recommendation exceeds its finite output bound")
                from config.settings import settings
                from src.work_board.input_artifacts import _write_payload
                relative = f"artifacts/memory/procedures/{job_id}.json"
                _write_payload(Path(settings.workspace_dir) / relative, raw)
                sha = hashlib.sha256(raw).hexdigest()
                if read_private_proof(relative, sha) != raw:
                    raise BoardError("procedure_output_changed", "The private output changed before readback")
                job = await durable_job_repository.record_artifact(job_id, file_path=relative,
                    artifact_type=OUTPUT_KIND, content=raw, owner=lease_owner, fencing_token=fence,
                    expected_revision=job["revision"])
                async def check(db, run):
                    if (run.job_kind != JOB_KIND or run.operator_session_id != operator.session_id
                        or run.owner_principal_id != owner or run.run_fingerprint != digest(binding)
                        or run.fencing_token != fence):
                        raise BoardError("procedure_job_binding_invalid", "The exact native recommendation authority changed")
                    await recheck_bundle(db, operator, bundle)
                job = await durable_job_repository.record_readback(job_id, target_path=relative, status="succeeded",
                    effect_type=OUTPUT_KIND, effect_id=f"procedure-recommendation:{job_id}", target_digest=bundle.bundle_digest,
                    content_sha256=sha, readback_id=f"procedure-recommendation-readback:{job_id}",
                    verified_at=datetime.now(timezone.utc).isoformat(), details={"verified": True, "memory_status": "no_learning"},
                    owner=lease_owner, fencing_token=fence, expected_revision=job["revision"], readback_authority_check=check)
                async def finalize(db, run):
                    await check(db, run)
                    if output["status"] != "proposed":
                        return
                    existing = await db.get(MemoryProposal, proposal_id, populate_existing=True)
                    if existing is not None:
                        if existing.schema_version != PROPOSAL_SCHEMA or existing.evidence_digest != bundle.bundle_digest:
                            raise BoardError("procedure_proposal_conflict", "The deterministic proposal identity changed")
                        return
                    membership = bounded_json(bundle.membership_json)
                    anchor = next(item for item in membership["members"] if item["feedback_tip"]
                        and item["feedback_tip"]["label"] == "helpful" and any(
                            outcome["task_id"] == item["task"]["task_id"] and outcome["verified"] for outcome in output["outcomes"]))
                    text = preview_text(bundle)
                    row = MemoryProposal(proposal_id=proposal_id, schema_version=PROPOSAL_SCHEMA,
                        owner_principal_id=owner, owner_session_id=operator.session_id,
                        source_task_id=anchor["task"]["task_id"], source_task_revision=anchor["task"]["task_revision"],
                        source_attempt_id=anchor["attempt"]["attempt_id"], source_attempt_fence=anchor["attempt"]["fencing_token"],
                        workflow_run_id=anchor["parent"]["run_identity"], workflow_run_revision=anchor["parent"]["revision"],
                        goal_id=scope.goal_id, goal_revision=scope.goal_revision, capability_id="guardian-routine.v2",
                        capability_version="guardian-routine.v2", typed_input_digest=anchor["task"]["typed_input_digest"],
                        source_context_digest=digest(asdict(scope)), candidate_set_digest=digest([scope.version_id]),
                        evidence_digest=bundle.bundle_digest, readback_kind=OUTPUT_KIND,
                        readback_ref=f"procedure-recommendation-readback:{job_id}", readback_digest=sha,
                        artifact_ref=relative, artifact_digest=sha, proposal_job_id=job_id,
                        request_idempotency_key=request.request_uuid, request_binding_digest=digest(binding),
                        memory_kind=MemoryKind.pattern, memory_scope_json=canonical(preference_scope(bundle)),
                        preview_text=text, preview_text_digest=hashlib.sha256(text.encode()).hexdigest(),
                        provenance_json=canonical({"schema": PROPOSAL_SCHEMA, "scope": asdict(scope),
                            "bundle_digest": bundle.bundle_digest, "membership": membership, "outcomes": output["outcomes"]}),
                        source_refs_json=canonical(membership["task_ids"]), confidence=0.5,
                        status=MemoryProposalStatus.proposed, reason_code="reviewed_outcomes_support_preference",
                        expires_at=deadline)
                    db.add(row)
                    await db.flush()
                await durable_job_repository.transition_job(job_id, "succeeded", owner=lease_owner,
                    fencing_token=fence, expected_revision=job["revision"], terminal_authority_check=finalize,
                    result={"status": output["status"], "bundle_digest": bundle.bundle_digest,
                        "proposal_id": output["proposal_id"], "memory_status": "no_learning"},
                    result_summary=output["reason_code"])
    except Exception as exc:
        current = await durable_job_repository.get_job(job_id)
        if current and current["status"] == "running":
            await durable_job_repository.transition_job(job_id, "blocked", owner=lease_owner, fencing_token=fence,
                expected_revision=current["revision"], reason=getattr(exc, "code", "procedure_recommendation_failed"),
                result={"memory_status": "no_learning"}, result_summary="No preference was adopted")
        raise
    return await inspect_recommendation(operator, routine_id, job_id)
