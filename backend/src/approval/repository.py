"""Persistence for pending approval requests."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import or_, update
from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import ApprovalRequest, Session
from src.db.session_refs import ensure_sessions_exist
from src.approval.runtime import (
    _CAPABILITY_APPROVAL_KEY,
    _approval_repository_proof_bytes,
    _seal_capability_approval,
)
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    validate_attachment_refs,
)


_DEFAULT_PENDING_TTL_SECONDS = 5 * 60.0


def _approval_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None


def _approval_is_expired(value: object, *, now: datetime | None = None) -> bool:
    """Treat malformed or elapsed approval expiry as unusable."""
    if value is None:
        return False
    expiry = _approval_expiry(value)
    if expiry is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expiry <= current


def _bounded_pending_expiry(value: object, *, now: datetime) -> datetime:
    """Return a finite, future expiry for a pending approval row."""
    default_expiry = now + timedelta(seconds=_DEFAULT_PENDING_TTL_SECONDS)
    expiry = _approval_expiry(value)
    if expiry is None or expiry <= now:
        return default_expiry
    return min(expiry, default_expiry)


def _pending_is_expired(value: object, *, now: datetime) -> bool:
    """Pending approvals must always have a usable finite decision window."""
    return value is None or _approval_is_expired(value, now=now)


def _approval_attachment_refs(request: ApprovalRequest) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(request.attachment_refs_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    try:
        return validate_attachment_refs(
            parsed,
            owner_principal_id=request.owner_principal_id,
        )
    except ConversationIdentityError:
        return []


def _validated_approval_attachment_refs(request: ApprovalRequest) -> list[dict[str, Any]]:
    """Revalidate persisted attachment receipts at the point of execution.

    Approval rows deliberately retain only signed receipt metadata.  A row can
    outlive that receipt, so read-time redaction is insufficient for an effect
    path: every execution must prove that each stored handoff is still valid.
    """
    try:
        parsed = json.loads(request.attachment_refs_json or "[]")
    except (TypeError, ValueError) as exc:
        raise ConversationIdentityError(
            "attachment_reference_invalid",
            "Stored approval attachment references are invalid.",
        ) from exc
    if not isinstance(parsed, list):
        raise ConversationIdentityError(
            "attachment_reference_invalid",
            "Stored approval attachment references are invalid.",
        )
    return validate_attachment_refs(
        parsed,
        owner_principal_id=request.owner_principal_id,
    )


async def _expire_approval_for_attachment_failure_in_session(
    db: Any,
    request: ApprovalRequest,
    *,
    now: datetime,
    error: ConversationIdentityError,
) -> None:
    """Fence an approved row using the caller's already-owned session."""
    try:
        details = json.loads(request.details_json) if request.details_json else {}
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    details["attachment_refs"] = []
    details["attachment_refs_status"] = (
        "expired" if error.code == "attachment_receipt_expired" else "unavailable"
    )
    await db.execute(
        update(ApprovalRequest)
        .execution_options(synchronize_session=False)
        .where(
            ApprovalRequest.id == request.id,
            ApprovalRequest.status == "approved",
        )
        .values(
            status="expired",
            resolved_at=now,
            attachment_refs_json="[]",
            details_json=json.dumps(details, sort_keys=True),
        )
    )


