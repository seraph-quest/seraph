"""Persistence for pending approval requests."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

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


class ApprovalRepository:
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

            request.status = "consumed"
            request.resolved_at = datetime.now(timezone.utc)
            db.add(request)
            return True

    async def has_approved(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        fingerprint: str,
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
            return result.scalars().first() is not None

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
