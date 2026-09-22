"""Owner-bound reusable guardian routines.

This module deliberately keeps routine metadata small.  Source observation,
package governance, approvals, and external follow-through remain owned by
their existing services; a routine only stores verified provenance and binds a
fresh invocation to current identities before delegating.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any, AsyncIterator, Mapping

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.db import engine as db_engine
from src.db.models import Goal, GuardianDecisionPacket, GuardianRoutine, GuardianRoutineVersion, WorkflowRunState
from src.extensions.capability_pack import CapabilityPackLifecycle, capability_pack_digest
from src.extensions.github_followthrough import (
    GitHubFollowthroughError,
    GitHubFollowthroughService,
    JOB_KIND as GITHUB_FOLLOWTHROUGH_JOB_KIND,
    PrepareRequest,
    _operation_id,
)
from src.extensions.workspace_package import save_workspace_contribution, workspace_capability_package_root
from src.guardian.source_watch import source_watch_service
from src.security.trust_contract import AuthorityGrant
from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    _serialize,
    durable_job_repository,
)
from src.workflows.routine_templates import (
    render_runbook,
    render_workflow,
    routine_slug,
    validate_generated_files,
)
from src.workflows.routine_steps import RoutineStepContext, github_followthrough, guardian_watch_run


ROUTINE_CAPABILITY_VERSION = "guardian-routine.v1"
ROUTINE_SERVICE_ID = "guardian-routine"
ROUTINE_INSTALL_TOOL = "guardian:routine-install"
ROUTINE_INVOKE_TOOL = "guardian:routine-invoke"
PACKAGE_ID = "seraph.workspace-capabilities"
ROUTINE_DEADLINE_SECONDS = 600
APPROVAL_TTL_SECONDS = 5 * 60


class RoutineError(ValueError):
    def __init__(self, code: str, message: str | None = None, *, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(message or code)


class RoutineFromRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_watch_job_id: str = Field(min_length=1, max_length=256)
    source_packet_id: str = Field(min_length=1, max_length=256)
    source_m3_job_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=80)


class RoutineVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_watch_job_id: str = Field(min_length=1, max_length=256)
    source_packet_id: str = Field(min_length=1, max_length=256)
    source_m3_job_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=80)
    expected_routine_revision: int = Field(ge=1)


class RoutineInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    approval_id: str = Field(min_length=1, max_length=256)


class RoutineActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)


class RoutineInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    source_watch_id: str = Field(min_length=1, max_length=256)
    expected_watch_revision: int = Field(ge=1)
    invocation_uuid: str = Field(min_length=1, max_length=80)


class RoutineExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=256)
    expected_routine_revision: int = Field(ge=1)


class RoutinePublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=160)
    body: str = Field(min_length=1, max_length=32_000)


class RoutineRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_routine_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class RoutineRollbackRequest(RoutineRevisionRequest):
    target_version: int = Field(ge=1)


class RoutineRecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _operator_has_grant(operator: Any, grant: AuthorityGrant) -> bool:
    grants = {
        str(getattr(item, "value", item))
        for item in (getattr(getattr(operator, "principal", None), "grants", ()) or ())
    }
    return grant.value in grants


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return result


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _same_revision(value: Any, expected: int) -> bool:
    try:
        return int(value) == int(expected)
    except (TypeError, ValueError, OverflowError):
        return False


def _verified_readback(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping) or job.get("status") != "succeeded":
        return False
    for effect in job.get("effects", []):
        if not isinstance(effect, Mapping):
            continue
        if (
            effect.get("receipt_kind") == "readback"
            and effect.get("status") == "succeeded"
            and effect.get("reconciled") is True
        ):
            return True
    return False


def _owner_matches(job: Mapping[str, Any] | None, principal_id: str, session_id: str) -> bool:
    if not isinstance(job, Mapping):
        return False
    authority = job.get("declared_authority")
    if not isinstance(authority, Mapping):
        authority = {}
    return (
        str(authority.get("goal_owner_principal_id") or authority.get("principal") or "") == principal_id
        and str(authority.get("goal_owner_session_id") or authority.get("session_id") or "") == session_id
    )


def _safe_invocation_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RoutineError("invocation_uuid_invalid", status_code=422) from exc
    return str(parsed)


def _child_job_id(invocation_uuid: str, step_id: str) -> str:
    """Return the stable UUIDv5 job identity for one fixed routine step."""

    namespace = uuid.UUID(invocation_uuid)
    child_uuid = uuid.uuid5(namespace, f"seraph:guardian-routine:{step_id}")
    return f"routine-child:{child_uuid.hex}"


def _job_checkpoint(job: Mapping[str, Any] | None, checkpoint_id: str) -> dict[str, Any] | None:
    if not isinstance(job, Mapping):
        return None
    for item in reversed(job.get("checkpoints", []) or []):
        if not isinstance(item, Mapping) or item.get("checkpoint_id") != checkpoint_id:
            continue
        payload = item.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else dict(item)
    return None


def _job_checkpoint_any(
    job: Mapping[str, Any] | None,
    checkpoint_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    """Return the newest checkpoint from a small, explicitly bound set."""

    if not isinstance(job, Mapping):
        return None
    wanted = set(checkpoint_ids)
    for item in reversed(job.get("checkpoints", []) or []):
        if not isinstance(item, Mapping) or item.get("checkpoint_id") not in wanted:
            continue
        payload = item.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else dict(item)
    return None


def _publication_binding_checkpoint(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Read either the pre-prepare adoption fence or its finalized binding."""

    return _job_checkpoint_any(
        job,
        ("routine-child:adoption_pending", "routine-child:prepared"),
    )


def _expected_publication_job_id(owner_principal_id: str, operation_uuid: str) -> str:
    """Derive M3's deterministic job id before calling its prepare boundary."""

    return f"ghfollow_{_operation_id(owner_principal_id, uuid.UUID(operation_uuid)).hex}"


