"""Strategist tick — periodic strategic reasoning via restricted agent."""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter

from config.settings import settings
from src.approval.runtime import get_current_trust_principal
from src.agent.strategist import parse_strategist_response, run_strategist_decision_completion
from src.audit.runtime import log_scheduler_job_event
from src.db.models import Goal
from src.goals.repository import deserialize_success_criterion, goal_repository
from src.guardian.goal_snapshot_to_file import (
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileResult,
    GoalSnapshotToFileService,
)
from src.guardian.state import build_guardian_state
from src.llm_runtime import (
    _finish_request,
    _mark_request_timed_out,
    _register_request,
    reset_current_llm_request_id,
    set_current_llm_request_id,
)
from src.models.schemas import WSResponse
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobLeaseError,
    DurableJobSpec,
    DurableJobTransitionError,
    durable_job_repository,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal

logger = logging.getLogger(__name__)

_STRATEGIST_SERVICE_ID = "service:strategist"
_STRATEGIST_RUNNER_ID = "scheduler:strategist_tick"
_STRATEGIST_CAPABILITY_VERSION = "strategist-tick-v1"


def _reasoning_digest(reasoning: object) -> str:
    """Keep free-form model reasoning out of durable receipts."""
    return hashlib.sha256(str(reasoning or "").encode("utf-8")).hexdigest()


def _service_principal_id() -> str:
    runtime_principal = get_current_trust_principal()
    if runtime_principal is not None and (
        not runtime_principal.authenticated or runtime_principal.revoked
    ):
        raise DurableJobTransitionError("strategist scheduler authority is not active")
    if runtime_principal is not None and runtime_principal.principal_id.strip():
        return runtime_principal.principal_id
    return _STRATEGIST_SERVICE_ID


def _occurrence_identity(now: datetime | None = None) -> str:
    """Return a stable idempotency identity for one scheduler interval."""
    observed_at = now or datetime.now(timezone.utc)
    interval_minutes = max(int(settings.strategist_interval_min or 1), 1)
    interval_seconds = interval_minutes * 60
    bucket = int(observed_at.timestamp()) // interval_seconds
    return f"strategist_tick:{bucket}"


async def _admit_and_claim_tick(*, observed_at: datetime) -> tuple[str, int] | None:
    """Admit one scheduled tick and claim it with a fenced durable lease.

    A duplicate scheduler fire never re-runs a terminal occurrence. An active
    occurrence owned by another scheduler is left visible for recovery rather
    than being executed twice.
    """
    occurrence = _occurrence_identity(observed_at)
    owner_principal_id = _service_principal_id()
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=occurrence,
            owner_kind="service",
            owner_principal_id=owner_principal_id,
            job_kind="strategist_tick",
            capability_version=_STRATEGIST_CAPABILITY_VERSION,
            idempotency_scope="scheduler-occurrence",
            idempotency_key=occurrence,
        ),
        inputs={"trigger": "interval", "occurrence": occurrence},
        priority=60,
        resource_claims=("cpu",),
        declared_authority={
            "principal": owner_principal_id,
            "owner_kind": "service",
            "service_id": _STRATEGIST_SERVICE_ID,
            "allowed_operations": ["guardian_state_read", "strategist_decision", "proactive_delivery"],
        },
        deadline_at=observed_at + timedelta(seconds=max(int(settings.agent_strategist_timeout), 1) + 30),
        max_attempts=1,
        service_id=_STRATEGIST_SERVICE_ID,
    )
    admission = await durable_job_repository.admit_job(spec)
    status = str(admission.get("status") or "")
    receipt_status = str(admission.get("receipt", {}).get("status") or "")

    # A terminal duplicate is an idempotent no-op. This also covers a
    # duplicate scheduler invocation after a process restart.
    if receipt_status == "deduped" and status in {"succeeded", "cancelled"}:
        logger.info("strategist_tick: occurrence already terminal (%s)", occurrence)
        return None
    if status in {"running", "awaiting_approval", "paused"}:
        logger.info("strategist_tick: occurrence already active (%s, status=%s)", occurrence, status)
        return None
    if status in {"blocked", "failed", "cancelled", "succeeded"}:
        logger.warning("strategist_tick: occurrence is not runnable (%s, status=%s)", occurrence, status)
        return None

    if status == "accepted":
        queued = await durable_job_repository.queue_job(occurrence)
        status = str(queued.get("status") or "")
    if status != "queued":
        logger.warning("strategist_tick: admission did not queue (%s, status=%s)", occurrence, status)
        return None

    try:
        claimed = await durable_job_repository.claim_job(
            occurrence,
            owner=_STRATEGIST_RUNNER_ID,
            lease_seconds=max(int(settings.agent_strategist_timeout), 1) + 30,
        )
    except DurableJobLeaseError as exc:
        # A concurrent scheduler instance may already own this occurrence;
        # infrastructure failures must propagate to the durable failure path.
        current = await durable_job_repository.get_job(occurrence)
        if current is not None and current.get("status") == "running":
            logger.info("strategist_tick: occurrence claim deferred (%s): %s", occurrence, exc)
            return None
        raise
    lease = claimed.get("lease") or {}
    token = lease.get("fencing_token")
    if str(claimed.get("status")) != "running" or lease.get("owner") != _STRATEGIST_RUNNER_ID or token is None:
        logger.warning("strategist_tick: claim missing active fence (%s)", occurrence)
        return None
    return occurrence, int(token)


