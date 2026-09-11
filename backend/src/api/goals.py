import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from src.audit.repository import audit_repository
from src.auth.service import AuthenticatedOperator
from src.goals.contracts import GoalAdmissionBudget, GoalCandidateRequest, GoalSuccessCriterion
from src.guardian.goal_snapshot_to_file import (
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileResult,
    GoalSnapshotToFileService,
    normalize_workspace_relative_path,
)
from src.memory.control import (
    get_strategy_delta,
    get_strategy_delta_by_source_event,
    list_strategy_deltas,
    record_strategy_delta_proposal,
    update_strategy_delta,
)
from src.goals.repository import (
    GoalOwnershipConflict,
    GoalRevisionConflict,
    deserialize_admission_budget,
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
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    level: str = "daily"
    domain: str = "productivity"
    parent_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    success_criterion: Optional[GoalSuccessCriterion] = None
    proactive_enabled: bool = False
    admission_budget: Optional[GoalAdmissionBudget] = Field(
        default=None,
        validation_alias=AliasChoices("admission_budget", "budget"),
    )


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    description: Optional[str] = None
    level: Optional[str] = None
    domain: Optional[str] = None
    status: Optional[str] = None
    parent_id: Optional[str] = None
    due_date: Optional[str] = None
    success_criterion: Optional[GoalSuccessCriterion] = None
    proactive_enabled: Optional[bool] = None
    admission_budget: Optional[GoalAdmissionBudget] = Field(
        default=None,
        validation_alias=AliasChoices("admission_budget", "budget"),
    )
    expected_revision: Optional[int] = Field(default=None, ge=1)


class GoalDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional keeps the existing DELETE-without-body API compatible while the
    # repository still binds that request to the revision read at admission.
    expected_revision: Optional[int] = Field(default=None, ge=1)


class GoalStrategyCorrection(BaseModel):
    """Authenticated, bounded correction for a goal's web-brief choice."""

    model_config = ConfigDict(extra="forbid")

    correction_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=1)
    query: Optional[str] = Field(default=None, min_length=1, max_length=500)
    file_path: Optional[str] = Field(default=None, min_length=1, max_length=512)
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("correction_id", "reason", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("file_path", mode="before")
    @classmethod
    def _normalize_file_path(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_workspace_relative_path(value)


class GoalStrategyRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: Any) -> str:
        return str(value or "").strip()


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
GOAL_SNAPSHOT_MANUAL_BUDGET_BOUNDARY = (
    "authenticated_manual_operator_request_outside_standing_goal_admission_budget"
)


def _require_authenticated_operator(request: Request) -> AuthenticatedOperator:
    """Use only middleware-authenticated identity; never accept a body actor."""

    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    if not isinstance(operator, AuthenticatedOperator) or principal is None:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    grants = {str(getattr(grant, "value", grant)) for grant in principal.grants}
    if (
        principal.principal_type is not PrincipalType.OPERATOR
        or
        not principal.authenticated
        or principal.revoked
        or not str(getattr(principal, "principal_id", "") or "").strip()
        or not session_id
        or str(getattr(principal, "session_id", "") or "").strip() != session_id
        or str(getattr(principal, "operator_session_id", "") or "").strip() != session_id
        or AuthorityGrant.CAPABILITY_EXECUTE.value not in grants
    ):
        raise HTTPException(status_code=401, detail={"code": "session_unavailable"})
    return operator


def _require_goal_owner(goal: Any, operator: AuthenticatedOperator) -> None:
    """Bind public goal reads/proposals to the canonical persisted owner."""

    owner_id = str(getattr(goal, "owner_principal_id", "") or "").strip()
    owner_session = str(getattr(goal, "owner_session_id", "") or "").strip()
    if not owner_id or not owner_session:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "goal_owner_unbound",
                "recovery": "Bind the goal through the authenticated goals API before using its public loop routes.",
            },
        )
    if owner_id != operator.principal.principal_id or owner_session != operator.session_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "goal_owner_mismatch"},
        )


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


def _strategy_storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "strategy_delta_storage_unavailable",
            "recovery": "Check the local database readiness receipt and retry after recovery.",
        },
    )


