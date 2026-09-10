"""Persistence for pending approval requests."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import update
from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import ApprovalRequest
from src.db.session_refs import ensure_sessions_exist


def fingerprint_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    approval_context: dict[str, Any] | None = None,
) -> str:
    """Build a stable fingerprint for a tool invocation."""
    fingerprint_payload: dict[str, Any] = {
        "tool_name": tool_name,
        "arguments": arguments,
    }
    if isinstance(approval_context, dict) and approval_context:
        fingerprint_payload["approval_context"] = approval_context
    payload = json.dumps(fingerprint_payload, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _approval_belongs_to_operator_session(
    request: ApprovalRequest,
    owner_operator_session_id: str,
) -> bool:
    """Check the explicit operator-session owner binding on an approval.

    Old rows used ``session_id`` for both conversation and authentication
    scope.  They are accepted only when those values are exactly the same;
    an ambiguous conversation-only row therefore fails closed.
    """
    owner_operator_session_id = str(owner_operator_session_id or "").strip()
    if not owner_operator_session_id:
        return False
    try:
        details = json.loads(request.details_json) if request.details_json else {}
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    explicit_owner = str(
        details.get("approval_owner_operator_session_id")
        or details.get("approval_owner_auth_session_id")
        or ""
    ).strip()
    if explicit_owner:
        return explicit_owner == owner_operator_session_id

    # ``approval_owner_session_id`` is the pre-migration field.  Its meaning
    # was ambiguous, so only preserve rows where it is also the persisted
    # repository session id (the old auth-session-only route).
    legacy_owner = str(details.get("approval_owner_session_id") or "").strip()
    return bool(legacy_owner) and legacy_owner == owner_operator_session_id and (
        request.session_id == owner_operator_session_id
    )


class ApprovalRepository:
    async def get(self, approval_id: str) -> ApprovalRequest | None:
        """Fetch an approval without resolving it."""

        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            request = result.scalars().first()
            if request is not None:
                db.expunge(request)
            return request

    async def get_or_create_pending(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        risk_level: str,
        summary: str,
        fingerprint: str,
        details: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        async with get_session() as db:
            existing = await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == session_id)
                .where(ApprovalRequest.tool_name == tool_name)
                .where(ApprovalRequest.fingerprint == fingerprint)
                .where(ApprovalRequest.status == "pending")
                .order_by(col(ApprovalRequest.created_at).desc())
            )
            request = existing.scalars().first()
            if request:
                db.expunge(request)
                return request

            request = ApprovalRequest(
                session_id=session_id,
                tool_name=tool_name,
                risk_level=risk_level,
                status="pending",
                fingerprint=fingerprint,
                summary=summary,
                details_json=json.dumps(details) if details is not None else None,
            )
            await ensure_sessions_exist(db, [session_id])
            db.add(request)
            await db.flush()
            db.expunge(request)
            return request

    async def resolve(self, approval_id: str, decision: str) -> ApprovalRequest | None:
        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            request = result.scalars().first()
            if request is None:
                return None
            if request.status != "pending":
                db.expunge(request)
                return request

            request.status = decision
            request.resolved_at = datetime.now(timezone.utc)
            db.add(request)
            await db.flush()
            db.expunge(request)
            return request

    async def merge_details(self, approval_id: str, details: dict[str, Any]) -> ApprovalRequest | None:
        """Merge additional metadata into an existing approval request."""
        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            request = result.scalars().first()
            if request is None:
                return None

            existing = json.loads(request.details_json) if request.details_json else {}
            existing.update(details)
            request.details_json = json.dumps(existing)
            db.add(request)
            await db.flush()
            db.expunge(request)
            return request

    async def consume_approved(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        fingerprint: str,
        owner_operator_session_id: str | None = None,
    ) -> bool:
        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == session_id)
                .where(ApprovalRequest.tool_name == tool_name)
                .where(ApprovalRequest.fingerprint == fingerprint)
                .where(ApprovalRequest.status == "approved")
                .order_by(col(ApprovalRequest.created_at).desc())
            )
            request = result.scalars().first()
            if request is None:
                return False
            if owner_operator_session_id is not None and not _approval_belongs_to_operator_session(
                request,
                owner_operator_session_id,
            ):
                return False

            request.status = "consumed"
            request.resolved_at = datetime.now(timezone.utc)
            db.add(request)
            return True

    async def consume_approved_for_resume(
        self,
        *,
        approval_id: str,
        owner_operator_session_id: str,
        operator_principal_id: str,
        job_id: str,
        owner_kind: str,
        owner_principal_id: str,
        service_id: str | None,
        authority_digest: str,
        goal_id: str | None,
        goal_revision: int | None,
        plan_revision: int | None,
        capability_version: str,
        budget_digest: str,
        expires_at: float,
        db: Any | None = None,
    ) -> dict[str, Any] | None:
        """Consume one current, authenticated approval for durable resume.

        The durable resume route must use the actual ``ApprovalRequest`` row.
        Caller-supplied approval fields are only a proposed binding; every
        immutable job/authority field is compared with the typed details that
        were persisted on that row before the conditional approved->consumed
        write.  Missing durable fields fail closed instead of becoming a
        synthetic approval mapping.
        """
        if db is None:
            async with get_session() as session:
                return await self._consume_approved_for_resume_in_session(
                    session,
                    approval_id=approval_id,
                    owner_operator_session_id=owner_operator_session_id,
                    operator_principal_id=operator_principal_id,
                    job_id=job_id,
                    owner_kind=owner_kind,
                    owner_principal_id=owner_principal_id,
                    service_id=service_id,
                    authority_digest=authority_digest,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    plan_revision=plan_revision,
                    capability_version=capability_version,
                    budget_digest=budget_digest,
                    expires_at=expires_at,
                )
        return await self._consume_approved_for_resume_in_session(
            db,
            approval_id=approval_id,
            owner_operator_session_id=owner_operator_session_id,
            operator_principal_id=operator_principal_id,
            job_id=job_id,
            owner_kind=owner_kind,
            owner_principal_id=owner_principal_id,
            service_id=service_id,
            authority_digest=authority_digest,
            goal_id=goal_id,
            goal_revision=goal_revision,
            plan_revision=plan_revision,
            capability_version=capability_version,
            budget_digest=budget_digest,
            expires_at=expires_at,
        )

    async def _consume_approved_for_resume_in_session(
        self,
        db: Any,
        *,
        approval_id: str,
        owner_operator_session_id: str,
        operator_principal_id: str,
        job_id: str,
        owner_kind: str,
        owner_principal_id: str,
        service_id: str | None,
        authority_digest: str,
        goal_id: str | None,
        goal_revision: int | None,
        plan_revision: int | None,
        capability_version: str,
        budget_digest: str,
        expires_at: float,
    ) -> dict[str, Any] | None:
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError, OverflowError):
            return None
        if not approval_id or not owner_operator_session_id or not operator_principal_id:
            return None
        result = await db.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.status == "approved",
                )
            )
        request = result.scalars().first()
        if request is None or not _approval_belongs_to_operator_session(
            request,
            owner_operator_session_id,
        ):
            return None
        try:
            details = json.loads(request.details_json) if request.details_json else {}
        except (TypeError, ValueError):
            return None
        if not isinstance(details, Mapping):
            return None

        def detail(*names: str) -> Any:
            for name in names:
                if name in details:
                    return details[name]
            return None

        required_bindings = (
            ("job_id", job_id, ("durable_job_id", "job_id")),
            ("owner_kind", owner_kind, ("durable_owner_kind", "owner_kind")),
            ("owner_principal_id", owner_principal_id, ("durable_owner_principal_id", "owner_principal_id")),
            ("service_id", service_id, ("durable_service_id", "service_id")),
            ("authority_digest", authority_digest, ("durable_authority_digest", "authority_digest")),
            ("goal_id", goal_id, ("durable_goal_id", "goal_id")),
            ("goal_revision", goal_revision, ("durable_goal_revision", "goal_revision")),
            ("plan_revision", plan_revision, ("durable_plan_revision", "plan_revision")),
            ("capability_version", capability_version, ("durable_capability_version", "capability_version")),
            ("budget_digest", budget_digest, ("durable_budget_digest", "budget_digest")),
        )
        for _field_name, expected, names in required_bindings:
            observed = detail(*names)
            if observed is None:
                return None
            if _field_name in {"goal_revision", "plan_revision"}:
                try:
                    observed = int(observed)
                except (TypeError, ValueError):
                    return None
            if observed != expected:
                return None
        observed_operator = str(
            detail("approval_operator_principal_id", "operator_principal_id") or ""
        ).strip()
        if observed_operator != str(operator_principal_id).strip():
            return None
        observed_approval_id = str(detail("durable_approval_id", "approval_id") or "").strip()
        if observed_approval_id != approval_id:
            return None
        try:
            observed_expires = float(detail("approval_expires_at", "expires_at"))
        except (TypeError, ValueError, OverflowError):
            return None
        if observed_expires != expires_at or observed_expires <= datetime.now(timezone.utc).timestamp():
            return None

        consumed = await db.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == "approved",
            )
            .values(status="consumed", resolved_at=datetime.now(timezone.utc))
        )
        if getattr(consumed, "rowcount", None) != 1:
            return None
        return {
            "approval_id": request.id,
            "status": "consumed",
            "session_id": request.session_id,
            "tool_name": request.tool_name,
            "fingerprint": request.fingerprint,
            "details": dict(details),
        }

    async def has_approved(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        fingerprint: str,
        owner_operator_session_id: str | None = None,
    ) -> bool:
        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == session_id)
                .where(ApprovalRequest.tool_name == tool_name)
                .where(ApprovalRequest.fingerprint == fingerprint)
                .where(ApprovalRequest.status == "approved")
                .order_by(col(ApprovalRequest.created_at).desc())
            )
            request = result.scalars().first()
            return request is not None and (
                owner_operator_session_id is None
                or _approval_belongs_to_operator_session(request, owner_operator_session_id)
            )

    async def list_pending(
        self,
        *,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        limit = min(max(limit, 1), 100)
        async with get_session() as db:
            stmt = (
                select(ApprovalRequest)
                .where(ApprovalRequest.status == "pending")
                .order_by(col(ApprovalRequest.created_at).desc())
                .limit(limit)
            )
            if session_id is not None:
                stmt = stmt.where(ApprovalRequest.session_id == session_id)

            result = await db.execute(stmt)
            requests = result.scalars().all()
            return [
                {
                    "id": request.id,
                    "session_id": request.session_id,
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level,
                    "status": request.status,
                    "fingerprint": request.fingerprint,
                    "summary": request.summary,
                    "created_at": request.created_at.isoformat(),
                    **(
                        json.loads(request.details_json)
                        if request.details_json
                        else {}
                    ),
                }
                for request in requests
            ]


approval_repository = ApprovalRepository()
