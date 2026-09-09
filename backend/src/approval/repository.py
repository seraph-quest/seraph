"""Persistence for pending approval requests."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import update
from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import ApprovalRequest
from src.db.session_refs import ensure_sessions_exist


# Operator approvals are intentionally short-lived. Callers may provide an
# explicit deadline, while ordinary requests receive this bounded default.
DEFAULT_APPROVAL_TTL_SECONDS = 300


@dataclass(frozen=True)
class ApprovalResolution:
    request: ApprovalRequest | None
    transitioned: bool
    reason: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_expiry(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            return _aware(datetime.fromisoformat(candidate))
        except ValueError:
            return None
    return None


def _details_expiry(details: dict[str, Any] | None) -> tuple[bool, datetime | None]:
    if not isinstance(details, dict):
        return False, None
    for key in ("approval_expires_at", "decision_expires_at", "expires_at"):
        if key in details:
            return True, _parse_expiry(details.get(key))
    return False, None


def _request_expiry(request: ApprovalRequest) -> datetime | None:
    return _parse_expiry(request.expires_at)


def _expiry_is_fresh(request: ApprovalRequest, now: datetime) -> bool:
    expiry = _request_expiry(request)
    return expiry is not None and expiry > now


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


def _approval_matches_resolution_owner(
    request: ApprovalRequest,
    owner_operator_session_id: str,
) -> bool:
    """Mirror the API owner contract immediately before a terminal write."""
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
    legacy_owner = str(details.get("approval_owner_session_id") or "").strip()
    if legacy_owner:
        return legacy_owner == owner_operator_session_id and request.session_id == owner_operator_session_id
    # Preserve the pre-migration auth-session-only contract.  Conversation
    # rows with a different session remain refused by this exact predicate.
    return bool(request.session_id) and request.session_id == owner_operator_session_id


def _approval_principal_id(request: ApprovalRequest) -> str:
    try:
        details = json.loads(request.details_json) if request.details_json else {}
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        return ""
    return str(details.get("approval_owner_principal_id") or "").strip()


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
        expires_at: datetime | str | float | int | None = None,
    ) -> ApprovalRequest:
        now = datetime.now(timezone.utc)
        explicit_details_expiry, details_expiry = _details_expiry(details)
        if expires_at is not None:
            resolved_expiry = _parse_expiry(expires_at)
        elif explicit_details_expiry:
            resolved_expiry = details_expiry
        else:
            resolved_expiry = now + timedelta(seconds=DEFAULT_APPROVAL_TTL_SECONDS)

        persisted_details = dict(details) if isinstance(details, dict) else {}
        if explicit_details_expiry and details_expiry is not None:
            # Keep the explicit deadline visible to API/UI projections while
            # the typed column remains the authoritative decision boundary.
            persisted_details.setdefault("approval_expires_at", details_expiry.isoformat())
        elif expires_at is not None and resolved_expiry is not None:
            persisted_details.setdefault("approval_expires_at", resolved_expiry.isoformat())

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
            if request and _expiry_is_fresh(request, now):
                db.expunge(request)
                return request

            request = ApprovalRequest(
                session_id=session_id,
                tool_name=tool_name,
                risk_level=risk_level,
                status="pending",
                fingerprint=fingerprint,
                summary=summary,
                details_json=json.dumps(persisted_details) if persisted_details else None,
                expires_at=resolved_expiry,
            )
            await ensure_sessions_exist(db, [session_id])
            db.add(request)
            await db.flush()
            db.expunge(request)
            return request

    async def resolve_with_metadata(
        self,
        approval_id: str,
        decision: str,
        *,
        now: datetime | None = None,
        owner_operator_session_id: str | None = None,
        owner_principal_id: str | None = None,
    ) -> ApprovalResolution:
        if decision not in {"approved", "denied"}:
            raise ValueError("approval decision must be approved or denied")
        now = _aware(now or datetime.now(timezone.utc))
        async with get_session() as db:
            result = await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            request = result.scalars().first()
            if request is None:
                return ApprovalResolution(None, False, "not_found")
            if request.status != "pending":
                db.expunge(request)
                return ApprovalResolution(request, False, "already_terminal")
            expiry = _request_expiry(request)
            if expiry is None:
                db.expunge(request)
                return ApprovalResolution(request, False, "expiry_missing")
            if expiry <= now:
                db.expunge(request)
                return ApprovalResolution(request, False, "expired")
            if owner_operator_session_id is not None and not _approval_matches_resolution_owner(
                request,
                owner_operator_session_id,
            ):
                db.expunge(request)
                return ApprovalResolution(request, False, "owner_mismatch")
            if owner_principal_id is not None:
                recorded_principal_id = _approval_principal_id(request)
                if recorded_principal_id and recorded_principal_id != str(owner_principal_id).strip():
                    db.expunge(request)
                    return ApprovalResolution(request, False, "owner_mismatch")

            # Status and fresh deadline are part of one conditional write. A
            # concurrent decision therefore becomes a no-op rather than a
            # second terminal transition or duplicate audit receipt.
            updated = await db.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == approval_id)
                .where(ApprovalRequest.status == "pending")
                .where(ApprovalRequest.expires_at.is_not(None))
                .where(ApprovalRequest.expires_at > now)
                .values(status=decision, resolved_at=now)
            )
            if updated.rowcount != 1:
                refreshed = await db.execute(
                    select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
                )
                request = refreshed.scalars().first()
                if request is not None:
                    db.expunge(request)
                if request is None:
                    return ApprovalResolution(None, False, "not_found")
                if request.status != "pending":
                    return ApprovalResolution(request, False, "already_terminal")
                refreshed_expiry = _request_expiry(request)
                if refreshed_expiry is None:
                    return ApprovalResolution(request, False, "expiry_missing")
                if refreshed_expiry <= now:
                    return ApprovalResolution(request, False, "expired")
                # A still-pending, fresh row means the conditional update lost
                # a race or the backend reported an ambiguous row count. Keep
                # the caller fail-closed without inventing a terminal receipt.
                return ApprovalResolution(request, False, "resolution_conflict")
            refreshed = await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            request = refreshed.scalars().first()
            if request is not None:
                db.expunge(request)
            return ApprovalResolution(request, True, "transitioned")

    async def resolve(self, approval_id: str, decision: str) -> ApprovalRequest | None:
        """Compatibility wrapper returning the affected request, if present."""
        return (await self.resolve_with_metadata(approval_id, decision)).request

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
            now = datetime.now(timezone.utc)
            if not _expiry_is_fresh(request, now):
                return False
            if owner_operator_session_id is not None and not _approval_belongs_to_operator_session(
                request,
                owner_operator_session_id,
            ):
                return False

            consumed = await db.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request.id)
                .where(ApprovalRequest.status == "approved")
                .where(ApprovalRequest.expires_at.is_not(None))
                .where(ApprovalRequest.expires_at > now)
                .values(status="consumed", resolved_at=now)
            )
            return consumed.rowcount == 1

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
            return request is not None and _expiry_is_fresh(request, datetime.now(timezone.utc)) and (
                owner_operator_session_id is None
                or _approval_belongs_to_operator_session(request, owner_operator_session_id)
            )

    async def list_pending(
        self,
        *,
        session_id: str | None = None,
        limit: int = 20,
        owner_operator_session_id: str | None = None,
    ) -> list[dict]:
        limit = min(max(limit, 1), 100)
        async with get_session() as db:
            stmt = (
                select(ApprovalRequest)
                .where(ApprovalRequest.status == "pending")
                .order_by(col(ApprovalRequest.created_at).desc())
            )
            if session_id is not None:
                stmt = stmt.where(ApprovalRequest.session_id == session_id)
            # Filter legacy/foreign rows after the bounded query so the
            # authenticated owner cannot see another operator's approval.
            # The larger cap preserves the requested owner's rows when a
            # foreign row is newer than it is.
            stmt = stmt.limit(100 if owner_operator_session_id else limit)

            result = await db.execute(stmt)
            requests = result.scalars().all()
            if owner_operator_session_id:
                requests = [
                    request
                    for request in requests
                    if _approval_belongs_to_operator_session(request, owner_operator_session_id)
                ][:limit]
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
                    # The typed column is authoritative; details are retained
                    # as metadata but may not override this value.
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                }
                for request in requests
            ]


approval_repository = ApprovalRepository()