_STRATEGY_TARGET_KEYS = {"query", "file_path", "priority", "strategy_delta_id"}


def _canonical_strategy_target(goal_id: str, criterion: GoalSuccessCriterion) -> dict[str, Any]:
    """Return the only target shape the correction endpoint may mutate."""

    target = criterion.target
    if not isinstance(target, dict):
        raise ValueError("goal criterion does not have an explicit web-brief target")
    unknown = set(target) - _STRATEGY_TARGET_KEYS
    if unknown:
        raise ValueError("goal criterion target contains unsupported strategy fields")
    raw_query = target.get("query")
    if not isinstance(raw_query, str):
        raise ValueError("goal criterion target query must be a string")
    query = raw_query.strip()
    if not query or len(query) > 500:
        raise ValueError("goal criterion target must contain a bounded query")
    raw_file_path = target.get("file_path")
    file_path = normalize_workspace_relative_path(
        raw_file_path if raw_file_path is not None else f"web-briefs/{goal_id}.md"
    )
    priority = target.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
        raise ValueError("goal criterion target priority must be an integer from 0 to 100")
    canonical = {
        "query": query,
        "file_path": file_path,
        "priority": priority,
    }
    strategy_delta_id = target.get("strategy_delta_id")
    if strategy_delta_id is not None:
        if not isinstance(strategy_delta_id, str):
            raise ValueError("goal criterion strategy_delta_id must be a string")
        normalized_delta_id = strategy_delta_id.strip()
        if not normalized_delta_id or len(normalized_delta_id) > 128:
            raise ValueError("goal criterion strategy_delta_id is invalid")
        canonical["strategy_delta_id"] = normalized_delta_id
    return canonical


def _strategy_delta_id(goal_id: str, correction_id: str) -> str:
    """Derive a stable bounded identity so retries cannot create another delta."""

    seed = f"strategy-correction:{goal_id}:{correction_id}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def _build_strategy_target(
    goal_id: str,
    criterion: GoalSuccessCriterion,
    correction: GoalStrategyCorrection,
    delta_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _canonical_strategy_target(goal_id, criterion)
    if correction.query is None and correction.file_path is None and correction.priority is None:
        raise ValueError("correction must change query, file_path, or priority")
    after = {
        "query": correction.query if correction.query is not None else before["query"],
        "file_path": correction.file_path if correction.file_path is not None else before["file_path"],
        "priority": correction.priority if correction.priority is not None else before["priority"],
        "strategy_delta_id": delta_id,
    }
    return before, after


async def _record_strategy_delta_audit(
    operator: AuthenticatedOperator,
    *,
    event_type: str,
    goal_id: str,
    delta_id: str,
    status: str,
    reason: str,
    goal_revision: int,
) -> dict[str, Any]:
    """Persist a correction receipt, returning degraded state on audit outage."""

    try:
        event = await audit_repository.log_event(
            actor=operator.principal.principal_id,
            event_type=event_type,
            tool_name="goal_strategy_correction",
            risk_level="medium",
            policy_mode="authenticated_operator",
            summary=(
                "Authenticated operator applied a bounded goal strategy correction"
                if status == "applied"
                else "Authenticated operator rolled back a bounded goal strategy correction"
            ),
            details={
                "goal_id": goal_id,
                "delta_id": delta_id,
                "status": status,
                "goal_revision": goal_revision,
                "reason": reason,
                "session_id_digest": _session_digest(operator.session_id),
                "authority_boundary": "authenticated_operator_goal_revision_cas",
            },
        )
        return {"status": "recorded", "event_id": event.id}
    except Exception:
        logger.exception("goal strategy correction audit could not be persisted")
        return {"status": "degraded", "reason": "audit_persistence_failed"}


async def _reject_pending_strategy_delta(delta_id: str) -> None:
    """Do not overwrite an applied receipt when a duplicate request loses CAS."""

    try:
        current = await get_strategy_delta(delta_id)
        if current is not None and current.status == "proposed":
            await update_strategy_delta(
                delta_id,
                status="rejected",
                expected_status="proposed",
            )
    except Exception:
        logger.exception("strategy delta rejection receipt could not be persisted")