class RoutineService:
    """Small service over existing package, watch, approval, and job stores."""

    async def _routine(self, routine_id: str, owner_principal_id: str) -> GuardianRoutine:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianRoutine).where(
                        GuardianRoutine.id == routine_id,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RoutineError("routine_not_found", status_code=404)
            db.expunge(row)
            return row

    @staticmethod
    def _require_routine_owner_session(routine: GuardianRoutine, owner_session_id: str) -> None:
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)

    async def _require_active_routine(
        self,
        routine_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        expected_revision: int,
    ) -> GuardianRoutine:
        """Re-read the canonical routine before any child admission/recovery."""

        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal", status_code=409)
        if routine.state != "active" or int(routine.revision or 0) != int(expected_revision):
            raise RoutineError("routine_not_active_or_stale")
        return routine

    async def _version(self, routine_id: str, version: int) -> GuardianRoutineVersion:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianRoutineVersion).where(
                        GuardianRoutineVersion.routine_id == routine_id,
                        GuardianRoutineVersion.version == version,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RoutineError("routine_version_not_found", status_code=404)
            db.expunge(row)
            return row

    @staticmethod
    def _version_json(version: GuardianRoutineVersion) -> dict[str, Any]:
        return {
            "id": version.id,
            "routine_id": version.routine_id,
            "version": version.version,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "installed_package_digest": version.installed_package_digest,
            "source_provenance": _load(version.source_provenance_json, {}),
            "source_repository": version.source_repository,
            "source_action": version.source_action,
            "source_issue_number": version.source_issue_number,
            "created_at": version.created_at.isoformat(),
            "installed_at": version.installed_at.isoformat() if version.installed_at else None,
        }

    async def read(self, routine_id: str, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        async with db_engine.get_session() as db:
            versions = (
                await db.execute(
                    select(GuardianRoutineVersion)
                    .where(GuardianRoutineVersion.routine_id == routine.id)
                    .order_by(GuardianRoutineVersion.version)
                )
            ).scalars().all()
            for item in versions:
                db.expunge(item)
        package = self._package_readback(owner_principal_id, owner_session_id)
        return {
            "id": routine.id,
            "owner_principal_id": routine.owner_principal_id,
            "state": routine.state,
            "revision": routine.revision,
            "current_version": routine.current_version,
            "name": routine.name,
            "versions": [self._version_json(item) for item in versions],
            "package": package,
        }

    async def list(self, *, owner_principal_id: str, owner_session_id: str) -> list[dict[str, Any]]:
        async with db_engine.get_session() as db:
            rows = (
                await db.execute(
                    select(GuardianRoutine)
                    .where(GuardianRoutine.owner_principal_id == owner_principal_id)
                    .where(GuardianRoutine.owner_session_id == owner_session_id)
                    .order_by(GuardianRoutine.updated_at.desc())
                )
            ).scalars().all()
            ids = [row.id for row in rows]
            for row in rows:
                db.expunge(row)
        result = []
        for routine_id in ids:
            result.append(await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id))
        return result

    @staticmethod
    def _package_readback(owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        try:
            lifecycle = CapabilityPackLifecycle()
            status = lifecycle.status(PACKAGE_ID, owner_principal_id=owner_principal_id, session_id=owner_session_id)
            active = status.get("active") if isinstance(status, Mapping) else None
            return {
                "status": str(active.get("status") if isinstance(active, Mapping) else "not_reviewed"),
                "digest": active.get("digest") if isinstance(active, Mapping) else None,
                "review_id": active.get("review_id") if isinstance(active, Mapping) else None,
            }
        except Exception as exc:
            return {"status": "blocked", "reason": type(exc).__name__}

    async def _source_proof(
        self,
        *,
        source_watch_job_id: str,
        source_packet_id: str,
        source_m3_job_id: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], GuardianDecisionPacket]:
        source_watch = await durable_job_repository.get_job(source_watch_job_id)
        m3_job = await durable_job_repository.get_job(source_m3_job_id)
        if not isinstance(source_watch, Mapping) or str(source_watch.get("job_kind") or "") != "guardian_source_watch":
            raise RoutineError("source_watch_job_kind_mismatch")
        if not isinstance(m3_job, Mapping) or str(m3_job.get("job_kind") or "") != GITHUB_FOLLOWTHROUGH_JOB_KIND:
            raise RoutineError("source_m3_job_kind_mismatch")
        if not _verified_readback(source_watch) or not _verified_readback(m3_job):
            raise RoutineError("source_runs_not_verified")
        # Both source jobs are part of the proof.  Accepting one owner match
        # would let a caller combine another operator's observation with its
        # own publication receipt.
        if not _owner_matches(source_watch, owner_principal_id, owner_session_id) or not _owner_matches(m3_job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_owner_mismatch", status_code=404)
        async with db_engine.get_session() as db:
            packet = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == source_packet_id))
            ).scalars().first()
            if packet is None:
                raise RoutineError("source_packet_not_found", status_code=404)
            db.expunge(packet)
        if packet.run_identity != source_watch_job_id or packet.status != "succeeded" or packet.verification_status != "passed":
            raise RoutineError("source_packet_not_verified")
        watch = await source_watch_service.get_watch(packet.source_watch_id, owner_principal_id=owner_principal_id)
        if watch is None or watch.get("goal_id") != packet.goal_id:
            raise RoutineError("source_watch_not_owned", status_code=404)
        if int(watch.get("goal_revision", 0)) != int(packet.goal_revision) or int(watch.get("plan_revision", 0)) != int(packet.plan_revision):
            raise RoutineError("source_watch_revision_stale")
        dossier_sha = str(packet.dossier_sha256 or _sha(packet.proposal_text))
        if str(packet.dossier_artifact_id or "").strip() == "":
            raise RoutineError("source_dossier_artifact_missing")
        dossier_path = str(packet.dossier_path or "").strip()
        if not dossier_path:
            raise RoutineError("source_dossier_artifact_missing")
        try:
            resolved_dossier = _safe_resolve(dossier_path)
            dossier_text, truncated = _read_workspace_text_bounded(resolved_dossier, max_bytes=72 * 1024)
        except (OSError, ValueError) as exc:
            raise RoutineError("source_dossier_artifact_unreadable") from exc
        if truncated or _sha(dossier_text) != dossier_sha:
            raise RoutineError("source_dossier_digest_mismatch")
        # The prepared M3 artifact is the immutable input authority. Durable
        # result projections intentionally redact the full publication body,
        # so routine proof must read the exact prepared payload and bind every
        # source identity before accepting a reusable destination.
        try:
            prepared = await GitHubFollowthroughService()._read_prepared(m3_job)
        except (GitHubFollowthroughError, OSError, ValueError, TypeError) as exc:
            raise RoutineError("source_m3_input_binding_missing") from exc
        if (
            str(prepared.job_id) != str(source_m3_job_id)
            or str(prepared.owner_principal_id) != str(owner_principal_id)
            or str(prepared.owner_session_id) != str(owner_session_id)
            or str(prepared.dossier_artifact_id) != str(packet.dossier_artifact_id)
            or str(prepared.dossier_sha256) != dossier_sha
            or str(prepared.source_watch_id) != str(packet.source_watch_id)
            or str(prepared.goal_id) != str(packet.goal_id)
            or not _same_revision(prepared.goal_revision, packet.goal_revision)
            or not _same_revision(prepared.plan_revision, packet.plan_revision)
        ):
            raise RoutineError("source_m3_dossier_binding_missing")
        m3_authority = m3_job.get("declared_authority") if isinstance(m3_job.get("declared_authority"), Mapping) else {}
        if (
            str(m3_authority.get("source_watch_id") or "") != str(packet.source_watch_id)
            or str(m3_authority.get("dossier_artifact_id") or "") != str(packet.dossier_artifact_id)
            or str(m3_authority.get("dossier_sha256") or "") != dossier_sha
        ):
            raise RoutineError("source_m3_authority_binding_missing")
        fixed_repository = str(prepared.repository).strip()
        fixed_action = str(prepared.action).strip()
        # A create_issue operation has no pre-existing issue number. Keep a
        # typed target marker so later invocations cannot retarget the action.
        fixed_target = str(
            prepared.issue_number if prepared.issue_number is not None else ("create_issue" if fixed_action == "create_issue" else "")
        ).strip()
        if not fixed_repository or fixed_action not in {"create_issue", "create_comment"} or not fixed_target:
            raise RoutineError("source_m3_destination_binding_missing")
        provenance = {
            "source_watch_job_id": source_watch_job_id,
            "source_packet_id": source_packet_id,
            "source_m3_job_id": source_m3_job_id,
            "source_watch_id": packet.source_watch_id,
            "goal_id": packet.goal_id,
            "goal_revision": packet.goal_revision,
            "plan_revision": packet.plan_revision,
            "dossier_artifact_id": packet.dossier_artifact_id,
            "dossier_sha256": dossier_sha,
            "m3_result_digest": m3_job.get("result", {}).get("digest") if isinstance(m3_job.get("result"), Mapping) else None,
            "source_repository": fixed_repository,
            "source_action": fixed_action,
            "source_target": fixed_target,
        }
        return provenance, packet

    async def _admit_user_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        idempotency_key: str,
        inputs: Mapping[str, Any],
        authority: Mapping[str, Any],
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str,
        goal_revision: int,
        plan_revision: int | None,
        candidate_id: str | None,
    ) -> dict[str, Any]:
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=DurableJobIdentity(
                    job_id=job_id,
                    owner_kind="user",
                    owner_principal_id=owner_principal_id,
                    job_kind=job_kind,
                    capability_version=ROUTINE_CAPABILITY_VERSION,
                    idempotency_scope=job_kind,
                    idempotency_key=idempotency_key,
                ),
                inputs=dict(inputs),
                session_id=owner_session_id,
                operator_session_id=owner_session_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                plan_revision=plan_revision,
                candidate_id=candidate_id,
                priority=50,
                declared_authority=dict(authority),
                deadline_at=_now() + timedelta(seconds=ROUTINE_DEADLINE_SECONDS),
                max_attempts=1,
                service_id=None,
                budget_microusd=0,
            )
        )
        if admitted.get("status") == "accepted":
            admitted = await durable_job_repository.queue_job(job_id, expected_revision=admitted.get("revision"))
            admitted = await durable_job_repository.claim_job(
                job_id,
                owner=f"routine:{job_id}",
                expected_revision=admitted.get("revision"),
                expected_fencing_token=(admitted.get("lease") or {}).get("fencing_token"),
                lease_seconds=ROUTINE_DEADLINE_SECONDS,
            )
        return admitted

    async def _admit_child_job(
        self,
        parent: Mapping[str, Any],
        *,
        child_id: str,
        step_id: str,
        inputs: Mapping[str, Any],
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Admit one fixed child under the currently leased routine parent.

        ``parent_fencing_token`` is part of the canonical durable runtime
        contract.  Every child state write is therefore rejected once the
        parent lease is lost or the parent is paused/recovered by another
        worker.  The helper has no generic tool or argument dispatch surface.
        """

        parent_id = str(parent.get("job_id") or "")
        parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        parent_fence = int(parent_lease.get("fencing_token") or 0)
        parent_owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
        owner_principal_id = str(parent_owner.get("principal_id") or "")
        session_id = str(parent.get("session_id") or parent.get("operator_session_id") or "")
        if not parent_id or parent.get("status") != "running" or parent_fence <= 0:
            raise RoutineError("routine_parent_lease_required")
        if not owner_principal_id or not session_id:
            raise RoutineError("routine_parent_owner_missing")
        parent_authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        routine_id = str(authority.get("routine_id") or parent_authority.get("routine_id") or "")
        try:
            routine_revision = int(authority.get("routine_revision") or parent_authority.get("routine_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise RoutineError("routine_revision_binding_invalid") from exc
        if not routine_id or routine_revision <= 0:
            raise RoutineError("routine_child_routine_binding_missing")
        # This read is deliberately inside the admission helper, immediately
        # before the durable child CAS.  pause/revoke flips the routine row
        # first, so a post-CAS child cannot appear after cancellation begins.
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=session_id,
            expected_revision=routine_revision,
        )
        child_authority = {
            **dict(authority),
            "routine_id": routine_id,
            "routine_revision": routine_revision,
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": session_id,
            "parent_job_id": parent_id,
            "parent_fencing_token": parent_fence,
            "step_id": step_id,
            "routine_invocation_job_id": parent_id,
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        existing = await durable_job_repository.get_job(child_id)
        if existing is not None:
            if (
                existing.get("parent_job_id") != parent_id
                or str(existing.get("job_kind") or "") != f"routine_{step_id}_child"
            ):
                raise RoutineError("routine_child_binding_conflict")
            if int(existing.get("parent_fencing_token") or 0) != parent_fence:
                # A blocked/terminal child is an immutable historical receipt
                # after parent recovery acquires a new fence.  It may be read
                # for idempotent recovery, but it must never be written under
                # the new parent lease.  A live child with an old fence is a
                # hard conflict and cannot be resumed.
                if existing.get("status") in {
                    "blocked",
                    "awaiting_approval",
                    "succeeded",
                    "degraded",
                    "cancelled",
                    "unknown_external_effect",
                    "cost_liability",
                    "failed",
                }:
                    return existing
                raise RoutineError("routine_child_parent_fence_stale")
            if existing.get("status") == "accepted":
                existing = await durable_job_repository.queue_job(child_id, expected_revision=existing.get("revision"))
            if existing.get("status") == "queued":
                existing = await durable_job_repository.claim_job(
                    child_id,
                    owner=f"routine-child:{child_id}",
                    expected_revision=existing.get("revision"),
                    expected_fencing_token=(existing.get("lease") or {}).get("fencing_token"),
                    lease_seconds=ROUTINE_DEADLINE_SECONDS,
                )
            return existing
        parent_deadline = parent.get("deadline_at")
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=DurableJobIdentity(
                    job_id=child_id,
                    owner_kind="user",
                    owner_principal_id=owner_principal_id,
                    job_kind=f"routine_{step_id}_child",
                    capability_version=ROUTINE_CAPABILITY_VERSION,
                    idempotency_scope="guardian-routine-child",
                    idempotency_key=f"{parent_id}:{step_id}",
                ),
                inputs=dict(inputs),
                session_id=session_id,
                operator_session_id=session_id,
                parent_job_id=parent_id,
                parent_fencing_token=parent_fence,
                goal_id=parent.get("goal_id"),
                goal_revision=parent.get("goal_revision"),
                plan_revision=parent.get("plan_revision"),
                priority=int(parent.get("priority") or 50),
                declared_authority=child_authority,
                deadline_at=parent_deadline or (_now() + timedelta(seconds=ROUTINE_DEADLINE_SECONDS)),
                max_attempts=1,
                budget_microusd=0,
                run_fingerprint=_sha(_dump({"parent": parent_id, "step": step_id, "inputs": dict(inputs)})),
            )
        )
        if admitted.get("status") == "accepted":
            queued = await durable_job_repository.queue_job(child_id, expected_revision=admitted.get("revision"))
            admitted = await durable_job_repository.claim_job(
                child_id,
                owner=f"routine-child:{child_id}",
                expected_revision=queued.get("revision"),
                expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
                lease_seconds=ROUTINE_DEADLINE_SECONDS,
            )
        try:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=session_id,
                expected_revision=routine_revision,
            )
        except RoutineError:
            current_child = await durable_job_repository.get_job(child_id) or admitted
            child_lease = current_child.get("lease") if isinstance(current_child.get("lease"), Mapping) else {}
            if current_child.get("status") in {"running", "queued", "accepted"}:
                try:
                    await durable_job_repository.cancel_job(
                        child_id,
                        owner=str(child_lease.get("owner") or "") or None,
                        fencing_token=int(child_lease.get("fencing_token") or 0) or None,
                        expected_revision=current_child.get("revision"),
                        reason="routine_state_changed_before_child_start",
                    )
                except Exception:
                    pass
            raise
        return admitted

    async def _record_child_checkpoint(
        self,
        child: Mapping[str, Any],
        *,
        checkpoint_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        return await durable_job_repository.record_checkpoint(
            str(child["job_id"]),
            checkpoint_id=checkpoint_id,
            state={"step_id": (child.get("declared_authority") or {}).get("step_id"), **dict(payload)},
            checkpoint_payload=dict(payload),
            safe=True,
            owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            expected_revision=int(child.get("revision") or 0),
        )

    async def _settle_child(
        self,
        child: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record a child result before changing its durable state."""

        current = await durable_job_repository.get_job(str(child["job_id"])) or dict(child)
        if current.get("status") != "running":
            return current
        lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        revision = int(current.get("revision") or 0)
        safe_result = {
            key: result.get(key)
            for key in ("status", "reason_code", "packet_id", "job_id", "approval_id", "m1_job_id")
            if result.get(key) is not None
        }
        digest = _sha(_dump({"status": status, "reason": reason, **safe_result}))
        effect = await durable_job_repository.record_effect(
            str(child["job_id"]),
            effect_type="guardian_routine_child",
            target_path=f"routine-child:{child['job_id']}",
            target_digest=digest,
            status="succeeded" if status in {"succeeded", "degraded"} else "blocked",
            details={"step_id": (current.get("declared_authority") or {}).get("step_id"), "reason": reason, **safe_result},
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(effect.get("revision") or revision)
        if status in {"succeeded", "degraded"}:
            readback = await durable_job_repository.record_readback(
                str(child["job_id"]),
                target_path=f"routine-child:{child['job_id']}",
                effect_id=(effect.get("receipt") or {}).get("effect_id"),
                effect_type="guardian_routine_child",
                target_digest=digest,
                status="succeeded",
                details={"verified": True, "reason": reason, **safe_result},
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
            revision = int(readback.get("revision") or revision)
        return await durable_job_repository.transition_job(
            str(child["job_id"]),
            status,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
            reason=reason,
            result={"step_id": (current.get("declared_authority") or {}).get("step_id"), **safe_result, "learning": "no_learning"},
            result_summary=reason,
        )

    async def _owned_child(
        self,
        child_job_id: str,
        *,
        step_id: str,
        context: RoutineStepContext,
    ) -> dict[str, Any]:
        child = await durable_job_repository.get_job(child_job_id)
        if child is None:
            raise RoutineError("routine_child_not_found", status_code=404)
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        owner = child.get("owner") if isinstance(child.get("owner"), Mapping) else {}
        if (
            str(authority.get("step_id") or "") != step_id
            or str(authority.get("routine_invocation_job_id") or "") != str(authority.get("parent_job_id") or "")
            or not str(context.runtime_job_id or "").strip()
            or str(authority.get("parent_job_id") or "") != str(context.runtime_job_id)
            or str(owner.get("principal_id") or "") != context.principal_id
            or str(child.get("session_id") or "") != context.session_id
            or str(lease.get("owner") or "") != context.lease_owner
            or int(lease.get("fencing_token") or 0) != int(context.fencing_token)
            or child.get("status") != "running"
        ):
            raise RoutineError("routine_child_lease_or_step_invalid")
        # The child lease alone is insufficient: a worker can retain it after
        # the routine parent has been recovered.  Read the canonical parent
        # immediately before the capability call and require the immutable
        # child binding to name that parent's current live fence.
        parent = await durable_job_repository.get_job(str(context.runtime_job_id))
        parent_lease = parent.get("lease") if isinstance(parent, Mapping) and isinstance(parent.get("lease"), Mapping) else {}
        if (
            not isinstance(parent, Mapping)
            or parent.get("status") != "running"
            or int(child.get("parent_fencing_token") or 0) <= 0
            or int(child.get("parent_fencing_token") or 0) != int(parent_lease.get("fencing_token") or 0)
        ):
            raise RoutineError("routine_child_parent_fence_stale")
        routine_id = str(authority.get("routine_id") or "")
        routine_revision = int(authority.get("routine_revision") or 0)
        if routine_id and routine_revision:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=context.principal_id,
                owner_session_id=context.session_id,
                expected_revision=routine_revision,
            )
        return child

    async def _hold_approval(self, job: Mapping[str, Any], *, tool_name: str, summary: str, owner_principal_id: str, owner_session_id: str) -> str:
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        fingerprint = fingerprint_tool_call(tool_name, {"job_id": job.get("job_id"), "authority_digest": job.get("authority_digest")})
        details = {
            "approval_operator_principal_id": owner_principal_id,
            "approval_owner_principal_id": owner_principal_id,
            "approval_owner_operator_session_id": owner_session_id,
            "operator_principal_id": owner_principal_id,
            "operator_session_id": owner_session_id,
            "approval_conversation_id": owner_session_id,
            "durable_job_id": job.get("job_id"),
            "durable_owner_kind": "user",
            "durable_owner_principal_id": owner_principal_id,
            "durable_service_id": None,
            "durable_authority_digest": job.get("authority_digest"),
            "durable_goal_id": job.get("goal_id"),
            "durable_goal_revision": job.get("goal_revision"),
            "durable_plan_revision": job.get("plan_revision"),
            "durable_capability_version": job.get("capability_version"),
            "durable_budget_digest": job.get("budget_digest"),
            "candidate_id": job.get("candidate_id"),
            "approval_expires_at": (_now() + timedelta(seconds=APPROVAL_TTL_SECONDS)).timestamp(),
            "authority_scope": dict(authority),
        }
        approval = await approval_repository.get_or_create_pending(
            session_id=owner_session_id,
            tool_name=tool_name,
            risk_level="high",
            summary=summary,
            fingerprint=fingerprint,
            details=details,
        )
        lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
        bound = await durable_job_repository.bind_approval_id(
            str(job["job_id"]),
            approval.id,
            owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            expected_revision=int(job.get("revision") or 0),
        )
        await approval_repository.update_pending_details(
            approval.id,
            owner_principal_id=owner_principal_id,
            operator_session_id=owner_session_id,
            updates={
                "durable_authority_digest": bound.get("authority_digest"),
                "authority_digest": bound.get("authority_digest"),
                "approval_expires_at": approval.expires_at.timestamp() if approval.expires_at else None,
            },
        )
        bound_lease = bound.get("lease") if isinstance(bound.get("lease"), Mapping) else {}
        await durable_job_repository.transition_job(
            str(job["job_id"]),
            "awaiting_approval",
            owner=str(bound_lease.get("owner") or ""),
            fencing_token=int(bound_lease.get("fencing_token") or 0),
            expected_revision=int(bound.get("revision") or 0),
            reason="routine_approval_required",
        )
        return str(approval.id)

    async def _cancel_m3_job_safely(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        m3_job_id: str,
    ) -> dict[str, Any]:
        """Ask M3 to cancel its reservation and return a visible receipt.

        M4 cannot claim a successful pause/revoke while M3 still owns an
        approval or dispatch reservation.  Cancellation errors are therefore
        returned as a bounded operator action instead of being swallowed.
        """

        if not owner_principal_id or not owner_session_id or not m3_job_id:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "m3_child_binding_missing",
                "operator_action": "reconcile_or_cancel",
            }
        try:
            result = await GitHubFollowthroughService().cancel(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                job_id=m3_job_id,
            )
        except Exception as exc:
            # M3 deliberately keeps unknown or in-flight effects for its
            # reconciliation path.  M4 must never overwrite that receipt.
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": type(exc).__name__,
                "operator_action": "reconcile_or_cancel",
            }
        status = str(result.get("status") or "blocked") if isinstance(result, Mapping) else "blocked"
        # The adapter may return ``cancelled`` after preserving an intent or
        # dispatched effect.  That is an unresolved external liability, not a
        # safe child cancellation.  Re-read the canonical M3 row and its
        # effect ledger before allowing M4 to settle its wrapper.
        try:
            canonical = await durable_job_repository.get_job(m3_job_id)
        except Exception as exc:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": type(exc).__name__,
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if not isinstance(canonical, Mapping):
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "m3_job_missing_after_cancel",
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        canonical_status = str(canonical.get("status") or status)
        effects = [item for item in canonical.get("effects") or [] if isinstance(item, Mapping)]
        unresolved_external = any(
            item.get("effect_type") == "github_publication"
            and item.get("status") in {"intent", "dispatched", "unknown"}
            for item in effects
        )
        release_pending = (
            isinstance(result, Mapping)
            and isinstance(result.get("connection_release"), Mapping)
            and result["connection_release"].get("status") == "pending"
        )
        if unresolved_external or release_pending or canonical_status in {
            "unknown_external_effect",
            "cost_liability",
        }:
            return {
                "ok": False,
                "status": canonical_status,
                "reason_code": "m3_external_effect_unresolved",
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if canonical_status in {"cancelled", "succeeded"}:
            return {"ok": True, "status": canonical_status, "m3_job_id": m3_job_id}
        return {
            "ok": False,
            "status": canonical_status,
            "reason_code": "m3_cancel_not_settled",
            "m3_job_id": m3_job_id,
            "operator_action": "reconcile_or_cancel",
        }

    async def _cancel_stale_child(
        self,
        child: Mapping[str, Any],
        *,
        parent: Mapping[str, Any],
        step_id: str,
        reason_code: str = "routine_child_parent_fence_stale",
        operator_action: str = "restart_routine_invocation",
        cancel_external: bool = True,
    ) -> dict[str, Any]:
        """Stop a child whose parent fence is no longer authoritative.

        Child transitions other than cancellation are parent-fenced by the
        durable runtime.  Cancellation is intentionally the one terminal
        escape hatch, so an old worker cannot keep a stale child live.  M3 is
        asked to settle first; its canonical effect receipt remains the
        authority when an external publication may already exist.
        """

        child_id = str(child.get("job_id") or "")
        owner = child.get("owner") if isinstance(child.get("owner"), Mapping) else {}
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        m3_job_id = ""
        m3_cancellation: dict[str, Any] | None = None
        if step_id == "github_followthrough":
            binding = _publication_binding_checkpoint(child) or {}
            m3_job_id = str(binding.get("m3_job_id") or "")
            if m3_job_id and cancel_external:
                child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
                m3_cancellation = await self._cancel_m3_job_safely(
                    owner_principal_id=str(owner.get("principal_id") or ""),
                    owner_session_id=str(
                        child.get("operator_session_id")
                        or child.get("session_id")
                        or child_authority.get("session_id")
                        or ""
                    ),
                    m3_job_id=m3_job_id,
                )

        child_result: Mapping[str, Any] | None = None
        status = str(child.get("status") or "")
        if status in {"accepted", "queued", "running", "awaiting_approval", "blocked"} and child_id:
            try:
                child_result = await durable_job_repository.cancel_job(
                    child_id,
                    owner=str(lease.get("owner") or "") or None,
                    fencing_token=int(lease.get("fencing_token") or 0) or None,
                    expected_revision=child.get("revision"),
                    reason=reason_code,
                )
            except Exception as exc:
                child_result = None
                return {
                    "status": "blocked",
                    "job_id": parent.get("job_id"),
                    "child_job_id": child_id,
                    "m3_job_id": m3_job_id or None,
                    "reason_code": "routine_stale_child_cancel_failed",
                    "recovery": "reconcile_or_cancel",
                    "operator_action": "reconcile_or_cancel",
                    "cancel_error": type(exc).__name__,
                    "operator_visible": True,
                    "learning": "no_learning",
                }

        m3_unresolved = bool(m3_cancellation and not m3_cancellation.get("ok"))
        result_status = str(child_result.get("status") or "cancelled") if child_result else status or "cancelled"
        recovery = "reconcile_or_cancel" if m3_unresolved else operator_action
        outcome = {
            "status": "blocked" if m3_unresolved else result_status,
            "job_id": parent.get("job_id"),
            "child_job_id": child_id,
            "child_status": result_status,
            "m3_job_id": m3_job_id or None,
            "reason_code": (
                str(m3_cancellation.get("reason_code") or "m3_external_effect_unresolved")
                if m3_unresolved and m3_cancellation
                else reason_code
            ),
            "recovery": recovery,
            "operator_action": recovery,
            "operator_visible": True,
            "learning": "no_learning",
        }
        latest = await durable_job_repository.get_job(str(parent.get("job_id") or "")) or dict(parent)
        if latest.get("status") == "running":
            parent_lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
            try:
                await durable_job_repository.transition_job(
                    str(parent["job_id"]),
                    "blocked",
                    owner=str(parent_lease.get("owner") or "") or None,
                    fencing_token=int(parent_lease.get("fencing_token") or 0) or None,
                    expected_revision=latest.get("revision"),
                    reason=str(outcome["reason_code"]),
                    result={
                        "child_job_id": child_id,
                        "m3_job_id": m3_job_id or None,
                        "recovery": recovery,
                        "operator_action": recovery,
                        "learning": "no_learning",
                    },
                    result_summary="stale routine child was stopped; operator recovery is required",
                )
            except Exception:
                # The current parent receipt is still returned below.  A
                # competing recovery owns the canonical transition and will
                # expose its own fence result to the operator.
                pass
        return outcome

    async def _list_routine_jobs(
        self,
        routine_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read every typed routine job for one persisted owner/session.

        ``DurableJobRepository.list_jobs`` intentionally caps operator pages at
        100. Pause/revoke is a fencing operation, so it must use a scoped
        durable query that cannot silently leave the 101st child live.
        """

        if not owner_principal_id or not owner_session_id:
            return
        page_size = 100
        offset = 0
        while True:
            async with db_engine.get_session() as db:
                rows = (
                    await db.execute(
                        select(WorkflowRunState)
                        .where(
                            WorkflowRunState.record_schema_version >= 2,
                            WorkflowRunState.owner_principal_id == owner_principal_id,
                            WorkflowRunState.operator_session_id == owner_session_id,
                            WorkflowRunState.job_kind.like("routine_%"),
                        )
                        .order_by(WorkflowRunState.updated_at.desc(), WorkflowRunState.run_identity.desc())
                        .offset(offset)
                        .limit(page_size)
                    )
                ).scalars().all()
                if not rows:
                    return
                page_jobs: list[dict[str, Any]] = []
                for row in rows:
                    authority = _load(row.declared_authority_json, {})
                    if not isinstance(authority, Mapping) or str(authority.get("routine_id") or "") != routine_id:
                        continue
                    db.expunge(row)
                    page_jobs.append(_serialize(row))
                page_complete = len(rows) < page_size
            for job in page_jobs:
                yield job
            if page_complete:
                return
            offset += len(rows)

    async def _cancel_pending_jobs(
        self,
        routine_id: str,
        *,
        reason: str,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cancel still-admissible children and return any unsettled receipts."""

        async def _job_stream() -> AsyncIterator[dict[str, Any]]:
            if owner_principal_id and owner_session_id:
                async for job in self._list_routine_jobs(
                    routine_id,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                ):
                    yield job
                return
            # Keep an explicit compatibility path for old internal callers;
            # pause/revoke and rollback always provide both persisted owner
            # bindings and therefore use the paged scoped query above.
            for job in await durable_job_repository.list_jobs(limit=100):
                yield job

        # Snapshot the complete scoped set before mutating any row.  Cancelling
        # a job changes ``updated_at`` and therefore changes the offset based
        # query used by ``_list_routine_jobs``; streaming and mutating in the
        # same pass can skip every other child after the first page.
        jobs = [job async for job in _job_stream()]
        failures: list[dict[str, Any]] = []
        parent_jobs: list[dict[str, Any]] = []
        for job in jobs:
            authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
            if str(authority.get("routine_id") or "") != routine_id:
                continue
            child_job_id = str(job.get("job_id") or "")
            step_id = str(authority.get("step_id") or "")
            if str(job.get("job_kind") or "") == "routine_invocation" and not step_id:
                # Defer the parent until every child cancellation is known.
                # A failed M3 cancellation must leave the parent visibly
                # recoverable rather than making a terminal success/cancel
                # receipt hide the unresolved external reservation.
                parent_jobs.append(job)
                continue
            m3_cancel_failed = False
            # The M3 job owns the external approval/connection reservation.
            # A routine pause/revoke must ask that service to cancel its
            # recorded child; an M4 metadata transition alone would leave a
            # live publication approval behind.
            if step_id == "github_followthrough":
                prepared = _publication_binding_checkpoint(job) or {}
                m3_job_id = str(prepared.get("m3_job_id") or authority.get("m3_job_id") or "")
                cancellation = await self._cancel_m3_job_safely(
                    owner_principal_id=str((job.get("owner") or {}).get("principal_id") or ""),
                    owner_session_id=str(
                        job.get("operator_session_id")
                        or job.get("session_id")
                        or authority.get("session_id")
                        or ""
                    ),
                    m3_job_id=m3_job_id,
                )
                if not cancellation.get("ok"):
                    m3_cancel_failed = True
                    failures.append(
                        {
                            "job_id": child_job_id,
                            "step_id": step_id,
                            "m3_job_id": m3_job_id or None,
                            "status": cancellation.get("status") or "blocked",
                            "reason_code": cancellation.get("reason_code") or "m3_cancel_failed",
                            "operator_action": cancellation.get("operator_action") or "reconcile_or_cancel",
                        }
                    )
            # Leave the M4 child in its durable pending/blocked state while
            # M3 still owns an unresolved reservation. Cancelling only the
            # wrapper would hide the operator action needed at the canonical
            # external-effect boundary.
            if m3_cancel_failed:
                continue
            if step_id == "guardian_watch_run":
                # M1 owns the watch fence and its approval-held job. Revoke
                # must release that canonical reservation before the child is
                # cancelled, otherwise a paused routine can strand a watch.
                m1_cancel_failed = False
                dispatch = _job_checkpoint(job, "routine-child:dispatch_started") or {}
                m1_job_id = str(dispatch.get("m1_job_id") or "")
                if not m1_job_id:
                    for effect in job.get("effects", []) or []:
                        details = effect.get("details") if isinstance(effect, Mapping) else None
                        if isinstance(details, Mapping) and details.get("m1_job_id"):
                            m1_job_id = str(details.get("m1_job_id"))
                            break
                watch_id = str(authority.get("source_watch_id") or "")
                if m1_job_id and watch_id:
                    try:
                        watch = await source_watch_service.get_watch(
                            watch_id,
                            owner_principal_id=owner_principal_id or str(job.get("owner", {}).get("principal_id") or ""),
                        )
                        m1_job = await durable_job_repository.get_job(m1_job_id)
                        if watch and m1_job and str(m1_job.get("status") or "") in {
                            "accepted", "queued", "running", "awaiting_approval", "blocked"
                        }:
                            active_fence = int(watch.get("active_job_fence") or 0)
                            if active_fence:
                                await source_watch_service.cancel_watch_job(
                                    watch_id=watch_id,
                                    job_id=m1_job_id,
                                    expected_plan_revision=int(watch.get("plan_revision") or 0),
                                    expected_fencing_token=active_fence,
                                    owner_principal_id=owner_principal_id or str(job.get("owner", {}).get("principal_id") or ""),
                                    owner_session_id=str(authority.get("session_id") or ""),
                                )
                    except Exception as exc:
                        # Keep the child and M1 receipts for the explicit
                        # recovery route if a concurrent worker owns the fence.
                        m1_cancel_failed = True
                        failures.append(
                            {
                                "job_id": child_job_id,
                                "step_id": step_id,
                                "status": "blocked",
                                "reason_code": type(exc).__name__,
                                "operator_action": "recover_or_cancel",
                            }
                        )
                if m1_cancel_failed:
                    continue
            status = str(job.get("status") or "")
            if status not in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
                continue
            lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
            try:
                await durable_job_repository.cancel_job(
                    child_job_id,
                    owner=str(lease.get("owner")) if status == "running" else None,
                    fencing_token=int(lease.get("fencing_token")) if status == "running" else None,
                    expected_revision=int(job.get("revision") or 0),
                    reason=reason,
                )
            except Exception as exc:
                # A concurrent claim or an uncertain effect is left for the
                # canonical runtime recovery path rather than being overwritten.
                failures.append(
                    {
                        "job_id": child_job_id,
                        "step_id": step_id,
                        "status": "blocked",
                        "reason_code": type(exc).__name__,
                        "operator_action": "reconcile_or_cancel" if step_id == "github_followthrough" else "recover_or_cancel",
                    }
                )
        for parent in parent_jobs:
            parent_id = str(parent.get("job_id") or "")
            parent_status = str(parent.get("status") or "")
            if parent_status not in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
                continue
            lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
            if failures:
                if parent_status == "blocked":
                    continue
                try:
                    await durable_job_repository.transition_job(
                        parent_id,
                        "blocked",
                        owner=str(lease.get("owner")) if parent_status == "running" else None,
                        fencing_token=int(lease.get("fencing_token")) if parent_status == "running" else None,
                        expected_revision=int(parent.get("revision") or 0),
                        reason="routine_child_cancellation_incomplete",
                        result={"recovery": "reconcile_or_cancel", "operator_action": "reconcile_or_cancel", "learning": "no_learning"},
                        result_summary="routine child cancellation is incomplete; reconcile before resuming",
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "job_id": parent_id,
                            "step_id": "routine_invocation",
                            "status": "blocked",
                            "reason_code": type(exc).__name__,
                            "operator_action": "reconcile_or_cancel",
                        }
                    )
                continue
            try:
                await durable_job_repository.cancel_job(
                    parent_id,
                    owner=str(lease.get("owner")) if parent_status == "running" else None,
                    fencing_token=int(lease.get("fencing_token")) if parent_status == "running" else None,
                    expected_revision=int(parent.get("revision") or 0),
                    reason=reason,
                )
            except Exception as exc:
                failures.append(
                    {
                        "job_id": parent_id,
                        "step_id": "routine_invocation",
                        "status": "blocked",
                        "reason_code": type(exc).__name__,
                        "operator_action": "recover_or_cancel",
                    }
                )
        return failures

    async def from_run(self, req: RoutineFromRunRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        provenance, packet = await self._source_proof(
            source_watch_job_id=req.source_watch_job_id,
            source_packet_id=req.source_packet_id,
            source_m3_job_id=req.source_m3_job_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        # Generate the definitive routine id once; the template must carry it.
        routine_id = uuid.uuid4().hex
        workflow = render_workflow(routine_id=routine_id, version=1, name=req.name)
        runbook = render_runbook(routine_id=routine_id, version=1, name=req.name)
        check = validate_generated_files(workflow=workflow, runbook=runbook, routine_id=routine_id, version=1)
        if not check["valid"]:
            raise RoutineError("routine_template_invalid", status_code=500)
        version = GuardianRoutineVersion(
            routine_id=routine_id,
            version=1,
            source_provenance_json=_dump(provenance),
            workflow_bytes=workflow,
            workflow_sha256=_sha(workflow),
            runbook_bytes=runbook,
            runbook_sha256=_sha(runbook),
            source_repository=str(provenance.get("source_repository") or "") or None,
            source_action=str(provenance.get("source_action") or "") or None,
            source_issue_number=(
                int(provenance["source_target"])
                if str(provenance.get("source_action") or "") == "create_comment"
                and str(provenance.get("source_target") or "").isdigit()
                else None
            ),
        )
        routine = GuardianRoutine(
            id=routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            name=req.name.strip(),
            state="prepared",
            revision=1,
            current_version=None,
        )
        async with db_engine.get_session() as db:
            db.add(routine)
            db.add(version)
            await db.flush()
        job_id = f"routine-install:{routine_id}:v1"
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "routine_id": routine_id,
            "routine_version": 1,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "source_provenance_sha256": _sha(version.source_provenance_json),
            "source_repository": version.source_repository,
            "source_action": version.source_action,
            "source_target": provenance.get("source_target"),
            "source_packet_id": provenance.get("source_packet_id"),
            "source_dossier_sha256": provenance.get("dossier_sha256"),
            "source_m3_job_id": provenance.get("source_m3_job_id"),
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        job = await self._admit_user_job(
            job_id=job_id,
            job_kind="routine_install",
            idempotency_key=f"{routine_id}:1",
            inputs={"routine_id": routine_id, "version": 1},
            authority=authority,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=packet.goal_id,
            goal_revision=packet.goal_revision,
            plan_revision=packet.plan_revision,
            candidate_id=None,
        )
        if job.get("status") != "running":
            raise RoutineError("routine_install_job_not_running")
        approval_id = await self._hold_approval(
            job,
            tool_name=ROUTINE_INSTALL_TOOL,
            summary=f"Install reviewed guardian routine {routine_id} version 1",
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        return {
            "status": "prepared",
            "routine": {"id": routine_id, "revision": 1, "state": "prepared", "current_version": None, "name": req.name.strip()},
            "version": self._version_json(version),
            "preview": {"workflow": workflow, "runbook": runbook, "workflow_sha256": version.workflow_sha256, "runbook_sha256": version.runbook_sha256},
            "approval_id": approval_id,
            "install_job_id": job_id,
        }

    async def add_version(self, routine_id: str, req: RoutineVersionRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != req.expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        provenance, packet = await self._source_proof(
            source_watch_job_id=req.source_watch_job_id,
            source_packet_id=req.source_packet_id,
            source_m3_job_id=req.source_m3_job_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        async with db_engine.get_session() as db:
            current = (
                await db.execute(select(GuardianRoutineVersion).where(GuardianRoutineVersion.routine_id == routine_id).order_by(GuardianRoutineVersion.version.desc()))
            ).scalars().first()
            next_version = int(current.version if current else 0) + 1
            workflow = render_workflow(routine_id=routine_id, version=next_version, name=req.name)
            runbook = render_runbook(routine_id=routine_id, version=next_version, name=req.name)
            row = GuardianRoutineVersion(
                routine_id=routine_id,
                version=next_version,
                source_provenance_json=_dump(provenance),
                workflow_bytes=workflow,
                workflow_sha256=_sha(workflow),
                runbook_bytes=runbook,
                runbook_sha256=_sha(runbook),
                source_repository=str(provenance.get("source_repository") or "") or None,
                source_action=str(provenance.get("source_action") or "") or None,
                source_issue_number=(
                    int(provenance["source_target"])
                    if str(provenance.get("source_action") or "") == "create_comment"
                    and str(provenance.get("source_target") or "").isdigit()
                    else None
                ),
            )
            result = await db.execute(
                update(GuardianRoutine)
                .where(
                    GuardianRoutine.id == routine_id,
                    GuardianRoutine.owner_principal_id == owner_principal_id,
                    GuardianRoutine.owner_session_id == owner_session_id,
                    GuardianRoutine.revision == routine.revision,
                    GuardianRoutine.state != "revoked",
                )
                .values(revision=GuardianRoutine.revision + 1, name=req.name.strip(), updated_at=_now())
            )
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
            db.add(row)
            await db.flush()
            db.expunge(row)
        job_id = f"routine-install:{routine_id}:v{next_version}"
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "routine_id": routine_id,
            "routine_version": next_version,
            "workflow_sha256": row.workflow_sha256,
            "runbook_sha256": row.runbook_sha256,
            "source_provenance_sha256": _sha(row.source_provenance_json),
            "source_repository": row.source_repository,
            "source_action": row.source_action,
            "source_target": provenance.get("source_target"),
            "source_packet_id": provenance.get("source_packet_id"),
            "source_dossier_sha256": provenance.get("dossier_sha256"),
            "source_m3_job_id": provenance.get("source_m3_job_id"),
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        job = await self._admit_user_job(
            job_id=job_id,
            job_kind="routine_install",
            idempotency_key=f"{routine_id}:{next_version}",
            inputs={"routine_id": routine_id, "version": next_version},
            authority=authority,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=packet.goal_id,
            goal_revision=packet.goal_revision,
            plan_revision=packet.plan_revision,
            candidate_id=None,
        )
        approval_id = await self._hold_approval(
            job,
            tool_name=ROUTINE_INSTALL_TOOL,
            summary=f"Install reviewed guardian routine {routine_id} version {next_version}",
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        return {"status": "prepared", "routine_id": routine_id, "version": self._version_json(row), "preview": {"workflow": workflow, "runbook": runbook}, "approval_id": approval_id, "install_job_id": job_id}

    async def _resume_approval(self, job: Mapping[str, Any], approval_id: str, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        approval = await approval_repository.get(approval_id)
        if approval is None or approval.status != "approved":
            raise RoutineError("approval_not_current")
        if (
            str(getattr(approval, "owner_principal_id", None) or "") != str(owner_principal_id or "")
            or str(getattr(approval, "operator_session_id", None) or "") != str(owner_session_id or "")
        ):
            raise RoutineError("approval_owner_session_mismatch", status_code=403)
        details = _load(approval.details_json, {})
        if not isinstance(details, Mapping) or str(details.get("durable_job_id") or "") != str(job.get("job_id")):
            raise RoutineError("approval_job_binding_mismatch")
        expires_at = float(details.get("approval_expires_at") or details.get("expires_at") or 0)
        if expires_at <= _now().timestamp():
            raise RoutineError("approval_expired")
        receipt = {
            "status": "approved",
            "authenticated": True,
            "approval_id": approval_id,
            "operator_principal_id": owner_principal_id,
            "operator_session_id": owner_session_id,
            "owner_kind": "user",
            "owner_principal_id": owner_principal_id,
            "service_id": None,
            "authority_digest": job.get("authority_digest"),
            "goal_id": job.get("goal_id"),
            "goal_revision": job.get("goal_revision"),
            "plan_revision": job.get("plan_revision"),
            "capability_version": job.get("capability_version"),
            "budget_microusd": 0,
            "budget_digest": job.get("budget_digest"),
            "expires_at": expires_at,
        }
        return await durable_job_repository.resume_approved_job(
            str(job["job_id"]),
            approval_receipt=receipt,
            approval_id=approval_id,
            authority_digest=str(job.get("authority_digest") or ""),
            goal_id=job.get("goal_id"),
            goal_revision=job.get("goal_revision"),
            plan_revision=job.get("plan_revision"),
            capability_version=str(job.get("capability_version") or ROUTINE_CAPABILITY_VERSION),
            owner_kind="user",
            owner_principal_id=owner_principal_id,
            service_id=None,
            budget_microusd=0,
            budget_digest=str(job.get("budget_digest") or ""),
            operator_principal_id=owner_principal_id,
            operator_session_id=owner_session_id,
            expires_at=expires_at,
            expected_revision=int(job.get("revision") or 0),
        )

    async def _reconcile_committed_install(
        self,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
        *,
        job_id: str,
        job: Mapping[str, Any],
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any] | None:
        """Close the durable install receipt after the package commit.

        Package rows and the durable job live in separate stores.  A worker can
        die after the package transaction and before its terminal transition.
        Repeated install calls use the verified package readback as the proof
        for a narrow, idempotent local-finalization retry.
        """

        # The caller may still hold detached pre-commit ORM objects.  Reload
        # both records so a process failure immediately after the package
        # transaction can be reconciled in the same request as well as on a
        # later retry.
        try:
            latest_routine = await self._routine(str(routine.id), owner_principal_id)
            latest_version = await self._version(str(routine.id), int(version.version))
        except RoutineError:
            return None
        routine = latest_routine
        version = latest_version
        durable_authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        try:
            durable_version = int(durable_authority.get("routine_version") or 0)
        except (TypeError, ValueError):
            return None
        if (
            str(job.get("job_kind") or "") != "routine_install"
            or str(durable_authority.get("routine_id") or "") != str(routine.id)
            or durable_version != int(version.version)
        ):
            return None
        package_digest = str(version.installed_package_digest or "")
        if not package_digest or routine.state not in {"installed", "active", "paused"}:
            return None
        package = self._package_readback(owner_principal_id, owner_session_id)
        if package.get("status") != "active" or package.get("digest") != package_digest:
            return None
        result = {
            "package_digest": package_digest,
            "routine_id": str(routine.id),
            "version": int(version.version),
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "learning": "no_learning",
        }

        async def _finish(current: Mapping[str, Any]) -> dict[str, Any] | None:
            if current.get("status") != "running":
                return None
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            lease_owner = str(lease.get("owner") or "")
            fencing_token = int(lease.get("fencing_token") or 0)
            if not lease_owner or fencing_token <= 0:
                return None
            try:
                settled = await durable_job_repository.transition_job(
                    job_id,
                    "succeeded",
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=current.get("revision"),
                    result=result,
                    result_summary="routine package commit reconciled into its durable install receipt",
                )
            except Exception:
                return None
            if settled.get("status") != "succeeded":
                return None
            return await self.read(
                str(routine.id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )

        current = await durable_job_repository.get_job(job_id) or dict(job)
        if current.get("status") == "succeeded":
            return await self.read(
                str(routine.id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        settled = await _finish(current)
        if settled is not None:
            return settled
        current = await durable_job_repository.get_job(job_id) or current

        # Recover an expired worker fence before taking a new local-finalize
        # lease.  A live competing lease is deliberately left pending.
        if current.get("status") == "running":
            try:
                current = await durable_job_repository.recover_stale_job(job_id)
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
        if current.get("status") == "blocked":
            try:
                current = await durable_job_repository.resume_job(
                    job_id,
                    expected_revision=current.get("revision"),
                    reason="routine_install_local_finalize_retry",
                )
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
        if current.get("status") == "accepted":
            try:
                current = await durable_job_repository.queue_job(
                    job_id,
                    expected_revision=current.get("revision"),
                    reason="routine_install_local_finalize_retry",
                )
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
        if current.get("status") == "queued":
            try:
                current = await durable_job_repository.claim_job(
                    job_id,
                    owner=f"routine:{job_id}:reconcile",
                    expected_revision=current.get("revision"),
                    expected_fencing_token=(current.get("lease") or {}).get("fencing_token"),
                    lease_seconds=ROUTINE_DEADLINE_SECONDS,
                )
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
        settled = await _finish(current)
        if settled is not None:
            return settled
        latest = await durable_job_repository.get_job(job_id) or current
        if latest.get("status") == "succeeded":
            return await self.read(
                str(routine.id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        raise RoutineError(
            "routine_install_local_finalize_pending",
            "the package is committed, but its durable install receipt is still pending recovery",
        )

    async def install(self, routine_id: str, req: RoutineInstallRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        version = await self._version(routine_id, req.version)
        provenance = _load(version.source_provenance_json, {})
        job_id = f"routine-install:{routine_id}:v{version.version}"
        job = await durable_job_repository.get_job(job_id)
        if job is None:
            raise RoutineError("routine_install_job_missing")
        if routine.revision != req.expected_routine_revision:
            # A crash after the package transaction committed increments the
            # routine revision before the durable install terminal transition.
            # Replaying the original request must still reach the proof-bound
            # reconciliation path; no approval-held or uncommitted job may
            # bypass the normal revision CAS.
            if job.get("status") != "awaiting_approval":
                reconciled = await self._reconcile_committed_install(
                    routine,
                    version,
                    job_id=job_id,
                    job=job,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
                if reconciled is not None:
                    return reconciled
            raise RoutineError("routine_revision_stale")
        if job.get("status") != "awaiting_approval":
            reconciled = await self._reconcile_committed_install(
                routine,
                version,
                job_id=job_id,
                job=job,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            if reconciled is not None:
                return reconciled
            raise RoutineError("routine_install_not_awaiting_approval")
        queued = await self._resume_approval(job, req.approval_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        claimed = await durable_job_repository.claim_job(
            job_id,
            owner=f"routine:{job_id}",
            expected_revision=queued.get("revision"),
            expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
            lease_seconds=ROUTINE_DEADLINE_SECONDS,
        )
        lease = claimed.get("lease") or {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        revision = int(claimed.get("revision") or 0)
        workflow_name = f"{routine_slug(routine_id, version.version)}.md"
        runbook_name = f"{routine_slug(routine_id, version.version)}.yaml"
        committed = False
        try:
            workflow_path = save_workspace_contribution("workflows", file_name=workflow_name, content=version.workflow_bytes)
            runbook_path = save_workspace_contribution("runbooks", file_name=runbook_name, content=version.runbook_bytes)
            # Re-read and parse the installed contribution before calculating
            # the package digest or settling the install job.  A successful
            # write receipt without parser/hash agreement is not an installed
            # routine and must remain recoverable/blocked.
            for path, content, expected_sha in (
                (workflow_path, version.workflow_bytes, version.workflow_sha256),
                (runbook_path, version.runbook_bytes, version.runbook_sha256),
            ):
                stored, truncated = _read_workspace_text_bounded(path, max_bytes=128 * 1024)
                if truncated or stored != content or _sha(stored) != expected_sha:
                    raise RoutineError("routine_install_readback_mismatch")
            validation = validate_generated_files(
                workflow=version.workflow_bytes,
                runbook=version.runbook_bytes,
                routine_id=routine_id,
                version=version.version,
            )
            if not validation.get("valid"):
                raise RoutineError("routine_install_parse_failed")
            root = workspace_capability_package_root()
            package_digest = capability_pack_digest(root)
            for path, content, kind in ((workflow_path, version.workflow_bytes, "routine_workflow"), (runbook_path, version.runbook_bytes, "routine_runbook")):
                artifact = await durable_job_repository.record_artifact(job_id, file_path=str(path.relative_to(settings.workspace_dir)), artifact_type=kind, content=content, owner=owner, fencing_token=fence, expected_revision=revision)
                revision = int(artifact.get("revision") or revision)
                effect = await durable_job_repository.record_effect(job_id, effect_type="routine_install", target_path=str(path.relative_to(settings.workspace_dir)), target_digest=_sha(content), content_sha256=_sha(content), status="succeeded", details={"verified": True, "package_digest": package_digest}, owner=owner, fencing_token=fence, expected_revision=revision)
                revision = int(effect.get("revision") or revision)
                readback = await durable_job_repository.record_readback(job_id, target_path=str(path.relative_to(settings.workspace_dir)), effect_id=(effect.get("receipt") or {}).get("effect_id"), effect_type="routine_install", target_digest=_sha(content), content_sha256=_sha(content), status="succeeded", details={"verified": True, "package_digest": package_digest}, owner=owner, fencing_token=fence, expected_revision=revision)
                revision = int(readback.get("revision") or revision)
            async with db_engine.get_session() as db:
                version_update = await db.execute(
                    update(GuardianRoutineVersion)
                    .where(GuardianRoutineVersion.id == version.id, GuardianRoutineVersion.installed_package_digest.is_(None))
                    .values(installed_package_digest=package_digest, installed_at=_now())
                )
                routine_update = await db.execute(
                    update(GuardianRoutine)
                    .where(
                        GuardianRoutine.id == routine_id,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                        GuardianRoutine.owner_session_id == owner_session_id,
                        GuardianRoutine.revision == routine.revision,
                        GuardianRoutine.state != "revoked",
                    )
                    .values(state="installed", current_version=version.version, revision=GuardianRoutine.revision + 1, updated_at=_now())
                )
                if version_update.rowcount != 1 or routine_update.rowcount != 1:
                    raise RoutineError("routine_install_revision_stale")
            committed = True
            await durable_job_repository.transition_job(
                job_id,
                "succeeded",
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
                result={"package_digest": package_digest, "routine_id": routine_id, "version": version.version, "workflow_sha256": version.workflow_sha256, "runbook_sha256": version.runbook_sha256, "learning": "no_learning"},
                result_summary="routine files installed, parsed, hashed, and read back",
            )
            return await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        except Exception as exc:
            if committed:
                # The package transaction is authoritative. Retry the local
                # terminal receipt instead of projecting an installed routine
                # while its install job remains running.
                try:
                    reconciled = await self._reconcile_committed_install(
                        routine,
                        version,
                        job_id=job_id,
                        job=claimed,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                except RoutineError:
                    raise
                if reconciled is not None:
                    return reconciled
            current = await durable_job_repository.get_job(job_id)
            if current and current.get("status") == "running":
                try:
                    await durable_job_repository.transition_job(job_id, "failed", owner=owner, fencing_token=fence, expected_revision=current.get("revision"), reason="install_incomplete")
                except Exception:
                    pass
            raise RoutineError("install_incomplete", str(exc)) from exc

    async def activate(self, routine_id: str, req: RoutineActivateRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        if routine.revision != req.expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, req.version)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        package = self._package_readback(owner_principal_id, owner_session_id)
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise RoutineError("package_review_required")
        async with db_engine.get_session() as db:
            result = await db.execute(update(GuardianRoutine).where(GuardianRoutine.id == routine_id, GuardianRoutine.owner_principal_id == owner_principal_id, GuardianRoutine.owner_session_id == owner_session_id, GuardianRoutine.revision == req.expected_routine_revision, GuardianRoutine.state != "revoked").values(state="active", current_version=req.version, revision=GuardianRoutine.revision + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        return await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)

    async def invoke(self, routine_id: str, req: RoutineInvokeRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.revision != req.expected_routine_revision or routine.state != "active":
            raise RoutineError("routine_not_active_or_stale")
        version = await self._version(routine_id, req.version)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        # Installation approval is durable, but package governance may have
        # changed since the version was installed.  Re-read the current
        # package review immediately before admitting a new invocation.
        package = self._package_readback(owner_principal_id, owner_session_id)
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise RoutineError("package_review_required")
        watch = await source_watch_service.get_watch(req.source_watch_id, owner_principal_id=owner_principal_id)
        if watch is None or watch.get("goal_id") != req.goal_id:
            raise RoutineError("source_watch_not_owned", status_code=404)
        if (
            int(watch.get("goal_revision", 0)) != int(req.expected_goal_revision)
            or int(watch.get("plan_revision", 0)) != int(req.expected_watch_revision)
        ):
            raise RoutineError("source_watch_revision_stale")
        async with db_engine.get_session() as db:
            goal = (await db.execute(select(Goal).where(Goal.id == req.goal_id))).scalars().first()
            if (
                goal is None
                or str(goal.owner_principal_id or "") != owner_principal_id
                or str(goal.owner_session_id or "") != owner_session_id
                or int(goal.revision or 0) != int(req.expected_goal_revision)
                or str(getattr(goal.status, "value", goal.status) or "") != "active"
            ):
                raise RoutineError("goal_binding_stale")
        connection = await GitHubFollowthroughService().get_connection(owner_principal_id)
        if connection.get("mode") != "active":
            raise RoutineError("github_connection_not_active")
        provenance = _load(version.source_provenance_json, {})
        fixed_repository = str(provenance.get("source_repository") or "")
        if fixed_repository and connection.get("repository") != fixed_repository:
            raise RoutineError("github_connection_repository_changed")
        invocation_uuid = _safe_invocation_uuid(req.invocation_uuid)
        job_id = f"routine-invocation:{routine_id}:{invocation_uuid}"
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "routine_id": routine_id,
            "routine_revision": routine.revision,
            "routine_version": req.version,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "package_digest": version.installed_package_digest,
            "source_watch_id": req.source_watch_id,
            "source_watch_revision": req.expected_watch_revision,
            "github_connection_id": connection.get("id"),
            "github_connection_revision": connection.get("revision"),
            "github_repository": connection.get("repository"),
            "github_action": provenance.get("source_action"),
            "github_target": provenance.get("source_target"),
            "invocation_uuid": invocation_uuid,
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        job = await self._admit_user_job(job_id=job_id, job_kind="routine_invocation", idempotency_key=f"{owner_principal_id}:{routine_id}:{invocation_uuid}", inputs={"routine_id": routine_id, "routine_version": req.version, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "invocation_uuid": invocation_uuid}, authority=authority, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id, goal_id=req.goal_id, goal_revision=req.expected_goal_revision, plan_revision=int(watch.get("plan_revision") or 1), candidate_id=None)
        receipt = job.get("receipt") if isinstance(job.get("receipt"), Mapping) else {}
        deduped = receipt.get("status") == "deduped"
        durable_authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        durable_approval_id = str(durable_authority.get("approval_id") or "") or None
        if deduped:
            # The durable row is authoritative for retries.  Never create or
            # replace an approval for a terminal, running, or already-held
            # invocation that won the idempotency race.
            return {
                "status": job.get("status"),
                "job_id": str(job.get("job_id") or job_id),
                "approval_id": durable_approval_id,
                "result": job.get("result"),
                "deduped": True,
                "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]},
            }
        if job.get("status") == "awaiting_approval":
            # A repository implementation may return the existing row without
            # the explicit dedupe receipt.  Its persisted authority still
            # wins, and a second approval must not be created.
            approval_id = durable_approval_id
            return {
                "status": job.get("status"),
                "job_id": str(job.get("job_id") or job_id),
                "approval_id": approval_id,
                "result": job.get("result"),
                "deduped": True,
                "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]},
            }
        approval_id = await self._hold_approval(job, tool_name=ROUTINE_INVOKE_TOOL, summary=f"Run guardian routine {routine_id} for goal {req.goal_id}", owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        return {"status": "awaiting_approval", "job_id": job_id, "approval_id": approval_id, "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]}}

    async def execute_invocation(self, routine_id: str, job_id: str, req: RoutineExecuteRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=req.expected_routine_revision,
        )
        job = await durable_job_repository.get_job(job_id)
        if not job or job.get("job_kind") != "routine_invocation" or not _owner_matches(job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        queued = await self._resume_approval(job, req.approval_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        claimed = await durable_job_repository.claim_job(job_id, owner=f"routine:{job_id}", expected_revision=queued.get("revision"), expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"), lease_seconds=ROUTINE_DEADLINE_SECONDS)
        parent_lease = claimed.get("lease") or {}
        parent_fence = int(parent_lease.get("fencing_token") or 0)
        parent_revision = int(claimed.get("revision") or 0)
        authority = claimed.get("declared_authority") if isinstance(claimed.get("declared_authority"), Mapping) else {}
        try:
            bound_routine_revision = int(authority.get("routine_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise RoutineError("routine_revision_binding_invalid") from exc
        try:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_revision=bound_routine_revision,
            )
        except RoutineError:
            await durable_job_repository.cancel_job(
                job_id,
                owner=str(parent_lease.get("owner") or "") or None,
                fencing_token=parent_fence or None,
                expected_revision=parent_revision,
                reason="routine_not_active_or_stale",
            )
            raise
        watch_id = str(authority.get("source_watch_id") or "")
        watch_revision = int(authority.get("source_watch_revision") or 0)
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        if not watch_id or not invocation_uuid or watch_revision <= 0:
            raise RoutineError("routine_invocation_binding_missing")
        watch_child_id = _child_job_id(invocation_uuid, "watch")
        watch_child = await self._admit_child_job(
            claimed,
            child_id=watch_child_id,
            step_id="guardian_watch_run",
            inputs={
                "routine_invocation_job_id": job_id,
                "source_watch_id": watch_id,
                "source_watch_revision": watch_revision,
                "invocation_uuid": invocation_uuid,
            },
            authority={
                "routine_id": routine_id,
                "routine_revision": bound_routine_revision,
                "routine_version": authority.get("routine_version"),
                "source_watch_id": watch_id,
                "source_watch_revision": watch_revision,
                "invocation_uuid": invocation_uuid,
            },
        )
        if watch_child.get("status") != "running":
            raise RoutineError("routine_watch_child_not_running")
        parent_checkpoint = await durable_job_repository.record_checkpoint(
            job_id,
            checkpoint_id="routine:watch_child_recorded",
            state={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "watch_id": watch_id},
            checkpoint_payload={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "watch_id": watch_id, "watch_revision": watch_revision},
            safe=True,
            owner=str(parent_lease.get("owner") or ""),
            fencing_token=parent_fence,
            expected_revision=parent_revision,
        )
        child_lease = watch_child.get("lease") or {}
        context = RoutineStepContext(
            principal_id=owner_principal_id,
            session_id=owner_session_id,
            lease_owner=str(child_lease.get("owner") or ""),
            fencing_token=int(child_lease.get("fencing_token") or 0),
            runtime_job_id=job_id,
        )
        # M1 owns source observation and its own local-write approval.  Never
        # manufacture that approval from the routine approval.
        try:
            result = await guardian_watch_run(watch_child_id, context=context, service=self)
        except Exception as exc:
            child_current = await durable_job_repository.get_job(watch_child_id)
            if child_current and child_current.get("status") == "running":
                await self._settle_child(
                    child_current,
                    result={"status": "blocked", "reason_code": type(exc).__name__},
                    status="blocked",
                    reason="watch_child_dispatch_failed",
                )
            current = await durable_job_repository.get_job(job_id)
            if current and current.get("status") == "running":
                parent_lease = current.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason="watch_child_dispatch_failed",
                    result={"learning": "no_learning", "child_job_id": watch_child_id, "reason_code": type(exc).__name__},
                    result_summary="the persisted watch child could not be dispatched",
                )
            return {"status": "blocked", "job_id": job_id, "child_job_id": watch_child_id, "learning": "no_learning"}
        m1_status = str(result.get("status") or "blocked")
        parent_current = await durable_job_repository.get_job(job_id) or parent_checkpoint
        if result.get("status") in {"no_change", "rebaseline_initialized"}:
            await self._finalize_parent(
                parent_current,
                result={"watch_child_job_id": watch_child_id, "status": result.get("status")},
                reason=str(result.get("status")),
            )
            return {"status": result.get("status"), "job_id": job_id, "learning": "no_learning", "publication": "skipped"}
        current = await durable_job_repository.get_job(job_id)
        if current and current.get("status") == "running":
            parent_lease = current.get("lease") or {}
            child_reason = "awaiting_child_approval" if m1_status == "awaiting_approval" else "watch_child_completed"
            await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id="routine:watch_readback_verified" if m1_status == "succeeded" else "routine:watch_child_blocked",
                state={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "status": m1_status},
                checkpoint_payload={
                    "step_id": "guardian_watch_run",
                    "child_job_id": watch_child_id,
                    "status": m1_status,
                    "m1_job_id": result.get("m1_job_id") or result.get("job_id"),
                    "packet_id": result.get("packet_id"),
                    "approval_id": result.get("approval_id"),
                    "reason_code": result.get("reason_code"),
                },
                safe=True,
                owner=str(parent_lease.get("owner") or ""),
                fencing_token=int(parent_lease.get("fencing_token") or 0),
                expected_revision=current.get("revision"),
            )
            current = await durable_job_repository.get_job(job_id) or current
            parent_lease = current.get("lease") or {}
            if m1_status not in {"succeeded", "awaiting_publication_preview"}:
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason=child_reason,
                    result={"learning": "no_learning", "child_job_id": watch_child_id, "child": {"status": m1_status}},
                    result_summary=child_reason,
                )
                return {"status": "blocked", "job_id": job_id, "child_job_id": watch_child_id, "child": result, "learning": "no_learning"}
            current = await durable_job_repository.get_job(job_id) or current
            parent_lease = current.get("lease") or {}
            await durable_job_repository.transition_job(
                job_id,
                "blocked",
                owner=parent_lease.get("owner"),
                fencing_token=parent_lease.get("fencing_token"),
                expected_revision=current.get("revision"),
                reason="awaiting_publication_preview",
                result={"learning": "no_learning", "child_job_id": watch_child_id, "child": {"status": m1_status}},
                result_summary="fresh M3 publication preview is required",
            )
        return {"status": "awaiting_publication_preview", "job_id": job_id, "child_job_id": watch_child_id, "child": result, "learning": "no_learning"}

    async def execute_generated_step(
        self,
        routine_invocation_job_id: str,
        step_id: str,
        *,
        context: RoutineStepContext,
    ) -> dict[str, Any]:
        """Execute one fixed generated workflow step through the same child CAS.

        Generated workflow files are declarative; they do not receive generic
        service handles or caller-supplied arguments. The invocation ID is
        resolved to the owner-bound routine parent, then to the deterministic
        M1 or M3 child before dispatch.
        """

        if not isinstance(context, RoutineStepContext):
            raise PermissionError("generated routine step requires trusted runtime context")
        requested_parent_id = str(routine_invocation_job_id or "").strip()
        if not requested_parent_id or context.runtime_job_id != requested_parent_id:
            raise PermissionError("routine runtime parent mismatch")
        if step_id not in {"guardian_watch_run", "github_followthrough"}:
            raise RoutineError("routine_step_not_allowed", status_code=422)
        if step_id == "github_followthrough" and not context.external_mutation_granted:
            raise PermissionError("routine follow-through requires external_mutation authority")
        parent = await durable_job_repository.get_job(str(routine_invocation_job_id).strip())
        if (
            not parent
            or parent.get("job_kind") != "routine_invocation"
            or not _owner_matches(parent, context.principal_id, context.session_id)
        ):
            raise RoutineError("routine_invocation_not_running")
        if parent.get("status") != "running":
            # A generated workflow may be resumed after the M1 child has
            # blocked on its own approval or after the M3 preview was created.
            # Only the persisted parent/checkpoint path may reopen it; callers
            # cannot supply a new child identity or mutable destination.
            if step_id != "github_followthrough" or parent.get("status") != "blocked":
                return {
                    "status": parent.get("status") or "blocked",
                    "job_id": str(routine_invocation_job_id),
                    "reason_code": "routine_invocation_not_running",
                    "operator_visible": True,
                    "learning": "no_learning",
                }
            parent = await self._claim_parent_for_recovery(
                parent,
                owner_principal_id=context.principal_id,
                owner_session_id=context.session_id,
            )
        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        routine_id = str(authority.get("routine_id") or "")
        routine_revision = int(authority.get("routine_revision") or 0)
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        if not routine_id or routine_revision <= 0 or not invocation_uuid:
            raise RoutineError("routine_invocation_binding_missing")
        await self._require_active_routine(
            routine_id,
            owner_principal_id=context.principal_id,
            owner_session_id=context.session_id,
            expected_revision=routine_revision,
        )
        child_suffix = "watch" if step_id == "guardian_watch_run" else "publication"
        child_id = _child_job_id(invocation_uuid, child_suffix)
        parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        if (
            str(parent_lease.get("owner") or "") != str(context.lease_owner or "")
            or int(parent_lease.get("fencing_token") or 0) != int(context.fencing_token or 0)
        ):
            # This invocation context belongs to a worker that lost the
            # parent lease.  Leave any child owned by the replacement worker
            # untouched and return a durable-looking operator receipt rather
            # than dispatching from stale authority.
            return {
                "status": "blocked",
                "job_id": str(routine_invocation_job_id),
                "child_job_id": child_id,
                "reason_code": "routine_parent_fence_stale",
                "recovery": "retry_current_invocation",
                "operator_action": "recover_or_cancel",
                "operator_visible": True,
                "learning": "no_learning",
            }
        child = await durable_job_repository.get_job(child_id)
        if child is None:
            if step_id == "github_followthrough":
                watch_checkpoint = _job_checkpoint(parent, "routine:watch_readback_verified")
                if not watch_checkpoint or str(watch_checkpoint.get("status") or "") != "succeeded":
                    return {
                        "status": "awaiting_child_approval",
                        "job_id": str(routine_invocation_job_id),
                        "child_job_id": child_id,
                        "reason_code": "awaiting_child_approval",
                        "operator_visible": True,
                        "learning": "no_learning",
                    }
                parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
                await durable_job_repository.record_checkpoint(
                    str(routine_invocation_job_id),
                    checkpoint_id="routine:publication_required",
                    state={"step_id": step_id, "child_job_id": child_id, "status": "awaiting_publication_preview"},
                    checkpoint_payload={"step_id": step_id, "child_job_id": child_id, "status": "awaiting_publication_preview"},
                    safe=True,
                    owner=str(parent_lease.get("owner") or ""),
                    fencing_token=int(parent_lease.get("fencing_token") or 0),
                    expected_revision=parent.get("revision"),
                )
                latest_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or parent
                latest_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else parent_lease
                if latest_parent.get("status") == "running":
                    await durable_job_repository.transition_job(
                        str(routine_invocation_job_id),
                        "blocked",
                        owner=str(latest_lease.get("owner") or ""),
                        fencing_token=int(latest_lease.get("fencing_token") or 0),
                        expected_revision=latest_parent.get("revision"),
                        reason="awaiting_publication_preview",
                        result={"learning": "no_learning", "child_job_id": child_id},
                        result_summary="fresh M3 publication preview is required",
                    )
                return {
                    "status": "awaiting_publication_preview",
                    "job_id": str(routine_invocation_job_id),
                    "child_job_id": child_id,
                    "reason_code": "awaiting_publication_preview",
                    "operator_visible": True,
                    "learning": "no_learning",
                }
            child = await self._admit_child_job(
                parent,
                child_id=child_id,
                step_id=step_id,
                inputs={
                    "routine_invocation_job_id": str(routine_invocation_job_id),
                    "invocation_uuid": invocation_uuid,
                },
                authority={
                    "routine_id": routine_id,
                    "routine_revision": routine_revision,
                    "routine_version": authority.get("routine_version"),
                    "source_watch_id": authority.get("source_watch_id"),
                    "source_watch_revision": authority.get("source_watch_revision"),
                    "invocation_uuid": invocation_uuid,
                },
            )
        else:
            parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
            child_parent_fence = int(child.get("parent_fencing_token") or 0)
            current_parent_fence = int(parent_lease.get("fencing_token") or 0)
            if (
                parent.get("status") != "running"
                or child_parent_fence <= 0
                or child_parent_fence != current_parent_fence
            ) and child.get("status") in {
                "accepted",
                "queued",
                "running",
                "awaiting_approval",
                "blocked",
            }:
                return await self._cancel_stale_child(
                    child,
                    parent=parent,
                    step_id=step_id,
                )
        if child.get("status") == "blocked" and step_id == "github_followthrough":
            prepared = _job_checkpoint(child, "routine-child:prepared") or {}
            if prepared.get("m3_job_id"):
                resumed = await durable_job_repository.resume_job(
                    str(child["job_id"]),
                    expected_revision=child.get("revision"),
                    reason="routine_followthrough_resume",
                )
                if resumed.get("status") == "queued":
                    child = await durable_job_repository.claim_job(
                        str(child["job_id"]),
                        owner=f"routine-child:{child['job_id']}",
                        expected_revision=resumed.get("revision"),
                        expected_fencing_token=(resumed.get("lease") or {}).get("fencing_token"),
                        lease_seconds=ROUTINE_DEADLINE_SECONDS,
                    )
        if child.get("status") != "running":
            if child.get("status") in {"succeeded", "degraded", "cancelled", "blocked", "failed"}:
                return {"status": child.get("status"), "child_job_id": child_id, "recovery": "terminal_child"}
            raise RoutineError("routine_child_not_running")
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        child_context = RoutineStepContext(
            principal_id=context.principal_id,
            session_id=context.session_id,
            lease_owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            external_mutation_granted=context.external_mutation_granted,
            runtime_job_id=str(routine_invocation_job_id),
        )
        if step_id == "guardian_watch_run":
            result = await self.execute_watch_step(child_id, context=child_context)
            latest_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or parent
            if latest_parent.get("status") == "running":
                parent_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                m1_status = str(result.get("status") or "blocked")
                if m1_status in {"no_change", "rebaseline_initialized"}:
                    await self._finalize_parent(
                        latest_parent,
                        result={"watch_child_job_id": child_id, "status": m1_status},
                        reason=m1_status,
                    )
                else:
                    checkpoint_id = "routine:watch_readback_verified" if m1_status == "succeeded" else "routine:watch_child_blocked"
                    checkpoint = await durable_job_repository.record_checkpoint(
                        str(routine_invocation_job_id),
                        checkpoint_id=checkpoint_id,
                        state={"step_id": step_id, "child_job_id": child_id, "status": m1_status},
                        checkpoint_payload={
                            "step_id": step_id,
                            "child_job_id": child_id,
                            "status": m1_status,
                            "m1_job_id": result.get("m1_job_id") or result.get("job_id"),
                            "packet_id": result.get("packet_id"),
                            "approval_id": result.get("approval_id"),
                            "reason_code": result.get("reason_code"),
                        },
                        safe=True,
                        owner=str(parent_lease.get("owner") or ""),
                        fencing_token=int(parent_lease.get("fencing_token") or 0),
                        expected_revision=latest_parent.get("revision"),
                    )
                    current_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or checkpoint
                    current_lease = current_parent.get("lease") if isinstance(current_parent.get("lease"), Mapping) else parent_lease
                    await durable_job_repository.transition_job(
                        str(routine_invocation_job_id),
                        "blocked",
                        owner=str(current_lease.get("owner") or ""),
                        fencing_token=int(current_lease.get("fencing_token") or 0),
                        expected_revision=current_parent.get("revision"),
                        reason=("awaiting_child_approval" if m1_status == "awaiting_approval" else "awaiting_publication_preview" if m1_status == "succeeded" else "watch_child_blocked"),
                        result={"learning": "no_learning", "child_job_id": child_id, "child": {"status": m1_status}},
                        result_summary="generated routine watch step requires the next guarded boundary",
                    )
            return result
        result = await self.execute_followthrough_step(child_id, context=child_context)
        latest_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or parent
        if latest_parent.get("status") == "running":
            m3_status = str(result.get("status") or "blocked")
            if m3_status == "succeeded":
                await self._finalize_parent(
                    latest_parent,
                    result={
                        "publication_child_job_id": child_id,
                        "m3_job_id": result.get("m3_job_id") or result.get("job_id"),
                        "status": m3_status,
                        "remote_id": result.get("remote_id"),
                        "browser_url": result.get("remote_url"),
                    },
                    reason="publication_readback_verified",
                )
            elif m3_status in {"unknown_external_effect", "cost_liability"}:
                parent_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                await durable_job_repository.transition_job(
                    str(routine_invocation_job_id),
                    m3_status,
                    owner=str(parent_lease.get("owner") or ""),
                    fencing_token=int(parent_lease.get("fencing_token") or 0),
                    expected_revision=latest_parent.get("revision"),
                    reason="publication_requires_reconciliation",
                    result={"learning": "no_learning", "child_job_id": child_id, "m3_job_id": result.get("m3_job_id") or result.get("job_id"), "operator_action": "reconcile_or_cancel"},
                    result_summary="publication outcome is uncertain and requires M3 reconciliation",
                )
            else:
                parent_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                if m3_status == "blocked":
                    result = {
                        **dict(result),
                        "recovery": "reconcile_or_cancel",
                        "operator_action": "reconcile_or_cancel",
                    }
                await durable_job_repository.transition_job(
                    str(routine_invocation_job_id),
                    "blocked",
                    owner=str(parent_lease.get("owner") or ""),
                    fencing_token=int(parent_lease.get("fencing_token") or 0),
                    expected_revision=latest_parent.get("revision"),
                    reason="awaiting_publication_approval" if m3_status == "awaiting_approval" else "publication_requires_reconciliation" if m3_status == "blocked" else "publication_child_blocked",
                    result={"learning": "no_learning", "child_job_id": child_id, "m3_job_id": result.get("m3_job_id") or result.get("job_id"), "operator_action": "reconcile_or_cancel" if m3_status == "blocked" else None},
                    result_summary="publication remains pending or blocked",
                )
        return result

    async def execute_watch_step(self, job_id: str, *, context: Any) -> dict[str, Any]:
        if not isinstance(context, RoutineStepContext):
            raise PermissionError("routine watch step requires trusted runtime context")
        child = await self._owned_child(job_id, step_id="guardian_watch_run", context=context)
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        watch_id = str(authority.get("source_watch_id") or "")
        watch_revision = int(authority.get("source_watch_revision") or 0)
        if not watch_id or watch_revision <= 0:
            raise RoutineError("routine_watch_child_binding_missing")
        await self._record_child_checkpoint(child, checkpoint_id="routine-child:dispatch_started", payload={"step_id": "guardian_watch_run", "watch_id": watch_id})
        try:
            result = await source_watch_service.run_watch(
                watch_id,
                occurrence_id=job_id,
                expected_plan_revision=watch_revision,
                expected_owner_session_id=context.session_id,
            )
        except Exception as exc:
            result = {"status": "blocked", "reason_code": type(exc).__name__, "job_id": job_id}
        status = str(result.get("status") or "blocked")
        child_status = {
            "no_change": "succeeded",
            "rebaseline_initialized": "succeeded",
            "succeeded": "succeeded",
            "degraded": "degraded",
            # The approval belongs to M1's own durable job.  Reusing it for
            # this child would be an approval-boundary violation, so retain a
            # blocked child receipt until recovery observes M1's outcome.
            "awaiting_approval": "blocked",
            "blocked": "blocked",
        }.get(status, "blocked")
        safe_result = dict(result)
        safe_result["m1_job_id"] = result.get("job_id")
        settled = await self._settle_child(child, result=safe_result, status=child_status, reason=status)
        return {**safe_result, "child_job_id": job_id, "child_status": settled.get("status")}

    async def execute_followthrough_step(self, job_id: str, *, context: Any) -> dict[str, Any]:
        if not isinstance(context, RoutineStepContext):
            raise PermissionError("routine follow-through step requires trusted runtime context")
        child = await self._owned_child(job_id, step_id="github_followthrough", context=context)
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        prepared_checkpoint = _publication_binding_checkpoint(child) or {}
        m3_job_id = str(authority.get("m3_job_id") or prepared_checkpoint.get("m3_job_id") or "")
        if not m3_job_id:
            raise RoutineError("routine_followthrough_child_binding_missing")
        await self._record_child_checkpoint(child, checkpoint_id="routine-child:dispatch_started", payload={"step_id": "github_followthrough", "m3_job_id": m3_job_id})
        try:
            result = await GitHubFollowthroughService().execute(
                owner_principal_id=context.principal_id,
                job_id=m3_job_id,
                owner_session_id=context.session_id,
                external_mutation_granted=context.external_mutation_granted,
            )
        except Exception as exc:
            result = {"status": "blocked", "reason_code": type(exc).__name__, "job_id": m3_job_id}
        status = str(result.get("status") or "blocked")
        child_status = {
            "succeeded": "succeeded",
            "degraded": "degraded",
            "unknown_external_effect": "unknown_external_effect",
            "cost_liability": "cost_liability",
            "awaiting_approval": "blocked",
            "blocked": "blocked",
            "cancelled": "cancelled",
        }.get(status, "blocked")
        settled = await self._settle_child(child, result=result, status=child_status, reason=status)
        return {**dict(result), "child_job_id": job_id, "m3_job_id": m3_job_id, "child_status": settled.get("status")}

    async def _claim_parent_for_recovery(
        self,
        job: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        if not _owner_matches(job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        current = dict(job)
        if current.get("status") == "blocked":
            resumed = await durable_job_repository.resume_job(
                str(current["job_id"]),
                expected_revision=current.get("revision"),
                reason="routine_recovery_resume",
            )
            current = resumed
        if current.get("status") == "queued":
            current = await durable_job_repository.claim_job(
                str(current["job_id"]),
                owner=f"routine:{current['job_id']}",
                expected_revision=current.get("revision"),
                expected_fencing_token=(current.get("lease") or {}).get("fencing_token"),
                lease_seconds=ROUTINE_DEADLINE_SECONDS,
            )
        if current.get("status") != "running":
            raise RoutineError("routine_parent_not_recoverable")
        return current

    async def _reacquire_parent_after_child(
        self,
        previous: Mapping[str, Any],
        *,
        routine_id: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any] | None:
        """Renew or reacquire the parent fence before a post-child write.

        Child execution is awaited work. During that wait a stale recovery,
        pause, or another worker may replace the parent lease. Re-reading the
        parent and heartbeating the same fence keeps the following checkpoint
        or transition bound to the worker that actually ran the child; a
        blocked parent is resumed through the normal queued claim path.
        """

        job_id = str(previous.get("job_id") or "")
        latest = await durable_job_repository.get_job(job_id) if job_id else None
        if not latest or not _owner_matches(latest, owner_principal_id, owner_session_id):
            return None
        authority = latest.get("declared_authority") if isinstance(latest.get("declared_authority"), Mapping) else {}
        if str(authority.get("routine_id") or "") != str(routine_id):
            return None
        try:
            routine_revision = int(authority.get("routine_revision") or 0)
        except (TypeError, ValueError):
            return None
        if routine_revision <= 0:
            return None
        try:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_revision=routine_revision,
            )
        except RoutineError:
            return None
        if latest.get("status") == "blocked":
            try:
                return await self._claim_parent_for_recovery(
                    latest,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            except RoutineError:
                return None
        if latest.get("status") != "running":
            return None
        previous_lease = previous.get("lease") if isinstance(previous.get("lease"), Mapping) else {}
        latest_lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
        owner = str(latest_lease.get("owner") or "")
        fencing_token = int(latest_lease.get("fencing_token") or 0)
        if (
            owner != str(previous_lease.get("owner") or "")
            or fencing_token != int(previous_lease.get("fencing_token") or 0)
            or not owner
            or fencing_token <= 0
        ):
            return None
        try:
            return await durable_job_repository.heartbeat_job(
                job_id,
                owner=owner,
                fencing_token=fencing_token,
                lease_seconds=ROUTINE_DEADLINE_SECONDS,
                expected_state="running",
                expected_revision=latest.get("revision"),
                expected_fencing_token=fencing_token,
            )
        except Exception:
            return None

    async def prepare_publication(
        self,
        routine_id: str,
        job_id: str,
        req: RoutinePublicationRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        external_mutation_granted: bool = False,
    ) -> dict[str, Any]:
        """Prepare exactly one fixed M3 destination from the persisted watch child."""

        if not external_mutation_granted:
            raise RoutineError("external_mutation_grant_required", status_code=403)

        routine = await self._routine(routine_id, owner_principal_id)
        parent = await durable_job_repository.get_job(job_id)
        if (
            not parent
            or parent.get("job_kind") != "routine_invocation"
            or not _owner_matches(parent, owner_principal_id, owner_session_id)
        ):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        try:
            routine_revision = int(authority.get("routine_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise RoutineError("routine_revision_binding_invalid") from exc
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=routine_revision,
        )
        expected_package = str(authority.get("package_digest") or "")
        package = self._package_readback(owner_principal_id, owner_session_id)
        if package.get("status") != "active" or package.get("digest") != expected_package:
            raise RoutineError("package_review_required")
        watch_checkpoint = _job_checkpoint(parent, "routine:watch_readback_verified")
        if not watch_checkpoint or str(watch_checkpoint.get("status") or "") != "succeeded":
            raise RoutineError("routine_watch_readback_required")
        packet_id = str(watch_checkpoint.get("packet_id") or "")
        m1_job_id = str(watch_checkpoint.get("m1_job_id") or "")
        if not packet_id or not m1_job_id:
            raise RoutineError("routine_watch_provenance_missing")
        m1_job = await durable_job_repository.get_job(m1_job_id)
        if not _verified_readback(m1_job):
            raise RoutineError("routine_watch_readback_required")
        async with db_engine.get_session() as db:
            packet = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))
            ).scalars().first()
            if packet is None or packet.status != "succeeded" or packet.verification_status != "passed":
                raise RoutineError("routine_watch_packet_not_verified")
            if packet.goal_id != parent.get("goal_id") or int(packet.goal_revision) != int(parent.get("goal_revision") or 0):
                raise RoutineError("routine_watch_goal_changed")
            if str(packet.dossier_artifact_id or "") == "" or str(packet.dossier_sha256 or "") == "":
                raise RoutineError("routine_dossier_missing")
            db.expunge(packet)
        provenance = _load((await self._version(routine_id, int(authority.get("routine_version") or 0))).source_provenance_json, {})
        action = str(authority.get("github_action") or provenance.get("source_action") or "")
        repository = str(authority.get("github_repository") or provenance.get("source_repository") or "")
        target = str(authority.get("github_target") or provenance.get("source_target") or "")
        if action not in {"create_issue", "create_comment"} or not repository or not target:
            raise RoutineError("routine_destination_binding_missing")
        connection = await GitHubFollowthroughService().get_connection(owner_principal_id)
        if (
            connection.get("mode") != "active"
            or connection.get("repository") != repository
            or str(connection.get("id") or "") != str(authority.get("github_connection_id") or "")
            or int(connection.get("revision") or 0) != int(authority.get("github_connection_revision") or 0)
        ):
            raise RoutineError("github_connection_binding_changed")
        current = await self._claim_parent_for_recovery(parent, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=routine_revision,
        )
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        publication_child_id = _child_job_id(invocation_uuid, "publication")
        # Derive the M3 identity before the external prepare call.  Persisting
        # this identity on the M4 child lets pause/revoke and restart recovery
        # adopt an approval-held M3 job even if the process dies between the
        # M3 admission and the later binding checkpoint.
        operation_uuid = str(uuid.uuid5(uuid.UUID(invocation_uuid), "seraph:guardian-routine:publication"))
        expected_m3_job_id = _expected_publication_job_id(owner_principal_id, operation_uuid)
        existing_child = await durable_job_repository.get_job(publication_child_id)
        if existing_child is not None:
            checkpoint = _job_checkpoint(existing_child, "routine-child:prepared")
            if checkpoint and checkpoint.get("m3_job_id"):
                m3_job_id = str(checkpoint["m3_job_id"])
                if m3_job_id != expected_m3_job_id:
                    raise RoutineError("routine_publication_binding_conflict")
                m3_job = await durable_job_repository.get_job(m3_job_id) or {}
                if not m3_job:
                    # The prior prepare may have crashed before M3 admission;
                    # retain the deterministic child and let the idempotent
                    # prepare below recreate the missing durable job.
                    checkpoint = None
                else:
                    existing_m3 = await GitHubFollowthroughService()._prepare_job_response(m3_job)
                    latest_parent = await durable_job_repository.get_job(job_id) or current
                    if latest_parent.get("status") == "running":
                        parent_lease = latest_parent.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=parent_lease.get("owner"),
                            fencing_token=parent_lease.get("fencing_token"),
                            expected_revision=latest_parent.get("revision"),
                            reason="awaiting_publication_approval" if m3_job.get("status") != "succeeded" else "publication_readback_pending",
                            result={"learning": "no_learning", "publication_child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                            result_summary="existing publication child is authoritative",
                        )
                    return {"status": existing_m3.get("status"), "child_job_id": publication_child_id, "m3": existing_m3}
            adoption = _publication_binding_checkpoint(existing_child)
            if adoption and adoption.get("m3_job_id"):
                m3_job_id = str(adoption["m3_job_id"])
                if m3_job_id != expected_m3_job_id:
                    raise RoutineError("routine_publication_binding_conflict")
                m3_job = await durable_job_repository.get_job(m3_job_id) or {}
                if m3_job:
                    existing_m3 = await GitHubFollowthroughService()._prepare_job_response(m3_job)
                    latest_parent = await durable_job_repository.get_job(job_id) or current
                    if latest_parent.get("status") == "running":
                        parent_lease = latest_parent.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=parent_lease.get("owner"),
                            fencing_token=parent_lease.get("fencing_token"),
                            expected_revision=latest_parent.get("revision"),
                            reason="awaiting_publication_approval" if m3_job.get("status") != "succeeded" else "publication_readback_pending",
                            result={"learning": "no_learning", "publication_child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                            result_summary="adopted publication child is authoritative",
                        )
                    return {"status": existing_m3.get("status"), "child_job_id": publication_child_id, "m3": existing_m3, "recovery": "adopted"}
        child = await self._admit_child_job(
            current,
            child_id=publication_child_id,
            step_id="github_followthrough",
            inputs={"routine_invocation_job_id": job_id, "invocation_uuid": invocation_uuid},
            authority={
                "routine_id": routine_id,
                "routine_revision": routine_revision,
                "routine_version": authority.get("routine_version"),
                "m3_job_id": expected_m3_job_id,
                "publication_operation_uuid": operation_uuid,
            },
        )
        # M3 creates its own durable approval job.  Record the deterministic
        # identity before crossing that boundary so a crash after M3 admission
        # can be adopted by pause/revoke/recovery instead of becoming an
        # unowned approval reservation.
        child = await self._record_child_checkpoint(
            child,
            checkpoint_id="routine-child:adoption_pending",
            payload={
                "m3_job_id": expected_m3_job_id,
                "publication_operation_uuid": operation_uuid,
                "status": "prepare_pending",
            },
        )
        child_lease = child.get("lease") or {}
        child_context = RoutineStepContext(
            principal_id=owner_principal_id,
            session_id=owner_session_id,
            lease_owner=str(child_lease.get("owner") or ""),
            fencing_token=int(child_lease.get("fencing_token") or 0),
            runtime_job_id=job_id,
        )
        # M3 derives the publication job and its approval from this stable
        # UUID.  The routine stores only the resulting child identity and
        # never embeds the publication body in its reusable files.
        try:
            issue_number: int | None = None
            if action == "create_comment":
                try:
                    issue_number = int(target)
                except (TypeError, ValueError) as exc:
                    raise RoutineError("routine_comment_target_invalid", status_code=422) from exc
            publication_request = PrepareRequest(
                conversation_id=owner_session_id,
                goal_id=str(parent.get("goal_id")),
                goal_revision=int(parent.get("goal_revision") or 0),
                dossier_artifact_id=str(packet.dossier_artifact_id),
                dossier_sha256=str(packet.dossier_sha256),
                connection_revision=int(connection.get("revision") or 0),
                action=action,
                title=req.title if action == "create_issue" else None,
                body=req.body,
                issue_number=issue_number,
                idempotency_key=operation_uuid,
            )
            prepared = await GitHubFollowthroughService().prepare(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                external_mutation_granted=external_mutation_granted,
                request=publication_request,
            )
        except Exception as exc:
            await self._settle_child(child, result={"status": "blocked", "reason_code": type(exc).__name__}, status="blocked", reason="publication_prepare_blocked")
            latest_parent = await durable_job_repository.get_job(job_id) or current
            if latest_parent.get("status") == "running":
                parent_lease = latest_parent.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=latest_parent.get("revision"),
                    reason="publication_prepare_blocked",
                    result={"learning": "no_learning", "publication_child_job_id": publication_child_id, "reason_code": type(exc).__name__},
                    result_summary="publication preparation is blocked",
                )
            if isinstance(exc, GitHubFollowthroughError):
                raise RoutineError(exc.code, str(exc), status_code=exc.status_code) from exc
            raise
        m3_job_id = str(prepared.get("job_id") or "")
        if not m3_job_id:
            await self._settle_child(child, result={"status": "blocked", "reason_code": "m3_job_id_missing"}, status="blocked", reason="publication_prepare_blocked")
            raise RoutineError("publication_child_missing")
        if m3_job_id != expected_m3_job_id:
            cancellation = await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            await self._settle_child(
                child,
                result={
                    "status": "blocked",
                    "reason_code": "routine_publication_binding_conflict",
                    "m3_job_id": m3_job_id,
                    "operator_action": cancellation.get("operator_action") if not cancellation.get("ok") else None,
                },
                status="blocked",
                reason="routine_publication_binding_conflict",
            )
            latest_parent = await durable_job_repository.get_job(job_id) or current
            if latest_parent.get("status") == "running":
                parent_lease = latest_parent.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=latest_parent.get("revision"),
                    reason="routine_publication_binding_conflict",
                    result={
                        "learning": "no_learning",
                        "publication_child_job_id": publication_child_id,
                        "m3_job_id": m3_job_id,
                        "operator_action": cancellation.get("operator_action") if not cancellation.get("ok") else "reconcile_or_cancel",
                    },
                    result_summary="M3 returned a job identity different from the deterministic routine binding",
                )
            raise RoutineError("routine_publication_binding_conflict")
        current = await durable_job_repository.get_job(job_id) or current
        # ``prepare`` creates an M3 durable job before M4 records its own
        # binding.  If pause/revoke won the race, cancel that M3 reservation
        # immediately; otherwise it would be invisible to the routine scan.
        if current.get("status") != "running":
            await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            return {
                "status": current.get("status"),
                "child_job_id": publication_child_id,
                "m3_job_id": m3_job_id,
                "recovery": "parent_not_running",
                "learning": "no_learning",
            }
        parent_lease = current.get("lease") or {}
        try:
            await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id="routine:publication_child_recorded",
                state={"step_id": "github_followthrough", "child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                checkpoint_payload={"step_id": "github_followthrough", "child_job_id": publication_child_id, "m3_job_id": m3_job_id, "approval_id": prepared.get("approval_id")},
                safe=True,
                owner=str(parent_lease.get("owner") or ""),
                fencing_token=int(parent_lease.get("fencing_token") or 0),
                expected_revision=current.get("revision"),
            )
            child = await durable_job_repository.get_job(publication_child_id) or child
            child = await self._record_child_checkpoint(child, checkpoint_id="routine-child:prepared", payload={"m3_job_id": m3_job_id, "approval_id": prepared.get("approval_id"), "status": prepared.get("status")})
        except Exception as exc:
            await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            raise RoutineError("routine_publication_binding_lost", str(exc)) from exc
        child_current = await durable_job_repository.get_job(publication_child_id) or child
        if str(prepared.get("status") or "") == "awaiting_approval":
            await self._settle_child(child_current, result={"status": "awaiting_approval", "approval_id": prepared.get("approval_id"), "m3_job_id": m3_job_id}, status="blocked", reason="awaiting_publication_approval")
            current = await durable_job_repository.get_job(job_id) or current
            parent_lease = current.get("lease") or {}
            await durable_job_repository.transition_job(
                job_id,
                "blocked",
                owner=parent_lease.get("owner"),
                fencing_token=parent_lease.get("fencing_token"),
                expected_revision=current.get("revision"),
                reason="awaiting_publication_approval",
                result={"learning": "no_learning", "child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                result_summary="fresh M3 publication approval is required",
            )
        return {"status": prepared.get("status"), "child_job_id": publication_child_id, "m3": prepared}

    async def _finalize_parent(
        self,
        parent: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        current = await durable_job_repository.get_job(str(parent["job_id"])) or dict(parent)
        if current.get("status") != "running":
            return current
        lease = current.get("lease") or {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        safe = {
            key: result.get(key)
            for key in ("watch_child_job_id", "publication_child_job_id", "m1_job_id", "m3_job_id", "packet_id", "status", "remote_id", "browser_url")
            if result.get(key) is not None
        }
        safe["learning"] = "no_learning"
        digest = _sha(_dump({"reason": reason, **safe}))
        effect = await durable_job_repository.record_effect(
            str(parent["job_id"]),
            effect_type="guardian_routine_outcome",
            target_path=f"routine:{(current.get('declared_authority') or {}).get('routine_id')}",
            target_digest=digest,
            status="succeeded",
            details={"verified": True, "learning": "no_learning", "reason": reason, **safe},
            owner=owner,
            fencing_token=fence,
            expected_revision=current.get("revision"),
        )
        readback = await durable_job_repository.record_readback(
            str(parent["job_id"]),
            target_path=f"routine:{(current.get('declared_authority') or {}).get('routine_id')}",
            effect_id=(effect.get("receipt") or {}).get("effect_id"),
            effect_type="guardian_routine_outcome",
            target_digest=digest,
            status="succeeded",
            details={"verified": True, "learning": "no_learning", "reason": reason, **safe},
            owner=owner,
            fencing_token=fence,
            expected_revision=effect.get("revision"),
        )
        finalized = await durable_job_repository.record_checkpoint(
            str(parent["job_id"]),
            checkpoint_id="routine:finalized",
            state={"reason": reason, **safe},
            checkpoint_payload={"reason": reason, **safe},
            safe=True,
            owner=owner,
            fencing_token=fence,
            expected_revision=readback.get("revision"),
        )
        return await durable_job_repository.transition_job(
            str(parent["job_id"]),
            "succeeded",
            owner=owner,
            fencing_token=fence,
            expected_revision=finalized.get("revision"),
            result=safe,
            result_summary=reason,
        )

    async def recover(
        self,
        routine_id: str,
        job_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        external_mutation_granted: bool = False,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        job = await durable_job_repository.get_job(job_id)
        if not job or job.get("job_kind") != "routine_invocation" or not _owner_matches(job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        if job.get("status") in {"succeeded", "degraded", "cancelled", "unknown_external_effect", "cost_liability"}:
            return {"status": job.get("status"), "job_id": job_id, "recovery": "terminal_receipt", "operator_visible": True}
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=int(authority.get("routine_revision") or routine.revision),
        )
        current = await self._claim_parent_for_recovery(job, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        watch_checkpoint = _job_checkpoint(current, "routine:watch_child_blocked") or _job_checkpoint(current, "routine:watch_readback_verified")
        publication_checkpoint = _job_checkpoint(current, "routine:publication_child_recorded")
        if not publication_checkpoint:
            # The child adoption checkpoint is written immediately before M3
            # prepare.  A crash after M3 creates its approval job but before
            # the later parent checkpoint must still be recoverable by the
            # deterministic invocation/child identity.
            invocation_uuid = str(authority.get("invocation_uuid") or "")
            if invocation_uuid:
                try:
                    operation_uuid = str(
                        uuid.uuid5(uuid.UUID(invocation_uuid), "seraph:guardian-routine:publication")
                    )
                    expected_m3_job_id = _expected_publication_job_id(owner_principal_id, operation_uuid)
                    publication_child_id = _child_job_id(invocation_uuid, "publication")
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RoutineError("routine_invocation_binding_missing") from exc
                child = await durable_job_repository.get_job(publication_child_id)
                child_checkpoint = _publication_binding_checkpoint(child)
                if child_checkpoint:
                    # Parent recovery replaces the parent fence.  A child
                    # bound to the old fence is historical work and cannot be
                    # adopted under the new lease.  Stop pending children
                    # before returning a reprepare/reconcile receipt so an
                    # adoption checkpoint can never strand a live wrapper.
                    current_parent_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                    child_parent_fence = int(child.get("parent_fencing_token") or 0) if isinstance(child, Mapping) else 0
                    current_parent_fence = int(current_parent_lease.get("fencing_token") or 0)
                    if child is not None and child_parent_fence != current_parent_fence:
                        child_status = str(child.get("status") or "")
                        if child_status in {
                            "accepted",
                            "queued",
                            "running",
                            "awaiting_approval",
                            "blocked",
                        }:
                            stale_child = await self._cancel_stale_child(
                                child,
                                parent=current,
                                step_id="github_followthrough",
                                reason_code="routine_publication_parent_fence_stale",
                                operator_action="reprepare_publication",
                            )
                        else:
                            stale_child = {
                                "status": "blocked",
                                "job_id": job_id,
                                "child_job_id": publication_child_id,
                                "child_status": child_status,
                                "reason_code": "routine_publication_parent_fence_stale",
                                "recovery": "reprepare_publication",
                                "operator_action": "reprepare_publication",
                                "operator_visible": True,
                                "learning": "no_learning",
                            }
                        return {
                            **stale_child,
                            "job_id": job_id,
                            "child_job_id": publication_child_id,
                            "recovery": "reprepare_publication" if not stale_child.get("m3_job_id") else stale_child.get("recovery"),
                            "operator_action": stale_child.get("operator_action") or "reprepare_publication",
                            "learning": "no_learning",
                            "operator_visible": True,
                        }
                    m3_job_id = str(child_checkpoint.get("m3_job_id") or "")
                    if not m3_job_id:
                        raise RoutineError("routine_publication_provenance_missing")
                    if m3_job_id != expected_m3_job_id:
                        latest = await durable_job_repository.get_job(job_id) or current
                        if latest.get("status") == "running":
                            lease = latest.get("lease") or {}
                            try:
                                await durable_job_repository.transition_job(
                                    job_id,
                                    "blocked",
                                    owner=lease.get("owner"),
                                    fencing_token=lease.get("fencing_token"),
                                    expected_revision=latest.get("revision"),
                                    reason="routine_publication_binding_conflict",
                                    result={
                                        "learning": "no_learning",
                                        "publication_child_job_id": publication_child_id,
                                        "m3_job_id": m3_job_id,
                                        "operator_action": "reconcile_or_cancel",
                                    },
                                    result_summary="persisted publication child binding does not match its deterministic M3 identity",
                                )
                            except Exception:
                                pass
                        return {
                            "status": "blocked",
                            "job_id": job_id,
                            "child_job_id": publication_child_id,
                            "m3_job_id": m3_job_id,
                            "reason_code": "routine_publication_binding_conflict",
                            "recovery": "reconcile_or_cancel",
                            "operator_action": "reconcile_or_cancel",
                            "learning": "no_learning",
                            "operator_visible": True,
                        }
                    m3_job = await durable_job_repository.get_job(m3_job_id)
                    if not m3_job:
                        return await self._cancel_stale_child(
                            child or {},
                            parent=current,
                            step_id="github_followthrough",
                            reason_code="routine_publication_child_missing",
                            operator_action="restart_routine_invocation",
                            cancel_external=False,
                        )
                    publication_checkpoint = {
                        **dict(child_checkpoint),
                        "child_job_id": publication_child_id,
                    }
                    parent_lease = current.get("lease") or {}
                    try:
                        await durable_job_repository.record_checkpoint(
                            job_id,
                            checkpoint_id="routine:publication_child_recorded",
                            state={
                                "step_id": "github_followthrough",
                                "child_job_id": publication_child_id,
                                "m3_job_id": m3_job_id,
                            },
                            checkpoint_payload={
                                "step_id": "github_followthrough",
                                "child_job_id": publication_child_id,
                                "m3_job_id": m3_job_id,
                                "approval_id": child_checkpoint.get("approval_id"),
                                "recovery": "adopted_child_checkpoint",
                            },
                            safe=True,
                            owner=str(parent_lease.get("owner") or ""),
                            fencing_token=int(parent_lease.get("fencing_token") or 0),
                            expected_revision=current.get("revision"),
                        )
                        current = await durable_job_repository.get_job(job_id) or current
                    except Exception as exc:
                        raise RoutineError("routine_publication_binding_lost", str(exc)) from exc
        if publication_checkpoint:
            m3_job_id = str(publication_checkpoint.get("m3_job_id") or "")
            if not m3_job_id:
                raise RoutineError("routine_publication_provenance_missing")
            publication_child_id = str(publication_checkpoint.get("child_job_id") or "")
            publication_child = (
                await durable_job_repository.get_job(publication_child_id)
                if publication_child_id
                else None
            )
            parent_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            if (
                publication_child
                and publication_child.get("status") in {
                    "accepted",
                    "queued",
                    "running",
                    "awaiting_approval",
                    "blocked",
                }
                and int(publication_child.get("parent_fencing_token") or 0)
                != int(parent_lease.get("fencing_token") or 0)
            ):
                return await self._cancel_stale_child(
                    publication_child,
                    parent=current,
                    step_id="github_followthrough",
                )
            m3_job = await durable_job_repository.get_job(m3_job_id)
            if not m3_job:
                raise RoutineError("routine_publication_child_missing")
            if m3_job.get("status") in {"awaiting_approval", "queued", "running"}:
                if not external_mutation_granted:
                    latest = await durable_job_repository.get_job(job_id) or current
                    if latest.get("status") == "running":
                        lease = latest.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=lease.get("owner"),
                            fencing_token=lease.get("fencing_token"),
                            expected_revision=latest.get("revision"),
                            reason="external_mutation_grant_required",
                            result={"learning": "no_learning", "m3_job_id": m3_job_id},
                            result_summary="publication recovery requires current external-mutation authority",
                        )
                    return {
                        "status": "blocked",
                        "job_id": job_id,
                        "m3_job_id": m3_job_id,
                        "reason_code": "external_mutation_grant_required",
                        "learning": "no_learning",
                        "operator_visible": True,
                    }
                # Recovery may sit blocked while an operator reviews the
                # publication. Re-read the routine and the parent lease at the
                # final handoff point so a pause, revoke, or lease takeover
                # cannot dispatch M3 under stale authority.
                fresh_parent = await durable_job_repository.get_job(job_id) or current
                fresh_authority = fresh_parent.get("declared_authority") if isinstance(fresh_parent.get("declared_authority"), Mapping) else authority
                await self._require_active_routine(
                    routine_id,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    expected_revision=int(fresh_authority.get("routine_revision") or routine.revision),
                )
                claimed_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                fresh_lease = fresh_parent.get("lease") if isinstance(fresh_parent.get("lease"), Mapping) else {}
                if (
                    fresh_parent.get("status") != "running"
                    or int(fresh_lease.get("fencing_token") or 0) != int(claimed_lease.get("fencing_token") or 0)
                ):
                    return {
                        "status": fresh_parent.get("status") or "blocked",
                        "job_id": job_id,
                        "m3_job_id": m3_job_id,
                        "reason_code": "routine_recovery_fence_stale",
                        "recovery": "reconcile",
                        "learning": "no_learning",
                        "operator_visible": True,
                    }
                current = fresh_parent
                m3_result = await GitHubFollowthroughService().execute(
                    owner_principal_id=owner_principal_id,
                    job_id=m3_job_id,
                    owner_session_id=owner_session_id,
                    external_mutation_granted=external_mutation_granted,
                )
                m3_job = await durable_job_repository.get_job(m3_job_id) or m3_job
            else:
                m3_result = m3_job
            reacquired = await self._reacquire_parent_after_child(
                current,
                routine_id=routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            if reacquired is None:
                latest = await durable_job_repository.get_job(job_id) or current
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "m3_job_id": m3_job_id,
                    "child": m3_result,
                    "reason_code": "routine_recovery_fence_stale",
                    "recovery": "reconcile",
                    "learning": "no_learning",
                    "operator_visible": True,
                    "parent_status": latest.get("status") or "blocked",
                }
            current = reacquired
            if m3_job.get("status") == "succeeded" and _verified_readback(m3_job):
                done = await self._finalize_parent(
                    current,
                    result={"publication_child_job_id": publication_checkpoint.get("child_job_id"), "m3_job_id": m3_job_id, "status": "succeeded", "remote_id": m3_result.get("remote_id"), "browser_url": m3_result.get("remote_url")},
                    reason="publication_readback_verified",
                )
                return {"status": "succeeded", "job_id": job_id, "child": m3_result, "learning": "no_learning", "durable": done}
            child_status = str(m3_job.get("status") or m3_result.get("status") or "blocked")
            if child_status in {"unknown_external_effect", "blocked"}:
                lease = current.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=lease.get("owner"),
                    fencing_token=lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason="publication_requires_reconciliation",
                    result={
                        "learning": "no_learning",
                        "m3_job_id": m3_job_id,
                        "child_status": child_status,
                        "operator_action": "reconcile_or_cancel",
                    },
                    result_summary="publication child is blocked or has an unknown external effect; reconcile before retry or cancel",
                )
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "m3_job_id": m3_job_id,
                    "child_status": child_status,
                    "child": m3_result,
                    "reason_code": "publication_requires_reconciliation",
                    "recovery": "reconcile_or_cancel",
                    "operator_action": "reconcile_or_cancel",
                    "learning": "no_learning",
                    "operator_visible": True,
                }
            lease = current.get("lease") or {}
            await durable_job_repository.transition_job(
                job_id,
                "blocked",
                owner=lease.get("owner"),
                fencing_token=lease.get("fencing_token"),
                expected_revision=current.get("revision"),
                reason="awaiting_publication_approval",
                result={"learning": "no_learning", "m3_job_id": m3_job_id, "child_status": child_status},
                result_summary="publication remains pending or requires reconciliation",
            )
            return {"status": "awaiting_publication_approval", "job_id": job_id, "m3_job_id": m3_job_id, "child": m3_result, "learning": "no_learning"}
        if watch_checkpoint:
            m1_job_id = str(watch_checkpoint.get("m1_job_id") or "")
            packet_id = str(watch_checkpoint.get("packet_id") or "")
            watch_child_id = str(watch_checkpoint.get("child_job_id") or "")
            if m1_job_id and packet_id:
                watch_child = (
                    await durable_job_repository.get_job(watch_child_id)
                    if watch_child_id
                    else None
                )
                parent_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                if (
                    watch_child
                    and watch_child.get("status") in {
                        "accepted",
                        "queued",
                        "running",
                        "awaiting_approval",
                        "blocked",
                    }
                    and int(watch_child.get("parent_fencing_token") or 0)
                    != int(parent_lease.get("fencing_token") or 0)
                ):
                    return await self._cancel_stale_child(
                        watch_child,
                        parent=current,
                        step_id="guardian_watch_run",
                    )
                m1_job = await durable_job_repository.get_job(m1_job_id)
                if m1_job and m1_job.get("status") == "awaiting_approval":
                    approval_id = str(watch_checkpoint.get("approval_id") or (m1_job.get("declared_authority") or {}).get("approval_id") or "")
                    approval = await approval_repository.get(approval_id)
                    if approval and approval.status == "approved":
                        async with db_engine.get_session() as db:
                            packet = (await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))).scalars().first()
                            if packet is None:
                                raise RoutineError("routine_watch_packet_not_found")
                            packet_digest = _sha(packet.proposal_text + packet.task_text)
                            watch_id = packet.source_watch_id
                            plan_revision = int(packet.plan_revision)
                            db.expunge(packet)
                        await source_watch_service.execute_packet(
                            watch_id=watch_id,
                            packet_id=packet_id,
                            expected_packet_digest=packet_digest,
                            approval_id=approval_id,
                            expected_approval_revision=m1_job.get("revision"),
                            owner_principal_id=owner_principal_id,
                            owner_session_id=owner_session_id,
                        )
                        m1_job = await durable_job_repository.get_job(m1_job_id) or m1_job
                        watch_checkpoint = {**watch_checkpoint, "status": "succeeded"}
                if m1_job and m1_job.get("status") == "succeeded" and _verified_readback(m1_job):
                    reacquired = await self._reacquire_parent_after_child(
                        current,
                        routine_id=routine_id,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                    if reacquired is None:
                        latest = await durable_job_repository.get_job(job_id) or current
                        return {
                            "status": "blocked",
                            "job_id": job_id,
                            "child_job_id": watch_child_id,
                            "packet_id": packet_id,
                            "reason_code": "routine_recovery_fence_stale",
                            "recovery": "reconcile",
                            "learning": "no_learning",
                            "operator_visible": True,
                            "parent_status": latest.get("status") or "blocked",
                        }
                    current = reacquired
                    latest = current
                    lease = latest.get("lease") or {}
                    updated = await durable_job_repository.record_checkpoint(
                        job_id,
                        checkpoint_id="routine:watch_readback_verified",
                        state={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "status": "succeeded"},
                        checkpoint_payload={**watch_checkpoint, "status": "succeeded", "m1_job_id": m1_job_id, "packet_id": packet_id},
                        safe=True,
                        owner=lease.get("owner"),
                        fencing_token=lease.get("fencing_token"),
                        expected_revision=latest.get("revision"),
                    )
                    latest = await durable_job_repository.get_job(job_id) or updated
                    lease = latest.get("lease") or {}
                    await durable_job_repository.transition_job(job_id, "blocked", owner=lease.get("owner"), fencing_token=lease.get("fencing_token"), expected_revision=latest.get("revision"), reason="awaiting_publication_preview", result={"learning": "no_learning", "watch_child_job_id": watch_child_id, "m1_job_id": m1_job_id, "packet_id": packet_id}, result_summary="fresh M3 publication preview is required")
                    return {"status": "awaiting_publication_preview", "job_id": job_id, "child_job_id": watch_child_id, "packet_id": packet_id, "learning": "no_learning"}
            latest = await durable_job_repository.get_job(job_id) or current
            if latest.get("status") == "running":
                lease = latest.get("lease") or {}
                await durable_job_repository.transition_job(job_id, "blocked", owner=lease.get("owner"), fencing_token=lease.get("fencing_token"), expected_revision=latest.get("revision"), reason="awaiting_child_approval", result={"learning": "no_learning", "watch_child_job_id": watch_child_id}, result_summary="the persisted M1 child still requires approval or recovery")
            return {"status": "awaiting_child_approval", "job_id": job_id, "child_job_id": watch_child_id, "learning": "no_learning"}
        return {"status": current.get("status"), "job_id": job_id, "recovery": "no_child_checkpoint", "operator_visible": True}

    async def pause_or_revoke(self, routine_id: str, *, state: str, expected_revision: int, reason: str, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        if state not in {"paused", "revoked"}:
            raise RoutineError("routine_state_invalid", status_code=422)
        routine = await self._routine(routine_id, owner_principal_id)
        if routine.revision != expected_revision:
            raise RoutineError("routine_revision_stale")
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        # Flip the canonical routine state first. Child admission and child
        # execution both re-read this CAS-bound revision, so a pause/revoke
        # cannot race a new child into an effect after the operator request.
        async with db_engine.get_session() as db:
            result = await db.execute(update(GuardianRoutine).where(GuardianRoutine.id == routine_id, GuardianRoutine.owner_principal_id == owner_principal_id, GuardianRoutine.owner_session_id == owner_session_id, GuardianRoutine.revision == expected_revision, GuardianRoutine.state != "revoked").values(state=state, revision=GuardianRoutine.revision + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        cancellation_failures = await self._cancel_pending_jobs(
            routine_id,
            reason=f"routine_{state}:{reason}",
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if cancellation_failures:
            return {
                "status": "blocked",
                "routine_state": state,
                "routine_id": routine_id,
                "reason": reason,
                "reason_code": "routine_child_cancellation_incomplete",
                "recovery": "reconcile_or_cancel",
                "operator_action": "reconcile_or_cancel",
                "blocked_jobs": cancellation_failures,
                "operator_visible": True,
            }
        return {"status": state, "routine_id": routine_id, "reason": reason}

    async def rollback(self, routine_id: str, req: RoutineRollbackRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        version = await self._version(routine_id, req.target_version)
        if routine.revision != req.expected_routine_revision or not version.installed_package_digest:
            raise RoutineError("routine_revision_or_version_invalid")
        package = self._package_readback(owner_principal_id, owner_session_id)
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise RoutineError("package_review_required")
        async with db_engine.get_session() as db:
            result = await db.execute(update(GuardianRoutine).where(GuardianRoutine.id == routine_id, GuardianRoutine.owner_principal_id == owner_principal_id, GuardianRoutine.owner_session_id == owner_session_id, GuardianRoutine.revision == req.expected_routine_revision, GuardianRoutine.state != "revoked").values(state="active", current_version=req.target_version, revision=GuardianRoutine.revision + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        cancellation_failures = await self._cancel_pending_jobs(
            routine_id,
            reason=f"routine_rollback:{req.reason}",
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if cancellation_failures:
            return {
                "status": "blocked",
                "routine_state": "active",
                "routine_id": routine_id,
                "reason_code": "routine_child_cancellation_incomplete",
                "recovery": "reconcile_or_cancel",
                "operator_action": "reconcile_or_cancel",
                "blocked_jobs": cancellation_failures,
                "operator_visible": True,
            }
        return await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)

routine_service = RoutineService()
routine_router = APIRouter(prefix="/capabilities/routines")


def _operator(request: Request):
    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _http_error(exc: RoutineError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


@routine_router.get("")
async def list_routines(request: Request):
    operator = _operator(request)
    return {"routines": await routine_service.list(owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)}


@routine_router.get("/{routine_id}")
async def get_routine(routine_id: str, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.read(routine_id, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/from-run")
async def create_routine(req: RoutineFromRunRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.from_run(req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/versions")
async def add_routine_version(routine_id: str, req: RoutineVersionRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.add_version(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/install")
async def install_routine(routine_id: str, req: RoutineInstallRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.install(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/activate")
async def activate_routine(routine_id: str, req: RoutineActivateRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.activate(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invoke")
async def invoke_routine(routine_id: str, req: RoutineInvokeRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.invoke(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invocations/{job_id}/execute")
async def execute_routine(routine_id: str, job_id: str, req: RoutineExecuteRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.execute_invocation(routine_id, job_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invocations/{job_id}/prepare-publication")
async def prepare_routine_publication(routine_id: str, job_id: str, req: RoutinePublicationRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.prepare_publication(
            routine_id,
            job_id,
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            external_mutation_granted=_operator_has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION),
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/pause")
async def pause_routine(routine_id: str, req: RoutineRevisionRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.pause_or_revoke(routine_id, state="paused", expected_revision=req.expected_routine_revision, reason=req.reason, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/revoke")
async def revoke_routine(routine_id: str, req: RoutineRevisionRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.pause_or_revoke(routine_id, state="revoked", expected_revision=req.expected_routine_revision, reason=req.reason, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/rollback")
async def rollback_routine(routine_id: str, req: RoutineRollbackRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.rollback(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invocations/{job_id}/recover")
async def recover_routine(routine_id: str, job_id: str, req: RoutineRecoverRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.recover(
            routine_id,
            job_id,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            external_mutation_granted=_operator_has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION),
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


__all__ = [
    "RoutineError",
    "RoutineService",
    "routine_router",
    "routine_service",
]
