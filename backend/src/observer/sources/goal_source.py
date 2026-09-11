"""Goal context source — reads active goals from the repository."""

import logging
from collections import defaultdict

from src.audit.runtime import log_integration_event
from src.approval.runtime import get_current_trust_principal
from src.security.trust_contract import PrincipalType

logger = logging.getLogger(__name__)

_REGISTERED_SERVICE_IDENTITIES = frozenset({"service:strategist"})


def _normalized(value: object) -> str | None:
    return str(value or "").strip() or None


def _resolve_scope(
    *,
    owner_principal_id: str | None,
    owner_session_id: str | None,
    service_id: str | None,
) -> tuple[str, str | None, str | None, str | None] | None:
    """Resolve a trusted owner scope or an explicitly registered service scope."""
    owner = _normalized(owner_principal_id)
    session = _normalized(owner_session_id)
    service = _normalized(service_id)
    if (owner is None) != (session is None):
        return None

    principal = get_current_trust_principal()
    principal_type = None
    if principal is not None:
        try:
            principal_type = PrincipalType(principal.principal_type)
        except (TypeError, ValueError):
            principal_type = None
    if owner is not None:
        # When a request/runtime principal is present, explicit scope fields
        # are assertions and cannot switch the observer to another operator.
        if principal is not None and (
            not principal.authenticated
            or principal.revoked
            or principal_type is not PrincipalType.OPERATOR
            or _normalized(principal.principal_id) != owner
            or _normalized(principal.operator_session_id) != session
        ):
            return None
        return "owner", owner, session, None

    if service is None and principal is not None:
        if (
            principal.authenticated
            and not principal.revoked
            and principal_type is PrincipalType.OPERATOR
        ):
            principal_owner = _normalized(principal.principal_id)
            principal_session = _normalized(principal.operator_session_id)
            if principal_owner and principal_session:
                return "owner", principal_owner, principal_session, None
        if (
            principal.authenticated
            and not principal.revoked
            and principal_type is PrincipalType.SERVICE
            and _normalized(principal.principal_id) in _REGISTERED_SERVICE_IDENTITIES
        ):
            service = _normalized(principal.principal_id)

    if service in _REGISTERED_SERVICE_IDENTITIES and principal is not None:
        if (
            not principal.authenticated
            or principal.revoked
            or principal_type is not PrincipalType.SERVICE
            or _normalized(principal.principal_id) != service
        ):
            return None
    if service in _REGISTERED_SERVICE_IDENTITIES:
        return "service", None, None, service
    return None


async def gather_goals(
    *,
    owner_principal_id: str | None = None,
    owner_session_id: str | None = None,
    service_id: str | None = None,
) -> dict:
    """Return active goals only inside an authenticated or registered scope."""
    try:
        from src.goals.repository import goal_repository

        scope = _resolve_scope(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            service_id=service_id,
        )
        if scope is None:
            await log_integration_event(
                integration_type="observer_source",
                name="goals",
                outcome="degraded",
                details={
                    "goal_count": 0,
                    "reason": "authenticated_owner_or_registered_service_scope_required",
                },
            )
            return {
                "active_goals_summary": "",
                "observer_source_status": "degraded",
                "observer_source_reason": "goal_scope_required",
            }

        scope_kind, scoped_owner, scoped_session, scoped_service = scope
        if scope_kind == "owner":
            goals = await goal_repository.list_goals(
                status="active",
                owner_principal_id=scoped_owner,
                owner_session_id=scoped_session,
            )
        else:
            # This is deliberately the only global read path. It is available
            # only to the fixed service identity above, never to an anonymous
            # or arbitrary service context.
            goals = await goal_repository.list_goals(status="active")

        if not goals:
            await log_integration_event(
                integration_type="observer_source",
                name="goals",
                outcome="empty_result",
                details={
                    "goal_count": 0,
                    "scope": scope_kind,
                    "service_id": scoped_service,
                },
            )
            return {"active_goals_summary": ""}

        by_domain: dict[str, list[str]] = defaultdict(list)
        for g in goals:
            by_domain[g.domain].append(g.title)

        parts = []
        for domain, titles in by_domain.items():
            truncated = titles[:3]
            suffix = f" (+{len(titles) - 3} more)" if len(titles) > 3 else ""
            parts.append(f"{domain}: {', '.join(truncated)}{suffix}")

        await log_integration_event(
            integration_type="observer_source",
            name="goals",
            outcome="succeeded",
            details={
                "goal_count": len(goals),
                "domain_count": len(by_domain),
                "scope": scope_kind,
                "service_id": scoped_service,
            },
        )
        return {"active_goals_summary": "; ".join(parts)}

    except Exception as exc:
        await log_integration_event(
            integration_type="observer_source",
            name="goals",
            outcome="failed",
            details={"error": str(exc)},
        )
        logger.exception("Goal source failed")
        return {"active_goals_summary": ""}
