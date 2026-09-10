"""Execution context shared with tool wrappers during agent runs."""

from contextvars import ContextVar, Token
import hashlib
import hmac
import json
import secrets
from typing import Any, Mapping

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
_current_fencing_token: ContextVar[str | None] = ContextVar("capability_fencing_token", default=None)

# Approval rows are consumed by the async repository and then handed to the
# synchronous capability host.  The short-lived binding below is an opaque
# repository receipt rather than a caller-controlled ``approved`` flag.  A
# process-local key keeps a hand-built mapping from crossing the host boundary;
# the durable approval row remains the source of authority.
_CAPABILITY_APPROVAL_KEY = secrets.token_bytes(32)


def seal_capability_approval(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Seal the exact approval row consumed immediately before an effect."""
    body = {str(key): value for key, value in payload.items() if key != "binding_mac"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    body["binding_mac"] = hmac.new(_CAPABILITY_APPROVAL_KEY, encoded, hashlib.sha256).hexdigest()
    return body


def verify_capability_approval(payload: Mapping[str, Any]) -> bool:
    """Verify a repository-issued approval binding without exposing its key."""
    supplied = payload.get("binding_mac")
    if not isinstance(supplied, str):
        return False
    body = {str(key): value for key, value in payload.items() if key != "binding_mac"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    expected = hmac.new(_CAPABILITY_APPROVAL_KEY, encoded, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


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


def set_runtime_trust_principal(principal: TrustPrincipal | None) -> Token:
    """Temporarily bind a verified service/job principal to the current turn."""
    return _current_trust_principal.set(principal)


def reset_runtime_trust_principal(token: Token) -> None:
    _current_trust_principal.reset(token)


def set_runtime_fencing_token(token: str | None) -> Token:
    """Bind a durable job lease fence to the current execution context."""
    return _current_fencing_token.set(str(token) if token is not None else None)


def reset_runtime_fencing_token(token: Token) -> None:
    _current_fencing_token.reset(token)


def get_current_fencing_token() -> str | None:
    return _current_fencing_token.get()


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
