"""Operator-invoked Specify/Decompose proposal staging.

Inference is advisory and produces a reviewable ``WorkBoardProposal`` row.
Only the explicit acceptance path creates tasks and dependency links.  The
module uses the existing strategist OpenRouter route and never falls back to a
local model or executes a proposed capability while staging.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import re
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.approval.runtime import get_current_approval_mode, reset_runtime_context, set_runtime_context
from src.auth.service import AuthenticatedOperator, bind_operator_principal
from src.db.engine import get_session
from src.db.models import Goal, WorkBoardLink, WorkBoardProposal, WorkBoardStatus, WorkBoardTask
from src.llm_runtime import (
    completion_with_fallback,
    fallback_model_ids,
    provider_profiles,
    resolve_runtime_profile,
)
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.configuration import effective_workload_policy
from src.model_fabric import bind_remote_inference_receipt
from src.security.trust_contract import canonical_digest
from src.work_board.contracts import (
    WorkBoardOwner,
    WorkBoardProposalAccept,
    WorkBoardProposalReject,
    WorkBoardProposalRequest,
    WorkBoardTaskCreate,
    _safe_reference,
)
from src.work_board.repository import (
    BoardError,
    BoardRevisionConflict,
    WorkBoardRepository,
    _begin_sqlite_immediate,
    _safe_receipt_refs,
    safe_sha256_digest,
)
from src.work_board.time import serialize_utc_datetime
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobError,
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    durable_job_repository,
)


PROPOSAL_TTL = timedelta(minutes=15)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROPOSAL_KINDS = frozenset({"specify", "decompose"})
_PROPOSAL_ROUTE = "strategist_agent"
_PROPOSAL_JOB_KIND = "work_board_proposal"
_PROPOSAL_CAPABILITY = "strategist_agent"
_PROPOSAL_RUNNER = "work-board-proposal"
_PROPOSAL_ATTEMPT_BUDGET_REASON = "proposal_admission_attempt_budget_exhausted"
_CAPABILITY_AUTHORITY_REQUIREMENTS: dict[str, str] = {
    "workflow.goal-snapshot-to-file": (
        "Operator capability-execute session; active owner-bound goal at the exact revision; "
        "configured success criterion, verifier, and evidence; enabled governed workflow tool; "
        "scoped artifact write followed by independent readback. This capability does not use remote inference."
    ),
    "guardian.research-watch.v1": (
        "Owner-scoped active research watch at the exact plan revision; reviewed goal consent and "
        "finite admission budget; current source policy. Research is read-only; it grants no external write."
    ),
    "engineering.repo-change.v1": (
        "Owner-scoped current repository candidate and evidence; rootless isolation profile; "
        "candidate path and test-command allowlists; exact repository-write approval; artifact and test readback."
    ),
    "work.github-followthrough.v1": (
        "Active owner GitHub connection with configured credential and matching revision; current "
        "external-mutation grant; exact destination approval; idempotency, independent GitHub readback, "
        "and unknown-effect reconciliation. Unknown effects never auto-retry."
    ),
    "guardian-routine.v1": (
        "Owner-scoped active routine and installed reviewed package at the exact versions/digests; "
        "current source-watch revision; every invocation uses fresh goal revision, grants, approvals, "
        "and budget. Prior invocations grant no authority."
    ),
}
_PRE_CONTACT_RETRY_REASONS = frozenset(
    {
        # These failures occur while rechecking the immutable binding, before
        # the provider-contact marker can be set.  Provider/effect failures
        # are deliberately absent: an empty ledger is not proof of no contact
        # for those outcomes.
        "goal_revision_stale",
        "proposal_binding_conflict",
        "proposal_admission_unavailable",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _safe_id(value: Any, *, field: str, max_length: int = 256) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > max_length or not _SAFE_ID.fullmatch(candidate):
        raise BoardError("invalid_proposal", f"The proposal {field} is not a safe identifier", status_code=422)
    return candidate


def registered_executor_id(capability_id: str) -> str:
    """Return the server-owned executor lane for one registered capability.

    The proposal model may describe a task, but it cannot choose an executor
    lane.  Seraph currently has one bounded work-board lane per registered
    capability; keeping this mapping in one helper makes both the preview and
    acceptance paths derive the same value.
    """

    from src.work_board.dispatcher import registered_executor_id as _registered_executor_id

    expected = _registered_executor_id(capability_id)
    if expected is None:
        raise BoardError(
            "capability_unregistered",
            "The proposal names no registered Seraph capability",
            status_code=409,
        )
    return expected


async def _provider_safe_identifier(
    value: Any,
    *,
    field: str,
    max_length: int = 256,
) -> str:
    """Validate and vault-check an identifier supplied by the model.

    Board identifiers are not prose, so replacing a secret with a redaction
    marker would produce a different, potentially colliding identifier.  Fail
    closed when vault redaction changes the value instead of persisting the
    marker or the provider-controlled secret.
    """

    candidate = _safe_id(value, field=field, max_length=max_length)
    redacted = await WorkBoardRepository._safe_text(candidate)
    if redacted != candidate:
        raise BoardError(
            "invalid_model_output",
            f"The proposed {field} contains protected data",
            status_code=409,
        )
    return candidate


async def _provider_safe_reference(value: Any, *, field: str) -> str:
    """Validate and vault-check a model-supplied typed input reference."""

    try:
        candidate = _safe_reference(str(value), field_name=field) or ""
    except ValueError as exc:
        raise BoardError(
            "invalid_model_output",
            f"The proposed {field} is unsafe",
            status_code=409,
        ) from exc
    redacted = await WorkBoardRepository._safe_text(candidate)
    if redacted != candidate:
        raise BoardError(
            "invalid_model_output",
            f"The proposed {field} contains protected data",
            status_code=409,
        )
    return candidate


def _safe_text(value: Any, *, field: str, max_length: int) -> str:
    candidate = str(value or "").strip()
    if len(candidate) > max_length:
        raise BoardError("invalid_proposal", f"The proposal {field} is too long", status_code=422)
    return candidate


def _decode_json(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError) as exc:
        raise BoardError("invalid_proposal", "The proposal payload is not valid JSON", status_code=409) from exc
    if not isinstance(payload, dict):
        raise BoardError("invalid_proposal", "The proposal payload must be an object", status_code=409)
    return payload


def _proposal_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _route_binding() -> tuple[str, str]:
    """Return the explicitly selected OpenRouter route and contract version."""
    try:
        policy = effective_workload_policy(_PROPOSAL_ROUTE)
    except Exception as exc:
        raise BoardError(
            "openrouter_policy_unavailable",
            "The governed OpenRouter workload policy is unavailable",
            status_code=409,
        ) from exc
    if bool(getattr(policy, "fallback_allowed", False)):
        raise BoardError(
            "openrouter_fallback_forbidden",
            "The proposal route has a configured fallback and is blocked",
            status_code=409,
        )
    allowed_provider_kinds = tuple(getattr(policy, "allowed_provider_kinds", ()) or ())
    if allowed_provider_kinds and set(allowed_provider_kinds) != {"openrouter"}:
        raise BoardError(
            "openrouter_policy_unavailable",
            "The proposal route is not governed by the OpenRouter provider policy",
            status_code=409,
        )
    profile_id = resolve_runtime_profile(runtime_path=_PROPOSAL_ROUTE, profile="openrouter")
    profile = provider_profiles().get(profile_id)
    if profile is None or profile.provider_kind != "openrouter":
        raise BoardError(
            "openrouter_route_unavailable",
            "The governed OpenRouter proposal route is unavailable",
            status_code=409,
        )
    if str(profile.api_base or "") != "https://openrouter.ai/api/v1":
        raise BoardError(
            "openrouter_route_unavailable",
            "The proposal route is not bound to the governed OpenRouter endpoint",
            status_code=409,
        )
    # The proposal route has no provider fallback.  Reject configuration that
    # would make the shared legacy wrapper try a second paid target.
    if profile.fallback_models or fallback_model_ids(runtime_path=_PROPOSAL_ROUTE):
        raise BoardError(
            "openrouter_fallback_forbidden",
            "The proposal route has a configured fallback and is blocked",
            status_code=409,
        )
    # ``strategist_agent`` is the canonical Seraph route identity used by the
    # model-fabric context.  ``profile_id`` is only the selected provider
    # profile and must not replace that route identity in durable bindings.
    return _PROPOSAL_ROUTE, profile.contract_hash


def _proposal_request_digest(
    *,
    task: WorkBoardTask,
    kind: str,
    idempotency_key: str,
) -> str:
    return _proposal_digest(
        {
            "kind": kind,
            "parent_task_id": task.task_id,
            "parent_revision": int(task.task_revision),
            "goal_id": task.goal_id,
            "goal_revision": int(task.goal_revision),
            "idempotency_key": idempotency_key,
            "title_digest": hashlib.sha256((task.title or "").encode("utf-8")).hexdigest(),
            "body_digest": hashlib.sha256((task.body or "").encode("utf-8")).hexdigest(),
        }
    )


def _proposal_input_digest(*, task: WorkBoardTask, kind: str) -> str:
    return _proposal_digest(
        {
            "kind": kind,
            "task_id": task.task_id,
            "goal_id": task.goal_id,
            "goal_revision": task.goal_revision,
            "title": (task.title or "")[:200],
            "body": (task.body or "")[:4_000],
        }
    )


async def _proposal_task_authority_summary(
    parent: WorkBoardTask,
    item: Mapping[str, Any],
) -> str:
    """Build a capability-specific, current authority preview from Seraph state.

    The summary is display evidence, never a grant. It lists the registered
    capability's actual preflight gates, the provider-free preflight result,
    and the effective finite runtime. Acceptance re-computes this value; the
    dispatcher remains the final admission authority for every attempt.
    """
    from src.work_board.dispatcher import (
        DEFAULT_RUNTIME_SECONDS,
        MAX_ATTEMPTS_PER_TASK,
        MAX_RUNTIME_SECONDS,
        REGISTERED_CAPABILITIES,
        _dispatcher,
    )

    capability_id = str(item.get("capability_id") or "")
    registered = REGISTERED_CAPABILITIES.get(capability_id)
    requirements = _CAPABILITY_AUTHORITY_REQUIREMENTS.get(capability_id)
    if registered is None or requirements is None:
        raise BoardError(
            "capability_unregistered",
            "The proposal has no complete server-owned capability authority contract",
            status_code=409,
        )
    capability_version = str(item.get("capability_version") or "")
    if capability_version != registered.version:
        raise BoardError(
            "capability_version_stale",
            "The capability registration changed before authority preview",
            status_code=409,
        )

    candidate = WorkBoardTask(
        task_id=str(item.get("task_id") or ""),
        owner_principal_id=parent.owner_principal_id,
        owner_session_id=parent.owner_session_id,
        origin_session_id=parent.origin_session_id or parent.owner_session_id,
        goal_id=parent.goal_id,
        goal_revision=parent.goal_revision,
        title=str(item.get("title") or ""),
        body=str(item.get("body") or ""),
        capability_id=capability_id,
        typed_input_ref=str(item.get("typed_input_ref") or ""),
        typed_input_digest=str(item.get("typed_input_digest") or ""),
        executor_id=str(item.get("executor_id") or ""),
        idempotency_scope=f"proposal-validation:{parent.task_id}",
        idempotency_key=f"{parent.task_id}:{item.get('task_id')}",
        status=WorkBoardStatus.todo,
    )
    try:
        readiness_code, _readiness_reason = await _dispatcher._current_readiness(candidate)
        runtime_seconds = await _dispatcher._effective_runtime(candidate)
    except Exception:
        # A preflight outage is shown as unavailable and the cockpit disables
        # acceptance. Never turn an inability to inspect live authority into a
        # ready-looking proposal.
        readiness_code = "authority_preflight_unavailable"
        _readiness_reason = "Seraph could not verify the current capability authority"
        runtime_seconds = None

    if readiness_code:
        # The code is generated by Seraph's fixed preflight branches. Avoid
        # carrying exception text into the durable proposal, even after
        # redaction; status plus the code is enough to identify recovery.
        safe_code = (
            readiness_code
            if re.fullmatch(r"[a-z0-9_]{1,80}", str(readiness_code))
            else "preflight_failed"
        )
        preflight = (
            f"BLOCKED code={safe_code}; capability-specific recovery is required"
            if safe_code != "authority_preflight_unavailable"
            else "UNAVAILABLE; acceptance is disabled until a fresh preview succeeds"
        )
    else:
        preflight = "READY; dispatch will recheck before claim"

    runtime = (
        f"{int(runtime_seconds)}s effective goal/job runtime"
        if runtime_seconds is not None
        else "unavailable; acceptance is disabled"
    )
    return (
        f"Owner: authenticated owner/session; goal {parent.goal_id} revision "
        f"{int(parent.goal_revision)}. Capability: {capability_id}@{capability_version}; "
        f"executor {item.get('executor_id')} is derived from the server registry. "
        f"Capability-specific authority requirements: {requirements} "
        f"Current provider-free preflight: {preflight}. Limits: at most "
        f"{MAX_ATTEMPTS_PER_TASK} task attempts; {runtime} (default "
        f"{DEFAULT_RUNTIME_SECONDS}s default, {MAX_RUNTIME_SECONDS}s hard cap). "
        "Accepting creates Todo only and grants no authority or external-effect approval. "
        "Every dispatch rechecks current owner session, goal revision, grants, policy, "
        "approval, idempotency, and required independent readback."
    )


def _proposal_admission_input_digest(proposal: WorkBoardProposal) -> str:
    """Recompute the exact input envelope digest used by durable admission."""
    return _proposal_digest(
        {
            "proposal_id": proposal.proposal_id,
            "kind": proposal.kind,
            "parent_task_id": proposal.parent_task_id,
            "parent_revision": proposal.parent_revision,
            "request_digest": proposal.request_digest,
        }
    )


def _proposal_effect_id(job_id: str) -> str:
    """Return the exact effect identity used by durable remote admission."""
    # stable_remote_inference_operation_id prefixes a bound job with
    # ``remote:``; the durable effect recorder then prefixes that operation
    # with ``remote_inference:``.  Keep the planned digest aligned with the
    # actual receipt so acceptance can prove the same effect settled.
    return f"remote_inference:remote:{job_id}"


def _authority_digest(owner: WorkBoardOwner, task: WorkBoardTask, route_id: str, capability_version: str) -> str:
    try:
        policy = effective_workload_policy(route_id)
    except Exception:
        # A route-policy outage must leave a durable, visibly blocked proposal
        # instead of escaping as an HTTP 500. This marker grants no authority.
        policy_binding: dict[str, Any] = {"status": "unavailable"}
    else:
        policy_binding = {
            "status": "available",
            "egress_class": getattr(policy.egress_class, "value", str(policy.egress_class)),
            "cloud_egress_acknowledged": bool(policy.cloud_egress_acknowledged),
            "allowed_profile_ids": list(policy.allowed_profile_ids),
            "allowed_provider_kinds": list(policy.allowed_provider_kinds),
            "fallback_allowed": bool(policy.fallback_allowed),
            "max_cost_microusd": policy.max_cost_microusd,
        }
    return _proposal_digest(
        {
            "principal": owner.principal_id,
            "session_id": owner.session_id,
            "goal_id": task.goal_id,
            "goal_revision": int(task.goal_revision),
            "route_id": route_id,
            "capability_id": _PROPOSAL_CAPABILITY,
            "capability_version": capability_version,
            "grant_revision": int(task.goal_revision),
            # Bind the current governed route policy to the durable proposal
            # identity.  The policy is read from Model Fabric's canonical
            # persisted configuration; it is never accepted from model output
            # or browser input.
            "policy": policy_binding,
        }
    )


async def _admit_proposal_job(
    *,
    owner: WorkBoardOwner,
    task: WorkBoardTask,
    proposal: WorkBoardProposal,
) -> tuple[str, str, int] | None:
    """Admit and fence the existing durable job before provider contact."""
    authority = {
        "principal": owner.principal_id,
        "owner_kind": "user",
        "session_id": owner.session_id,
        "allowed_operations": ["work_board_proposal", "model_inference"],
        "capability_id": proposal.capability_id,
        "capability_version": proposal.capability_version,
        "grant_revision": int(proposal.grant_revision),
        "finite_authority": True,
        "policy_digest": proposal.authority_digest,
    }
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=proposal.admission_job_id,
            owner_kind="user",
            owner_principal_id=owner.principal_id,
            job_kind=_PROPOSAL_JOB_KIND,
            capability_version=proposal.capability_version,
            idempotency_scope="work-board-proposal",
            idempotency_key=proposal.proposal_id,
        ),
        inputs={
            "proposal_id": proposal.proposal_id,
            "kind": proposal.kind,
            "parent_task_id": proposal.parent_task_id,
            "parent_revision": proposal.parent_revision,
            "request_digest": proposal.request_digest,
        },
        session_id=owner.session_id,
        conversation_id=owner.session_id,
        operator_session_id=owner.session_id,
        goal_id=task.goal_id,
        goal_revision=task.goal_revision,
        priority=task.priority,
        resource_claims=("remote-inference",),
        declared_authority=authority,
        # One claim can be recovered once after a crash before the provider
        # contact marker/effect ledger is written.  The proposal fence below
        # still permits no retry after contact or any effect is persisted.
        max_attempts=2,
        run_fingerprint=proposal.request_digest,
    )
    # A prior process can fail after the deterministic row is admitted but
    # before the proposal contact marker is written.  Reconcile that exact
    # failed row through the runtime's CAS retry contract before asking the
    # normal admission path to dedupe it.  Never turn a failed/ambiguous row
    # into a fresh admission.
    job_id = proposal.admission_job_id
    existing = await durable_job_repository.get_job(job_id)
    if isinstance(existing, Mapping) and str(existing.get("status") or "") == "failed":
        recovered = await _recover_pre_contact_admission(proposal, existing, task=task)
        if recovered is None:
            return None
    admission = await durable_job_repository.admit_job(spec)
    status = str(admission.get("status") or "")
    receipt_status = str((admission.get("receipt") or {}).get("status") or "")
    job_id = proposal.admission_job_id
    if receipt_status == "deduped" and status == "failed":
        recovered = await _recover_pre_contact_admission(proposal, admission, task=task)
        if recovered is not None:
            admission = recovered
            status = str(admission.get("status") or "")
            receipt_status = str((admission.get("receipt") or {}).get("status") or "")
    if receipt_status == "deduped":
        # A previously running/terminal provider operation has unknown cost
        # and effect liability unless its result is already persisted.
        if status not in {"accepted", "queued"}:
            return None
    if status == "accepted":
        queued = await durable_job_repository.queue_job(job_id)
        status = str(queued.get("status") or "")
    if status != "queued":
        return None
    lease_owner = f"{_PROPOSAL_RUNNER}:{proposal.proposal_id}"
    claimed = await durable_job_repository.claim_job(
        job_id,
        owner=lease_owner,
        lease_seconds=120,
    )
    lease = claimed.get("lease") if isinstance(claimed, Mapping) else None
    fence = int((lease or {}).get("fencing_token") or 0)
    if str(claimed.get("status") or "") != "running" or fence <= 0:
        return None
    return job_id, lease_owner, fence


def _failed_pre_contact_admission_matches(
    proposal: WorkBoardProposal,
    projection: Mapping[str, Any],
    *,
    task: WorkBoardTask | None = None,
) -> bool:
    """Prove one failed proposal row stopped before provider contact.

    The durable runtime is the source of the retry CAS.  This predicate only
    admits a retry when the proposal marker, job identity, owner/session,
    immutable binding, failure reason, deadline, attempts, and effect ledger
    all agree.  Unknown or provider-shaped failures remain reconciliation-only.
    """

    if proposal.provider_contact_started or proposal.provider_contact_state != "not_started":
        return False
    if proposal.status not in {"pending_inference", "blocked"}:
        return False
    if str(projection.get("status") or "") != "failed":
        return False
    if str(projection.get("job_id") or projection.get("run_identity") or "") != str(proposal.admission_job_id):
        return False
    if str(projection.get("job_kind") or "") != _PROPOSAL_JOB_KIND:
        return False
    owner = projection.get("owner")
    if not isinstance(owner, Mapping):
        return False
    if (
        str(owner.get("kind") or "") != "user"
        or str(owner.get("principal_id") or "") != str(proposal.owner_principal_id)
        or str(owner.get("service_id") or "")
        or str(projection.get("session_id") or "") != str(proposal.owner_session_id)
        or str(projection.get("operator_session_id") or "") != str(proposal.owner_session_id)
    ):
        return False
    identity = projection.get("idempotency")
    if not isinstance(identity, Mapping):
        return False
    if (
        str(identity.get("scope") or "") != "work-board-proposal"
        or str(identity.get("key") or "") != str(proposal.proposal_id)
    ):
        return False
    if (
        str(projection.get("capability_version") or "") != str(proposal.capability_version)
        or str(projection.get("goal_revision") or "") != str(proposal.goal_revision)
        or str(projection.get("input_digest") or "") != _proposal_admission_input_digest(proposal)
        or str(projection.get("authority_digest") or "") != str(proposal.authority_digest)
        or str(projection.get("run_fingerprint") or "") != str(proposal.request_digest)
        or str(projection.get("failure_reason") or "") not in _PRE_CONTACT_RETRY_REASONS
    ):
        return False
    authority = projection.get("declared_authority")
    if not isinstance(authority, Mapping):
        return False
    if (
        str(authority.get("principal") or "") != str(proposal.owner_principal_id)
        or str(authority.get("owner_kind") or "") != "user"
        or str(authority.get("session_id") or "") != str(proposal.owner_session_id)
        or str(authority.get("capability_id") or "") != str(proposal.capability_id)
        or str(authority.get("capability_version") or "") != str(proposal.capability_version)
        or str(authority.get("grant_revision") or "") != str(proposal.grant_revision)
        or authority.get("finite_authority") is not True
    ):
        return False
    if task is not None:
        if (
            str(task.task_id) != str(proposal.parent_task_id)
            or int(task.task_revision) != int(proposal.parent_revision)
            or int(task.goal_revision) != int(proposal.goal_revision)
            or str(projection.get("goal_id") or "") != str(task.goal_id or "")
            or proposal.request_digest
            != _proposal_request_digest(
                task=task,
                kind=proposal.kind,
                idempotency_key=proposal.idempotency_key,
            )
            or proposal.input_digest != _proposal_input_digest(task=task, kind=proposal.kind)
        ):
            return False
    expected_effect_digest = hashlib.sha256(
        _proposal_effect_id(proposal.admission_job_id).encode("utf-8")
    ).hexdigest()[:16]
    if proposal.effect_id_digest != expected_effect_digest:
        return False
    effects = projection.get("effects")
    if not isinstance(effects, list) or effects:
        # The retry runtime will append its own no-effect reconciliation
        # marker.  Before that CAS the ledger must still be empty.
        return False
    try:
        attempt_count = int(projection.get("attempt_count") or 0)
        max_attempts = int(projection.get("max_attempts") or 0)
    except (TypeError, ValueError):
        return False
    if max_attempts <= attempt_count:
        return False
    deadline = projection.get("deadline_at")
    if deadline:
        try:
            if _aware(datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))) <= _now():
                return False
        except ValueError:
            return False
    return True


async def _recover_pre_contact_admission(
    proposal: WorkBoardProposal,
    projection: Mapping[str, Any],
    *,
    task: WorkBoardTask | None = None,
) -> dict[str, Any] | None:
    """Retry one failed pre-contact job through the durable CAS contract."""

    if not _failed_pre_contact_admission_matches(proposal, projection, task=task):
        return None
    job_id = proposal.admission_job_id
    try:
        expected_revision = int(projection.get("revision"))
    except (TypeError, ValueError):
        return None
    receipt = {
        "effect_id": f"job-failure:{job_id}",
        "effect_type": "job_failure",
        "target_path": f"job:{job_id}",
        "status": "read_back",
        "outcome": "no_external_effect",
    }
    try:
        recovered = await durable_job_repository.retry_job(
            job_id,
            owner_kind="user",
            owner_principal_id=proposal.owner_principal_id,
            service_id=None,
            reconciliation_receipt=receipt,
            expected_revision=expected_revision,
        )
    except Exception:
        return None
    if not isinstance(recovered, Mapping) or str(recovered.get("status") or "") != "queued":
        return None
    return dict(recovered)


async def _transition_proposal_job(
    job_id: str,
    *,
    lease_owner: str,
    fence: int,
    status: str,
    reason: str | None = None,
    result_summary: str | None = None,
) -> None:
    await durable_job_repository.transition_job(
        job_id,
        status,
        owner=lease_owner,
        fencing_token=fence,
        reason=reason,
        result_summary=result_summary,
    )


def _proposal_payload(proposal: WorkBoardProposal) -> dict[str, Any]:
    payload = _decode_json(proposal.proposal_json)
    blocked_reason = payload.get("blocked_reason")
    recovery_action = None
    if proposal.provider_contact_started or proposal.provider_contact_state == "unknown":
        recovery_action = "reconcile_external_effect"
    elif (
        blocked_reason == _PROPOSAL_ATTEMPT_BUDGET_REASON
        or (
            isinstance(blocked_reason, str)
            and blocked_reason.startswith("proposal_admission_")
        )
    ):
        # The deterministic job identity has consumed its bounded pre-contact
        # recovery budget.  A same-key retry cannot claim it again; require
        # operator reconciliation instead of advertising a retry that will
        # deterministically fail at the durable runtime.
        recovery_action = "reconcile_admission_binding"
    elif proposal.status == "blocked" and blocked_reason:
        recovery_action = "retry_same_binding_after_prerequisite"
    return {
        "kind": proposal.kind,
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.revision,
        "parent_task_id": proposal.parent_task_id,
        "parent_revision": proposal.parent_revision,
        "idempotency_key": proposal.idempotency_key,
        "proposal_digest": proposal.proposal_digest,
        "expires_at": serialize_utc_datetime(proposal.expires_at),
        "proposed_tasks": payload.get("proposed_tasks", []),
        "proposed_links": payload.get("proposed_links", []),
        "estimated_cost": proposal.estimated_cost,
        "blocked_reason": blocked_reason,
        "recovery_action": recovery_action,
        "status": proposal.status,
        "request_digest": proposal.request_digest,
        "route_id": proposal.route_id,
        "capability_id": proposal.capability_id,
        "capability_version": proposal.capability_version,
        "grant_revision": proposal.grant_revision,
        "input_digest": proposal.input_digest,
        "admission_job_id": proposal.admission_job_id,
        "effect_id_digest": proposal.effect_id_digest,
        "provider_contact_state": proposal.provider_contact_state,
    }


def _blocked_payload(
    proposal: WorkBoardProposal,
    reason: str,
    *,
    contact_state: str | None = None,
) -> dict[str, Any]:
    payload = _decode_json(proposal.proposal_json)
    payload["blocked_reason"] = reason
    proposal.proposal_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    proposal.status = "blocked"
    if contact_state is not None:
        proposal.provider_contact_state = contact_state
    proposal.revision += 1
    return _proposal_payload(proposal)


async def _pre_contact_retryable(
    proposal: WorkBoardProposal,
    *,
    task: WorkBoardTask | None = None,
) -> bool:
    """Prove a proposal never crossed its durable admission boundary.

    A configuration block happens before job admission and provider contact.
    Such a row may be reopened with the same idempotency key after the route
    is repaired.  Any visible durable job, generated proposal, or provider
    marker makes the row terminal/reconciliation-only instead.
    ``effect_id_digest`` is the planned stable operation identity; it is not an
    effect receipt and is therefore safe to retain while reopening the row.
    """
    if proposal.provider_contact_started or proposal.provider_contact_state != "not_started":
        return False
    if proposal.proposal_digest:
        return False
    payload = _decode_json(proposal.proposal_json)
    if payload.get("proposed_tasks") or payload.get("proposed_links"):
        return False
    if not proposal.admission_job_id:
        return True
    try:
        durable_job = await durable_job_repository.get_job(proposal.admission_job_id)
    except Exception:
        # Inability to prove that no durable job exists is a reconciliation
        # condition, never permission to retry a paid operation.
        return False
    if durable_job is None:
        return True
    return _failed_pre_contact_admission_matches(proposal, durable_job, task=task)


def _reopen_pre_contact_proposal(proposal: WorkBoardProposal) -> None:
    payload = _decode_json(proposal.proposal_json)
    payload["blocked_reason"] = None
    proposal.proposal_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    proposal.status = "pending_inference"
    proposal.provider_contact_state = "not_started"
    proposal.revision += 1


def _proposal_is_expirable(proposal: WorkBoardProposal) -> bool:
    """Expire stale reviewable output without hiding an unknown provider contact."""
    if proposal.status == "proposed":
        return True
    return (
        proposal.status == "pending_inference"
        and not proposal.provider_contact_started
        and proposal.provider_contact_state == "not_started"
    )


def _extract_completion_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise BoardError("invalid_model_output", "The governed proposal route returned no proposal", status_code=409)
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, Mapping))
    if not isinstance(content, str) or not content.strip():
        raise BoardError("invalid_model_output", "The governed proposal route returned no proposal", status_code=409)
    return content.strip()


def _parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise BoardError("invalid_model_output", "The proposal route returned invalid structured output", status_code=409) from exc
    if not isinstance(payload, dict):
        raise BoardError("invalid_model_output", "The proposal route returned an object proposal", status_code=409)
    return payload


async def _normalise_tasks(
    raw: Mapping[str, Any],
    *,
    kind: str,
    parent: WorkBoardTask,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str | None]:
    raw_tasks = raw.get("proposed_tasks")
    if not isinstance(raw_tasks, list):
        # Accept the concise one-task form from an intercepted transport.
        raw_tasks = [raw] if kind == "specify" and raw.get("title") else []
    expected_count = 1 if kind == "specify" else None
    if expected_count is not None and len(raw_tasks) != expected_count:
        raise BoardError("invalid_model_output", "Specify must return exactly one task", status_code=409)
    if kind == "decompose" and not 1 <= len(raw_tasks) <= 5:
        raise BoardError("invalid_model_output", "Decompose must return one to five tasks", status_code=409)
    tasks: list[dict[str, Any]] = []
    ids: list[str] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, Mapping):
            raise BoardError("invalid_model_output", "Every proposed task must be an object", status_code=409)
        task_id = await _provider_safe_identifier(
            item.get("task_id") or f"proposal-child-{index}",
            field="task_id",
        )
        if task_id in ids:
            raise BoardError("invalid_model_output", "Proposal task IDs must be unique", status_code=409)
        ids.append(task_id)
        title = await WorkBoardRepository._safe_text(
            _safe_text(item.get("title"), field="title", max_length=200)
        )
        body = await WorkBoardRepository._safe_text(
            _safe_text(item.get("body"), field="body", max_length=4_000)
        )
        capability = await _provider_safe_identifier(
            item.get("capability_id"),
            field="capability_id",
            max_length=128,
        )
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES

        registered = REGISTERED_CAPABILITIES.get(capability)
        if registered is None:
            raise BoardError(
                "invalid_model_output",
                "The proposal names no registered Seraph capability",
                status_code=409,
            )
        expected_executor = registered_executor_id(capability)
        supplied_executor = item.get("executor_id")
        if supplied_executor is not None and str(supplied_executor).strip():
            supplied_executor = await _provider_safe_identifier(
                supplied_executor,
                field="executor_id",
                max_length=128,
            )
            if supplied_executor != expected_executor:
                raise BoardError(
                    "invalid_model_output",
                    "The proposed executor does not match the registered capability lane",
                    status_code=409,
                )
        executor = expected_executor
        typed_ref_raw = item.get("typed_input_ref")
        typed_digest_raw = item.get("typed_input_digest")
        if not typed_ref_raw or not typed_digest_raw:
            raise BoardError("invalid_model_output", "Every proposed task requires typed input reference and digest", status_code=409)
        typed_ref = await _provider_safe_reference(typed_ref_raw, field="typed_input_ref")
        typed_digest = str(typed_digest_raw).lower().strip()
        if not _SAFE_DIGEST.fullmatch(typed_digest):
            raise BoardError("invalid_model_output", "Every proposed typed input digest must be SHA-256", status_code=409)
        cost = item.get("cost_estimate")
        cost_text = (
            None
            if cost is None
            else await WorkBoardRepository._safe_text(
                _safe_text(cost, field="cost_estimate", max_length=64)
            )
        )
        dependencies = item.get("dependencies") or []
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise BoardError("invalid_model_output", "Proposal dependencies must be safe task IDs", status_code=409)
        dependencies = [
            await _provider_safe_identifier(dep, field="dependency")
            for dep in dependencies
        ]
        if len(dependencies) != len(set(dependencies)):
            raise BoardError(
                "invalid_model_output",
                "Proposal dependency edges must be unique",
                status_code=409,
            )
        normalized_item = {
            "task_id": task_id,
            "title": title,
            "body": body,
            "goal_id": parent.goal_id,
            "goal_revision": parent.goal_revision,
            "capability_id": capability,
            "typed_input_ref": typed_ref,
            "typed_input_digest": typed_digest,
            "executor_id": executor,
            "capability_version": registered.version,
            "dependencies": dependencies,
            "cost_estimate": cost_text,
        }
        # This live, capability-specific preview is derived from Seraph state.
        # A model-supplied authority field is ignored.
        normalized_item["authority"] = await _proposal_task_authority_summary(
            parent,
            normalized_item,
        )
        tasks.append(normalized_item)
    links: list[dict[str, str]] = []
    if kind == "specify":
        # Specify completes the source card's execution contract.  It does
        # not create a second task or a blocking dependency; acceptance
        # applies this one normalized specification to the same Triage row.
        links = []
    else:
        # Every proposed child remains linked to the source task.  Additional
        # sibling edges describe the staged dependency graph; they never
        # replace the source-parent handoff edge.
        links = [
            {"parent_task_id": parent.task_id, "child_task_id": item["task_id"]}
            for item in tasks
        ]
        for item in tasks:
            for dependency in item["dependencies"]:
                if dependency == parent.task_id:
                    continue
                if dependency not in ids:
                    raise BoardError("invalid_model_output", "Proposal dependency is outside the proposal graph", status_code=409)
                links.append({"parent_task_id": dependency, "child_task_id": item["task_id"]})
    edge_keys = [(str(link["parent_task_id"]), str(link["child_task_id"])) for link in links]
    if len(edge_keys) != len(set(edge_keys)):
        raise BoardError(
            "invalid_model_output",
            "Proposal dependency edges must be unique",
            status_code=409,
        )
    _validate_acyclic(tasks, links, parent.task_id)
    estimated = raw.get("estimated_cost")
    estimated_cost = (
        None
        if estimated is None
        else await WorkBoardRepository._safe_text(
            _safe_text(estimated, field="estimated_cost", max_length=64)
        )
    )
    return tasks, links, estimated_cost


def _validate_acyclic(tasks: list[Mapping[str, Any]], links: list[Mapping[str, str]], parent_id: str) -> None:
    edges: dict[str, set[str]] = {str(item["task_id"]): set() for item in tasks}
    for link in links:
        parent = str(link["parent_task_id"])
        child = str(link["child_task_id"])
        if parent == child:
            raise BoardError("invalid_model_output", "Proposal dependency graph contains a self-cycle", status_code=409)
        if child in edges and parent in edges:
            edges[parent].add(child)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise BoardError("invalid_model_output", "Proposal dependency graph contains a cycle", status_code=409)
        if node in visited:
            return
        visiting.add(node)
        for child in edges.get(node, ()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def _validate_proposed_typed_inputs(
    tasks: list[Mapping[str, Any]],
    *,
    parent: WorkBoardTask,
) -> None:
    """Validate model-proposed input files before any child row is created.

    ``_normalise_tasks`` checks that the provider returned safe-looking
    references and digests.  That is insufficient for acceptance: the
    reference must resolve inside Seraph's canonical workspace, the bytes must
    match the digest, and the JSON envelope must match the registered
    capability schema.  Reuse the dispatcher parser against in-memory task
    projections so acceptance has one source of truth and does not persist an
    invented executable reference.
    """
    from src.work_board.dispatcher import (
        REGISTERED_CAPABILITIES,
        TypedInputError,
        _parse_typed_input,
        registered_executor_id as derive_registered_executor_id,
    )

    for item in tasks:
        capability_id = str(item.get("capability_id") or "")
        capability = REGISTERED_CAPABILITIES.get(capability_id)
        if capability is None:
            raise BoardError(
                "capability_unregistered",
                "The accepted proposal names no registered capability",
                status_code=409,
            )
        if str(item.get("capability_version") or "") != capability.version:
            raise BoardError(
                "capability_version_stale",
                "The accepted capability version changed",
                status_code=409,
            )
        candidate = WorkBoardTask(
            task_id=str(item.get("task_id") or ""),
            owner_principal_id=parent.owner_principal_id,
            owner_session_id=parent.owner_session_id,
            origin_session_id=parent.origin_session_id or parent.owner_session_id,
            goal_id=parent.goal_id,
            goal_revision=parent.goal_revision,
            title=str(item.get("title") or ""),
            body=str(item.get("body") or ""),
            capability_id=capability_id,
            typed_input_ref=str(item.get("typed_input_ref") or ""),
            typed_input_digest=str(item.get("typed_input_digest") or ""),
            executor_id=str(item.get("executor_id") or ""),
            idempotency_scope=f"proposal-validation:{parent.task_id}",
            idempotency_key=f"{parent.task_id}:{item.get('task_id')}",
            status=WorkBoardStatus.todo,
        )
        try:
            _parse_typed_input(candidate)
        except TypedInputError as exc:
            raise BoardError(exc.code, str(exc), status_code=409) from exc
        except (OSError, ValueError) as exc:
            # Missing/unavailable canonical workspace state is a blocked
            # acceptance prerequisite, never permission to persist a guessed
            # task input.
            raise BoardError(
                "typed_input_unavailable",
                "The proposed typed input cannot be verified in the canonical workspace",
                status_code=409,
            ) from exc


async def _validate_proposal_authority_preview(
    tasks: list[Mapping[str, Any]],
    *,
    parent: WorkBoardTask,
    expected_previews: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Reject acceptance unless every proposed task was previewed with the
    current server-derived authority summary.

    This is separate from typed-input validation so workspace schema checks
    remain independently reusable and testable. It still runs in the locked
    acceptance transaction before any task or dependency row is written.
    """
    from src.work_board.dispatcher import REGISTERED_CAPABILITIES

    validated: dict[str, str] = {}
    for item in tasks:
        capability_id = str(item.get("capability_id") or "")
        capability = REGISTERED_CAPABILITIES.get(capability_id)
        if capability is None or str(item.get("capability_version") or "") != capability.version:
            raise BoardError(
                "capability_version_stale",
                "The proposed capability registration changed before acceptance",
                status_code=409,
            )
        task_id = str(item.get("task_id") or "")
        expected = (
            await _proposal_task_authority_summary(parent, item)
            if expected_previews is None
            else expected_previews.get(task_id, "")
        )
        if "Current provider-free preflight: UNAVAILABLE" in expected:
            raise BoardError(
                "proposal_authority_preview_unavailable",
                "Current capability authority could not be verified; refresh the preview before acceptance",
                status_code=409,
            )
        if str(item.get("authority") or "") != expected:
            raise BoardError(
                "proposal_authority_preview_missing",
                "The capability authority or runtime preview changed; refresh the proposal before acceptance",
                status_code=409,
            )
        validated[task_id] = expected
    return validated