async def _record_proactive_permission(
    operator: AuthenticatedOperator,
    *,
    goal_id: str,
    enabled: bool,
    revision: int,
    phase: str,
) -> None:
    """Record an authenticated permission authorization or applied change."""

    try:
        await audit_repository.log_event(
            actor=operator.principal.principal_id,
            event_type=(
                "goal_proactive_permission_authorized"
                if phase == "before_enable"
                else "goal_proactive_permission_changed"
            ),
            tool_name="goal_scheduler",
            risk_level="medium",
            policy_mode="authenticated_operator",
            summary=(
                "Authenticated operator authorized goal proactive permission"
                if phase == "before_enable"
                else "Authenticated operator changed goal proactive permission"
            ),
            details={
                "goal_id": goal_id,
                "goal_revision": revision,
                "proactive_enabled": bool(enabled),
                "phase": phase,
                "session_id_digest": _session_digest(operator.session_id),
                "service_id": GOAL_SNAPSHOT_SERVICE_ID,
            },
        )
    except Exception as exc:
        logger.exception("goal proactive permission receipt could not be persisted")
        raise HTTPException(
            status_code=503,
            detail={"code": "proactive_permission_audit_unavailable"},
        ) from exc


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
        "proactive_enabled": bool(getattr(goal, "proactive_enabled", False)),
        "owner_principal_id": getattr(goal, "owner_principal_id", None),
        "owner_session_id": getattr(goal, "owner_session_id", None),
        "admission_budget": (
            deserialize_admission_budget(goal).model_dump(mode="json")
            if deserialize_admission_budget(goal) else None
        ),
    }


@router.get("/goals")
async def list_goals(
    request: Request,
    level: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = None,
):
    """List the authenticated operator's goals, optionally filtered."""
    operator = _require_authenticated_operator(request)
    goals = await goal_repository.list_goals(
        level=level,
        domain=domain,
        status=status,
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
    )
    return [_goal_payload(goal) for goal in goals]


@router.get("/goals/tree")
async def get_goal_tree(request: Request):
    """Get the authenticated operator's goal tree as nested structure."""
    operator = _require_authenticated_operator(request)
    return await goal_repository.get_tree(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
    )


@router.get("/goals/dashboard")
async def get_goal_dashboard(request: Request):
    """Get summary stats for the authenticated operator's goals."""
    operator = _require_authenticated_operator(request)
    return await goal_repository.get_dashboard(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
    )


