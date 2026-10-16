"""Operator admission for Cordis memory mutations (#1064).

Only authenticated operator HTTP handlers may import this module (a guard
test enforces it): the admission is the operator's review, so agent tools,
the agent loop, native tools and the Cordis dispatch path must never reach
it. It repeats the memory routes' operator checks itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping

from sqlalchemy import select

from .dispatch import NativeServiceBlocked
from .memory_mutations import (DECISION_EFFECTS, MEMORY_JOB_KIND, MUTATION_METHODS, OUTSTANDING_LIMIT,
                               REVIEW_ACTIONS, _assert_memory_owner, complete_mutation, review_binding_mac,
                               sealed_result, validate_mutation_candidate, wire_inputs)
from .protocol import ProtocolError


def require_memory_operator(operator) -> None:
    """Repeat ``authenticated_memory_context``'s operator checks.

    The executor is reachable only from an authenticated operator HTTP
    handler; it still refuses any other principal shape itself.
    """
    from src.auth.service import AuthenticatedOperator
    from src.security.trust_contract import AuthorityGrant, PrincipalType
    principal = getattr(operator, "principal", None)
    principal_type = getattr(getattr(principal, "principal_type", None), "value", None)
    grants = {str(getattr(grant, "value", grant)) for grant in getattr(principal, "grants", ())}
    session_id = str(getattr(operator, "session_id", "") or "")
    if (not isinstance(operator, AuthenticatedOperator) or principal is None
            or principal_type != PrincipalType.OPERATOR.value
            or not bool(getattr(principal, "authenticated", False)) or bool(getattr(principal, "revoked", False))
            or not str(getattr(principal, "principal_id", "") or "") or not session_id
            or str(getattr(principal, "session_id", "") or "") != session_id
            or AuthorityGrant.CAPABILITY_EXECUTE.value not in grants):
        raise NativeServiceBlocked("native_memory_operator_required")


def _require(request: Mapping, fields: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(request, Mapping) or not fields <= set(request) or set(request) - fields - optional:
        raise NativeServiceBlocked("native_memory_request_invalid")
    return dict(request)


async def build_candidate(operator, method: str, request: Mapping) -> dict:
    """Bind the operator's request to current canonical rows; returns a candidate."""
    from src.db.engine import get_session
    from src.db.models import Memory, MemoryProposal
    session_id = operator.session_id
    principal_id = operator.principal.principal_id
    if method == "memory.propose":
        request = _require(request, {"task_id", "attempt_id", "expected_task_revision"})
        candidate = {"schema_version": 1, "method": method, **request}
    elif method == "memory.applyReviewed":
        request = _require(request, {"proposal_id", "action", "expected_revision", "expected_preview_text_digest",
                                     "expected_task_revision", "expected_goal_revision"},
                           {"decision_effect", "corrects_memory_id", "preferred_capability_id"})
        if not request.get("expected_preview_text_digest"):
            # Client-supplied and never defaulted, exactly as the cockpit route.
            raise NativeServiceBlocked("native_memory_preview_digest_required")
        if request["action"] not in REVIEW_ACTIONS:
            raise NativeServiceBlocked("native_memory_review_action_unsupported")
        async with get_session() as db:
            proposal = await db.scalar(select(MemoryProposal).where(
                MemoryProposal.proposal_id == request["proposal_id"],
                MemoryProposal.owner_principal_id == principal_id,
                MemoryProposal.owner_session_id == session_id))
            if proposal is None:
                raise NativeServiceBlocked("native_memory_owner_forbidden")
            if (proposal.schema_version in {"task_method_proposal.v1", "procedure_recommendation.v1",
                                            "opportunity_recommendation.v1"}
                    or proposal.capability_id == "memory.opportunity-preference.v1"):
                raise NativeServiceBlocked("native_memory_specialized_review_required")
            if (int(proposal.revision or 0) != int(request["expected_revision"])
                    or proposal.preview_text_digest != request["expected_preview_text_digest"]):
                raise NativeServiceBlocked("native_memory_proposal_changed")
            # The operator's task and goal revisions, as the cockpit route requires them.
            if int(proposal.source_task_revision or 0) != int(request["expected_task_revision"]):
                raise NativeServiceBlocked("native_memory_stale_task_revision")
            if int(proposal.goal_revision or 0) != int(request["expected_goal_revision"]):
                raise NativeServiceBlocked("native_memory_stale_goal_revision")
            if proposal.corrects_memory_id and request.get("corrects_memory_id") != proposal.corrects_memory_id:
                # The operator must have seen, and named, the row a correction replaces.
                raise NativeServiceBlocked("native_memory_correction_target_unseen")
            effect = request.get("decision_effect") or "none"
            if effect not in DECISION_EFFECTS:
                raise NativeServiceBlocked("native_memory_decision_effect_invalid")
            candidate = {
                "schema_version": 1, "method": method, "proposal_id": proposal.proposal_id,
                "expected_revision": int(proposal.revision), "expected_task_revision": int(proposal.source_task_revision),
                "expected_goal_revision": int(proposal.goal_revision), "action": request["action"],
                "decision_effect": effect, "corrects_memory_id": request.get("corrects_memory_id"),
                "preferred_capability_id": request.get("preferred_capability_id"),
                "preview_mac": await review_binding_mac(
                    proposal, action=request["action"], decision_effect=effect,
                    corrects_memory_id=request.get("corrects_memory_id") or proposal.corrects_memory_id,
                    preferred_capability_id=request.get("preferred_capability_id"))}
    elif method == "memory.forget":
        request = _require(request, {"memory_id", "acknowledge_irreversible"})
        if request["acknowledge_irreversible"] is not True:
            raise NativeServiceBlocked("native_memory_forget_acknowledgement_required")
        async with get_session() as db:
            memory = await db.get(Memory, request["memory_id"])
            _assert_memory_owner(memory, session_id)
        candidate = {"schema_version": 1, "method": method, "memory_id": request["memory_id"],
                     "mode": "tombstone", "acknowledge_irreversible": True}
    else:
        raise NativeServiceBlocked("native_memory_method_unsupported")
    try:
        return validate_mutation_candidate(method, candidate)
    except ProtocolError as exc:
        raise NativeServiceBlocked("native_memory_request_invalid") from exc