async def _transition_tick(
    job_id: str,
    *,
    status: str,
    fencing_token: int,
    reason: str | None = None,
    result: object | None = None,
    result_summary: str | None = None,
) -> None:
    """Record terminal state; a failed durable write must remain observable."""
    await durable_job_repository.transition_job(
        job_id,
        status,
        owner=_STRATEGIST_RUNNER_ID,
        fencing_token=fencing_token,
        reason=reason,
        result=result,
        result_summary=result_summary,
    )


async def _record_failure_state(
    job_id: str,
    *,
    fencing_token: int,
    reason: str,
    result_summary: str,
) -> bool:
    """Attempt failure persistence and expose a stale lease if the DB is down."""
    try:
        await _transition_tick(
            job_id,
            status="failed",
            fencing_token=fencing_token,
            reason=reason,
            result_summary=result_summary,
        )
    except Exception:
        logger.exception("strategist_tick: failed to persist durable failure (%s)", job_id)
        return False
    return True


async def _record_unclaimed_failure(job_id: str, *, reason: str) -> bool:
    """Fail an admitted job that never obtained a runner lease."""
    try:
        await durable_job_repository.fail_unclaimed_job(
            job_id,
            owner_principal_id=_service_principal_id(),
            service_id=_STRATEGIST_SERVICE_ID,
            reason=reason,
            result_summary="strategist tick failed before a runner lease was claimed",
        )
    except Exception:
        logger.exception("strategist_tick: failed to persist unclaimed failure (%s)", job_id)
        return False
    return True


def _delivery_value(result) -> str | None:
    delivery_decision = getattr(result, "delivery_decision", None)
    if delivery_decision is not None:
        return delivery_decision.value
    return getattr(result, "value", None)


def _policy_action_value(result) -> str | None:
    action = getattr(result, "action", None)
    if action is not None:
        return action.value
    return None


def _proactive_goal_sort_key(goal: Goal) -> tuple[int, float, int, str]:
    due = getattr(goal, "due_date", None)
    due_timestamp = due.timestamp() if due is not None else float("inf")
    return (
        0 if due is not None else 1,
        due_timestamp,
        int(getattr(goal, "sort_order", 0) or 0),
        str(getattr(goal, "id", "")),
    )