async def _expire_approval_for_attachment_failure(
    request: ApprovalRequest,
    *,
    now: datetime,
    error: ConversationIdentityError,
) -> None:
    """Fence an approved row in a transaction independent of resume state.

    Durable resume callers may be inside a transaction that intentionally
    rolls back after this method returns ``None``.  Re-read and commit the
    quarantine in its own session so the fail-closed expiry and redaction
    survive that caller error.
    """
    async with get_session() as quarantine_db:
        persisted = (
            await quarantine_db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == request.id)
            )
        ).scalars().first()
        if persisted is None or persisted.status != "approved":
            return
        await _expire_approval_for_attachment_failure_in_session(
            quarantine_db,
            persisted,
            now=now,
            error=error,
        )


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
        or request.operator_session_id
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
        details = dict(details or {})
        canonical_session_id = str(session_id or "").strip() or None
        supplied_conversation_id = str(
            details.get("conversation_id")
            or details.get("approval_conversation_id")
            or canonical_session_id
            or ""
        ).strip() or None
        if supplied_conversation_id != canonical_session_id:
            raise ConversationIdentityError(
                "conversation_session_mismatch",
                "Approval conversation identity must equal its session id.",
            )
        supplied_owner = str(
            details.get("owner_principal_id")
            or details.get("approval_owner_principal_id")
            or ""
        ).strip() or None
        supplied_operator_session = str(
            details.get("operator_session_id")
            or details.get("approval_owner_operator_session_id")
            or details.get("approval_owner_auth_session_id")
            or ""
        ).strip() or None
        attachment_value = details.get("attachment_refs", details.get("attachments"))
        if ("attachment_refs" in details or "attachments" in details) and attachment_value:
            if supplied_owner is None:
                raise ConversationIdentityError(
                    "conversation_owner_missing",
                    "Attachment approval metadata requires an owner principal.",
                )
            safe_attachment_refs = validate_attachment_refs(
                attachment_value,
                owner_principal_id=supplied_owner,
            )
        else:
            safe_attachment_refs = []
        if "attachment_refs" in details or "attachments" in details:
            details["attachment_refs"] = safe_attachment_refs
            details.pop("attachments", None)
        pending_now = datetime.now(timezone.utc)
        pending_expires_at = _bounded_pending_expiry(
            details.get("expires_at", details.get("approval_expires_at")),
            now=pending_now,
        )
        details["expires_at"] = pending_expires_at.timestamp()
        if "approval_expires_at" in details:
            details["approval_expires_at"] = pending_expires_at.timestamp()
        channel = str(details.get("channel") or "web").strip()
        transport = str(details.get("transport") or "rest").strip()
        identity = build_conversation_identity(
            conversation_id=canonical_session_id,
            thread_id=details.get("thread_id") or canonical_session_id,
            owner_principal_id=supplied_owner or "ambient",
            operator_session_id=supplied_operator_session,
            device_id=details.get("device_id"),
            channel=channel,
            transport=transport,
            correlation_id=details.get("correlation_id") or f"approval:{fingerprint}",
            causation_id=details.get("causation_id"),
            require_owner=False,
        )

        async with get_session() as db:
            existing = await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == canonical_session_id)
                .where(ApprovalRequest.tool_name == tool_name)
                .where(ApprovalRequest.fingerprint == fingerprint)
                .where(ApprovalRequest.status == "pending")
                .order_by(col(ApprovalRequest.created_at).desc())
            )
            request = existing.scalars().first()
            if request:
                if supplied_owner and request.owner_principal_id not in (None, supplied_owner):
                    raise ConversationIdentityError(
                        "conversation_owner_mismatch",
                        "Approval request belongs to another operator.",
                    )
                if not _pending_is_expired(request.expires_at, now=pending_now):
                    db.expunge(request)
                    return request
                # Expire the old row atomically before creating a new bounded
                # request with the same fingerprint.
                await db.execute(
                    update(ApprovalRequest)
                    .execution_options(synchronize_session=False)
                    .where(
                        ApprovalRequest.id == request.id,
                        ApprovalRequest.status == "pending",
                    )
                    .values(status="expired", resolved_at=pending_now)
                )

            request = ApprovalRequest(
                session_id=canonical_session_id,
                conversation_id=identity.conversation_id or None,
                thread_id=identity.thread_id or None,
                owner_principal_id=supplied_owner,
                operator_session_id=supplied_operator_session,
                device_id=identity.device_id,
                channel=identity.channel,
                transport=identity.transport,
                correlation_id=identity.correlation_id,
                causation_id=identity.causation_id,
                attachment_refs_json=json.dumps(safe_attachment_refs, sort_keys=True),
                challenge=(str(details.get("challenge") or "").strip() or None),
                action=(str(details.get("action") or "").strip() or None),
                expires_at=pending_expires_at,
                tool_name=tool_name,
                risk_level=risk_level,
                status="pending",
                fingerprint=fingerprint,
                summary=summary,
                details_json=json.dumps(details) if details else None,
            )
            await ensure_sessions_exist(db, [canonical_session_id])
            if canonical_session_id and supplied_owner:
                session_result = await db.execute(
                    select(Session).where(Session.id == canonical_session_id)
                )
                session = session_result.scalar_one_or_none()
                if session is None:
                    raise ConversationIdentityError(
                        "conversation_session_not_found",
                        "The approval conversation session was not found.",
                    )
                if session.owner_principal_id not in (None, supplied_owner):
                    raise ConversationIdentityError(
                        "conversation_owner_mismatch",
                        "Approval conversation belongs to another operator.",
                    )
                if session.owner_principal_id is None:
                    session.owner_principal_id = supplied_owner
                    db.add(session)
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
            now = datetime.now(timezone.utc)
            if _approval_is_expired(request.expires_at, now=now):
                transition = await db.execute(
                    update(ApprovalRequest)
                    .execution_options(synchronize_session=False)
                    .where(
                        ApprovalRequest.id == approval_id,
                        ApprovalRequest.status == "pending",
                    )
                    .values(status="expired", resolved_at=now)
                )
                if transition.rowcount == 1:
                    await db.refresh(request)
                db.expunge(request)
                return request

            # CAS prevents two decision callers from both resolving the same
            # pending row after a race or process restart.
            transition = await db.execute(
                update(ApprovalRequest)
                .execution_options(synchronize_session=False)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.status == "pending",
                    or_(ApprovalRequest.expires_at.is_(None), ApprovalRequest.expires_at > now),
                )
                .values(status=decision, resolved_at=now)
            )
            if transition.rowcount != 1:
                await db.refresh(request)
                db.expunge(request)
                return request
            await db.refresh(request)
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

            details = dict(details)
            if "attachment_refs" in details or "attachments" in details:
                raw_attachment_refs = details.get("attachment_refs", details.get("attachments"))
                owner_principal_id = str(request.owner_principal_id or "").strip() or None
                if raw_attachment_refs:
                    if owner_principal_id is None:
                        raise ConversationIdentityError(
                            "conversation_owner_missing",
                            "Attachment approval metadata requires an owner principal.",
                        )
                    details["attachment_refs"] = validate_attachment_refs(
                        raw_attachment_refs,
                        owner_principal_id=owner_principal_id,
                    )
                else:
                    details["attachment_refs"] = []
                details.pop("attachments", None)
            existing = json.loads(request.details_json) if request.details_json else {}
            if not isinstance(existing, dict):
                existing = {}
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
    ) -> dict[str, Any] | bool | None:
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
            now = datetime.now(timezone.utc)
            try:
                _validated_approval_attachment_refs(request)
            except ConversationIdentityError as exc:
                await _expire_approval_for_attachment_failure_in_session(
                    db,
                    request,
                    now=now,
                    error=exc,
                )
                return False
            if _approval_is_expired(request.expires_at, now=now):
                await db.execute(
                    update(ApprovalRequest)
                    .execution_options(synchronize_session=False)
                    .where(
                        ApprovalRequest.id == request.id,
                        ApprovalRequest.status == "approved",
                    )
                    .values(status="expired", resolved_at=now)
                )
                return False
            # Conditional update is the one-use fence. A concurrent caller
            # sees rowcount zero and cannot replay the approved action.
            consumed = await db.execute(
                update(ApprovalRequest)
                .execution_options(synchronize_session=False)
                .where(
                    ApprovalRequest.id == request.id,
                    ApprovalRequest.status == "approved",
                    or_(ApprovalRequest.expires_at.is_(None), ApprovalRequest.expires_at > now),
                )
                .values(status="consumed", resolved_at=now)
            )
            if consumed.rowcount != 1:
                return None
            details: dict[str, Any]
            try:
                parsed_details = json.loads(request.details_json) if request.details_json else {}
            except (TypeError, ValueError):
                parsed_details = {}
            details = dict(parsed_details) if isinstance(parsed_details, Mapping) else {}
            binding_payload = {
                "approval_id": str(request.id),
                "status": "consumed",
                "session_id": str(request.session_id or ""),
                "tool_name": str(request.tool_name),
                "fingerprint": str(request.fingerprint),
                "owner_operator_session_id": str(owner_operator_session_id or ""),
                "approval_expires_at": details.get("approval_expires_at"),
                "approval_context": (
                    dict(details["approval_context"])
                    if isinstance(details.get("approval_context"), Mapping)
                    else None
                ),
                "approval_resolved_at": now.isoformat(),
                "consumed_at": now.isoformat(),
            }
            repository_proof = hmac.new(
                _CAPABILITY_APPROVAL_KEY,
                _approval_repository_proof_bytes(binding_payload),
                hashlib.sha256,
            ).hexdigest()
            return _seal_capability_approval(
                binding_payload,
                repository_proof=repository_proof,
            )

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
                    quarantine_in_separate_session=False,
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
            quarantine_in_separate_session=True,
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
        quarantine_in_separate_session: bool,
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
        now = datetime.now(timezone.utc)
        try:
            _validated_approval_attachment_refs(request)
        except ConversationIdentityError as exc:
            if quarantine_in_separate_session:
                await _expire_approval_for_attachment_failure(
                    request,
                    now=now,
                    error=exc,
                )
            else:
                await _expire_approval_for_attachment_failure_in_session(
                    db,
                    request,
                    now=now,
                    error=exc,
                )
            return None
        if _approval_is_expired(request.expires_at, now=now):
            await db.execute(
                update(ApprovalRequest)
                .execution_options(synchronize_session=False)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.status == "approved",
                )
                .values(status="expired", resolved_at=now)
            )
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
            .execution_options(synchronize_session=False)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == "approved",
                or_(ApprovalRequest.expires_at.is_(None), ApprovalRequest.expires_at > now),
            )
            .values(status="consumed", resolved_at=now)
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
            if request is None:
                return False
            if owner_operator_session_id is not None and not _approval_belongs_to_operator_session(
                request, owner_operator_session_id
            ):
                return False
            if _approval_is_expired(request.expires_at):
                await db.execute(
                    update(ApprovalRequest)
                    .where(
                        ApprovalRequest.id == request.id,
                        ApprovalRequest.status == "approved",
                    )
                    .values(status="expired", resolved_at=datetime.now(timezone.utc))
                )
                return False
            return True

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
            output: list[dict[str, Any]] = []
            for request in requests:
                if _approval_is_expired(request.expires_at):
                    await db.execute(
                        update(ApprovalRequest)
                        .execution_options(synchronize_session=False)
                        .where(
                            ApprovalRequest.id == request.id,
                            ApprovalRequest.status == "pending",
                        )
                        .values(status="expired", resolved_at=datetime.now(timezone.utc))
                    )
                    continue
                try:
                    details = json.loads(request.details_json) if request.details_json else {}
                except (TypeError, ValueError):
                    details = {}
                if not isinstance(details, dict):
                    details = {}
                owner_principal_id = request.owner_principal_id or details.get("approval_owner_principal_id")
                if "attachment_refs" in details:
                    try:
                        details["attachment_refs"] = validate_attachment_refs(
                            details["attachment_refs"],
                            owner_principal_id=owner_principal_id,
                        )
                    except ConversationIdentityError as exc:
                        details["attachment_refs"] = []
                        details["attachment_refs_status"] = (
                            "expired" if exc.code == "attachment_receipt_expired" else "unavailable"
                        )
                conversation_id = request.conversation_id or details.get("approval_conversation_id") or request.session_id
                thread_id = request.thread_id or details.get("thread_id") or conversation_id
                operator_session_id = (
                    request.operator_session_id
                    or details.get("approval_owner_operator_session_id")
                    or details.get("approval_owner_auth_session_id")
                )
                attachment_refs = _approval_attachment_refs(request)
                if not attachment_refs and "attachment_refs" in details:
                    attachment_refs = details["attachment_refs"]
                # Server-owned lineage wins over caller-provided detail keys.
                output.append(
                    {
                        **details,
                        "id": request.id,
                        "session_id": request.session_id,
                        "tool_name": request.tool_name,
                        "risk_level": request.risk_level,
                        "status": request.status,
                        "fingerprint": request.fingerprint,
                        "summary": request.summary,
                        "conversation_id": conversation_id,
                        "thread_id": thread_id,
                        "owner_principal_id": owner_principal_id,
                        "operator_session_id": operator_session_id,
                        "device_id": request.device_id or details.get("device_id"),
                        "channel": request.channel or details.get("channel") or "web",
                        "transport": request.transport or details.get("transport") or "rest",
                        "correlation_id": request.correlation_id or details.get("correlation_id"),
                        "causation_id": request.causation_id or details.get("causation_id"),
                        "attachment_refs": attachment_refs,
                        "challenge": request.challenge or details.get("challenge"),
                        "action": request.action or details.get("action"),
                        "expires_at": request.expires_at.isoformat() if request.expires_at is not None else details.get("expires_at"),
                        "created_at": request.created_at.isoformat(),
                    }
                )
            return output


approval_repository = ApprovalRepository()
