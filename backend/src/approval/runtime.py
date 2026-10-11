"""Execution context shared with tool wrappers during agent runs."""

from contextvars import ContextVar, Token
import asyncio
import inspect
import hashlib
import hmac
import json
import secrets
import threading
import time
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
_current_lease_owner: ContextVar[str | None] = ContextVar("durable_lease_owner", default=None)
_current_discovery_invocation: ContextVar[Any] = ContextVar("original_discovery_invocation", default=None)


class _DiscoveryInvocation:
    """One registered wrapper lifetime; provenance only, never a grant."""
    __slots__ = ("_bindings", "_live", "_consumed")

    def __init__(self, *args, **kwargs):
        raise RuntimeError("goal_discovery_invocation_internal_only")

    def __setattr__(self, name, value):
        raise RuntimeError("goal_discovery_invocation_immutable")

    def validate(self, *, service):
        from src.scheduler import engine
        from src.guardian.goal_discovery import run_goal_discovery_tick
        (original_service, scheduler, registration, wrapper, callback, loop,
            principal, task, job_id) = self._bindings
        if (type(self) is not _DiscoveryInvocation or not self._live
                or _current_discovery_invocation.get() is not self
                or service is not original_service
                or getattr(service, "_active_discovery_invocation", None) is not self
                or engine._scheduler is not scheduler or engine._scheduler_loop is not loop
                or asyncio.get_running_loop() is not loop or asyncio.current_task() is not task
                or scheduler.get_job("goal_public_discovery") is not registration
                or registration.func is not wrapper or callback is not run_goal_discovery_tick
                or registration.id != "goal_public_discovery"
                or get_current_trust_principal() is not principal
                or type(principal) is not TrustPrincipal
                or principal.principal_id != "service:scheduler:goal_public_discovery"
                or principal.principal_type is not PrincipalType.SERVICE
                or not principal.authenticated or principal.revoked or principal.grants != ()
                or principal.job_id != job_id or not job_id.startswith("scheduler:goal_public_discovery:")):
            raise RuntimeError("goal_discovery_invocation_unavailable")
        service._validate_original_admission_owner(jobs=service.jobs,
            host=service._lifecycle_host, task=task)
        return self


def _bind_discovery_invocation(service, *, scheduler, registration, wrapper, callback,
        loop, principal, task, job_id):
    from src.scheduler import engine
    if inspect.currentframe().f_back.f_code is not engine._open_discovery_invocation.__code__:
        raise RuntimeError("goal_discovery_invocation_internal_only")
    if (_current_discovery_invocation.get() is not None
            or getattr(service, "_active_discovery_invocation", None) is not None):
        raise RuntimeError("goal_discovery_invocation_unavailable")
    marker = object.__new__(_DiscoveryInvocation)
    object.__setattr__(marker, "_bindings", (service, scheduler, registration, wrapper,
        callback, loop, principal, task, job_id))
    object.__setattr__(marker, "_live", True)
    object.__setattr__(marker, "_consumed", False)
    token = _current_discovery_invocation.set(marker)
    service._active_discovery_invocation = marker
    try:
        marker.validate(service=service)
    except BaseException:
        _close_discovery_invocation(marker, token)
        raise
    return marker, token


def _get_current_discovery_invocation(service, *, consume=False):
    marker = _current_discovery_invocation.get()
    if type(marker) is not _DiscoveryInvocation:
        raise RuntimeError("goal_discovery_invocation_unavailable")
    marker.validate(service=service)
    if consume:
        if marker._consumed:
            raise RuntimeError("goal_discovery_invocation_consumed")
        object.__setattr__(marker, "_consumed", True)
    return marker


def _invalidate_discovery_invocation():
    marker = _current_discovery_invocation.get()
    if type(marker) is _DiscoveryInvocation:
        object.__setattr__(marker, "_live", False)