async def _prepare_governed_proposal(
    task: WorkBoardTask,
    *,
    kind: str,
    operator: AuthenticatedOperator,
    job_id: str,
    route_id: str,
) -> tuple[list[dict[str, Any]], Any, Any, str]:
    """Redact task text and preflight the governed route before its marker.

    The task card is operator data and may contain credentials or private
    source text.  The canonical vault redactor runs before durable provider
    contact is marked, so a route/policy/redaction denial remains a known
    no-contact prerequisite block.  The transformation digest and redaction
    flag are carried into the exact inference context used by the transport.
    """
    raw_title = (task.title or "")[:200]
    raw_body = (task.body or "")[:4_000]
    safe_title = await WorkBoardRepository._safe_text(raw_title)
    safe_body = await WorkBoardRepository._safe_text(raw_body)
    if safe_title == "[redaction unavailable]" or safe_body == "[redaction unavailable]":
        raise BoardError(
            "proposal_redaction_unavailable",
            "The vault redaction prerequisite is unavailable; provider contact was not started",
            status_code=409,
        )
    transformation_digest = canonical_digest(
        {
            "title_before": raw_title,
            "body_before": raw_body,
            "title_after": safe_title,
            "body_after": safe_body,
            "kind": kind,
        }
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Return only one JSON object for a Seraph work-board proposal. "
                "Do not execute tools, invent authority, include secrets, or include markdown."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "kind": kind,
                    "task_id": task.task_id,
                    "goal_id": task.goal_id,
                    "goal_revision": task.goal_revision,
                    "title_data": safe_title,
                    "body_data": safe_body,
                    "required_fields": [
                        "title", "body", "capability_id", "typed_input_ref",
                        "typed_input_digest", "executor_id",
                    ],
                    "authority_display": (
                        "Seraph derives authority from the authenticated task owner, goal revision, "
                        "registered capability, and fixed work-board limits. Do not return an authority field."
                    ),
                    "max_children": 5 if kind == "decompose" else 1,
                },
                sort_keys=True,
            ),
        },
    ]
    principal = replace(
        bind_operator_principal(operator, operator.session_id),
        job_id=job_id,
    )
    try:
        context = build_canonical_inference_context(
            route_id,
            payload=messages,
            output_tokens=1_024,
            timeout_seconds=120,
            principal=principal,
            session_id=principal.session_id,
            job_id=principal.job_id,
            transformation_digest=transformation_digest,
            redaction_applied=True,
        )
    except Exception as exc:
        raise BoardError(
            "proposal_policy_preflight_required",
            "The governed proposal policy denied preflight; provider contact was not started",
            status_code=409,
        ) from exc
    return messages, principal, context, transformation_digest


