"""Execution context shared with tool wrappers during agent runs."""

from contextvars import ContextVar, Token

from src.security.trust_contract import (
    AuthorityGrant,
    PrincipalType,
    TrustPrincipal,
    canonical_digest,
)

_current_session_id: ContextVar[str | None] = ContextVar("approval_session_id", default=None)
_current_approval_mode: ContextVar[str] = ContextVar("approval_mode", default="high_risk")
_current_trust_principal: ContextVar[TrustPrincipal | None] = ContextVar(
    "trust_principal",
    default=None,
)


def set_runtime_context(
    session_id: str | None,
    approval_mode: str,
    *,
    trust_principal: TrustPrincipal | None = None,
) -> tuple[Token, Token, Token]:
    """Set session/approval context for the current agent execution."""
    return (
        _current_session_id.set(session_id),
        _current_approval_mode.set(approval_mode),
        _current_trust_principal.set(trust_principal),
    )


def reset_runtime_context(tokens: tuple[Token, ...]) -> None:
    """Restore the previous execution context."""
    if len(tokens) not in {2, 3}:
        raise ValueError("runtime context token bundle is invalid")
    session_token, approval_token = tokens[:2]
    if len(tokens) == 3:
        _current_trust_principal.reset(tokens[2])
    _current_session_id.reset(session_token)
    _current_approval_mode.reset(approval_token)


def get_current_session_id() -> str | None:
    return _current_session_id.get()


def get_current_approval_mode() -> str:
    return _current_approval_mode.get()


def get_current_trust_principal() -> TrustPrincipal | None:
    """Return the authenticated principal bound to the current runtime turn."""
    return _current_trust_principal.get()


def scheduled_workflow_service_principal(
    *,
    scheduled_job_id: str,
    session_id: str,
) -> TrustPrincipal:
    """Create the bounded service identity used by scheduled workflow runs."""
    if not scheduled_job_id.strip() or not session_id.strip():
        raise ValueError("Scheduled workflow authority requires job and session identities")
    job_digest = canonical_digest({"scheduled_job_id": scheduled_job_id})
    return TrustPrincipal(
        principal_id="service:scheduled-workflow",
        principal_type=PrincipalType.SERVICE,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id=session_id,
        job_id=f"scheduled-job:{job_digest[:24]}",
    )