def _close_discovery_invocation(marker, token):
    object.__setattr__(marker, "_live", False)
    service = marker._bindings[0]
    if getattr(service, "_active_discovery_invocation", None) is marker:
        service._active_discovery_invocation = None
    _current_discovery_invocation.reset(token)

# Approval rows are consumed by the async repository and then handed to the
# synchronous capability host.  The short-lived binding below is an opaque
# repository receipt rather than a caller-controlled ``approved`` flag.  The
# durable approval row remains the source of authority; the process-local
# registry prevents a caller from manufacturing a valid binding by merely
# importing a signing helper.
_CAPABILITY_APPROVAL_KEY = secrets.token_bytes(32)
_CAPABILITY_APPROVAL_RECEIPT_TTL_SECONDS = 300.0
_CAPABILITY_APPROVAL_RECEIPT_LIMIT = 1024
_CAPABILITY_APPROVAL_RECEIPTS: dict[str, tuple[str, float]] = {}
_CAPABILITY_APPROVAL_RECEIPTS_LOCK = threading.Lock()


def _approval_payload_body(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    body = {str(key): value for key, value in payload.items() if key != "binding_mac"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return body, encoded


def _approval_repository_proof_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonicalize the consumed row and exact binding for repository proof.

    This helper only defines the bytes covered by the repository provenance
    MAC.  It does not issue a receipt or mutate the process-local registry.
    The row fields are repeated explicitly so mutable caller fields cannot be
    substituted for the row identity that was conditionally consumed.
    """
    body, _ = _approval_payload_body(payload)
    row = {
        "approval_id": body.get("approval_id"),
        "status": body.get("status"),
        "session_id": body.get("session_id"),
        "tool_name": body.get("tool_name"),
        "fingerprint": body.get("fingerprint"),
        "resolved_at": body.get("approval_resolved_at"),
    }
    return json.dumps(
        {"row": row, "binding": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def seal_capability_approval(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reject caller-side approval fabrication.

    Approval bindings are issued only by ``ApprovalRepository`` after an
    approved database row has been conditionally changed to ``consumed``.
    Keeping this historical name as a failing shim makes accidental use
    visible while avoiding a public signing primitive.
    """
    raise RuntimeError("approval_seal_internal_only")


def _seal_capability_approval(
    payload: Mapping[str, Any],
    *,
    repository_proof: str | None = None,
) -> dict[str, Any]:
    """Issue an opaque binding for a repository-consumed approval row.

    The public helper above cannot mint an approval.  This seam accepts only
    a MAC computed by ``ApprovalRepository`` after its conditional
    approved->consumed update.  The MAC covers the exact row identity,
    ``resolved_at``, and binding payload.  A payload or caller-constructed
    model therefore cannot manufacture a repository receipt.
    """
    if not isinstance(repository_proof, str) or not repository_proof:
        raise RuntimeError("approval_seal_proof_missing")
    body, encoded = _approval_payload_body(payload)
    if (
        body.get("status") != "consumed"
        or not body.get("approval_id")
        or not body.get("session_id")
        or not body.get("tool_name")
        or not body.get("fingerprint")
        or not body.get("approval_resolved_at")
    ):
        raise RuntimeError("approval_seal_proof_invalid")
    expected_proof = hmac.new(
        _CAPABILITY_APPROVAL_KEY,
        _approval_repository_proof_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(repository_proof, expected_proof):
        raise RuntimeError("approval_seal_proof_invalid")
    receipt_token = secrets.token_urlsafe(32)
    body["receipt_token"] = receipt_token
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    body["binding_mac"] = hmac.new(_CAPABILITY_APPROVAL_KEY, encoded, hashlib.sha256).hexdigest()
    issued_at = time.monotonic()
    with _CAPABILITY_APPROVAL_RECEIPTS_LOCK:
        cutoff = issued_at - _CAPABILITY_APPROVAL_RECEIPT_TTL_SECONDS
        for token, (_, token_time) in list(_CAPABILITY_APPROVAL_RECEIPTS.items()):
            if token_time < cutoff:
                _CAPABILITY_APPROVAL_RECEIPTS.pop(token, None)
        while len(_CAPABILITY_APPROVAL_RECEIPTS) >= _CAPABILITY_APPROVAL_RECEIPT_LIMIT:
            _CAPABILITY_APPROVAL_RECEIPTS.pop(next(iter(_CAPABILITY_APPROVAL_RECEIPTS)))
        _CAPABILITY_APPROVAL_RECEIPTS[receipt_token] = (
            hashlib.sha256(encoded).hexdigest(),
            issued_at,
        )
    return body


def verify_capability_approval(payload: Mapping[str, Any]) -> bool:
    """Verify a currently issued repository approval binding.

    Verification is deliberately process-local and short lived.  A binding
    from a prior process cannot authorize an effect after restart, while the
    durable approval row remains auditable as consumed.
    """
    verified, _ = _approval_binding_record(payload)
    return verified


def _approval_binding_record(payload: Mapping[str, Any]) -> tuple[bool, str | None]:
    supplied = payload.get("binding_mac")
    receipt_token = payload.get("receipt_token")
    if not isinstance(supplied, str) or not isinstance(receipt_token, str):
        return False, None
    body = {str(key): value for key, value in payload.items() if key != "binding_mac"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    expected = hmac.new(_CAPABILITY_APPROVAL_KEY, encoded, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        return False, None
    with _CAPABILITY_APPROVAL_RECEIPTS_LOCK:
        issued = _CAPABILITY_APPROVAL_RECEIPTS.get(receipt_token)
        if issued is None:
            return False, None
        digest, issued_at = issued
        if time.monotonic() - issued_at > _CAPABILITY_APPROVAL_RECEIPT_TTL_SECONDS:
            _CAPABILITY_APPROVAL_RECEIPTS.pop(receipt_token, None)
            return False, None
        return hmac.compare_digest(digest, hashlib.sha256(encoded).hexdigest()), receipt_token


def _consume_capability_approval(payload: Mapping[str, Any]) -> bool:
    """Consume one repository-issued receipt at the host boundary."""
    verified, receipt_token = _approval_binding_record(payload)
    if not verified or receipt_token is None:
        return False
    with _CAPABILITY_APPROVAL_RECEIPTS_LOCK:
        # Re-check under the write lock so two concurrent effects cannot use
        # one consumed approval receipt.
        if receipt_token not in _CAPABILITY_APPROVAL_RECEIPTS:
            return False
        _CAPABILITY_APPROVAL_RECEIPTS.pop(receipt_token, None)
    return True


def set_runtime_context(
    session_id: str | None,
    approval_mode: str,
    *,
    trust_principal: TrustPrincipal | None = None,
) -> tuple[Token, Token, Token]:
    """Set session/approval context for the current agent execution."""
    _invalidate_discovery_invocation()
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
        _invalidate_discovery_invocation()
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
    _invalidate_discovery_invocation()
    return _current_trust_principal.set(principal)


def reset_runtime_trust_principal(token: Token) -> None:
    _invalidate_discovery_invocation()
    _current_trust_principal.reset(token)


def set_runtime_fencing_token(token: str | None) -> Token:
    """Bind a durable job lease fence to the current execution context."""
    return _current_fencing_token.set(str(token) if token is not None else None)


def reset_runtime_fencing_token(token: Token) -> None:
    _current_fencing_token.reset(token)


def get_current_fencing_token() -> str | None:
    return _current_fencing_token.get()


def set_runtime_lease_owner(owner: str | None) -> Token:
    """Bind the durable lease owner while a workflow step executes."""
    normalized = str(owner).strip() if owner is not None else ""
    return _current_lease_owner.set(normalized or None)


def reset_runtime_lease_owner(token: Token) -> None:
    _current_lease_owner.reset(token)


def get_current_lease_owner() -> str | None:
    return _current_lease_owner.get()


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