async def _invoke_governed_proposal(
    *,
    messages: list[dict[str, Any]],
    principal: Any,
    context: Any,
    job_id: str,
    lease_owner: str,
    fencing_token: int,
) -> str:
    """Call one explicitly selected governed OpenRouter route only."""
    tokens = set_runtime_context(
        principal.session_id,
        get_current_approval_mode(),
        trust_principal=principal,
    )
    try:
        with bind_remote_inference_receipt(
            repository=durable_job_repository,
            job_id=job_id,
            owner=lease_owner,
            fencing_token=int(fencing_token),
        ):
            response = await completion_with_fallback(
                messages=messages,
                temperature=0.1,
                max_tokens=1_024,
                timeout=120,
                runtime_path=context.runtime_path,
                profile="openrouter",
                request_context=context,
            )
        return _extract_completion_content(response)
    finally:
        reset_runtime_context(tokens)


async def _expire_proposal_if_due(
    db,
    proposal: WorkBoardProposal,
    *,
    transaction_locked: bool = False,
) -> WorkBoardProposal:
    """Expire one proposal with a revision-checked terminal transition.

    Expiry can be discovered while serving a read, so first acquire SQLite's
    writer lock and reload the row when the caller does not already hold it.
    The conditional update keeps a stale projection from expiring a row that a
    concurrent operator action has already changed.
    """
    now = _now()
    if not _proposal_is_expirable(proposal) or _aware(proposal.expires_at) > now:
        return proposal

    if not transaction_locked:
        await _begin_sqlite_immediate(db)
        await db.refresh(proposal)

    now = _now()
    if not _proposal_is_expirable(proposal) or _aware(proposal.expires_at) > now:
        return proposal

    expected_revision = int(proposal.revision)
    statement = (
        update(WorkBoardProposal)
        .where(
            WorkBoardProposal.proposal_id == proposal.proposal_id,
            WorkBoardProposal.owner_principal_id == proposal.owner_principal_id,
            WorkBoardProposal.owner_session_id == proposal.owner_session_id,
            WorkBoardProposal.revision == expected_revision,
            WorkBoardProposal.status == proposal.status,
            WorkBoardProposal.expires_at <= now,
        )
    )
    if proposal.status == "pending_inference":
        statement = statement.where(
            WorkBoardProposal.provider_contact_started.is_(False),
            WorkBoardProposal.provider_contact_state == "not_started",
        )
    updated = await db.execute(
        statement.values(
            status="expired",
            revision=expected_revision + 1,
        ).execution_options(synchronize_session=False)
    )
    await db.refresh(proposal)
    if int(updated.rowcount or 0) != 1:
        # The revision/status predicate lost to another transition. The
        # refreshed row is authoritative; never overwrite it from this read.
        return proposal
    return proposal


