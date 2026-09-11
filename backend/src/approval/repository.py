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


def _pending_approval_matches_identity(
    request: ApprovalRequest,
    *,
    conversation_id: str | None,
    owner_principal_id: str | None,
    owner_operator_session_id: str | None,
) -> bool:
    """Match a pending row without allowing a legacy row to shadow it.

    ``get_or_create_pending`` historically selected the newest row before
    checking its owner.  That made an ownerless legacy row win over a newer
    row requested by an authenticated operator.  Treat every owner binding as
    part of the lookup key and require the canonical conversation on rows
    that can be reused.
    """
    expected_conversation = str(conversation_id or "").strip()
    if not expected_conversation:
        return False
    actual_conversation = str(
        getattr(request, "conversation_id", None)
        or _approval_detail_value(
            _approval_details(request),
            "conversation_id",
            "approval_conversation_id",
        )
        or ""
    ).strip()
    if actual_conversation != expected_conversation:
        return False

    details = _approval_details(request)
    actual_owner = str(
        getattr(request, "owner_principal_id", None)
        or _approval_detail_value(
            details,
            "owner_principal_id",
            "approval_owner_principal_id",
        )
        or ""
    ).strip()
    actual_operator_session = str(
        getattr(request, "operator_session_id", None)
        or _approval_detail_value(
            details,
            "operator_session_id",
            "approval_owner_operator_session_id",
            "approval_owner_auth_session_id",
            "approval_owner_session_id",
        )
        or ""
    ).strip()
    expected_owner = str(owner_principal_id or "").strip()
    expected_operator_session = str(owner_operator_session_id or "").strip()

    # Bound callers may reuse only a row carrying both halves of the same
    # owner identity.  An ownerless legacy row is deliberately not a match.
    if expected_owner or expected_operator_session:
        return bool(
            expected_owner
            and expected_operator_session
            and actual_owner == expected_owner
            and actual_operator_session == expected_operator_session
        )

    # An unbound caller can reuse only an explicitly unbound row.  This keeps
    # a bound operator's approval from being selected by an ambient request.
    return not actual_owner and not actual_operator_session


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
        details_json = getattr(request, "details_json", None)
        details = json.loads(details_json) if details_json else {}
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    explicit_owner = str(
        details.get("approval_owner_operator_session_id")
        or details.get("approval_owner_auth_session_id")
        or getattr(request, "operator_session_id", None)
        or ""
    ).strip()
    if explicit_owner:
        return explicit_owner == owner_operator_session_id

    # ``approval_owner_session_id`` is the pre-migration field.  Its meaning
    # was ambiguous, so only preserve rows where it is also the persisted
    # repository session id (the old auth-session-only route).
    legacy_owner = str(details.get("approval_owner_session_id") or "").strip()
    return bool(legacy_owner) and legacy_owner == owner_operator_session_id and (
        getattr(request, "session_id", None) == owner_operator_session_id
    )


_APPROVAL_BINDING_FIELDS: dict[str, tuple[str, ...]] = {
    "workflow_id": (
        "workflow_id",
        "workflow_run_identity",
        "run_identity",
        "workflow_run_id",
        "durable_job_id",
        "job_id",
    ),
    "goal_id": ("goal_id", "durable_goal_id"),
    "criterion_id": ("criterion_id", "durable_criterion_id"),
    "goal_revision": ("goal_revision", "durable_goal_revision"),
    "plan_revision": ("plan_revision", "durable_plan_revision"),
    "candidate_id": (
        "candidate_id",
        "workflow_candidate_id",
        "durable_candidate_id",
    ),
}


