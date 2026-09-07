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
        async with self._session() as db:
            await ensure_sessions_exist(db, [spec.session_id])
            existing = (
                await db.execute(
                    select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == binding)
                )
            ).scalars().first()
            if existing is not None:
                if existing.run_identity != identity.job_id or existing.input_digest != input_digest:
                    raise DurableJobIdempotencyConflict("idempotency binding already belongs to a different invocation")
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
                authority_digest=_digest(spec.declared_authority),
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
            if run.deadline_at and run.deadline_at <= now:
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

        An ownerless receipt is allowed only before execution is claimed.  A
        running job must supply the current lease owner and fencing token so a
        stale runner cannot append an artifact after restart recovery.
        """
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status == "running" and (owner is None or fencing_token is None):
                raise DurableJobLeaseError("active jobs require owner and fencing token for artifact writes")
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
            conditions = [WorkflowRunState.run_identity == job_id]
            if owner is not None:
                conditions.extend((WorkflowRunState.lease_owner == owner, WorkflowRunState.fencing_token == fencing_token))
            result_update = await db.execute(update(WorkflowRunState).where(*conditions).values(artifact_receipts_json=_canonical(existing[-100:]), updated_at=now, heartbeat_at=now))
            if result_update.rowcount != 1:
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "artifact", "status": "recorded", **receipt})

    async def retry_job(
        self,
        job_id: str,
        *,
        owner: str,
        reconciled: bool,
        reconciliation_receipt: str | None = None,
    ) -> dict[str, Any]:
        if not reconciled:
            raise DurableJobTransitionError("failed jobs require external-effect reconciliation before retry")
        if not _text(owner):
            raise DurableJobLeaseError("owner is required to request a retry")
        # A failed attempt releases its lease.  The retry owner is recorded in
        # the receipt; the next queued claim obtains a fresh fencing token.
        result = await self.transition_job(job_id, "queued", reason="explicit_retry_reconciled")
        result["receipt"]["reconciliation_receipt"] = _digest(reconciliation_receipt or "operator_reconciled")
        return result

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
        if run.lease_expires_at and run.lease_expires_at <= _utc_now():
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
    "DurableJobError",
    "DurableJobNotFound",
    "DurableJobTransitionError",
    "DurableJobIdempotencyConflict",
    "DurableJobLeaseError",
    "DurableJobIdentity",
    "DurableJobSpec",
    "DurableJobRepository",
    "durable_job_repository",
]