@router.post("/goals")
async def create_goal(body: GoalCreate, request: Request):
    """Create a new goal."""
    operator = _require_authenticated_operator(request)
    if body.parent_id:
        parent = await goal_repository.get(body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent goal not found")
        # This also rejects a legacy/unbound parent before the child row exists.
        _require_goal_owner(parent, operator)
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    try:
        goal = await goal_repository.create(
            title=body.title,
            level=body.level,
            domain=body.domain,
            parent_id=body.parent_id,
            description=body.description,
            due_date=due,
            success_criterion=body.success_criterion,
            proactive_enabled=False,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            admission_budget=body.admission_budget,
        )
    except GoalOwnershipConflict as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.proactive_enabled:
        await _record_proactive_permission(
            operator,
            goal_id=goal.id,
            enabled=True,
            revision=max(int(goal.revision or 1), 1),
            phase="before_enable",
        )
        goal = await goal_repository.update(
            goal_id=goal.id,
            proactive_enabled=True,
            expected_owner_principal_id=operator.principal.principal_id,
            expected_owner_session_id=operator.session_id,
            expected_revision=max(int(goal.revision or 1), 1),
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
        "proactive_enabled": bool(getattr(goal, "proactive_enabled", False)),
        "owner_principal_id": getattr(goal, "owner_principal_id", None),
        "owner_session_id": getattr(goal, "owner_session_id", None),
    }


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, body: GoalUpdate, request: Request):
    """Update a goal."""
    operator = _require_authenticated_operator(request)
    current = await goal_repository.get(goal_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    # Legacy rows without both owner fields have no trustworthy public-session
    # binding.  They must be migrated through an explicit trusted path rather
    # than claimed by whichever authenticated session submits this request.
    _require_goal_owner(current, operator)
    current_revision = max(int(current.revision or 1), 1)
    if body.expected_revision is not None and body.expected_revision != current_revision:
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
    requested_parent = body.parent_id if "parent_id" in body.model_fields_set else None
    if "parent_id" in body.model_fields_set and requested_parent:
        parent = await goal_repository.get(requested_parent)
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent goal not found")
        _require_goal_owner(parent, operator)
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    if body.proactive_enabled:
        await _record_proactive_permission(
            operator,
            goal_id=goal_id,
            enabled=True,
            revision=current_revision,
            phase="before_enable",
        )
    try:
        update_kwargs = {
            "goal_id": goal_id,
            "title": body.title,
            "description": body.description,
            "level": body.level,
            "domain": body.domain,
            "status": body.status,
            "due_date": due,
            "success_criterion": body.success_criterion,
            "proactive_enabled": body.proactive_enabled,
            "admission_budget": body.admission_budget,
            "expected_owner_principal_id": operator.principal.principal_id,
            "expected_owner_session_id": operator.session_id,
            "expected_revision": body.expected_revision,
        }
        if "parent_id" in body.model_fields_set:
            update_kwargs["parent_id"] = requested_parent
        goal = await goal_repository.update(
            **update_kwargs,
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
    except GoalOwnershipConflict as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    if body.proactive_enabled is False:
        await _record_proactive_permission(
            operator,
            goal_id=goal_id,
            enabled=False,
            revision=max(int(goal.revision or 1), 1),
            phase="after_disable",
        )
    criterion = deserialize_success_criterion(goal)
    return {
        "status": "ok",
        "id": goal.id,
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": criterion.model_dump(mode="json") if criterion else None,
        "proactive_enabled": bool(getattr(goal, "proactive_enabled", False)),
    }


@router.post("/goals/{goal_id}/strategy-corrections")
async def apply_goal_strategy_correction(
    goal_id: str,
    body: GoalStrategyCorrection,
    request: Request,
):
    """Apply one authenticated, reversible correction to a web-brief target.

    The operator identity comes from middleware.  The compare-and-swap goal
    revision is the only authority for changing the target; the correction
    cannot enable proactivity, add capabilities, or widen source scope.
    """

    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    _require_goal_owner(goal, operator)
    current_revision = max(int(goal.revision or 1), 1)
    delta_id = _strategy_delta_id(goal_id, body.correction_id)
    try:
        existing = await get_strategy_delta_by_source_event(body.correction_id)
    except SQLAlchemyError as exc:
        raise _strategy_storage_unavailable() from exc
    if body.expected_revision != current_revision:
        if (
            existing is not None
            and existing.goal_id == goal_id
            and existing.delta_id == delta_id
            and existing.goal_revision_before == body.expected_revision
            and existing.status in {"applied", "proposed"}
        ):
            criterion = deserialize_success_criterion(goal)
            try:
                current_target = _canonical_strategy_target(goal_id, criterion) if criterion else None
            except ValueError:
                current_target = None
            if current_target == existing.after:
                if existing.status == "proposed":
                    finalized = await update_strategy_delta(
                        existing.delta_id,
                        status="applied",
                        goal_revision_after=current_revision,
                        expected_status="proposed",
                    )
                    if finalized is None:
                        return JSONResponse(
                            status_code=503,
                            content={
                                "status": "degraded",
                                "goal": _goal_payload(goal),
                                "delta": existing.as_payload(),
                                "audit_receipt": {
                                    "status": "degraded",
                                    "reason": "strategy_delta_receipt_missing",
                                },
                            },
                        )
                    existing = finalized
                    audit_receipt = await _record_strategy_delta_audit(
                        operator,
                        event_type="goal_strategy_delta_applied",
                        goal_id=goal_id,
                        delta_id=existing.delta_id,
                        status="applied",
                        reason=existing.reason,
                        goal_revision=current_revision,
                    )
                    if audit_receipt["status"] != "recorded":
                        return JSONResponse(
                            status_code=503,
                            content={
                                "status": "replayed",
                                "goal": _goal_payload(goal),
                                "delta": existing.as_payload(),
                                "audit_receipt": audit_receipt,
                            },
                        )
                return {
                    "status": "replayed",
                    "goal": _goal_payload(goal),
                    "delta": existing.as_payload(),
                    "audit_receipt": {"status": "replayed"},
                }
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
    criterion = deserialize_success_criterion(goal)
    if criterion is None or criterion.verifier_kind is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_correction_requires_configured_criterion"},
        )
    if criterion.verifier_kind.value != "artifact_readback":
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_correction_requires_artifact_readback"},
        )

    if existing is not None:
        if existing.goal_id != goal_id or existing.delta_id != delta_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "correction_id_already_bound_to_another_delta"},
            )
        if existing.goal_revision_before != body.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={"code": "correction_id_reused_with_different_revision"},
            )
        if existing.status == "applied":
            current_target = _canonical_strategy_target(goal_id, criterion)
            if current_target != existing.after:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "strategy_target_changed_after_correction"},
                )
            payload = {
                "status": "replayed",
                "goal": _goal_payload(goal),
                "delta": existing.as_payload(),
                "audit_receipt": {"status": "replayed"},
            }
            return payload
        if existing.status != "proposed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "strategy_correction_not_replayable",
                    "status": existing.status,
                },
            )
        before = existing.before
        after = existing.after
        try:
            requested_before, requested_after = _build_strategy_target(
                goal_id,
                criterion,
                body,
                delta_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_strategy_correction", "reason": str(exc)},
            ) from exc
        if requested_before != before or requested_after != after:
            raise HTTPException(
                status_code=409,
                detail={"code": "correction_id_reused_with_different_target"},
            )
        if _canonical_strategy_target(goal_id, criterion) != before:
            raise HTTPException(
                status_code=409,
                detail={"code": "strategy_target_changed_before_retry"},
            )
    else:
        try:
            before, after = _build_strategy_target(goal_id, criterion, body, delta_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_strategy_correction", "reason": str(exc)},
            ) from exc
        try:
            existing = await record_strategy_delta_proposal(
                goal_id=goal_id,
                source_event_id=body.correction_id,
                before=before,
                after=after,
                author_id=operator.principal.principal_id,
                goal_revision_before=current_revision,
                reason=body.reason,
                delta_id=delta_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_strategy_correction", "reason": str(exc)},
            ) from exc
        except SQLAlchemyError as exc:
            raise _strategy_storage_unavailable() from exc
        if existing.delta_id != delta_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "correction_id_replay_identity_mismatch"},
            )
        if existing.before != before or existing.after != after:
            raise HTTPException(
                status_code=409,
                detail={"code": "correction_id_reused_with_different_target"},
            )

    try:
        updated_criterion = criterion.model_copy(update={"target": after})
        updated_goal = await goal_repository.update(
            goal_id=goal_id,
            success_criterion=updated_criterion,
            expected_owner_principal_id=operator.principal.principal_id,
            expected_owner_session_id=operator.session_id,
            expected_revision=body.expected_revision,
        )
    except GoalRevisionConflict as exc:
        await _reject_pending_strategy_delta(existing.delta_id)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_goal_revision",
                "goal_id": exc.goal_id,
                "expected_revision": exc.expected,
                "current_revision": exc.current,
                "recovery": "Refresh the goal and submit a new correction id.",
            },
        ) from exc
    except GoalOwnershipConflict as exc:
        await _reject_pending_strategy_delta(existing.delta_id)
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    except ValueError as exc:
        await _reject_pending_strategy_delta(existing.delta_id)
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_strategy_correction", "reason": str(exc)},
        ) from exc
    if updated_goal is None:
        await _reject_pending_strategy_delta(existing.delta_id)
        raise HTTPException(status_code=404, detail="Goal not found")

    try:
        updated_delta = await update_strategy_delta(
            existing.delta_id,
            status="applied",
            goal_revision_after=max(int(updated_goal.revision or 1), 1),
            expected_status=("proposed", "rejected"),
        )
    except Exception:
        logger.exception("strategy delta status could not be persisted after goal update")
        updated_delta = None
    if updated_delta is None:
        logger.error("strategy delta disappeared after goal update: %s", existing.delta_id)
        payload = {
            "status": "degraded",
            "goal": _goal_payload(updated_goal),
            "delta": existing.as_payload(),
            "audit_receipt": {"status": "degraded", "reason": "strategy_delta_receipt_missing"},
        }
        return JSONResponse(status_code=503, content=payload)

    audit_receipt = await _record_strategy_delta_audit(
        operator,
        event_type="goal_strategy_delta_applied",
        goal_id=goal_id,
        delta_id=updated_delta.delta_id,
        status="applied",
        reason=body.reason,
        goal_revision=max(int(updated_goal.revision or 1), 1),
    )
    payload = {
        "status": "applied",
        "goal": _goal_payload(updated_goal),
        "delta": updated_delta.as_payload(),
        "audit_receipt": audit_receipt,
    }
    if audit_receipt["status"] != "recorded":
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.post("/goals/{goal_id}/strategy-corrections/{delta_id}/rollback")
async def rollback_goal_strategy_correction(
    goal_id: str,
    delta_id: str,
    body: GoalStrategyRollback,
    request: Request,
):
    """Restore the exact prior bounded target under a fresh goal CAS."""

    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    _require_goal_owner(goal, operator)
    try:
        delta = await get_strategy_delta(delta_id)
    except SQLAlchemyError as exc:
        raise _strategy_storage_unavailable() from exc
    if delta is None or delta.goal_id != goal_id:
        raise HTTPException(status_code=404, detail="Strategy delta not found")
    if delta.status == "rolled_back":
        return {
            "status": "replayed",
            "goal": _goal_payload(goal),
            "delta": delta.as_payload(),
            "audit_receipt": {"status": "replayed"},
        }
    if delta.status != "applied":
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_delta_not_rollbackable", "status": delta.status},
        )
    current_revision = max(int(goal.revision or 1), 1)
    criterion = deserialize_success_criterion(goal)
    if criterion is None or criterion.verifier_kind is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_rollback_requires_configured_criterion"},
        )
    if criterion.verifier_kind.value != "artifact_readback":
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_rollback_requires_artifact_readback"},
        )
    try:
        current_target = _canonical_strategy_target(goal_id, criterion)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_target_unreadable", "reason": str(exc)},
        ) from exc
    if body.expected_revision != current_revision:
        # If the goal update succeeded but the delta receipt write was lost,
        # allow the same rollback request to finish the durable state only when
        # the target and expected one-step revision prove that exact recovery.
        if (
            current_target == delta.before
            and delta.goal_revision_after is not None
            and current_revision == delta.goal_revision_after + 1
        ):
            finalized = await update_strategy_delta(
                delta.delta_id,
                status="rolled_back",
                goal_revision_after=current_revision,
                rollback_target_id=delta.delta_id,
                expected_status="applied",
            )
            if finalized is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "degraded",
                        "goal": _goal_payload(goal),
                        "delta": delta.as_payload(),
                        "audit_receipt": {
                            "status": "degraded",
                            "reason": "strategy_delta_receipt_missing",
                        },
                    },
                )
            audit_receipt = await _record_strategy_delta_audit(
                operator,
                event_type="goal_strategy_delta_rolled_back",
                goal_id=goal_id,
                delta_id=finalized.delta_id,
                status="rolled_back",
                reason=body.reason,
                goal_revision=current_revision,
            )
            payload = {
                "status": "rolled_back",
                "goal": _goal_payload(goal),
                "delta": finalized.as_payload(),
                "audit_receipt": audit_receipt,
            }
            if audit_receipt["status"] != "recorded":
                return JSONResponse(status_code=503, content=payload)
            return payload
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_goal_revision",
                "goal_id": goal_id,
                "expected_revision": body.expected_revision,
                "current_revision": current_revision,
                "recovery": "Refresh the goal and resubmit the rollback against the current revision.",
            },
        )
    if current_target != delta.after:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "strategy_target_changed_after_correction",
                "recovery": "Inspect the current goal and create a new correction or rollback target.",
            },
        )
    try:
        restored_criterion = criterion.model_copy(update={"target": delta.before})
        updated_goal = await goal_repository.update(
            goal_id=goal_id,
            success_criterion=restored_criterion,
            expected_owner_principal_id=operator.principal.principal_id,
            expected_owner_session_id=operator.session_id,
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
                "recovery": "Refresh the goal and resubmit the rollback against the current revision.",
            },
        ) from exc
    except GoalOwnershipConflict as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    if updated_goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        updated_delta = await update_strategy_delta(
            delta.delta_id,
            status="rolled_back",
            goal_revision_after=max(int(updated_goal.revision or 1), 1),
            rollback_target_id=delta.delta_id,
            expected_status="applied",
        )
    except Exception:
        logger.exception("strategy delta rollback status could not be persisted")
        updated_delta = None
    if updated_delta is None:
        payload = {
            "status": "degraded",
            "goal": _goal_payload(updated_goal),
            "delta": delta.as_payload(),
            "audit_receipt": {"status": "degraded", "reason": "strategy_delta_receipt_missing"},
        }
        return JSONResponse(status_code=503, content=payload)
    audit_receipt = await _record_strategy_delta_audit(
        operator,
        event_type="goal_strategy_delta_rolled_back",
        goal_id=goal_id,
        delta_id=updated_delta.delta_id,
        status="rolled_back",
        reason=body.reason,
        goal_revision=max(int(updated_goal.revision or 1), 1),
    )
    payload = {
        "status": "rolled_back",
        "goal": _goal_payload(updated_goal),
        "delta": updated_delta.as_payload(),
        "audit_receipt": audit_receipt,
    }
    if audit_receipt["status"] != "recorded":
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.delete("/goals/{goal_id}")
async def delete_goal(
    goal_id: str,
    request: Request,
    body: GoalDeleteRequest | None = None,
):
    """Delete a goal and its descendants."""
    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    # Preserve the repository's descendant deletion/tombstone behavior only
    # after the canonical public owner/session binding has been verified.
    _require_goal_owner(goal, operator)
    current_revision = max(int(goal.revision or 1), 1)
    expected_revision = (
        body.expected_revision
        if body is not None and body.expected_revision is not None
        else current_revision
    )
    if expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_goal_revision",
                "goal_id": goal_id,
                "expected_revision": expected_revision,
                "current_revision": current_revision,
                "recovery": "Refresh the goal and resubmit against the current revision.",
            },
        )
    try:
        success = await goal_repository.delete(
            goal_id,
            expected_owner_principal_id=operator.principal.principal_id,
            expected_owner_session_id=operator.session_id,
            expected_revision=expected_revision,
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
    except GoalOwnershipConflict as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"status": "ok"}