async def _run_opted_in_goal_snapshot(
    *,
    parent_job_id: str,
    parent_fencing_token: int,
) -> dict[str, object]:
    """Run at most one explicitly enabled goal through the existing adapter."""

    goals = await goal_repository.list_goals(status="active")
    eligible: list[tuple[Goal, object]] = []
    for goal in goals:
        if not bool(getattr(goal, "proactive_enabled", False)):
            continue
        criterion = deserialize_success_criterion(goal)
        if criterion is None or criterion.verifier_kind is None:
            continue
        if criterion.verifier_kind.value != "artifact_readback" or not criterion.evidence_refs:
            continue
        eligible.append((goal, criterion))
    if not eligible:
        details = {"status": "skipped", "reason": "no_eligible_proactive_goal"}
        await durable_job_repository.record_effect(
            parent_job_id,
            effect_type="goal_snapshot_admission",
            status="unknown",
            details=details,
            owner=_STRATEGIST_RUNNER_ID,
            fencing_token=parent_fencing_token,
        )
        return details

    goal, criterion = sorted(eligible, key=lambda item: _proactive_goal_sort_key(item[0]))[0]
    revision = max(int(goal.revision or 1), 1)
    # Keep the child authority/session stable across strategist occurrences;
    # parent_job_id remains the lineage/fence, while the candidate identity
    # supplies the scheduler idempotency boundary.
    session_id = f"goal-snapshot:scheduler:{goal.id}:{revision}"
    principal = TrustPrincipal(
        principal_id="service:goal-snapshot",
        principal_type=PrincipalType.SERVICE,
        authenticated=True,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id=session_id,
    )
    request = GoalSnapshotToFileRequest(
        goal_id=goal.id,
        goal_revision=revision,
        file_path=f"goal-snapshots/{goal.id}.md",
        owner_principal_id="service:goal-snapshot",
        service_id="service:goal-snapshot",
        session_id=session_id,
        parent_job_id=parent_job_id,
        parent_fencing_token=parent_fencing_token,
        evidence_refs=list(criterion.evidence_refs),
        reason="scheduled_proactive_goal_snapshot",
        expected_outcome=criterion.description,
    )
    result = await GoalSnapshotToFileService(authority_principal=principal).run(request)
    if not isinstance(result, GoalSnapshotToFileResult):
        raise TypeError("goal snapshot service returned an invalid result")
    effect_status = (
        "succeeded"
        if result.execution_status == "succeeded" and result.verification == "passed"
        else "blocked"
        if result.execution_status == "blocked"
        else "failed"
    )
    details = {
        "status": result.execution_status,
        "verification": result.verification,
        "learning": result.learning,
        "goal_id": result.goal_id,
        "goal_revision": result.goal_revision,
        "job_id": result.job_id,
        "artifact_ref": result.artifact_ref,
        "reason": result.reason,
        "operator_visible": True,
    }
    await durable_job_repository.record_effect(
        parent_job_id,
        effect_type="goal_snapshot_admission",
        status=effect_status,
        details=details,
        owner=_STRATEGIST_RUNNER_ID,
        fencing_token=parent_fencing_token,
    )
    return details


