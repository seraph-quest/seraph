import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.audit.repository import audit_repository
from src.auth.service import AuthenticatedOperator
from src.goals.contracts import GoalCandidateRequest, GoalSuccessCriterion
from src.guardian.goal_snapshot_to_file import (
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileResult,
    GoalSnapshotToFileService,
)
from src.goals.repository import (
    GoalRevisionConflict,
    deserialize_success_criterion,
    goal_repository,
)
from src.guardian.goal_conditioned_loop import (
    list_goal_loop_receipts,
    propose_goal_candidate,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal

logger = logging.getLogger(__name__)

router = APIRouter()


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1)
    level: str = "daily"
    domain: str = "productivity"
    parent_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    success_criterion: Optional[GoalSuccessCriterion] = None


class GoalUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None
    success_criterion: Optional[GoalSuccessCriterion] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)


class GoalSnapshotRunRequest(BaseModel):
    """Bounded operator input for the first real goal-loop canary."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    file_path: Optional[str] = Field(default=None, min_length=1, max_length=512)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="operator_requested_goal_snapshot", max_length=1_000)
    expected_outcome: str = Field(default="", max_length=1_000)
    cancel_requested: bool = False


GOAL_SNAPSHOT_SERVICE_ID = "service:goal-snapshot"


def _require_authenticated_operator(request: Request) -> AuthenticatedOperator:
    """Use only middleware-authenticated identity; never accept a body actor."""

    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    if not isinstance(operator, AuthenticatedOperator) or principal is None:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    grants = {str(getattr(grant, "value", grant)) for grant in principal.grants}
    if (
        not principal.authenticated
        or principal.revoked
        or not session_id
        or AuthorityGrant.CAPABILITY_EXECUTE.value not in grants
    ):
        raise HTTPException(status_code=401, detail={"code": "session_unavailable"})
    return operator


def _goal_snapshot_service_principal(operator: AuthenticatedOperator) -> TrustPrincipal:
    """Return the fixed least-privilege service identity for one operator run."""

    return TrustPrincipal(
        principal_id=GOAL_SNAPSHOT_SERVICE_ID,
        principal_type=PrincipalType.SERVICE,
        authenticated=True,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id=operator.session_id,
    )


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _goal_payload(goal) -> dict:
    criterion = deserialize_success_criterion(goal)
    return {
        "id": goal.id,
        "parent_id": goal.parent_id,
        "title": goal.title,
        "description": goal.description,
        "level": goal.level,
        "domain": goal.domain,
        "status": goal.status,
        "due_date": goal.due_date.isoformat() if goal.due_date else None,
        "created_at": goal.created_at.isoformat(),
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": criterion.model_dump(mode="json") if criterion else None,
    }


@router.get("/goals")
async def list_goals(
    level: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = None,
):
    """List goals, optionally filtered."""
    goals = await goal_repository.list_goals(level=level, domain=domain, status=status)
    return [_goal_payload(goal) for goal in goals]


@router.get("/goals/tree")
async def get_goal_tree():
    """Get the full goal tree as nested structure."""
    return await goal_repository.get_tree()


@router.get("/goals/dashboard")
async def get_goal_dashboard():
    """Get summary stats for the goals UI."""
    return await goal_repository.get_dashboard()


@router.post("/goals")
async def create_goal(body: GoalCreate):
    """Create a new goal."""
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    goal = await goal_repository.create(
        title=body.title,
        level=body.level,
        domain=body.domain,
        parent_id=body.parent_id,
        description=body.description,
        due_date=due,
        success_criterion=body.success_criterion,
    )
    return {
        "id": goal.id,
        "title": goal.title,
        "level": goal.level,
        "domain": goal.domain,
        "status": goal.status,
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": (
            body.success_criterion.model_dump(mode="json")
            if body.success_criterion
            else None
        ),
    }


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, body: GoalUpdate):
    """Update a goal."""
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    try:
        goal = await goal_repository.update(
            goal_id=goal_id,
            title=body.title,
            description=body.description,
            status=body.status,
            due_date=due,
            success_criterion=body.success_criterion,
            expected_revision=body.expected_revision,
        )
    except GoalRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_goal_revision",
                "goal_id": exc.goal_id,
                "expected_revision": exc.expected,
                "current_revision": exc.current,
                "recovery": "Refresh the goal and resubmit against the current revision.",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    criterion = deserialize_success_criterion(goal)
    return {
        "status": "ok",
        "id": goal.id,
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": criterion.model_dump(mode="json") if criterion else None,
    }


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str):
    """Delete a goal and its descendants."""
    success = await goal_repository.delete(goal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"status": "ok"}


@router.get("/goals/{goal_id}/loop")
async def inspect_goal_loop(goal_id: str):
    """Inspect a goal's criterion and candidate/outcome receipts."""
    goal = await goal_repository.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    criterion = deserialize_success_criterion(goal)
    return {
        "goal": _goal_payload(goal),
        "criterion": criterion.model_dump(mode="json") if criterion else None,
        "receipts": await list_goal_loop_receipts(goal_id),
    }


