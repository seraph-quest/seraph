"""Bounded durable invocation contract backed by ``WorkflowRunState``.

The workflow state table is the canonical execution record.  This module adds
the typed admission/lifecycle operations needed by capabilities without
introducing another queue or state machine.  It deliberately records hashes
and structural metadata for inputs, checkpoints, and results; callers must
store sensitive values in their existing governed stores.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.artifacts.registry import build_artifact_record
from src.db.models import WorkflowRunState
from src.db.session_refs import ensure_sessions_exist


DURABLE_JOB_RECORD_SCHEMA_VERSION = 2

# Higher priority values are selected first by a future broker.  The contract
# itself only persists the value and never starts a second scheduler.
DURABLE_JOB_STATUSES = (
    "accepted",
    "queued",
    "running",
    "awaiting_approval",
    "paused",
    "blocked",
    "failed",
    "succeeded",
    "cancelled",
)

DURABLE_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset({"queued", "blocked", "failed", "cancelled"}),
    "queued": frozenset({"running", "blocked", "failed", "cancelled"}),
    "running": frozenset({
        "awaiting_approval",
        "paused",
        "blocked",
        "failed",
        "succeeded",
        "cancelled",
    }),
    "awaiting_approval": frozenset({"queued", "blocked", "failed", "cancelled"}),
    "paused": frozenset({"queued", "blocked", "failed", "cancelled"}),
    "blocked": frozenset({"queued", "failed", "cancelled"}),
    "failed": frozenset({"queued"}),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
}
DURABLE_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "cancelled"})

# Remote admission remains process-local, but an existing durable job may keep
# an operator-safe projection of its admission lifecycle in the canonical
# effect ledger.  These values describe the broker receipt; they are not a
# second durable job state machine.
REMOTE_INFERENCE_RECEIPT_STATUSES = frozenset(
    {"queued", "running", "blocked", "succeeded", "settled", "failed", "cancelled", "expired", "rejected"}
)
REMOTE_INFERENCE_EFFECT_STATUSES = {
    "queued": "unknown",
    "running": "unknown",
    "blocked": "blocked",
    "succeeded": "succeeded",
    "settled": "succeeded",
    "failed": "failed",
    "cancelled": "failed",
    "expired": "failed",
    "rejected": "failed",
}
REMOTE_INFERENCE_RECEIPT_FIELDS = (
    "schema_version",
    "operation_id",
    "job_id",
    "owner_id",
    "parent_job_id",
    "runtime_path",
    "priority",
    "priority_rank",
    "status",
    "queue_position",
    "active_operation_id",
    "fencing_token",
    "reason_code",
    "queued",
    "max_queued",
    "serial_gpu",
    "resource_class",
    "serial_remote_inference",
    "cancel_requested",
    "reconciliation_required",
    "recovery_action",
    "deadline_exceeded",
    "callback_completed",
    "capability_version",
    "owner_outstanding",
    "max_owner_outstanding",
    "cost_reserved_microusd",
    "owner_cost_budget_microusd",
    "unknown_cost_outstanding",
    "cost_settled_microusd",
    "operator_visible",
)


class DurableJobError(RuntimeError):
    """Base error for rejected durable job operations."""


class DurableJobNotFound(DurableJobError):
    pass


class DurableJobTransitionError(DurableJobError, ValueError):
    pass


class DurableJobIdempotencyConflict(DurableJobError, ValueError):
    pass


class DurableJobLeaseError(DurableJobError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("deadline_at must be an ISO timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _string_list(value: Iterable[Any] | None) -> list[str]:
    if value is None:
        return []
    return sorted({str(item).strip() for item in value if str(item or "").strip()})


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _safe_structure(value: Any, *, max_depth: int = 3) -> Any:
    """Keep durable receipts structural and never persist secret values."""
    if max_depth <= 0:
        return "[redacted]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(marker in lowered for marker in ("secret", "token", "password", "credential", "api_key", "private_key")):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = _safe_structure(item, max_depth=max_depth - 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_structure(item, max_depth=max_depth - 1) for item in list(value)[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_inputs_digest(inputs: Any) -> tuple[str, dict[str, Any]]:
    keys = sorted(str(key) for key in inputs.keys()) if isinstance(inputs, dict) else []
    return _digest(inputs), {"redacted": True, "keys": keys, "shape": type(inputs).__name__}


def _binding(
    *,
    owner_principal_id: str,
    goal_id: str | None,
    goal_revision: int | None,
    idempotency_scope: str,
    dedupe_key: str,
) -> str:
    return _digest({
        "owner_principal_id": owner_principal_id,
        "goal_id": goal_id or "",
        "goal_revision": goal_revision,
        "idempotency_scope": idempotency_scope,
        "candidate_dedupe_key": dedupe_key,
    })


def _validate_owner_fields(
    *, owner_kind: str, owner_principal_id: str, service_id: str | None
) -> None:
    """Validate the small owner identity shape this repository persists."""
    if owner_kind not in {"user", "service"}:
        raise ValueError("owner_kind must be user or service")
    if not _text(owner_principal_id):
        raise ValueError("owner_principal_id is required")
    if owner_kind == "service" and not _text(service_id):
        raise ValueError("service jobs require service_id")
    if owner_kind == "user" and _text(service_id):
        raise ValueError("user jobs cannot set service_id")


def _validate_admission_authority(spec: "DurableJobSpec") -> None:
    identity = spec.identity
    _validate_owner_fields(
        owner_kind=identity.owner_kind,
        owner_principal_id=identity.owner_principal_id,
        service_id=spec.service_id,
    )
    authority = spec.declared_authority
    authority_principal = authority.get("principal")
    if _text(authority_principal) != identity.owner_principal_id:
        raise ValueError("declared authority principal must match owner_principal_id")
    authority_kind = authority.get("owner_kind", authority.get("kind"))
    if authority_kind is not None and _text(authority_kind) != identity.owner_kind:
        raise ValueError("declared authority owner kind must match owner_kind")
    authority_service_id = authority.get("service_id")
    if authority_service_id is not None and _text(authority_service_id) != _text(spec.service_id):
        raise ValueError("declared authority service_id must match service_id")
    if identity.owner_kind == "service" and _text(authority_service_id) != _text(spec.service_id):
        raise ValueError("service authority must declare the matching service_id")
    if identity.owner_kind == "user" and _text(authority_service_id):
        raise ValueError("user authority cannot declare service_id")


def _deadline_identity(value: datetime | str | None) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed else None


def _normalized_json_list(raw: str | None) -> str:
    parsed = _json_load(raw, None)
    if not isinstance(parsed, list):
        return "<invalid>"
    return _canonical(_string_list(parsed))


def _admission_conflicts(
    existing: WorkflowRunState,
    *,
    spec: "DurableJobSpec",
    input_digest: str,
    authority_digest: str,
    deadline: datetime | None,
) -> list[str]:
    """Return immutable admission fields that differ without exposing values."""
    identity = spec.identity
    expected = {
        "job_id": identity.job_id,
        "input_digest": input_digest,
        "job_kind": identity.job_kind,
        "capability_version": identity.capability_version,
        "owner_kind": identity.owner_kind,
        "owner_principal_id": identity.owner_principal_id,
        "service_id": spec.service_id,
        "authority_digest": authority_digest,
        "dependencies": _canonical(_string_list(spec.dependencies)),
        "resource_claims": _canonical(_string_list(spec.resource_claims)),
        "deadline_at": _deadline_identity(deadline),
        "priority": int(spec.priority),
        "max_attempts": int(spec.max_attempts),
        "session_id": spec.session_id,
        "parent_job_id": spec.parent_job_id,
        "goal_id": spec.goal_id,
        "goal_revision": spec.goal_revision,
        "plan_revision": spec.plan_revision,
        "candidate_id": spec.candidate_id,
    }
    actual = {
        "job_id": getattr(existing, "run_identity", None),
        "input_digest": getattr(existing, "input_digest", None),
        "job_kind": getattr(existing, "job_kind", None),
        "capability_version": getattr(existing, "capability_version", None),
        "owner_kind": getattr(existing, "owner_kind", None),
        "owner_principal_id": getattr(existing, "owner_principal_id", None),
        "service_id": getattr(existing, "service_id", None),
        "authority_digest": getattr(existing, "authority_digest", None),
        "dependencies": _normalized_json_list(getattr(existing, "dependencies_json", None)),
        "resource_claims": _normalized_json_list(getattr(existing, "resource_claims_json", None)),
        "deadline_at": _deadline_identity(getattr(existing, "deadline_at", None)),
        "priority": int(getattr(existing, "priority", 0) or 0),
        "max_attempts": int(getattr(existing, "max_attempts", 0) or 0),
        "session_id": getattr(existing, "session_id", None),
        "parent_job_id": getattr(existing, "parent_job_id", None),
        "goal_id": getattr(existing, "goal_id", None),
        "goal_revision": getattr(existing, "goal_revision", None),
        "plan_revision": getattr(existing, "plan_revision", None),
        "candidate_id": getattr(existing, "candidate_id", None),
    }
    # Scheduled child work is deliberately deduped across strategist parent
    # occurrences.  The first admitted child retains its original lineage;
    # a later parent may replay that same child only after its own fence has
    # been checked.  Other callers keep parent identity immutable.
    if identity.idempotency_scope == "goal-snapshot-to-file-scheduler":
        for field_name in ("parent_job_id", "deadline_at"):
            expected.pop(field_name, None)
            actual.pop(field_name, None)
    return [field_name for field_name, value in expected.items() if actual[field_name] != value]


def _canonical_reconciliation_receipt(value: Any) -> tuple[str, str]:
    """Return a redacted canonical receipt and digest, rejecting empty input."""
    if isinstance(value, Mapping):
        if not value:
            raise ValueError("reconciliation_receipt must be nonempty")
        safe = _safe_structure(dict(value))
    elif isinstance(value, str) and value.strip():
        # Opaque external receipt identifiers are represented by a digest so a
        # secret-bearing token cannot be copied into the durable row.
        safe = {"opaque_receipt_digest": _digest(value.strip())}
    else:
        raise ValueError("reconciliation_receipt must be a nonempty mapping or string")
    canonical = _canonical(safe)
    if canonical in {"{}", "null", "\"\""}:
        raise ValueError("reconciliation_receipt must be nonempty")
    return canonical, _digest(safe)


def _canonical_remote_inference_receipt(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Allowlist and redact a typed remote admission receipt before storage."""
    if not isinstance(value, Mapping):
        raise ValueError("remote inference receipt must be a mapping")
    raw = dict(value)
    status = _text(raw.get("status"))
    if status not in REMOTE_INFERENCE_RECEIPT_STATUSES:
        raise ValueError(f"unsupported remote inference receipt status: {status or '<empty>'}")
    required = ("operation_id", "job_id", "owner_id")
    missing = [field_name for field_name in required if not _text(raw.get(field_name))]
    if missing:
        raise ValueError("remote inference receipt requires " + ", ".join(missing))
    safe = _safe_structure(
        {field_name: raw[field_name] for field_name in REMOTE_INFERENCE_RECEIPT_FIELDS if field_name in raw}
    )
    # The status is validated above and normalized explicitly so a custom
    # mapping cannot smuggle a non-string status into the durable projection.
    safe["status"] = status
    return safe, _digest(safe)