async def run_strategist_tick() -> None:
    """Review context and decide if proactive intervention is warranted."""
    started_at = perf_counter()
    observed_at = datetime.now(timezone.utc)
    durable_job_id: str | None = None
    durable_fencing_token: int | None = None
    durable_failure_persisted: bool | None = None
    llm_request_id: str | None = None
    try:
        durable_job_id = _occurrence_identity(observed_at)
        claimed = await _admit_and_claim_tick(observed_at=observed_at)
        if claimed is None:
            return
        durable_job_id, durable_fencing_token = claimed
        guardian_state = await build_guardian_state(
            refresh_observer=True,
            memory_query="current priorities, commitments, and recent intervention patterns",
        )
        await durable_job_repository.record_checkpoint(
            durable_job_id,
            checkpoint_id="guardian_state_loaded",
            state={
                "confidence": guardian_state.confidence.overall,
                "memory_query": "current priorities, commitments, and recent intervention patterns",
            },
            owner=_STRATEGIST_RUNNER_ID,
            fencing_token=durable_fencing_token,
        )
        proactive_snapshot: dict[str, object]
        try:
            proactive_snapshot = await _run_opted_in_goal_snapshot(
                parent_job_id=durable_job_id,
                parent_fencing_token=durable_fencing_token,
            )
        except Exception as exc:
            proactive_snapshot = {
                "status": "blocked",
                "reason": f"goal_snapshot_scheduler_error:{type(exc).__name__}",
                "operator_visible": True,
            }
            try:
                await durable_job_repository.record_effect(
                    durable_job_id,
                    effect_type="goal_snapshot_admission",
                    status="blocked",
                    details=proactive_snapshot,
                    owner=_STRATEGIST_RUNNER_ID,
                    fencing_token=durable_fencing_token,
                )
            except Exception:
                logger.exception("strategist_tick: failed to persist goal snapshot degraded receipt")
        await durable_job_repository.record_checkpoint(
            durable_job_id,
            checkpoint_id="goal_snapshot_considered",
            state=proactive_snapshot,
            owner=_STRATEGIST_RUNNER_ID,
            fencing_token=durable_fencing_token,
        )
        llm_request_id = f"strategist_tick:{started_at}"
        _register_request(llm_request_id)
        llm_request_token = set_current_llm_request_id(llm_request_id)
        reset_current_llm_request_id(llm_request_token)
        raw = await run_strategist_decision_completion(
            guardian_state=guardian_state,
        )

        decision = parse_strategist_response(str(raw))

        if not decision.should_intervene:
            await durable_job_repository.record_effect(
                durable_job_id,
                effect_type="strategist_decision",
                status="succeeded",
                details={
                    "should_intervene": False,
                    "reasoning_digest": _reasoning_digest(decision.reasoning),
                },
                owner=_STRATEGIST_RUNNER_ID,
                fencing_token=durable_fencing_token,
            )
            await _transition_tick(
                durable_job_id,
                status="succeeded",
                fencing_token=durable_fencing_token,
                result={
                    "should_intervene": False,
                    "reasoning_digest": _reasoning_digest(decision.reasoning),
                },
                result_summary="no intervention required",
            )
            await log_scheduler_job_event(
                job_name="strategist_tick",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": decision.reasoning,
                    "request_id": llm_request_id,
                    "durable_job_id": durable_job_id,
                },
            )
            logger.info("strategist_tick: no intervention needed — %s", decision.reasoning)
            return

        from src.observer.delivery import deliver_or_queue

        message = WSResponse(
            type="proactive",
            content=decision.content,
            intervention_type=decision.intervention_type,
            urgency=decision.urgency,
            reasoning=decision.reasoning,
        )
        result = await deliver_or_queue(
            message,
            guardian_confidence=guardian_state.confidence.overall,
        )
        delivery_value = _delivery_value(result)
        policy_action_value = _policy_action_value(result)
        await durable_job_repository.record_effect(
            durable_job_id,
            effect_type="proactive_delivery",
            # ``deliver_or_queue`` returns the policy decision, while the
            # transport receipt is persisted by the delivery coordinator. Do
            # not turn a policy value into a false claim that a user received
            # the message.
            status="unknown",
            details={
                "delivery_policy": delivery_value,
                "policy_action": policy_action_value,
                "intervention_type": decision.intervention_type,
                "verification": "delivery_coordinator_receipt",
            },
            owner=_STRATEGIST_RUNNER_ID,
            fencing_token=durable_fencing_token,
        )
        await _transition_tick(
            durable_job_id,
            status="succeeded",
            fencing_token=durable_fencing_token,
            result={
                "should_intervene": True,
                "delivery": delivery_value,
                "policy_action": policy_action_value,
            },
            result_summary="proactive decision completed; delivery has a separate transport receipt",
        )
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "intervention_type": decision.intervention_type,
                "urgency": decision.urgency,
                "delivery": _delivery_value(result),
                "policy_action": _policy_action_value(result),
                "request_id": llm_request_id,
                "durable_job_id": durable_job_id,
            },
        )
        logger.info(
            "strategist_tick: intervention handled (type=%s, urgency=%d, delivery=%s, action=%s)",
            decision.intervention_type,
            decision.urgency,
            _delivery_value(result),
            _policy_action_value(result),
        )

    except asyncio.TimeoutError:
        if llm_request_id is not None:
            _mark_request_timed_out(llm_request_id)
        if durable_job_id is not None and durable_fencing_token is not None:
            durable_failure_persisted = await _record_failure_state(
                durable_job_id,
                fencing_token=durable_fencing_token,
                reason="strategist_timeout",
                result_summary="strategist decision timed out",
            )
        elif durable_job_id is not None:
            durable_failure_persisted = await _record_unclaimed_failure(
                durable_job_id,
                reason="strategist_timeout_before_claim",
            )
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="timed_out",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "timeout_seconds": settings.agent_strategist_timeout,
                "request_id": llm_request_id,
                "durable_job_id": durable_job_id,
                "durable_failure_persisted": durable_failure_persisted,
            },
        )
        logger.warning("strategist_tick: agent timed out after %ds", settings.agent_strategist_timeout)
    except Exception as exc:
        if durable_job_id is not None and durable_fencing_token is not None:
            durable_failure_persisted = await _record_failure_state(
                durable_job_id,
                fencing_token=durable_fencing_token,
                reason=type(exc).__name__,
                result_summary="strategist tick failed before verified completion",
            )
        elif durable_job_id is not None:
            durable_failure_persisted = await _record_unclaimed_failure(
                durable_job_id,
                reason=type(exc).__name__,
            )
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
                "request_id": llm_request_id,
                "durable_job_id": durable_job_id,
                "durable_failure_persisted": durable_failure_persisted,
            },
        )
        logger.exception("strategist_tick failed")
    finally:
        if llm_request_id is not None:
            _finish_request(llm_request_id)