@router.get("/goals/{goal_id}/loop")
async def inspect_goal_loop(goal_id: str, request: Request):
    """Inspect a goal's criterion and candidate/outcome receipts."""
    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    _require_goal_owner(goal, operator)
    criterion = deserialize_success_criterion(goal)
    try:
        strategy_deltas = await list_strategy_deltas(goal_id)
    except SQLAlchemyError as exc:
        raise _strategy_storage_unavailable() from exc
    return {
        "goal": _goal_payload(goal),
        "criterion": criterion.model_dump(mode="json") if criterion else None,
        "receipts": await list_goal_loop_receipts(goal_id),
        "strategy_deltas": [delta.as_payload() for delta in strategy_deltas],
    }


@router.post("/goals/{goal_id}/candidates")
async def propose_goal_loop_candidate(
    goal_id: str,
    body: GoalCandidateRequest,
    request: Request,
):
    """Create one bounded candidate decision for operator inspection."""
    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    _require_goal_owner(goal, operator)
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
    Reviewed standing-goal admission budgets apply to scheduler admissions;
    this explicit operator canary is a separate manual boundary and records
    that fact in its operator and audit receipts.
    """

    operator = _require_authenticated_operator(request)
    goal = await goal_repository.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    _require_goal_owner(goal, operator)
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
            goal_owner_principal_id=goal.owner_principal_id,
            goal_owner_session_id=goal.owner_session_id,
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
        "budget_boundary": GOAL_SNAPSHOT_MANUAL_BUDGET_BOUNDARY,
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
                "budget_boundary": GOAL_SNAPSHOT_MANUAL_BUDGET_BOUNDARY,
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
