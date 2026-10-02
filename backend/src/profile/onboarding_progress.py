"""Owner-scoped first-result progress, using the existing activity ledger."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.api.goals import _require_authenticated_operator, _goal_payload, _session_digest, GOAL_SNAPSHOT_SERVICE_ID
from src.goals.repository import goal_repository
from src.goals.contracts import GoalAdmissionBudget, GoalSuccessCriterion
from src.audit.repository import audit_repository
from src.db import engine as db_engine
from src.db.models import AuditEvent, WorkBoardStatus, GuardianSourceWatch
from src.guardian.source_watch import parse_public_https_url, source_watch_service, _goal_admission
from src.profile.service import mark_onboarding_complete
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.review import _verified_workflow_readback
from src.work_board.dispatcher import _parse_typed_input, TypedInputError
from src.workflows.job_runtime import durable_job_repository, _digest
from src.tools.filesystem_tool import _safe_resolve, _open_workspace_file

EVENT_TYPE = "onboarding_first_result_step"


class FirstResultProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    starter: Literal["local_snapshot", "public_watch"] = "local_snapshot"
    step: Literal["choose", "preview", "goal_saved", "watch_saved", "task_saved", "admitted", "result_opened"] = "choose"
    journey_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")
    title: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=2048)
    goal_id: str | None = Field(default=None, max_length=128)
    goal_revision: int | None = Field(default=None, ge=1)
    watch_id: str | None = Field(default=None, max_length=128)
    plan_revision: int | None = Field(default=None, ge=1)
    input_artifact_id: str | None = Field(default=None, max_length=512)
    task_id: str | None = Field(default=None, max_length=128)
    watch_task_id: str | None = Field(default=None, max_length=128)

    @field_validator("source")
    @classmethod
    def public_source_only(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Use a public HTTPS URL without credentials, query parameters, or fragments.")
        hostname = (parsed.hostname or "").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError("Use a public HTTPS source.")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Use a public HTTPS source.")
        parse_public_https_url(value)
        return value


async def prepare_starter(request: Request, body: FirstResultProgress) -> dict:
    """Reserve one immutable starter in canonical storage, including cross-tab races."""
    operator = _require_authenticated_operator(request)
    if not body.title.strip() or (body.starter == "public_watch" and not body.source):
        raise HTTPException(status_code=422, detail={"code": "setup_starter_input_required"})
    identity = hashlib.sha256(json.dumps([operator.principal.principal_id, operator.session_id, body.journey_id]).encode()).hexdigest()
    goal_id = f"setup-{identity}"
    now = datetime.now(timezone.utc)
    public = body.starter == "public_watch"
    criterion = GoalSuccessCriterion(
        criterion_id=f"first-result:{body.journey_id}", description="The local goal snapshot exists and independent artifact readback passes.",
        verifier_kind="artifact_readback", target={"starter": body.starter, "source": body.source, "result": "verified local goal snapshot"},
        evidence_refs=[f"operator:first-result:{body.journey_id}"],
    )
    try:
        goal = await goal_repository.create(
            setup_goal_id=goal_id, title=body.title.strip(),
            proactive_enabled=public,
            setup_permission_event=AuditEvent(
                id=f"setup-initial-consent-{identity}",
                actor=operator.principal.principal_id, session_id=operator.session_id,
                event_type="goal_proactive_permission_authorized", tool_name="goal_scheduler",
                risk_level="medium", policy_mode="authenticated_operator",
                summary="Authenticated operator authorized initial first-result goal permission",
                details_json=json.dumps({"goal_id": goal_id, "goal_revision": 1, "proactive_enabled": True,
                    "phase": "before_enable", "session_id_digest": _session_digest(operator.session_id),
                    "service_id": GOAL_SNAPSHOT_SERVICE_ID}),
            ) if public else None,
            description="First-result setup: " + body.starter,
            success_criterion=criterion, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            admission_budget=GoalAdmissionBudget(reviewed_grant=public, grant_id=f"first-result:{body.journey_id}" if public else None,
                max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120, notifications_per_day=0,
                period_started_at=now, period_expires_at=now + timedelta(hours=1), timezone="UTC"),
        )
        watch = None
        if public:
            admitted, reason, _budget = _goal_admission(goal)
            if not admitted:
                raise HTTPException(status_code=409, detail={"code": reason,
                    "recovery": "This saved goal's source consent is disabled, revoked, expired, or otherwise blocked. Open Goals to explicitly review consent and budget; replaying setup never renews permission."})
            watch = await source_watch_service.create_watch(
                setup_watch_id=f"setup-{identity}", owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
                goal_id=goal.id, expected_goal_revision=goal.revision,
                sources=[{"source_key": "primary", "kind": "public_https_text", "target": body.source, "priority": 3}],
                criteria={"include_terms": [], "exclude_terms": [], "min_changed_lines": 1, "min_changed_chars": 1, "max_material_sources": 1},
                schedule={"cron": "0 8 * * *", "timezone": "UTC", "enabled": False}, write_mode="approval_each_run",
            )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={"code": "setup_authorization_storage_unavailable",
            "recovery": "Initial setup authorization could not be saved atomically. Retry after database recovery. If a saved goal is disabled, review its consent explicitly in Goals."}) from exc
    except ValueError as exc:
        if str(exc) == "setup_initial_permission_already_recorded":
            raise HTTPException(status_code=409, detail={"code": str(exc),
                "recovery": "Initial consent for this journey was already recorded. Open Goals to explicitly review recovery; replay cannot recreate a deleted grant."}) from exc
        raise HTTPException(status_code=409, detail={"code": str(exc), "recovery": "This saved journey already has different inputs. Reload its saved setup before continuing."}) from exc
    return {"goal": _goal_payload(goal), "watch": watch}


async def load_progress(request: Request) -> dict:
    operator = _require_authenticated_operator(request)
    async with db_engine.get_session() as db:
        event = (await db.execute(select(AuditEvent).where(
            AuditEvent.actor == operator.principal.principal_id,
            AuditEvent.session_id == operator.session_id,
            AuditEvent.event_type == EVENT_TYPE,
        ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1))).scalar_one_or_none()
    return {"progress": json.loads(event.details_json) if event else None}


async def save_progress(request: Request, body: FirstResultProgress) -> dict:
    if body.step == "result_opened":
        raise HTTPException(status_code=422, detail={"code": "result_readback_required", "recovery": "Open the verified result to finish setup."})
    operator = _require_authenticated_operator(request)
    # Progress is an observation of operator steps, never an authority grant.
    await audit_repository.log_event(
        event_type=EVENT_TYPE, actor=operator.principal.principal_id,
        session_id=operator.session_id, summary=f"First result setup: {body.step}",
        details=body.model_dump(mode="json"),
    )
    return {"progress": body.model_dump(mode="json")}


async def record_setup_skip(request: Request) -> None:
    operator = _require_authenticated_operator(request)
    progress = (await load_progress(request))["progress"] or {}
    await audit_repository.log_event(
        event_type="onboarding_first_result_skipped", actor=operator.principal.principal_id,
        session_id=operator.session_id, summary="Operator skipped first-result setup; accepted work remains in Work",
        details={key: progress.get(key) for key in ("journey_id", "goal_id", "task_id", "watch_task_id")},
    )


async def open_verified_result(request: Request, task_id: str) -> dict:
    operator = _require_authenticated_operator(request)
    progress = (await load_progress(request))["progress"]
    if not progress or progress.get("task_id") != task_id:
        raise HTTPException(status_code=404, detail={"code": "setup_result_unavailable"})
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    try:
        async with db_engine.get_session() as db:
            detail = await WorkBoardRepository().get_detail(db, owner, task_id)
            task = detail["task"]
            attempt = detail["attempts"][0] if detail["attempts"] else None
            expected_capability = "workflow.goal-snapshot-to-file"
            if task.capability_id != expected_capability or task.goal_id != progress.get("goal_id") or task.status is not WorkBoardStatus.done or attempt is None:
                raise HTTPException(status_code=409, detail={"code": "setup_result_not_verified", "recovery": "Refresh the task; finish its approval or recovery step in Work."})
            proof = await _verified_workflow_readback(db, task, attempt)
            if not proof:
                raise HTTPException(status_code=409, detail={"code": "setup_result_not_verified", "recovery": "Inspect the task readback in Work before retrying."})
    except BoardError as exc:
        raise HTTPException(status_code=404, detail={"code": "setup_result_unavailable"}) from exc
    root = await durable_job_repository.get_job(attempt.workflow_run_id)
    memory = "no_learning" if isinstance(root, dict) and any(
        effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
        and effect.get("content_sha256") == proof["content_sha256"]
        and isinstance(effect.get("details"), dict) and effect["details"].get("learning") == "no_learning"
        for effect in root.get("effects", []) if isinstance(effect, dict)
    ) else None
    if memory != "no_learning":
        raise HTTPException(status_code=409, detail={"code": "setup_memory_receipt_required", "recovery": "Inspect the task memory receipt in Work; setup remains incomplete."})
    observation = None
    if progress["starter"] == "public_watch":
        try:
            async with db_engine.get_session() as db:
                watch_detail = await WorkBoardRepository().get_detail(db, owner, progress.get("watch_task_id") or "")
                watch_task = watch_detail["task"]
                watch_attempt = watch_detail["attempts"][0] if watch_detail["attempts"] else None
                watch_proof = await _verified_workflow_readback(db, watch_task, watch_attempt) if watch_attempt else None
                typed_input = _parse_typed_input(watch_task)
                watch_row = (await db.execute(select(GuardianSourceWatch).where(
                    GuardianSourceWatch.id == progress.get("watch_id"),
                    GuardianSourceWatch.owner_principal_id == owner.principal_id,
                    GuardianSourceWatch.owner_session_id == owner.session_id,
                ))).scalar_one_or_none()
            watch = await source_watch_service.get_watch(progress.get("watch_id") or "", owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
            watch_root = await durable_job_repository.get_job(watch_attempt.workflow_run_id) if watch_attempt else None
            valid_memory = isinstance(watch_root, dict) and watch_root.get("result", {}).get("digest") == _digest({"status": "succeeded", "learning": "no_learning", "memory_status": "no_learning"})
            authority = watch_root.get("declared_authority", {}) if isinstance(watch_root, dict) else {}
            observed_plan = progress.get("plan_revision")
            exact_lineage = (
                watch_row is not None and typed_input == {"watch_id": watch_row.id, "expected_plan_revision": observed_plan}
                and authority.get("watch_id") == watch_row.id
                and authority.get("plan_revision") == observed_plan
                and authority.get("goal_id") == watch_row.goal_id == task.goal_id
                and authority.get("goal_revision") == watch_row.goal_revision == watch_task.goal_revision == task.goal_revision
                and authority.get("goal_owner_principal_id") == watch_row.owner_principal_id == owner.principal_id
                and authority.get("goal_owner_session_id") == watch_row.owner_session_id == owner.session_id
                and authority.get("source_set_digest") == watch_row.source_set_digest
                and authority.get("criteria_digest") == watch_row.criteria_digest
                and isinstance(observed_plan, int)
                # Setup permits only its explicit pause after the observed plan.
                and watch_row.plan_revision == observed_plan + 1
                and json.loads(watch_row.sources_json)[0].get("target") == progress.get("source")
                and len(json.loads(watch_row.sources_json)) == 1
                and authority.get("capability_id") == "guardian.research-watch.v1"
                and watch_root.get("status") == "succeeded"
                and watch_root.get("job_kind") == "guardian_source_watch"
                and watch_root.get("plan_revision") == observed_plan
                and watch_proof and watch_proof.get("content_sha256") == hashlib.sha256(b"baseline_initialized").hexdigest()
            )
            if not exact_lineage or not watch_proof or watch_task.status is not WorkBoardStatus.done or watch_task.capability_id != "guardian.research-watch.v1" or watch_task.goal_id != task.goal_id or not watch or watch["state"] != "paused" or watch["schedule"]["enabled"] or watch.get("last_status") != "baseline_initialized" or not watch.get("baselines") or not valid_memory:
                raise ValueError("source observation incomplete")
            observation = {"status": "baseline_initialized", "sources": watch["sources"], "baselines": watch["baselines"], "readback_id": watch_proof["readback_id"], "memory_status": "no_learning", "schedule_state": "paused"}
        except (BoardError, ValueError, TypeError, TypedInputError) as exc:
            raise HTTPException(status_code=409, detail={"code": "setup_source_readback_required", "recovery": "Finish the source observation and pause its watch before creating the local result."}) from exc
    artifacts = json.loads(task.artifact_refs_json or "[]")
    artifact = next((item for item in artifacts if isinstance(item, dict) and item.get("content_sha256") == proof["content_sha256"] and item.get("file_path")), None)
    if artifact is None:
        raise HTTPException(status_code=409, detail={"code": "setup_artifact_readback_required"})
    try:
        path = _safe_resolve(artifact["file_path"])
        # Reuse the descriptor-relative no-follow workspace boundary, including
        # parent-directory replacement and single-link regular-file checks.
        with _open_workspace_file(path, flags=os.O_RDONLY) as descriptor:
            data = bytearray()
            while len(data) <= 64 * 1024:
                chunk = os.read(descriptor, 64 * 1024 + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
        if len(data) > 64 * 1024 or hashlib.sha256(data).hexdigest() != proof["content_sha256"]:
            raise ValueError("artifact changed")
        content = data.decode("utf-8")
    except (OSError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=409, detail={"code": "setup_artifact_changed", "recovery": "Inspect the local artifact in Work. Do not rerun an uncertain task automatically."}) from exc
    progress = {**progress, "step": "result_opened"}
    if (await load_progress(request))["progress"].get("step") != "result_opened":
        await audit_repository.log_event(event_type=EVENT_TYPE, actor=owner.principal_id, session_id=owner.session_id, summary="First verified goal result opened", details=progress)
    # The activity checkpoint and legacy profile live in separate transactions.
    # Reconcile the flag on every authoritative retry, after output revalidation.
    try:
        await mark_onboarding_complete()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={"code": "setup_profile_completion_unavailable",
            "recovery": "Profile completion could not be saved. Open this same verified result again after database recovery; accepted work is retained."}) from exc
    return {"progress": progress, "task_id": task_id, "content": content, "file_path": artifact["file_path"], "content_sha256": proof["content_sha256"], "readback_id": proof["readback_id"], "memory_status": memory, "observation": observation}