def _approval_details(request: ApprovalRequest) -> dict[str, Any]:
    try:
        details_json = getattr(request, "details_json", None)
        parsed = json.loads(details_json) if details_json else {}
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _approval_detail_value(details: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in details:
            return details[name]
    nested_context = details.get("approval_context")
    if isinstance(nested_context, Mapping):
        for name in names:
            if name in nested_context:
                return nested_context[name]
    return None


def _approval_binding_matches(
    request: ApprovalRequest,
    *,
    session_id: str | None,
    owner_operator_session_id: str | None,
    owner_principal_id: str | None,
    approval_binding: Mapping[str, Any] | None,
) -> bool:
    """Return whether an approved row is the exact caller-owned capability.

    The generic approval APIs are used by tools and extension lifecycle calls,
    so the database query deliberately starts broad enough to find all rows
    with the same fingerprint.  Identity is then checked on every row before
    choosing one.  This prevents a newer row owned by another operator (or a
    duplicate row with a different run binding) from blocking or authorizing
    the current caller.
    """
    expected_session = str(session_id or "").strip()
    expected_operator_session = str(owner_operator_session_id or "").strip()
    expected_principal = str(owner_principal_id or "").strip()
    # Every selection/consumption path is an effect-authority lookup.  A
    # fingerprint and conversation id are not enough to prove who may spend
    # the approval, so ownerless legacy callers fail closed as well.
    if not expected_session or not expected_operator_session or not expected_principal:
        return False

    details = _approval_details(request)
    actual_conversation = str(
        getattr(request, "conversation_id", None)
        or _approval_detail_value(details, "conversation_id", "approval_conversation_id")
        or ""
    ).strip()
    if actual_conversation != expected_session:
        return False
    actual_principal = str(
        getattr(request, "owner_principal_id", None)
        or _approval_detail_value(
            details,
            "owner_principal_id",
            "approval_owner_principal_id",
        )
        or ""
    ).strip()
    if actual_principal != expected_principal:
        return False
    if not _approval_belongs_to_operator_session(request, expected_operator_session):
        return False

    if not isinstance(approval_binding, Mapping):
        return True
    for expected_name, aliases in _APPROVAL_BINDING_FIELDS.items():
        if expected_name not in approval_binding or approval_binding[expected_name] is None:
            continue
        expected = approval_binding[expected_name]
        if isinstance(expected, str):
            expected = expected.strip()
            if not expected:
                continue
        observed = _approval_detail_value(details, *aliases)
        if observed is None:
            return False
        if expected_name in {"goal_revision", "plan_revision"}:
            if type(expected) is not int or type(observed) is not int:
                return False
        elif isinstance(expected, str):
            observed = str(observed).strip()
        if observed != expected:
            return False
    return True


def _approval_rows(result: Any) -> list[ApprovalRequest]:
    """Materialize every candidate before applying the exact identity filter."""
    scalar_rows = result.scalars()
    all_rows = getattr(scalar_rows, "all", None)
    return list(all_rows()) if callable(all_rows) else []


def _select_exact_approval_rows(
    requests: list[ApprovalRequest],
    *,
    session_id: str | None,
    owner_operator_session_id: str | None,
    owner_principal_id: str | None,
    approval_binding: Mapping[str, Any] | None,
) -> list[ApprovalRequest]:
    return [
        request
        for request in requests
        if _approval_binding_matches(
            request,
            session_id=session_id,
            owner_operator_session_id=owner_operator_session_id,
            owner_principal_id=owner_principal_id,
            approval_binding=approval_binding,
        )
    ]


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
            pending_rows = _approval_rows(existing)
            matching_rows = [
                request
                for request in pending_rows
                if _pending_approval_matches_identity(
                    request,
                    conversation_id=canonical_session_id,
                    owner_principal_id=supplied_owner,
                    owner_operator_session_id=supplied_operator_session,
                )
            ]
            current_rows = [
                request
                for request in matching_rows
                if not _pending_is_expired(request.expires_at, now=pending_now)
            ]
            if len(current_rows) > 1:
                raise ConversationIdentityError(
                    "approval_ambiguous",
                    "Multiple pending approvals match the same operator identity.",
                )
            if current_rows:
                request = current_rows[0]
                db.expunge(request)
                return request
            # Expire matching stale rows atomically before creating a new
            # bounded request.  Rows belonging to another owner remain
            # untouched and cannot shadow the request being created.
            for request in matching_rows:
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
        owner_principal_id: str | None = None,
        approval_binding: Mapping[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any] | bool | None:
        async with get_session() as db:
            query = (
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == session_id)
                .where(ApprovalRequest.tool_name == tool_name)
                .where(ApprovalRequest.fingerprint == fingerprint)
                .where(ApprovalRequest.status == "approved")
            )
            if approval_id is not None:
                query = query.where(ApprovalRequest.id == approval_id)
            else:
                query = query.order_by(col(ApprovalRequest.created_at).desc())
            result = await db.execute(query)
            raw_requests = _approval_rows(result)
            requests = _select_exact_approval_rows(
                raw_requests,
                session_id=session_id,
                owner_operator_session_id=owner_operator_session_id,
                owner_principal_id=owner_principal_id,
                approval_binding=approval_binding,
            )
            now = datetime.now(timezone.utc)
            current_requests: list[ApprovalRequest] = []
            for candidate in requests:
                if _approval_is_expired(candidate.expires_at, now=now):
                    await db.execute(
                        update(ApprovalRequest)
                        .execution_options(synchronize_session=False)
                        .where(
                            ApprovalRequest.id == candidate.id,
                            ApprovalRequest.status == "approved",
                        )
                        .values(status="expired", resolved_at=now)
                    )
                    continue
                current_requests.append(candidate)
            # Ambiguous approvals are never resolved by recency.  A caller can
            # retry after an operator explicitly fences the duplicate rows.
            if len(current_requests) != 1:
                return False
            request = current_requests[0]
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
            sealed = _seal_capability_approval(
                binding_payload,
                repository_proof=repository_proof,
            )
            return sealed

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
        session_id: str | None = None,
        conversation_id: str | None = None,
        criterion_id: str | None = None,
        candidate_id: str | None = None,
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
                    session_id=session_id,
                    conversation_id=conversation_id,
                    criterion_id=criterion_id,
                    candidate_id=candidate_id,
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
            session_id=session_id,
            conversation_id=conversation_id,
            criterion_id=criterion_id,
            candidate_id=candidate_id,
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
        session_id: str | None,
        conversation_id: str | None,
        criterion_id: str | None,
        candidate_id: str | None,
    ) -> dict[str, Any] | None:
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError, OverflowError):
            return None
        expected_session = str(session_id or "").strip()
        expected_conversation = str(conversation_id or "").strip()
        expected_owner_operator_session = str(owner_operator_session_id or "").strip()
        expected_operator_principal = str(operator_principal_id or "").strip()
        expected_owner_principal = str(owner_principal_id or "").strip()
        if (
            not approval_id
            or not expected_session
            or not expected_conversation
            or not expected_owner_operator_session
            or not expected_operator_principal
            or not expected_owner_principal
        ):
            return None
        result = await db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == "approved",
            )
        )
        request = result.scalars().first()
        if request is None:
            return None
        # Typed resume must consume the exact row bound to the durable run.
        # The row columns are canonical; details are only an additional
        # receipt and cannot repair missing or conflicting row identity.
        if (
            str(getattr(request, "session_id", None) or "").strip() != expected_session
            or str(getattr(request, "conversation_id", None) or "").strip() != expected_conversation
            or str(getattr(request, "operator_session_id", None) or "").strip() != expected_owner_operator_session
            or str(getattr(request, "owner_principal_id", None) or "").strip() != expected_owner_principal
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
            return _approval_detail_value(details, *names)

        # These identities are part of the durable run contract even when
        # their values are absent.  Comparing the normalized pair makes a
        # candidate-present approval unable to authorize a candidate-absent
        # run, and rejects missing criterion/candidate receipts for a run that
        # declared them.
        def exact_optional_identity(
            expected: Any,
            *names: str,
            allow_missing: bool = False,
        ) -> bool:
            expected_value = str(expected).strip() if expected is not None else ""
            observed = detail(*names)
            if observed is None:
                return allow_missing or not expected_value
            observed_value = str(observed).strip() if observed is not None else ""
            return observed_value == expected_value

        if not exact_optional_identity(
            expected_conversation,
            "conversation_id",
            "approval_conversation_id",
            allow_missing=True,
        ):
            return None
        if not exact_optional_identity(
            expected_session,
            "session_id",
            "approval_session_id",
            allow_missing=True,
        ):
            return None
        if not exact_optional_identity(
            criterion_id,
            "criterion_id",
            "durable_criterion_id",
        ):
            return None
        if not exact_optional_identity(
            candidate_id,
            "candidate_id",
            "workflow_candidate_id",
            "durable_candidate_id",
        ):
            return None
        if not exact_optional_identity(
            expected_owner_operator_session,
            "operator_session_id",
            "approval_owner_operator_session_id",
            "approval_owner_auth_session_id",
            allow_missing=True,
        ):
            return None
        if not exact_optional_identity(
            expected_owner_principal,
            "owner_principal_id",
            "approval_owner_principal_id",
            "durable_owner_principal_id",
            allow_missing=True,
        ):
            return None
        if not exact_optional_identity(
            expected_operator_principal,
            "approval_operator_principal_id",
            "operator_principal_id",
        ):
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
                if type(expected) is not int or expected <= 0:
                    return None
                if type(observed) is not int or observed <= 0:
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
        owner_principal_id: str | None = None,
        approval_binding: Mapping[str, Any] | None = None,
    ) -> bool:
        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == session_id)
                .where(ApprovalRequest.tool_name == tool_name)
                .where(ApprovalRequest.fingerprint == fingerprint)
                .where(ApprovalRequest.status == "approved")
            )
            requests = _select_exact_approval_rows(
                _approval_rows(result),
                session_id=session_id,
                owner_operator_session_id=owner_operator_session_id,
                owner_principal_id=owner_principal_id,
                approval_binding=approval_binding,
            )
            now = datetime.now(timezone.utc)
            current_requests: list[ApprovalRequest] = []
            for request in requests:
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
                    continue
                current_requests.append(request)
            if len(current_requests) != 1:
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