async def _get_proposal(
    db,
    owner: WorkBoardOwner,
    proposal_id: str,
    *,
    transaction_locked: bool = False,
) -> WorkBoardProposal:
    proposal = (
        await db.execute(
            select(WorkBoardProposal).where(
                WorkBoardProposal.proposal_id == proposal_id,
                WorkBoardProposal.owner_principal_id == owner.principal_id,
                WorkBoardProposal.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise BoardError("proposal_not_found", "The requested proposal does not exist", status_code=404)
    return await _expire_proposal_if_due(
        db,
        proposal,
        transaction_locked=transaction_locked,
    )


async def _find_idempotent_proposal(
    db,
    *,
    owner: WorkBoardOwner,
    task_id: str,
    parent_revision: int,
    kind: str,
    idempotency_key: str,
) -> WorkBoardProposal | None:
    """Read one proposal for the exact task revision and idempotency key."""

    result = await db.execute(
        select(WorkBoardProposal)
        .where(
            WorkBoardProposal.owner_principal_id == owner.principal_id,
            WorkBoardProposal.owner_session_id == owner.session_id,
            WorkBoardProposal.parent_task_id == task_id,
            WorkBoardProposal.parent_revision == int(parent_revision),
            WorkBoardProposal.kind == kind,
            WorkBoardProposal.idempotency_key == idempotency_key,
        )
        .order_by(WorkBoardProposal.created_at.asc(), WorkBoardProposal.proposal_id.asc())
    )
    matches = list(result.scalars().all())
    if len(matches) > 1:
        raise BoardError(
            "proposal_idempotency_reconciliation_required",
            "Multiple legacy proposal receipts share this idempotency key; operator reconciliation is required",
            status_code=409,
        )
    return matches[0] if matches else None


async def _find_unresolved_binding_proposal(
    db,
    *,
    owner: WorkBoardOwner,
    task: WorkBoardTask,
    kind: str,
    route_id: str | None,
    capability_version: str | None,
) -> WorkBoardProposal | None:
    """Find a prior contacted/unknown proposal a fresh key cannot bypass."""
    result = await db.execute(
        select(WorkBoardProposal)
        .where(
            WorkBoardProposal.owner_principal_id == owner.principal_id,
            WorkBoardProposal.owner_session_id == owner.session_id,
            WorkBoardProposal.parent_task_id == task.task_id,
            WorkBoardProposal.kind == kind,
            WorkBoardProposal.parent_revision == task.task_revision,
            WorkBoardProposal.goal_revision == task.goal_revision,
        )
        .order_by(WorkBoardProposal.created_at.asc(), WorkBoardProposal.proposal_id.asc())
        .limit(20)
    )
    for proposal in result.scalars().all():
        if proposal.capability_id != _PROPOSAL_CAPABILITY:
            continue
        if route_id is not None and proposal.route_id != route_id:
            continue
        if capability_version is not None and proposal.capability_version != capability_version:
            continue
        if int(proposal.grant_revision or 0) != int(task.goal_revision):
            continue
        if proposal.input_digest != _proposal_input_digest(task=task, kind=kind):
            continue
        if route_id is not None and capability_version is not None:
            if proposal.authority_digest != _authority_digest(owner, task, route_id, capability_version):
                continue
        # With an unavailable route there is no current authority digest to
        # compare.  The owner/session, parent revision, goal revision, and
        # immutable input digest still prove this is the same bounded task
        # request; the unresolved provider marker must block a new key.
        if proposal.status not in {"pending_inference", "blocked"}:
            continue
        if proposal.provider_contact_started or proposal.provider_contact_state in {"started", "unknown"}:
            return proposal
        # A proposal row can lose the marker in a crash window after durable
        # admission.  A fresh key must not bypass an existing, failed,
        # terminal, or otherwise ambiguous job even when the row still says
        # ``not_started``.  Only the same binding may attempt its bounded
        # recovery, and only that path proves an empty effect ledger.
        if proposal.admission_job_id:
            try:
                durable_job = await durable_job_repository.get_job(proposal.admission_job_id)
            except Exception:
                # Failure to prove the durable identity is itself ambiguous;
                # preserve the old row as the reconciliation receipt.
                return proposal
            if durable_job is not None:
                return proposal
    return None


async def list_proposals(
    owner: WorkBoardOwner,
    task_id: str,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return owner/session-bound proposal receipts for restart recovery."""

    repository = WorkBoardRepository()
    async with get_session() as db:
        await repository._owned_task(db, owner, task_id)
        statement = (
            select(WorkBoardProposal)
            .where(
                WorkBoardProposal.owner_principal_id == owner.principal_id,
                WorkBoardProposal.owner_session_id == owner.session_id,
                WorkBoardProposal.parent_task_id == task_id,
            )
            .order_by(WorkBoardProposal.created_at.desc(), WorkBoardProposal.proposal_id.desc())
        )
        if kind is not None:
            if kind not in _PROPOSAL_KINDS:
                raise BoardError("invalid_proposal_kind", "The proposal kind is not supported", status_code=422)
            statement = statement.where(WorkBoardProposal.kind == kind)
        proposals = list((await db.execute(statement.limit(20))).scalars().all())
        for proposal in proposals:
            if _proposal_is_expirable(proposal) and _aware(proposal.expires_at) <= _now():
                await _expire_proposal_if_due(db, proposal)
        payloads: list[dict[str, Any]] = []
        for proposal in proposals:
            if proposal.provider_contact_started or proposal.provider_contact_state != "not_started":
                payloads.append(await _reconcile_started_proposal(db, owner, proposal))
                continue
            payloads.append(_proposal_payload(proposal))
        await db.flush()
        return payloads


async def get_proposal(
    owner: WorkBoardOwner,
    proposal_id: str,
) -> dict[str, Any]:
    """Return one owner/session-bound proposal receipt after a reload."""

    async with get_session() as db:
        proposal = await _get_proposal(db, owner, proposal_id)
        if proposal.provider_contact_started or proposal.provider_contact_state != "not_started":
            # _reconcile_started_proposal already returns the safe API
            # projection.  Do not pass that mapping through the ORM serializer
            # a second time after an expiry or restart reconciliation.
            return await _reconcile_started_proposal(db, owner, proposal)
        return _proposal_payload(proposal)


async def _reconcile_started_proposal(
    db,
    owner: WorkBoardOwner,
    proposal: WorkBoardProposal,
) -> dict[str, Any]:
    """Return a stable pending receipt or block an abandoned provider marker.

    A proposal row marked as provider-contacted is never allowed to silently
    re-enter the transport.  An actively queued/running durable job remains a
    pending receipt; every other unresolved marker becomes an operator-visible
    reconciliation block.
    """
    if proposal.status != "pending_inference":
        return _proposal_payload(proposal)
    projection = None
    try:
        projection = await durable_job_repository.get_job(proposal.admission_job_id)
    except Exception:
        projection = None
    status = str((projection or {}).get("status") or "") if isinstance(projection, Mapping) else ""
    if status == "running" and not _durable_job_lease_live(projection):
        return _blocked_payload(
            proposal,
            "proposal_provider_contact_reconciliation_required"
            if proposal.provider_contact_started or proposal.provider_contact_state in {"started", "unknown"}
            else "proposal_admission_reconciliation_required",
            contact_state=(
                "unknown"
                if proposal.provider_contact_started or proposal.provider_contact_state in {"started", "unknown"}
                else "not_started"
            ),
        )
    if status == "succeeded":
        # A crash may occur after the structured output was durably staged but
        # before the proposal row was advanced to ``proposed``.  Promote that
        # exact staged digest only after the exact durable job/effect binding
        # is terminally successful; never call the provider again.
        if _proposal_job_is_terminal_success(proposal, projection) and _proposal_has_staged_output(proposal):
            proposal.status = "proposed"
            proposal.provider_contact_state = "succeeded"
            proposal.revision += 1
            await db.flush()
            return _proposal_payload(proposal)
        return _blocked_payload(
            proposal,
            "proposal_provider_contact_reconciliation_required",
            contact_state="unknown",
        )
    if status in {"accepted", "queued", "running", "awaiting_approval", "paused"}:
        return _proposal_payload(proposal)
    contacted = proposal.provider_contact_started or proposal.provider_contact_state in {"started", "unknown"}
    return _blocked_payload(
        proposal,
        "proposal_provider_contact_reconciliation_required" if contacted else "proposal_admission_reconciliation_required",
        contact_state=proposal.provider_contact_state or ("unknown" if contacted else "not_started"),
    )


def _durable_job_lease_live(projection: Mapping[str, Any] | None) -> bool:
    """Require an unexpired runtime lease before trusting a running job."""

    if not isinstance(projection, Mapping):
        return False
    lease = projection.get("lease")
    if not isinstance(lease, Mapping):
        return False
    raw_expiry = lease.get("expires_at")
    if not isinstance(raw_expiry, str) or not raw_expiry.strip():
        return False
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    return _aware(expiry) > _now()


def _proposal_job_is_pending(
    proposal: WorkBoardProposal,
    projection: Mapping[str, Any] | None,
) -> bool:
    """Accept only an exact, effect-free durable admission as pending.

    A proposal may remain pending after a process restart only while its
    durable job is accepted/queued or owns a live running lease.  Failed,
    cancelled, terminal, missing, and expired-running projections are
    reconciliation states; treating them as pending would invite a second
    provider admission under a different process.
    """
    if not isinstance(projection, Mapping):
        return False
    job_id = str(projection.get("job_id") or projection.get("run_identity") or "")
    if job_id != str(proposal.admission_job_id):
        return False
    effects = projection.get("effects")
    if not isinstance(effects, list) or effects:
        return False
    status = str(projection.get("status") or "")
    if status in {"accepted", "queued"}:
        return True
    return status == "running" and _durable_job_lease_live(projection)


def _proposal_job_is_terminal_success(
    proposal: WorkBoardProposal,
    projection: Mapping[str, Any] | None,
) -> bool:
    """Prove the proposal's exact durable job settled successfully."""
    if not isinstance(projection, Mapping):
        return False
    if str(projection.get("job_id") or projection.get("run_identity") or "") != str(proposal.admission_job_id):
        return False
    if str(projection.get("status") or "") != "succeeded":
        return False
    effects = projection.get("effects")
    if not isinstance(effects, list) or not effects:
        return False
    expected_digest = str(proposal.effect_id_digest or "")
    matched_effect = False
    for effect in effects:
        if not isinstance(effect, Mapping) or str(effect.get("status") or "") not in {
            "succeeded",
            "read_back",
            "reconciled",
        }:
            return False
        effect_digest = str(effect.get("effect_id_digest") or "")
        effect_id = effect.get("effect_id")
        if not effect_digest and isinstance(effect_id, str) and effect_id:
            effect_digest = hashlib.sha256(effect_id.encode("utf-8")).hexdigest()[:16]
        if expected_digest and effect_digest == expected_digest:
            matched_effect = True
    return matched_effect if expected_digest else True


def _proposal_has_staged_output(proposal: WorkBoardProposal) -> bool:
    payload = _decode_json(proposal.proposal_json)
    if not proposal.proposal_digest:
        return False
    canonical = {
        "proposed_tasks": payload.get("proposed_tasks"),
        "proposed_links": payload.get("proposed_links"),
        "blocked_reason": payload.get("blocked_reason"),
    }
    return _proposal_digest(canonical) == proposal.proposal_digest


async def _validate_decompose_source(
    db,
    *,
    repository: WorkBoardRepository,
    owner: WorkBoardOwner,
    task: WorkBoardTask,
) -> None:
    """Require an executable Todo source before staging child proposals.

    A Decompose proposal adds blocking parent links.  Allowing it from a
    rough Triage card would create children that can never pass the parent's
    execution/readiness contract, so the source must already be a complete
    Todo task with current owner-bound goal authority and a verified typed
    input.  The source task remains ordinary operator data; its executor lane
    is not rewritten here.
    """

    if task.status is not WorkBoardStatus.todo:
        raise BoardError(
            "decompose_requires_todo",
            "Decompose requires a fully specified Todo source task",
            status_code=409,
        )
    await repository.validate_task_goal(db, owner, task)
    from src.work_board.dispatcher import (
        REGISTERED_CAPABILITIES,
        TypedInputError,
        _parse_typed_input,
        registered_executor_id as derive_registered_executor_id,
    )

    capability_id = str(task.capability_id or "").strip()
    if capability_id not in REGISTERED_CAPABILITIES:
        raise BoardError(
            "capability_unregistered",
            "The Decompose source names no registered Seraph capability",
            status_code=409,
        )
    expected_executor = derive_registered_executor_id(capability_id)
    if expected_executor is None:
        raise BoardError(
            "capability_unregistered",
            "The Decompose source names no registered Seraph capability",
            status_code=409,
        )
    if not str(task.executor_id or "").strip():
        raise BoardError(
            "executor_missing",
            "The Decompose source has no bound executor",
            status_code=409,
        )
    if str(task.executor_id).strip() != expected_executor:
        raise BoardError(
            "executor_lane_mismatch",
            "The Decompose source executor does not match the registered capability lane",
            status_code=409,
        )
    if not str(task.typed_input_ref or "").strip() or not str(task.typed_input_digest or "").strip():
        raise BoardError(
            "typed_input_missing",
            "The Decompose source has no complete typed input reference",
            status_code=409,
        )
    try:
        _parse_typed_input(task)
    except TypedInputError as exc:
        raise BoardError(exc.code, str(exc), status_code=409) from exc
    except (OSError, ValueError) as exc:
        raise BoardError(
            "typed_input_unavailable",
            "The Decompose source typed input cannot be verified in the canonical workspace",
            status_code=409,
        ) from exc


async def create_proposal(
    owner: WorkBoardOwner,
    task_id: str,
    *,
    kind: str,
    request: WorkBoardProposalRequest,
    operator: AuthenticatedOperator,
) -> dict[str, Any]:
    if kind not in _PROPOSAL_KINDS:
        raise BoardError("invalid_proposal_kind", "The proposal kind is not supported", status_code=422)
    repository = WorkBoardRepository()
    route_error: str | None = None
    try:
        route_id, capability_version = _route_binding()
    except BoardError as exc:
        route_id, capability_version = _PROPOSAL_ROUTE, "unavailable"
        route_error = exc.code
    async with get_session() as db:
        # Serialize proposal reservation against dispatcher promotion. A
        # pending or proposed triage request keeps this exact task revision
        # from being admitted until the operator accepts or rejects it.
        await _begin_sqlite_immediate(db)
        task = await repository._owned_task(db, owner, task_id)
        if task.task_revision != request.expected_revision:
            raise BoardRevisionConflict(task.task_id, request.expected_revision, task.task_revision)
        if task.status not in {WorkBoardStatus.triage, WorkBoardStatus.todo}:
            raise BoardError("triage_not_allowed", "Specify and Decompose are available only before execution", status_code=409)
        if kind == "decompose":
            await _validate_decompose_source(
                db,
                repository=repository,
                owner=owner,
                task=task,
            )
        existing = await _find_idempotent_proposal(
            db,
            owner=owner,
            task_id=task.task_id,
            parent_revision=task.task_revision,
            kind=kind,
            idempotency_key=request.idempotency_key,
        )
        if existing is not None:
            expected_digest = _proposal_request_digest(
                task=task,
                kind=kind,
                idempotency_key=request.idempotency_key,
            )
            if existing.request_digest != expected_digest:
                raise BoardError(
                    "proposal_idempotency_conflict",
                    "The proposal idempotency key is bound to a different request",
                    status_code=409,
                )
            if (
                _proposal_is_expirable(existing)
                and _aware(existing.expires_at) <= _now()
            ):
                await _expire_proposal_if_due(
                    db,
                    existing,
                    transaction_locked=True,
                )
                return _proposal_payload(existing)
            pre_contact_retryable = await _pre_contact_retryable(existing, task=task)
            if route_error is None and (
                existing.route_id != route_id
                or existing.capability_id != _PROPOSAL_CAPABILITY
                or existing.capability_version != capability_version
                or int(existing.grant_revision or 0) != int(task.goal_revision)
                or existing.authority_digest != _authority_digest(owner, task, route_id, capability_version)
                or existing.input_digest != _proposal_input_digest(task=task, kind=kind)
            ):
                # The full route/capability/authority/input binding is
                # immutable for an idempotency key, even when no provider
                # contact occurred.  A repaired route must use a fresh key;
                # silently rebinding here would make a retry a different
                # request with the same durable identity.
                raise BoardError(
                    "proposal_binding_conflict",
                    "The existing proposal is bound to a different route, authority, or input revision",
                    status_code=409,
                )
            if existing.provider_contact_started or existing.provider_contact_state != "not_started":
                return await _reconcile_started_proposal(db, owner, existing)
            # A same-binding retry may cross the provider boundary only after
            # the live owner/session goal has been rechecked.  A known route
            # failure never reaches this boundary, so it can remain a truthful
            # no-contact prerequisite block even when a legacy task lacks a
            # current Goal row.
            if route_error is None:
                await repository.validate_task_goal(db, owner, task)
            if existing.status != "pending_inference":
                if existing.status == "blocked" and pre_contact_retryable:
                    _reopen_pre_contact_proposal(existing)
                else:
                    return _proposal_payload(existing)
            proposal_id = existing.proposal_id
            task_snapshot = task
            # A previous process reserved this row but never crossed the
            # provider marker.  The same durable binding may be resumed.
            resume_existing = True
        else:
            # A fresh idempotency key cannot bypass another proposal for the
            # same task/binding while its provider contact or effect is still
            # unresolved.  This lookup deliberately runs before reserving a
            # second durable job.  When route preflight is unavailable, omit
            # route/version matching and retain the task/goal/input binding
            # proof so a contacted row still blocks a bypass.
            unresolved = await _find_unresolved_binding_proposal(
                db,
                owner=owner,
                task=task,
                kind=kind,
                route_id=route_id if route_error is None else None,
                capability_version=capability_version if route_error is None else None,
            )
            if unresolved is not None:
                return await _reconcile_started_proposal(db, owner, unresolved)
            if route_error is None:
                # A triage request is an execution-authority boundary.  Bind
                # it to the current owner/session goal before reserving a
                # durable inference job; acceptance-time validation alone
                # would allow a revoked goal to spend remote inference first.
                await repository.validate_task_goal(db, owner, task)
            resume_existing = False
            request_digest = _proposal_request_digest(
                task=task,
                kind=kind,
                idempotency_key=request.idempotency_key,
            )
            input_digest = _proposal_input_digest(task=task, kind=kind)
            proposal = WorkBoardProposal(
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                parent_task_id=task.task_id,
                parent_revision=task.task_revision,
                goal_revision=task.goal_revision,
                kind=kind,
                idempotency_key=request.idempotency_key,
                request_digest=request_digest,
                capability_id=_PROPOSAL_CAPABILITY,
                capability_version=capability_version,
                authority_digest=_authority_digest(owner, task, route_id, capability_version),
                grant_revision=int(task.goal_revision),
                input_digest=input_digest,
                route_id=route_id,
                status="pending_inference",
                proposal_json=json.dumps({"proposed_tasks": [], "proposed_links": []}),
                expires_at=_now() + PROPOSAL_TTL,
            )
            proposal.admission_job_id = f"work-board-proposal:{proposal.proposal_id}"
            proposal.effect_id_digest = hashlib.sha256(
                _proposal_effect_id(proposal.admission_job_id).encode("utf-8")
            ).hexdigest()[:16]
            db.add(proposal)
            try:
                await db.flush()
            except IntegrityError:
                existing = await _find_idempotent_proposal(
                    db,
                    owner=owner,
                    task_id=task.task_id,
                    parent_revision=task.task_revision,
                    kind=kind,
                    idempotency_key=request.idempotency_key,
                )
                if existing is not None:
                    if existing.request_digest != request_digest:
                        raise BoardError(
                            "proposal_idempotency_conflict",
                            "The proposal idempotency key is bound to a different request",
                            status_code=409,
                        )
                    if (
                        _proposal_is_expirable(existing)
                        and _aware(existing.expires_at) <= _now()
                    ):
                        await _expire_proposal_if_due(
                            db,
                            existing,
                            transaction_locked=True,
                        )
                        return _proposal_payload(existing)
                    if existing.provider_contact_started or existing.provider_contact_state != "not_started":
                        return await _reconcile_started_proposal(db, owner, existing)
                    proposal = existing
                else:
                    raise
            task_snapshot = task
            proposal_id = proposal.proposal_id
        # Commit the pending idempotency reservation before crossing the
        # governed inference boundary.  Retries therefore cannot issue a
        # second admission while the first request is unresolved.
        await db.commit()
    if route_error is not None:
        async with get_session() as db:
            proposal = await _get_proposal(db, owner, proposal_id)
            if proposal.status != "pending_inference":
                return _proposal_payload(proposal)
            return _blocked_payload(proposal, route_error)

    job_binding: tuple[str, str, int] | None = None
    try:
        async with get_session() as db:
            proposal = await _get_proposal(db, owner, proposal_id)
            if proposal.status != "pending_inference":
                return _proposal_payload(proposal)
            if proposal.provider_contact_started or proposal.provider_contact_state != "not_started":
                return await _reconcile_started_proposal(db, owner, proposal)
        # Redact and preflight policy before creating/claiming the durable
        # inference admission.  A known denial here is no-contact and can be
        # safely retried after the prerequisite is repaired.
        prepared_prompt = await _prepare_governed_proposal(
            task_snapshot,
            kind=kind,
            operator=operator,
            job_id=proposal.admission_job_id,
            route_id=route_id,
        )
        job_binding = await _admit_proposal_job(owner=owner, task=task_snapshot, proposal=proposal)
        if job_binding is None:
            async with get_session() as db:
                proposal = await _get_proposal(db, owner, proposal_id)
                if not proposal.provider_contact_started and proposal.provider_contact_state == "not_started":
                    # Another caller may have admitted the same deterministic
                    # job while this process was racing for the provider
                    # marker.  Preserve its pending receipt; do not turn an
                    # active operation into a false terminal block.
                    try:
                        active_job = await durable_job_repository.get_job(proposal.admission_job_id)
                    except Exception:
                        active_job = None
                    if _proposal_job_is_pending(proposal, active_job):
                        return _proposal_payload(proposal)
                return _blocked_payload(proposal, "proposal_admission_reconciliation_required")
        job_id, lease_owner, fence = job_binding
        marker_lost = False
        async with get_session() as db:
            current_task = await repository._owned_task(db, owner, task_snapshot.task_id)
            if (
                current_task.task_revision != proposal.parent_revision
                or current_task.goal_revision != proposal.goal_revision
            ):
                raise BoardError(
                    "goal_revision_stale",
                    "The task goal changed before provider contact",
                    status_code=409,
                )
            await repository.validate_task_goal(db, owner, current_task)
            current_route, current_version = _route_binding()
            if (
                current_route != proposal.route_id
                or current_version != proposal.capability_version
                or _authority_digest(owner, current_task, current_route, current_version)
                != proposal.authority_digest
            ):
                raise BoardError(
                    "proposal_binding_conflict",
                    "The governed route or goal authority changed before provider contact",
                    status_code=409,
                )
            marker_now = _now()
            claimed = await db.execute(
                update(WorkBoardProposal)
                .where(
                    WorkBoardProposal.proposal_id == proposal_id,
                    WorkBoardProposal.status == "pending_inference",
                    WorkBoardProposal.provider_contact_started.is_(False),
                    WorkBoardProposal.provider_contact_state == "not_started",
                    WorkBoardProposal.expires_at > marker_now,
                )
                .values(provider_contact_started=True, provider_contact_state="started")
            )
            if int(claimed.rowcount or 0) != 1:
                current = await _get_proposal(db, owner, proposal_id)
                if current.provider_contact_started or current.provider_contact_state != "not_started":
                    return await _reconcile_started_proposal(db, owner, current)
                # A list/get expiry or another pre-contact terminalization won
                # the CAS.  The already-claimed durable job must be settled
                # before this path returns; it must never reach the provider.
                marker_lost = True
                current_status = current.status
                if current_status == "pending_inference":
                    _blocked_payload(
                        current,
                        "proposal_expired_before_provider_contact"
                        if _aware(current.expires_at) <= marker_now
                        else "proposal_contact_claim_reconciliation_required",
                        contact_state="unknown",
                    )
                    await db.flush()
        if marker_lost:
            try:
                await _transition_proposal_job(
                    job_id,
                    lease_owner=lease_owner,
                    fence=fence,
                    status="cancelled",
                    reason="proposal_expired_before_provider_contact",
                    result_summary="proposal expired before provider contact",
                )
            except Exception:
                async with get_session() as db:
                    current = await _get_proposal(db, owner, proposal_id)
                    if current.status != "blocked":
                        _blocked_payload(
                            current,
                            "proposal_admission_reconciliation_required",
                            contact_state="unknown",
                        )
                    await db.flush()
                async with get_session() as db:
                    return _proposal_payload(await _get_proposal(db, owner, proposal_id))
            async with get_session() as db:
                return _proposal_payload(await _get_proposal(db, owner, proposal_id))
        raw = await _invoke_governed_proposal(
            messages=prepared_prompt[0],
            principal=prepared_prompt[1],
            context=prepared_prompt[2],
            job_id=job_id,
            lease_owner=lease_owner,
            fencing_token=fence,
        )
        model_payload = _parse_model_json(raw)
        tasks, links, estimated_cost = await _normalise_tasks(
            model_payload,
            kind=kind,
            parent=task_snapshot,
        )
        canonical = {
            "proposed_tasks": tasks,
            "proposed_links": links,
            "blocked_reason": None,
        }
        digest = _proposal_digest(canonical)
        async with get_session() as db:
            proposal = await _get_proposal(db, owner, proposal_id)
            if proposal.status != "pending_inference":
                return _proposal_payload(proposal)
            # Persist the structured output while the contact marker is still
            # explicit.  This creates a crash-safe handoff point: a restart
            # can reconcile the exact durable job and promote this digest
            # without contacting the provider again.
            proposal.proposal_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
            proposal.proposal_digest = digest
            proposal.estimated_cost = estimated_cost
            await db.flush()
        await _transition_proposal_job(
            job_id,
            lease_owner=lease_owner,
            fence=fence,
            status="succeeded",
            result_summary="structured work-board proposal produced",
        )
        async with get_session() as db:
            proposal = await _get_proposal(db, owner, proposal_id)
            if proposal.status == "pending_inference":
                proposal.status = "proposed"
                proposal.provider_contact_state = "succeeded"
                proposal.revision += 1
                await db.flush()
            return _proposal_payload(proposal)
    except BoardError as exc:
        reason = exc.code
    except DurableJobTransitionError as exc:
        reason = (
            _PROPOSAL_ATTEMPT_BUDGET_REASON
            if "attempt budget exhausted" in str(exc).lower()
            else "proposal_admission_unavailable"
        )
    except (DurableJobAdmissionDenied, DurableJobError):
        reason = "proposal_admission_unavailable"
    except Exception:
        reason = "proposal_provider_contact_unknown"
    if job_binding is not None:
        try:
            await _transition_proposal_job(
                job_binding[0],
                lease_owner=job_binding[1],
                fence=job_binding[2],
                status="failed",
                reason=reason,
                result_summary="proposal execution requires operator reconciliation",
            )
        except Exception:
            pass
    async with get_session() as db:
        proposal = await _get_proposal(db, owner, proposal_id)
        if proposal.status != "pending_inference":
            return _proposal_payload(proposal)
        return _blocked_payload(
            proposal,
            reason,
            contact_state="unknown" if proposal.provider_contact_started else "not_started",
        )


async def accept_proposal(
    owner: WorkBoardOwner,
    proposal_id: str,
    request: WorkBoardProposalAccept,
) -> dict[str, Any]:
    repository = WorkBoardRepository()
    # Recheck capability authority before taking SQLite's immediate writer
    # lock. The provider-free preflight reads the canonical goal and adapter
    # state through their existing repositories; doing those reads under the
    # proposal's write transaction can self-block on SQLite. The task remains
    # Todo after acceptance, so the dispatcher still owns the final, fresher
    # admission check before any execution claim.
    async with get_session() as preview_db:
        preview_proposal = await _get_proposal(preview_db, owner, proposal_id)
        if preview_proposal.revision != request.expected_proposal_revision:
            raise BoardError(
                "stale_proposal_revision",
                "The proposal changed before authority preflight",
                status_code=409,
            )
        if preview_proposal.status != "proposed":
            raise BoardError(
                "proposal_not_acceptible",
                "Only a proposed triage result can be accepted",
                status_code=409,
            )
        preview_parent = (
            await preview_db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == preview_proposal.parent_task_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if preview_parent is None:
            raise BoardError("task_not_found", "The proposal parent task no longer exists", status_code=404)
        if (
            preview_parent.task_revision != request.expected_parent_revision
            or preview_parent.task_revision != preview_proposal.parent_revision
        ):
            raise BoardRevisionConflict(
                preview_parent.task_id,
                request.expected_parent_revision,
                preview_parent.task_revision,
            )
        preview_parent_snapshot = preview_parent.model_copy(deep=True)
        # Check the live goal before reading capability versions or their
        # preflight. A stale goal has a stable recovery code even if a
        # capability was also replaced since this proposal was drafted.
        preview_goal = (
            await preview_db.execute(
                select(Goal).where(
                    Goal.id == preview_parent.goal_id,
                    Goal.owner_principal_id == owner.principal_id,
                    Goal.owner_session_id == owner.session_id,
                    Goal.revision == preview_parent.goal_revision,
                )
            )
        ).scalar_one_or_none()
        if (
            preview_goal is None
            or str(getattr(preview_goal.status, "value", preview_goal.status))
            not in {"active", "draft"}
        ):
            raise BoardError(
                "goal_revision_stale",
                "The proposal goal authority is no longer current",
                status_code=409,
            )
        preview_proposal_json = str(preview_proposal.proposal_json or "")
        authority_preview_revision = preview_proposal.revision
        authority_preview_digest = str(preview_proposal.proposal_digest or "")
    preview_payload = _decode_json(preview_proposal_json)
    preview_tasks = preview_payload.get("proposed_tasks")
    if not isinstance(preview_tasks, list) or any(not isinstance(item, Mapping) for item in preview_tasks):
        raise BoardError("invalid_proposal", "The proposal has no executable task preview", status_code=409)
    authority_previews = await _validate_proposal_authority_preview(
        preview_tasks,
        parent=preview_parent_snapshot,
    )

    async with get_session() as db:
        # Serialize the full validation and materialization window.  The
        # initial proposal lookup itself opens a transaction; locking after
        # validation would commit stale ORM objects and allow a concurrent
        # acceptance/expiry to win before child creation.
        await _begin_sqlite_immediate(db)
        proposal = await _get_proposal(
            db,
            owner,
            proposal_id,
            transaction_locked=True,
        )
        if (
            proposal.revision != authority_preview_revision
            or str(proposal.proposal_digest or "") != authority_preview_digest
        ):
            raise BoardError(
                "proposal_authority_preview_stale",
                "The proposal changed after capability preflight; refresh before acceptance",
                status_code=409,
            )
        if proposal.revision != request.expected_proposal_revision:
            # Expiry may have advanced the proposal revision. Persist that
            # terminal transition before returning the typed stale conflict.
            await db.commit()
            raise BoardError("stale_proposal_revision", "The proposal changed before acceptance", status_code=409)
        if proposal.status == "expired":
            await db.commit()
            raise BoardError("proposal_expired", "The proposal expired before operator acceptance", status_code=409)
        if proposal.status != "proposed":
            raise BoardError("proposal_not_acceptible", "Only a proposed triage result can be accepted", status_code=409)
        parent = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == proposal.parent_task_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            raise BoardError("task_not_found", "The proposal parent task no longer exists", status_code=404)
        if parent.task_revision != request.expected_parent_revision or parent.task_revision != proposal.parent_revision:
            raise BoardRevisionConflict(parent.task_id, request.expected_parent_revision, parent.task_revision)
        # Re-read the goal revision and owner before task creation.  The
        # repository performs the same check for every child task.
        if parent.goal_revision != proposal.goal_revision:
            raise BoardError("stale_goal_revision", "The proposal goal revision changed", status_code=409)
        # Repeat the owner/revision/status check under the immediate write
        # lock before comparing capability snapshots or creating any task.
        goal = (
            await db.execute(
                select(Goal).where(
                    Goal.id == parent.goal_id,
                    Goal.owner_principal_id == owner.principal_id,
                    Goal.owner_session_id == owner.session_id,
                    Goal.revision == parent.goal_revision,
                )
            )
        ).scalar_one_or_none()
        if goal is None or str(getattr(goal.status, "value", goal.status)) not in {"active", "draft"}:
            raise BoardError("goal_revision_stale", "The proposal goal authority is no longer current", status_code=409)
        if proposal.input_digest != _proposal_input_digest(task=parent, kind=proposal.kind):
            raise BoardError("proposal_binding_conflict", "The proposal input binding is stale", status_code=409)
        if (
            proposal.route_id != _PROPOSAL_ROUTE
            or proposal.capability_id != _PROPOSAL_CAPABILITY
            or not proposal.capability_version
            or proposal.grant_revision != parent.goal_revision
            or proposal.authority_digest != _authority_digest(
                owner,
                parent,
                proposal.route_id,
                proposal.capability_version,
            )
        ):
            raise BoardError("proposal_binding_conflict", "The proposal authority binding is stale", status_code=409)
        try:
            durable_job = await durable_job_repository.get_job(proposal.admission_job_id)
        except Exception as exc:
            raise BoardError(
                "proposal_job_reconciliation_required",
                "The exact proposal admission cannot be verified before acceptance",
                status_code=409,
            ) from exc
        if not _proposal_job_is_terminal_success(proposal, durable_job):
            raise BoardError(
                "proposal_job_reconciliation_required",
                "The exact proposal admission has not settled successfully",
                status_code=409,
            )
        payload = _decode_json(proposal.proposal_json)
        tasks = payload.get("proposed_tasks")
        links = payload.get("proposed_links")
        if not isinstance(tasks, list) or not isinstance(links, list):
            raise BoardError("invalid_proposal", "The proposal has no executable structured graph", status_code=409)
        canonical = {
            "proposed_tasks": tasks,
            "proposed_links": links,
            "blocked_reason": payload.get("blocked_reason"),
        }
        if proposal.proposal_digest != _proposal_digest(canonical):
            raise BoardError(
                "proposal_digest_mismatch",
                "The staged proposal content changed before acceptance",
                status_code=409,
            )
        staged_ids: set[str] = set()
        for item in tasks:
            if not isinstance(item, Mapping):
                raise BoardError("invalid_proposal", "The proposal task is malformed", status_code=409)
            child_id = str(item.get("task_id") or "")
            if not child_id or child_id in staged_ids:
                raise BoardError("invalid_proposal", "The proposal task IDs must be unique", status_code=409)
            staged_ids.add(child_id)
        edge_keys: set[tuple[str, str]] = set()
        for link in links:
            if not isinstance(link, Mapping):
                raise BoardError("invalid_proposal", "The proposal link is malformed", status_code=409)
            parent_id = str(link.get("parent_task_id") or "")
            child_id = str(link.get("child_task_id") or "")
            if (
                not parent_id
                or child_id not in staged_ids
                or (parent_id != proposal.parent_task_id and parent_id not in staged_ids)
                or (parent_id, child_id) in edge_keys
                or parent_id == child_id
            ):
                raise BoardError("invalid_proposal", "The proposal dependency graph is malformed", status_code=409)
            edge_keys.add((parent_id, child_id))
        _validate_acyclic(tasks, links, parent.task_id)
        # Do this before the first child INSERT.  A syntactically safe model
        # reference is not an executable input until the canonical workspace
        # file, digest, envelope, and registered capability schema all pass.
        _validate_proposed_typed_inputs(tasks, parent=parent)
        await _validate_proposal_authority_preview(
            tasks,
            parent=parent,
            expected_previews=authority_previews,
        )
        if proposal.kind == "specify":
            if links:
                raise BoardError(
                    "invalid_proposal",
                    "A Specify proposal cannot add a parent dependency",
                    status_code=409,
                )
            if len(tasks) != 1:
                raise BoardError(
                    "invalid_proposal",
                    "A Specify proposal must contain exactly one typed specification",
                    status_code=409,
                )
            item = tasks[0]
            from src.work_board.dispatcher import REGISTERED_CAPABILITIES

            capability_id = str(item.get("capability_id") or "")
            capability = REGISTERED_CAPABILITIES.get(capability_id)
            if capability is None:
                raise BoardError(
                    "capability_unregistered",
                    "The accepted Specify names no registered Seraph capability",
                    status_code=409,
                )
            if str(item.get("capability_version") or "") != capability.version:
                raise BoardError(
                    "capability_version_stale",
                    "The accepted Specify capability version changed",
                    status_code=409,
                )
            expected_executor = registered_executor_id(capability_id)
            supplied_executor = str(item.get("executor_id") or "").strip()
            if supplied_executor and supplied_executor != expected_executor:
                raise BoardError(
                    "executor_lane_mismatch",
                    "The accepted Specify executor does not match the registered capability lane",
                    status_code=409,
                )
            expected_parent_revision = int(parent.task_revision)
            previous_status = parent.status.value
            observed_at = _now()
            await repository._cas_task_update(
                db,
                owner,
                parent,
                expected_revision=expected_parent_revision,
                values={
                    "title": str(item.get("title") or ""),
                    "body": str(item.get("body") or ""),
                    "capability_id": capability_id,
                    "typed_input_ref": str(item.get("typed_input_ref") or ""),
                    "typed_input_digest": str(item.get("typed_input_digest") or ""),
                    "executor_id": expected_executor,
                    "status": WorkBoardStatus.todo,
                    "block_kind": None,
                    "block_reason": None,
                    "block_source_status": None,
                    "task_revision": expected_parent_revision + 1,
                    "updated_at": observed_at,
                },
            )
            event = await repository._event(
                db,
                parent,
                owner,
                kind="task.specified",
                metadata={
                    "proposal_id": proposal.proposal_id,
                    "from_status": previous_status,
                    "status": parent.status.value,
                    "task_revision": parent.task_revision,
                },
            )
            proposal.status = "accepted"
            proposal.revision += 1
            await db.flush()
            return {
                "proposal_id": proposal.proposal_id,
                "status": proposal.status,
                "proposal_revision": proposal.revision,
                "task_ids": [parent.task_id],
                "task_id": parent.task_id,
                "event_id": event.event_id,
            }
        # Keep the child creation and every dependency edge in this locked
        # transaction.  WorkBoardRepository.add_link normally starts its own
        # SQLite immediate section for an HTTP mutation; acceptance passes a
        # transaction-safe path so it cannot commit already-created child
        # rows before a later duplicate/cycle error is discovered.
        created: dict[str, WorkBoardTask] = {}
        for index, item in enumerate(tasks, start=1):
            if not isinstance(item, Mapping):
                raise BoardError("invalid_proposal", "The proposal task is malformed", status_code=409)
            from src.work_board.dispatcher import REGISTERED_CAPABILITIES

            capability_id = str(item.get("capability_id") or "")
            capability = REGISTERED_CAPABILITIES.get(capability_id)
            if capability is None:
                raise BoardError("capability_unregistered", "The accepted proposal names no registered capability", status_code=409)
            if str(item.get("capability_version") or capability.version) != capability.version:
                raise BoardError("capability_version_stale", "The accepted capability version changed", status_code=409)
            expected_executor = registered_executor_id(capability_id)
            supplied_executor = str(item.get("executor_id") or "").strip()
            if supplied_executor and supplied_executor != expected_executor:
                raise BoardError(
                    "executor_lane_mismatch",
                    "The accepted child executor does not match the registered capability lane",
                    status_code=409,
                )
            request_task = WorkBoardTaskCreate(
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
                goal_id=parent.goal_id,
                goal_revision=parent.goal_revision,
                status=WorkBoardStatus.todo,
                capability_id=capability_id,
                typed_input_ref=str(item.get("typed_input_ref") or ""),
                typed_input_digest=str(item.get("typed_input_digest") or ""),
                executor_id=expected_executor,
                priority=parent.priority,
                idempotency_scope=f"proposal:{proposal.proposal_id}",
                idempotency_key=f"child-{index}",
                requires_review=parent.requires_review,
                reviewer_id=parent.reviewer_id,
            )
            mutation = await repository.create_task(db, owner, request_task, origin_session_id=owner.session_id)
            created[str(item.get("task_id"))] = mutation.task
        for link in links:
            if not isinstance(link, Mapping):
                raise BoardError("invalid_proposal", "The proposal link is malformed", status_code=409)
            parent_id = str(link.get("parent_task_id") or "")
            child_id = str(link.get("child_task_id") or "")
            link_parent = parent.task_id if parent_id == proposal.parent_task_id else created.get(parent_id)
            child = created.get(child_id)
            if link_parent is None or child is None:
                raise BoardError("invalid_proposal", "The proposal link references no staged task", status_code=409)
            from src.work_board.contracts import WorkBoardLinkCreate

            await repository.add_link(
                db,
                owner,
                WorkBoardLinkCreate(
                    parent_task_id=link_parent.task_id if isinstance(link_parent, WorkBoardTask) else str(link_parent),
                    child_task_id=child.task_id,
                    expected_child_revision=child.task_revision,
                ),
                acquire_lock=False,
            )
        proposal.status = "accepted"
        proposal.revision += 1
        await db.flush()
        return {
            "proposal_id": proposal.proposal_id,
            "status": proposal.status,
            "proposal_revision": proposal.revision,
            "task_ids": [task.task_id for task in created.values()],
        }


async def reject_proposal(
    owner: WorkBoardOwner,
    proposal_id: str,
    request: WorkBoardProposalReject,
) -> dict[str, Any]:
    async with get_session() as db:
        await _begin_sqlite_immediate(db)
        proposal = await _get_proposal(
            db,
            owner,
            proposal_id,
            transaction_locked=True,
        )
        if proposal.revision != request.expected_proposal_revision:
            # Expiry may have advanced this row while the caller held an older
            # projection. Commit that terminal state before returning 409.
            await db.commit()
            raise BoardError("stale_proposal_revision", "The proposal changed before rejection", status_code=409)
        if proposal.status == "expired":
            await db.commit()
            raise BoardError("proposal_expired", "The proposal expired before operator rejection", status_code=409)
        # A pending row may already own a live durable job or have crossed the
        # provider-contact marker.  Rejecting it here would hide that job and
        # leave its cost/effect liability orphaned.  Only a fully persisted,
        # reviewable proposal can be rejected by this endpoint.
        if proposal.status != "proposed":
            raise BoardError(
                "proposal_not_rejectable",
                "Only a fully proposed review row can be rejected",
                status_code=409,
            )
        expected_revision = int(request.expected_proposal_revision)
        updated = await db.execute(
            update(WorkBoardProposal)
            .where(
                WorkBoardProposal.proposal_id == proposal_id,
                WorkBoardProposal.owner_principal_id == owner.principal_id,
                WorkBoardProposal.owner_session_id == owner.session_id,
                WorkBoardProposal.status == "proposed",
                WorkBoardProposal.revision == expected_revision,
            )
            .values(
                status="rejected",
                revision=expected_revision + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if int(updated.rowcount or 0) != 1:
            await db.refresh(proposal)
            await db.commit()
            raise BoardError("stale_proposal_revision", "The proposal changed before rejection", status_code=409)
        await db.refresh(proposal)
        return {
            "proposal_id": proposal.proposal_id,
            "status": proposal.status,
            "proposal_revision": proposal.revision,
        }


__all__ = [
    "accept_proposal",
    "create_proposal",
    "get_proposal",
    "list_proposals",
    "reject_proposal",
]
