"""Identity bindings shared by approval producers and decision routes."""

from __future__ import annotations

from src.security.trust_contract import PrincipalType, TrustPrincipal


def build_approval_owner_details(
    *,
    session_id: str | None,
    principal: TrustPrincipal | None,
) -> dict[str, str]:
    """Persist auth-session ownership separately from execution context.

    ``TrustPrincipal.session_id`` is the execution/conversation scope used by
    trust decisions.  For interactive operator turns it can be a conversation
    id, so it must never be used as the browser authentication-session owner.
    """
    details: dict[str, str] = {}
    conversation_id = str(session_id or "").strip()
    if conversation_id:
        details["approval_conversation_id"] = conversation_id
    if principal is None:
        return details

    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    if principal_id:
        details["approval_owner_principal_id"] = principal_id

    operator_session_id = str(
        getattr(principal, "operator_session_id", "") or ""
    ).strip()
    try:
        principal_type = PrincipalType(principal.principal_type)
    except (AttributeError, TypeError, ValueError):
        principal_type = None
    # Pre-contract callers sometimes supplied an operator principal whose
    # execution session was also its authentication session. Preserve only
    # that unambiguous case; never derive a browser owner from a conversation-
    # bound principal.
    if (
        not operator_session_id
        and principal_type is PrincipalType.OPERATOR
        and conversation_id
        and str(getattr(principal, "session_id", "") or "").strip() == conversation_id
    ):
        operator_session_id = conversation_id
    if operator_session_id:
        details["approval_owner_operator_session_id"] = operator_session_id
    return details


def approval_owner_operator_session_id(
    *,
    session_id: str | None,
    principal: TrustPrincipal | None,
) -> str | None:
    details = build_approval_owner_details(session_id=session_id, principal=principal)
    owner = details.get("approval_owner_operator_session_id")
    return owner if owner else None