@router.post("/goals/{goal_id}/candidates")
async def propose_goal_loop_candidate(goal_id: str, body: GoalCandidateRequest):
    """Create one bounded candidate decision for operator inspection."""
    try:
        decision = await propose_goal_candidate(goal_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Goal not found") from exc
    return {
        **decision.model_dump(mode="json"),
        "execution": {
            "available": False,
            "reason": "proposal_only_until_governed_admission_prerequisites_are_available",
        },
    }


@router.post("/goals/{goal_id}/snapshot")
async def run_goal_snapshot(goal_id: str, body: GoalSnapshotRunRequest, request: Request):
    """Run the first real goal-loop canary through the canonical runtime.

    This is an authenticated operator request, not a model-granted permission
    or a second execution path.  The service identity is fixed in code and is
    bound to the operator session for the child durable job; all goal, authority,
    workflow, artifact, and readback checks remain in ``GoalSnapshotToFileService``.
    """

    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    current_revision = max(int(goal.revision or 1), 1)
    if body.expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_goal_revision",
                "goal_id": goal_id,
                "expected_revision": body.expected_revision,
                "current_revision": current_revision,
                "recovery": "Refresh the goal and resubmit against the current revision.",
            },
        )

    file_path = body.file_path or f"goal-snapshots/{goal_id}.md"
    service_principal = _goal_snapshot_service_principal(operator)
    try:
        service_request = GoalSnapshotToFileRequest(
            goal_id=goal_id,
            goal_revision=current_revision,
            file_path=file_path,
            owner_principal_id=GOAL_SNAPSHOT_SERVICE_ID,
            service_id=GOAL_SNAPSHOT_SERVICE_ID,
            session_id=operator.session_id,
            evidence_refs=body.evidence_refs,
            reason=body.reason,
            expected_outcome=body.expected_outcome,
            cancel_requested=body.cancel_requested,
        )
        result = await GoalSnapshotToFileService(
            authority_principal=service_principal,
        ).run(service_request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_goal_snapshot_request", "reason": str(exc)}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "goal_snapshot_authority_denied", "reason": str(exc)}) from exc

    payload: dict[str, Any] = result.model_dump(mode="json") if isinstance(result, GoalSnapshotToFileResult) else dict(result)
    payload["operator_receipt"] = {
        "principal_id": operator.principal.principal_id,
        "session_id_digest": _session_digest(operator.session_id),
        "delegated_service_id": GOAL_SNAPSHOT_SERVICE_ID,
        "authority_boundary": "authenticated_operator_to_fixed_service",
    }
    try:
        await audit_repository.log_event(
            actor=operator.principal.principal_id,
            event_type="goal_snapshot_operator_run",
            tool_name="workflow.goal-snapshot-to-file",
            risk_level="low",
            policy_mode="authenticated_operator",
            summary="Authenticated operator requested goal snapshot canary",
            details={
                "goal_id": goal_id,
                "goal_revision": current_revision,
                "session_id_digest": _session_digest(operator.session_id),
                "delegated_service_id": GOAL_SNAPSHOT_SERVICE_ID,
                "execution_status": payload.get("execution_status"),
                "verification": payload.get("verification"),
                "learning": payload.get("learning"),
                "job_id": payload.get("job_id"),
                "artifact_ref": payload.get("artifact_ref"),
                "reason": payload.get("reason"),
            },
        )
    except Exception:
        logger.exception("goal snapshot operator receipt could not be persisted")
        payload["audit_receipt"] = {
            "status": "degraded",
            "reason": "audit_persistence_failed",
        }
        return JSONResponse(status_code=503, content=payload)
    payload["audit_receipt"] = {"status": "recorded"}
    return payload