def _validate_retry_actor(
    run: WorkflowRunState,
    *,
    owner_kind: str,
    owner_principal_id: str,
    service_id: str | None,
) -> None:
    """Require the retry actor to be the persisted job owner identity."""
    _validate_owner_fields(
        owner_kind=owner_kind,
        owner_principal_id=owner_principal_id,
        service_id=service_id,
    )
    if (
        getattr(run, "owner_kind", None) != owner_kind
        or getattr(run, "owner_principal_id", None) != owner_principal_id
        or getattr(run, "service_id", None) != service_id
    ):
        raise DurableJobLeaseError("retry actor is not the authenticated job owner")


def _serialize(run: WorkflowRunState, *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an operator-safe durable job projection."""
    payload = {
        "job_id": run.run_identity,
        "run_identity": run.run_identity,
        "record_schema_version": int(getattr(run, "record_schema_version", DURABLE_JOB_RECORD_SCHEMA_VERSION) or 0),
        "parent_job_id": getattr(run, "parent_job_id", None),
        "owner": {
            "kind": getattr(run, "owner_kind", "legacy"),
            "principal_id": getattr(run, "owner_principal_id", None),
            "service_id": getattr(run, "service_id", None),
        },
        "job_kind": getattr(run, "job_kind", "workflow"),
        "capability_version": getattr(run, "capability_version", "workflow-v1"),
        "workflow_name": run.workflow_name,
        "tool_name": run.tool_name,
        "session_id": run.session_id,
        "goal_id": getattr(run, "goal_id", None),
        "goal_revision": getattr(run, "goal_revision", None),
        "plan_revision": getattr(run, "plan_revision", None),
        "candidate_id": getattr(run, "candidate_id", None),
        "status": run.status,
        "priority": int(getattr(run, "priority", 50) or 0),
        "dependencies": _json_load(getattr(run, "dependencies_json", None), []),
        "resource_claims": _json_load(getattr(run, "resource_claims_json", None), []),
        "input_digest": getattr(run, "input_digest", None),
        "authority_digest": getattr(run, "authority_digest", None),
        "idempotency": {
            "scope": getattr(run, "idempotency_scope", None),
            "key": getattr(run, "idempotency_key", None),
            "binding": getattr(run, "idempotency_binding", None),
        },
        "deadline_at": run.deadline_at.isoformat() if getattr(run, "deadline_at", None) else None,
        "lease": {
            "owner": getattr(run, "lease_owner", None),
            "expires_at": run.lease_expires_at.isoformat() if getattr(run, "lease_expires_at", None) else None,
            "fencing_token": int(getattr(run, "fencing_token", 0) or 0),
        },
        "attempt_count": int(getattr(run, "attempt_count", 0) or 0),
        "max_attempts": int(getattr(run, "max_attempts", 1) or 1),
        "failure_reason": getattr(run, "failure_reason", None),
        "result": {
            "digest": getattr(run, "result_digest", None),
            "summary": getattr(run, "result_summary", None),
        },
        "checkpoints": _json_load(getattr(run, "checkpoint_receipts_json", None), []),
        "artifacts": _json_load(getattr(run, "artifact_receipts_json", None), []),
        "effects": _json_load(getattr(run, "effect_receipts_json", None), []),
        "started_at": run.started_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "claim_boundary": "durable_job_contract_on_workflow_state_not_exactly_once_external_execution",
    }
    if receipt is not None:
        payload["receipt"] = receipt
    return payload


@dataclass(frozen=True, slots=True)
class DurableJobIdentity:
    """Stable identity fields used for idempotent admission."""

    job_id: str
    owner_kind: str
    owner_principal_id: str
    job_kind: str
    capability_version: str
    idempotency_scope: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "job_id",
            "owner_kind",
            "owner_principal_id",
            "job_kind",
            "capability_version",
            "idempotency_scope",
            "idempotency_key",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        if self.owner_kind not in {"user", "service"}:
            raise ValueError("owner_kind must be user or service")


@dataclass(frozen=True, slots=True)
class DurableJobSpec:
    identity: DurableJobIdentity
    inputs: Any = field(default_factory=dict)
    session_id: str | None = None
    parent_job_id: str | None = None
    parent_fencing_token: int | None = None
    goal_id: str | None = None
    goal_revision: int | None = None
    plan_revision: int | None = None
    candidate_id: str | None = None
    priority: int = 50
    dependencies: tuple[str, ...] = ()
    resource_claims: tuple[str, ...] = ()
    declared_authority: dict[str, Any] = field(default_factory=dict)
    deadline_at: datetime | str | None = None
    max_attempts: int = 1
    service_id: str | None = None


class DurableJobRepository:
    """Persistence operations for the one canonical workflow job record."""

    async def admit_job(self, spec: DurableJobSpec) -> dict[str, Any]:
        identity = spec.identity
        if not spec.declared_authority:
            raise ValueError("declared_authority is required before admission")
        _validate_admission_authority(spec)
        if not 0 <= int(spec.priority) <= 100:
            raise ValueError("priority must be between 0 and 100")
        if int(spec.max_attempts) < 1:
            raise ValueError("max_attempts must be at least 1")
        deadline = _as_utc(spec.deadline_at)
        now = _utc_now()
        input_digest, safe_inputs = _safe_inputs_digest(spec.inputs)
        binding = _binding(
            owner_principal_id=identity.owner_principal_id,
            goal_id=spec.goal_id,
            goal_revision=spec.goal_revision,
            idempotency_scope=identity.idempotency_scope,
            dedupe_key=identity.idempotency_key,
        )
        authority_digest = _digest(spec.declared_authority)
        async with self._session() as db:
            if spec.parent_job_id is not None:
                if spec.parent_fencing_token is None:
                    raise DurableJobLeaseError("parent fencing token is required for child admission")
                if spec.parent_job_id == identity.job_id:
                    raise DurableJobTransitionError("a durable job cannot parent itself")
                parent = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.run_identity == spec.parent_job_id
                        )
                    )
                ).scalars().first()
                if parent is None:
                    raise DurableJobNotFound(spec.parent_job_id)
                if parent.status != "running":
                    raise DurableJobTransitionError(
                        f"parent job is not running (current={parent.status})"
                    )
                if (
                    parent.lease_owner is None
                    or parent.lease_expires_at is None
                    or _as_utc(parent.lease_expires_at) <= now
                    or int(parent.fencing_token or 0) != int(spec.parent_fencing_token)
                ):
                    raise DurableJobLeaseError("parent job fence is stale or expired")
            await ensure_sessions_exist(db, [spec.session_id])
            existing = (
                await db.execute(
                    select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == binding)
                )
            ).scalars().first()
            if existing is not None:
                conflicts = _admission_conflicts(
                    existing,
                    spec=spec,
                    input_digest=input_digest,
                    authority_digest=authority_digest,
                    deadline=deadline,
                )
                if conflicts:
                    raise DurableJobIdempotencyConflict(
                        "idempotency binding conflicts on immutable fields: "
                        + ", ".join(conflicts)
                    )
                receipt = {
                    "kind": "job_admission",
                    "status": "deduped",
                    "job_id": existing.run_identity,
                    "idempotency_binding": binding,
                    "terminal_noop": existing.status in DURABLE_JOB_TERMINAL_STATUSES,
                    "operator_visible": True,
                }
                db.expunge(existing)
                return _serialize(existing, receipt=receipt)

            by_id = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity.job_id))
            ).scalars().first()
            if by_id is not None:
                raise DurableJobIdempotencyConflict("job_id already belongs to a different invocation")
            status = "failed" if deadline and deadline <= now else "accepted"
            failure_reason = "deadline_expired" if status == "failed" else None
            run = WorkflowRunState(
                run_identity=identity.job_id,
                root_run_identity=identity.job_id,
                parent_run_identity=spec.parent_job_id,
                parent_job_id=spec.parent_job_id,
                workflow_name=identity.job_kind,
                tool_name=identity.job_kind,
                session_id=spec.session_id,
                status=status,
                run_fingerprint=input_digest,
                arguments_json=_canonical(safe_inputs),
                approval_context_json=_canonical(_safe_structure(spec.declared_authority)),
                record_schema_version=DURABLE_JOB_RECORD_SCHEMA_VERSION,
                job_kind=identity.job_kind,
                owner_kind=identity.owner_kind,
                owner_principal_id=identity.owner_principal_id,
                service_id=spec.service_id,
                goal_id=spec.goal_id,
                goal_revision=spec.goal_revision,
                plan_revision=spec.plan_revision,
                candidate_id=spec.candidate_id,
                capability_version=identity.capability_version,
                input_digest=input_digest,
                authority_digest=authority_digest,
                idempotency_scope=identity.idempotency_scope,
                idempotency_key=identity.idempotency_key,
                idempotency_binding=binding,
                priority=int(spec.priority),
                dependencies_json=_canonical(_string_list(spec.dependencies)),
                resource_claims_json=_canonical(_string_list(spec.resource_claims)),
                declared_authority_json=_canonical(_safe_structure(spec.declared_authority)),
                deadline_at=deadline,
                max_attempts=int(spec.max_attempts),
                failure_reason=failure_reason,
                checkpoint_receipts_json="[]",
                artifact_receipts_json="[]",
                effect_receipts_json="[]",
            )
            db.add(run)
            try:
                await db.flush()
            except IntegrityError as exc:
                raise DurableJobIdempotencyConflict("concurrent admission claimed the idempotency binding") from exc
            db.expunge(run)
            receipt = {
                "kind": "job_admission",
                "status": status,
                "job_id": identity.job_id,
                "idempotency_binding": binding,
                "operator_visible": True,
            }
            return _serialize(run, receipt=receipt)

    async def create_job(self, spec: DurableJobSpec) -> dict[str, Any]:
        """Compatibility alias emphasizing that ScheduledJob is only a trigger."""
        return await self.admit_job(spec)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self._session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            ).scalars().first()
            if run is None:
                return None
            db.expunge(run)
            return _serialize(run)

    async def transition_job(
        self,
        job_id: str,
        to_status: str,
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
        reason: str | None = None,
        result: Any = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        if to_status not in DURABLE_JOB_STATUSES:
            raise DurableJobTransitionError(f"unknown durable job status: {to_status}")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            current = str(run.status)
            if current in DURABLE_JOB_TERMINAL_STATUSES:
                if current == to_status:
                    db.expunge(run)
                    return _serialize(run, receipt={"kind": "transition", "status": "deduped", "terminal_noop": True})
                raise DurableJobTransitionError(f"terminal job cannot transition {current} -> {to_status}")
            if current == "running" and (owner is None or fencing_token is None):
                raise DurableJobLeaseError(
                    "active jobs require owner and fencing token for every transition"
                )
            if to_status not in DURABLE_JOB_TRANSITIONS.get(current, frozenset()):
                raise DurableJobTransitionError(f"illegal durable job transition {current} -> {to_status}")
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            now = _utc_now()
            values: dict[str, Any] = {
                "status": to_status,
                "updated_at": now,
                "heartbeat_at": now,
                "failure_reason": reason if to_status in {"blocked", "failed"} else None,
                "finished_at": now if to_status in DURABLE_JOB_TERMINAL_STATUSES or to_status == "failed" else None,
            }
            if to_status in {"blocked", "failed", "succeeded", "cancelled"}:
                values["lease_owner"] = None
                values["lease_expires_at"] = None
            if result is not None:
                values["result_digest"] = _digest(result)
                values["result_summary"] = _text(result_summary, "result recorded")
            elif result_summary is not None:
                values["result_summary"] = _text(result_summary)
            conditions = [WorkflowRunState.run_identity == job_id, WorkflowRunState.status == current]
            if owner is not None:
                conditions.append(WorkflowRunState.lease_owner == owner)
            if fencing_token is not None:
                conditions.append(WorkflowRunState.fencing_token == fencing_token)
            result_update = await db.execute(update(WorkflowRunState).where(*conditions).values(**values))
            if result_update.rowcount != 1:
                raise DurableJobLeaseError("durable job changed or lease fencing token is stale")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "transition",
                "status": "recorded",
                "from": current,
                "to": to_status,
                "reason": _text(reason) or None,
                "fencing_token": refreshed.fencing_token,
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    async def queue_job(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self.transition_job(job_id, "queued", **kwargs)

    async def cancel_job(self, job_id: str, *, owner: str | None = None, fencing_token: int | None = None, reason: str = "operator_cancelled") -> dict[str, Any]:
        return await self.transition_job(job_id, "cancelled", owner=owner, fencing_token=fencing_token, reason=reason)

    async def fail_unclaimed_job(
        self,
        job_id: str,
        *,
        owner_principal_id: str,
        service_id: str,
        reason: str,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        """Fail an admitted job before lease acquisition with an atomic owner fence.

        Admission and queue failures happen before a runner lease exists. This
        narrow path is still conditional on the authenticated service owner and
        accepted/queued status, so a competing claim or authority change wins
        the race instead of being overwritten by an ownerless transition.
        """
        _validate_owner_fields(
            owner_kind="service",
            owner_principal_id=owner_principal_id,
            service_id=service_id,
        )
        async with self._session() as db:
            now = _utc_now()
            updated = await db.execute(
                update(WorkflowRunState)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status.in_(("accepted", "queued")),
                    WorkflowRunState.owner_kind == "service",
                    WorkflowRunState.owner_principal_id == owner_principal_id,
                    WorkflowRunState.service_id == service_id,
                )
                .values(
                    status="failed",
                    failure_reason=_text(reason, "unclaimed_job_failed"),
                    result_summary=result_summary,
                    updated_at=now,
                    heartbeat_at=now,
                    finished_at=now,
                )
            )
            if updated.rowcount != 1:
                current = await self._fetch(db, job_id)
                db.expunge(current)
                return _serialize(
                    current,
                    receipt={
                        "kind": "unclaimed_failure",
                        "status": "not_recorded",
                        "reason": "job_changed_before_owner_fence",
                        "operator_visible": True,
                    },
                )
            failed = await self._fetch(db, job_id)
            receipt = {
                "kind": "unclaimed_failure",
                "status": "recorded",
                "reason": _text(reason, "unclaimed_job_failed"),
                "owner_principal_id": owner_principal_id,
                "service_id": service_id,
                "operator_visible": True,
            }
            db.expunge(failed)
            return _serialize(failed, receipt=receipt)

    async def claim_job(self, job_id: str, *, owner: str, lease_seconds: int = 300) -> dict[str, Any]:
        owner = _text(owner)
        if not owner:
            raise DurableJobLeaseError("owner is required to claim a job")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        expires = now + timedelta(seconds=int(lease_seconds))
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                db.expunge(run)
                return _serialize(run, receipt={"kind": "claim", "status": "terminal_noop"})
            if run.status != "queued":
                raise DurableJobTransitionError(f"only queued jobs may be claimed (current={run.status})")
            persisted_deadline = _as_utc(run.deadline_at)
            if persisted_deadline and persisted_deadline <= now:
                await db.execute(
                    update(WorkflowRunState)
                    .where(WorkflowRunState.run_identity == job_id, WorkflowRunState.status == "queued")
                    .values(status="failed", failure_reason="deadline_expired", updated_at=now, heartbeat_at=now, finished_at=now)
                )
                failed = await self._fetch(db, job_id)
                db.expunge(failed)
                return _serialize(failed, receipt={"kind": "claim", "status": "failed", "reason": "deadline_expired"})
            if run.attempt_count >= run.max_attempts:
                raise DurableJobTransitionError("attempt budget exhausted")
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == "queued",
                or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= now),
            ]
            result_update = await db.execute(
                update(WorkflowRunState)
                .where(*conditions)
                .values(
                    status="running",
                    lease_owner=owner,
                    lease_expires_at=expires,
                    fencing_token=WorkflowRunState.fencing_token + 1,
                    attempt_count=WorkflowRunState.attempt_count + 1,
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            if result_update.rowcount != 1:
                raise DurableJobLeaseError("job is currently owned by another active runner")
            claimed = await self._fetch(db, job_id)
            receipt = {
                "kind": "claim",
                "status": "claimed",
                "owner": owner,
                "fencing_token": claimed.fencing_token,
                "lease_expires_at": expires.isoformat(),
                "attempt": claimed.attempt_count,
                "operator_visible": True,
            }
            db.expunge(claimed)
            return _serialize(claimed, receipt=receipt)

    async def record_checkpoint(
        self,
        job_id: str,
        *,
        checkpoint_id: str,
        state: Any,
        owner: str,
        fencing_token: int,
        safe: bool = True,
    ) -> dict[str, Any]:
        if not _text(checkpoint_id):
            raise ValueError("checkpoint_id is required")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if owner is None or fencing_token is None:
                raise DurableJobLeaseError("owner and fencing token are required for checkpoint writes")
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            if run.status not in {"running", "paused", "awaiting_approval"}:
                raise DurableJobTransitionError(f"checkpoint not allowed from {run.status}")
            state_digest = _digest(state)
            receipt = {
                "checkpoint_id": checkpoint_id,
                "state_digest": state_digest,
                "state_keys": sorted(str(key) for key in state.keys()) if isinstance(state, dict) else [],
                "safe": bool(safe),
                "recorded_at": _utc_now().isoformat(),
                "fencing_token": fencing_token,
            }
            existing = _json_load(run.checkpoint_receipts_json, [])
            existing = [item for item in existing if isinstance(item, dict) and item.get("checkpoint_id") != checkpoint_id]
            existing.append(receipt)
            now = _utc_now()
            result_update = await db.execute(
                update(WorkflowRunState)
                .where(WorkflowRunState.run_identity == job_id, WorkflowRunState.status == run.status, WorkflowRunState.fencing_token == fencing_token, WorkflowRunState.lease_owner == owner)
                .values(checkpoint_receipts_json=_canonical(existing[-50:]), updated_at=now, heartbeat_at=now)
            )
            if result_update.rowcount != 1:
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "checkpoint", "status": "recorded", **receipt})

    async def record_artifact(
        self,
        job_id: str,
        *,
        file_path: str,
        artifact_type: str = "workspace_file",
        content: str | bytes | None = None,
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Persist an operator-safe artifact receipt.

        An ownerless, content-free receipt is allowed only in ``accepted``
        before execution is claimed. A leased/running job must supply the
        current lease owner and fencing token so a stale runner cannot append
        an artifact after restart recovery.
        """
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                raise DurableJobTransitionError(f"terminal job cannot record artifacts ({run.status})")
            lease_present = bool(run.lease_owner or run.lease_expires_at)
            if lease_present or run.status == "running":
                if owner is None or fencing_token is None:
                    raise DurableJobLeaseError("owner and fencing token are required for leased artifact writes")
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            elif owner is None and fencing_token is None:
                # The only ownerless path is a pre-execution receipt with no
                # content mutation.  Queued/failed/blocked rows need an
                # explicit owner even when they currently have no lease.
                if run.status != "accepted" or content is not None:
                    raise DurableJobLeaseError(
                        "owner and fencing token are required to alter artifact evidence"
                    )
            else:
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            record = build_artifact_record(
                file_path=file_path,
                artifact_type=artifact_type,
                producer=run.job_kind,
                run_id=job_id,
                session_id=run.session_id,
                content=content,
            )
            receipt = {
                "artifact_id": record["artifact_id"],
                "artifact_type": record["artifact_type"],
                "file_path": record["file_path"],
                "producer": record["producer"],
                "content_sha256": record["content_sha256"],
                "size_bytes": record["size_bytes"],
                "exists": record["exists"],
                "recorded_at": _utc_now().isoformat(),
            }
            existing = _json_load(run.artifact_receipts_json, [])
            existing = [item for item in existing if isinstance(item, dict) and item.get("artifact_id") != receipt["artifact_id"]]
            existing.append(receipt)
            now = _utc_now()
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
            ]
            if owner is not None:
                conditions.extend((WorkflowRunState.lease_owner == owner, WorkflowRunState.fencing_token == fencing_token))
            else:
                conditions.extend((WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at.is_(None)))
            result_update = await db.execute(update(WorkflowRunState).where(*conditions).values(artifact_receipts_json=_canonical(existing[-100:]), updated_at=now, heartbeat_at=now))
            if result_update.rowcount != 1:
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "artifact", "status": "recorded", **receipt})

    async def record_effect(
        self,
        job_id: str,
        *,
        effect_type: str,
        target_path: str | None = None,
        status: str = "succeeded",
        content_sha256: str | None = None,
        details: dict[str, Any] | None = None,
        receipt_kind: str = "effect",
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Persist a bounded effect or readback receipt on the job record.

        The effect ledger is intentionally part of the canonical durable job
        row.  A running job must use its current lease and fencing token, so a
        stale runner cannot report an external write or verification after
        restart recovery.  ``details`` is structural and redacted before it
        is persisted; callers should pass digests rather than content.
        """
        effect_type = _text(effect_type)
        receipt_kind = _text(receipt_kind, "effect")
        status = _text(status, "unknown")
        if not effect_type:
            raise ValueError("effect_type is required")
        if receipt_kind not in {"effect", "readback"}:
            raise ValueError("receipt_kind must be effect or readback")
        if status not in {"succeeded", "failed", "unknown", "blocked"}:
            raise ValueError("effect status is not supported")
        if content_sha256 is not None and not _text(content_sha256):
            content_sha256 = None
        safe_details = _safe_structure(details or {})
        effect_id = "eff_" + _digest({
            "job_id": job_id,
            "receipt_kind": receipt_kind,
            "effect_type": effect_type,
            "target_path": target_path or "",
            "status": status,
            "content_sha256": content_sha256 or "",
            "details": safe_details,
        })[:24]
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                raise DurableJobTransitionError(f"terminal job cannot record effects ({run.status})")
            lease_present = bool(run.lease_owner or run.lease_expires_at)
            if run.status == "running" and (owner is None or fencing_token is None):
                raise DurableJobLeaseError("active jobs require owner and fencing token for effect writes")
            if lease_present or run.status == "running":
                if owner is None or fencing_token is None:
                    raise DurableJobLeaseError("owner and fencing token are required for leased effect writes")
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            elif owner is None and fencing_token is None:
                if run.status != "accepted":
                    raise DurableJobLeaseError("owner and fencing token are required to alter effect evidence")
            else:
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            receipt = {
                "effect_id": effect_id,
                "receipt_kind": receipt_kind,
                "effect_type": effect_type,
                "target_path": _text(target_path) or None,
                "status": status,
                "content_sha256": content_sha256,
                "details": safe_details,
                "recorded_at": _utc_now().isoformat(),
                "fencing_token": fencing_token,
            }
            existing = _json_load(run.effect_receipts_json, [])
            existing = [
                item
                for item in existing
                if isinstance(item, dict) and item.get("effect_id") != effect_id
            ]
            existing.append(receipt)
            now = _utc_now()
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
            ]
            if owner is not None:
                conditions.extend((WorkflowRunState.lease_owner == owner, WorkflowRunState.fencing_token == fencing_token))
            else:
                conditions.extend((WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at.is_(None)))
            result_update = await db.execute(
                update(WorkflowRunState)
                .where(*conditions)
                .values(effect_receipts_json=_canonical(existing[-100:]), updated_at=now, heartbeat_at=now)
            )
            if result_update.rowcount != 1:
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": receipt_kind, "status": "recorded", **receipt})

    async def record_readback(
        self,
        job_id: str,
        *,
        target_path: str,
        status: str,
        content_sha256: str | None = None,
        details: dict[str, Any] | None = None,
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Record an explicit readback receipt in the canonical effect ledger."""
        return await self.record_effect(
            job_id,
            effect_type="readback",
            receipt_kind="readback",
            target_path=target_path,
            status=status,
            content_sha256=content_sha256,
            details=details,
            owner=owner,
            fencing_token=fencing_token,
        )

    async def record_remote_inference_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Persist one redacted remote admission receipt on an existing job.

        This is an explicit caller-adoption seam for the process-local remote
        broker.  It records the broker status as an effect receipt and leaves
        the canonical job lifecycle under the existing job methods.  In
        particular, an uncertain ``blocked`` receipt cannot release or retry
        the remote lease, and a queued/cancelled/expired receipt cannot invoke
        a provider callback through this method.

        A durable job row is required; this method never creates one.  Active
        durable jobs must provide their existing owner/fencing pair, while an
        ``accepted`` row may record an admission receipt before it is claimed.
        """
        safe_receipt, receipt_digest = _canonical_remote_inference_receipt(receipt)
        job_id = str(safe_receipt["job_id"])
        current = await self.get_job(job_id)
        if current is None:
            raise DurableJobNotFound(job_id)
        persisted_owner = str(current["owner"].get("principal_id") or "")
        if persisted_owner != str(safe_receipt["owner_id"]):
            raise DurableJobLeaseError("remote inference receipt owner does not match the durable job owner")
        admission_status = str(safe_receipt["status"])
        return await self.record_effect(
            job_id,
            effect_type="remote_inference_admission",
            target_path=f"remote_inference:{safe_receipt['operation_id']}",
            status=REMOTE_INFERENCE_EFFECT_STATUSES[admission_status],
            details={
                "admission_status": admission_status,
                "receipt": safe_receipt,
                "receipt_digest": receipt_digest,
            },
            owner=owner,
            fencing_token=fencing_token,
        )

    async def retry_job(
        self,
        job_id: str,
        *,
        owner_kind: str,
        owner_principal_id: str,
        service_id: str | None = None,
        reconciliation_receipt: Any,
        reconciled: bool | None = None,
    ) -> dict[str, Any]:
        if reconciled is False:
            raise DurableJobTransitionError("failed jobs require external-effect reconciliation before retry")
        canonical_receipt, receipt_digest = _canonical_reconciliation_receipt(reconciliation_receipt)
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status != "failed":
                raise DurableJobTransitionError(f"only failed jobs may be retried (current={run.status})")
            _validate_retry_actor(
                run,
                owner_kind=owner_kind,
                owner_principal_id=owner_principal_id,
                service_id=service_id,
            )
            existing_effects = _json_load(run.effect_receipts_json, [])
            if not isinstance(existing_effects, list):
                existing_effects = []
            existing_effects.append(
                {
                    "kind": "reconciliation",
                    "status": "reconciled",
                    "receipt": _json_load(canonical_receipt, {}),
                    "receipt_digest": receipt_digest,
                    "owner_kind": owner_kind,
                    "owner_principal_id": owner_principal_id,
                    "service_id": service_id,
                    "recorded_at": _utc_now().isoformat(),
                }
            )
            now = _utc_now()
            updated = await db.execute(
                update(WorkflowRunState)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == "failed",
                    WorkflowRunState.owner_kind == owner_kind,
                    WorkflowRunState.owner_principal_id == owner_principal_id,
                    WorkflowRunState.service_id == service_id,
                )
                .values(
                    status="queued",
                    failure_reason=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    effect_receipts_json=_canonical(existing_effects[-100:]),
                    updated_at=now,
                    heartbeat_at=now,
                )
            )
            if updated.rowcount != 1:
                raise DurableJobLeaseError("retry actor is unauthorized or job changed")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "retry",
                "status": "recorded",
                "owner_kind": owner_kind,
                "owner_principal_id": owner_principal_id,
                "service_id": service_id,
                "reconciliation_receipt_digest": receipt_digest,
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    async def recover_stale_jobs(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        observed_at = now or _utc_now()
        recovered: list[dict[str, Any]] = []
        async with self._session() as db:
            result = await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.status == "running",
                    or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= observed_at),
                )
            )
            runs = result.scalars().all()
            for run in runs:
                old_owner = run.lease_owner
                expected_token = run.fencing_token
                updated = await db.execute(
                    update(WorkflowRunState)
                    .where(WorkflowRunState.run_identity == run.run_identity, WorkflowRunState.status == "running", WorkflowRunState.fencing_token == expected_token)
                    .values(
                        status="blocked",
                        failure_reason="stale_lease_requires_reconciliation",
                        lease_owner=None,
                        lease_expires_at=None,
                        fencing_token=WorkflowRunState.fencing_token + 1,
                        updated_at=observed_at,
                        heartbeat_at=observed_at,
                    )
                )
                if updated.rowcount != 1:
                    continue
                refreshed = await self._fetch(db, run.run_identity)
                receipt = {
                    "kind": "restart_recovery",
                    "status": "blocked",
                    "reason": "stale_lease_requires_reconciliation",
                    "previous_owner": old_owner,
                    "fencing_token": refreshed.fencing_token,
                    "operator_action": "reconcile_effects_then_retry_or_cancel",
                    "operator_visible": True,
                }
                db.expunge(refreshed)
                recovered.append(_serialize(refreshed, receipt=receipt))
        return recovered

    def _assert_lease(self, run: WorkflowRunState, *, owner: str | None, fencing_token: int | None) -> None:
        if owner is None and fencing_token is None:
            return
        if not owner or fencing_token is None or run.lease_owner != owner or run.fencing_token != fencing_token:
            raise DurableJobLeaseError("active owner lease and fencing token are required")
        persisted_expiry = _as_utc(run.lease_expires_at)
        if persisted_expiry and persisted_expiry <= _utc_now():
            raise DurableJobLeaseError("job lease has expired")

    async def _fetch(self, db: Any, job_id: str) -> WorkflowRunState:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalars().first()
        if run is None:
            raise DurableJobNotFound(job_id)
        return run

    @staticmethod
    def _session():
        # Resolve dynamically so the existing durable_state DB fixture and
        # migration shims can patch one canonical session factory.
        from src.workflows import durable_state

        return durable_state.get_session()


durable_job_repository = DurableJobRepository()


__all__ = [
    "DURABLE_JOB_RECORD_SCHEMA_VERSION",
    "DURABLE_JOB_STATUSES",
    "DURABLE_JOB_TRANSITIONS",
    "DURABLE_JOB_TERMINAL_STATUSES",
    "REMOTE_INFERENCE_RECEIPT_STATUSES",
    "REMOTE_INFERENCE_EFFECT_STATUSES",
    "DurableJobError",
    "DurableJobNotFound",
    "DurableJobTransitionError",
    "DurableJobIdempotencyConflict",
    "DurableJobLeaseError",
    "DurableJobIdentity",
    "DurableJobSpec",
    "DurableJobRepository",
    "_canonical_remote_inference_receipt",
    "durable_job_repository",
]