async def outstanding_admission_check(db, run) -> None:
    """At most four live memory-mutation jobs per Root and per principal.

    Runs inside ``admit_job``'s writer before the insert; an exact idempotent
    replay returns before it and is never counted twice.
    """
    from src.db.models import WorkflowRunState
    from src.workflows.job_runtime import DURABLE_JOB_TERMINAL_STATUSES, DurableJobAdmissionDenied
    now = datetime.now(timezone.utc)
    for column, value in ((WorkflowRunState.operator_session_id, run.operator_session_id),
                          (WorkflowRunState.owner_principal_id, run.owner_principal_id)):
        # A bounded row select, not an aggregate: the product SQLite
        # authorizer denies ``count`` (the #1007 census failure).
        live = (await db.execute(select(WorkflowRunState.run_identity).where(
            WorkflowRunState.job_kind == MEMORY_JOB_KIND, column == value,
            WorkflowRunState.status.not_in(tuple(DURABLE_JOB_TERMINAL_STATUSES)),
            WorkflowRunState.deadline_at > now).limit(OUTSTANDING_LIMIT))).scalars().all()
        if len(live) >= OUTSTANDING_LIMIT:
            raise DurableJobAdmissionDenied("native_memory_outstanding_limit")


async def execute_memory_mutation(*, operator, method: str, request: Mapping, idempotency_key: str,
                                  source_role: str = "operator", host=None, jobs=None) -> dict:
    """Operator HTTP handler entry point: admit, cross once, verify, complete."""
    from .bridge import HostBlocked, cordis_host
    from .service_jobs import _block, _jobs, admit_service_job, claim_service_job, open_invocation
    from src.workflows.job_runtime import DurableJobAdmissionDenied
    require_memory_operator(operator)
    if str(source_role or "").strip().lower() != "operator":
        # The memory routes' ``memory_source_role_forbidden``.
        raise NativeServiceBlocked("memory_source_role_forbidden")
    if method not in MUTATION_METHODS:
        raise NativeServiceBlocked("native_memory_method_unsupported")
    host = host or cordis_host
    jobs = _jobs(jobs)
    if not host.admitting or host.reviewed is None:
        raise NativeServiceBlocked("native_memory_host_unavailable")
    candidate = await build_candidate(operator, method, request)
    try:
        job, replayed = await admit_service_job(
            operator=operator, origin_method=method, native_branch="base", candidate=candidate,
            idempotency_key=idempotency_key, reviewed_composition=host.reviewed, jobs=jobs,
            admission_authority_check=outstanding_admission_check)
    except DurableJobAdmissionDenied as exc:
        raise NativeServiceBlocked(exc.reason) from exc
    if job["status"] != "accepted":
        # An exact retry reuses original truth; it never re-executes.
        return {"job": job, "replayed": True, "effect_committed": job["status"] == "succeeded"}
    owner, fence = await claim_service_job(job, host=host, jobs=jobs)
    scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=host,
                                  admission_replayed=replayed, candidate=candidate, jobs=jobs)
    job_id = job["job_id"]
    try:
        observed = await host.request_service(method, wire_inputs(candidate, job_id), original_scope=scope)
    except HostBlocked:
        observed = None
    async with jobs._session() as db:
        sealed = sealed_result(await jobs._fetch(db, job_id))
    if sealed is None:
        # Nothing committed: the attempt blocks with the child's typed reason.
        reason = (observed or {}).get("reason_code") or "native_memory_execution_blocked"
        await _block(jobs, scope, reason)
        raise NativeServiceBlocked(reason)
    if observed is not None and observed != sealed["projection"]:
        # The effect is committed; a fabricated child answer never settles
        # the job, and the committed effect is never replayed.
        await _block(jobs, scope, "native_child_result_mismatch")
        raise NativeServiceBlocked("native_child_result_mismatch")
    return await complete_mutation(jobs, job_id, owner, fence, method)
