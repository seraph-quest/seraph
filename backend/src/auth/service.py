from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import secrets
import asyncio
import threading
import weakref

from sqlalchemy import select, update

from config.settings import settings
from src.db.engine import get_session
from src.db.models import OperatorSession
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


class AuthFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AuthenticatedOperator:
    session_id: str
    principal: TrustPrincipal
    idle_expires_at: datetime
    absolute_expires_at: datetime


def auth_enabled() -> bool:
    return bool(settings.operator_auth_secret or settings.operator_auth_secret_hash)


def require_auth_configured() -> None:
    if not auth_enabled():
        raise AuthFailure("auth_not_configured")


def validate_auth_configuration() -> None:
    production = settings.deployment_environment.strip().lower() in {"prod", "production"}
    if production and not auth_enabled():
        raise RuntimeError("operator authentication credential is required in production")
    if production and not settings.operator_auth_cookie_secure:
        raise RuntimeError("secure operator authentication cookies are required in production")
    if settings.operator_auth_allow_unauthenticated_tests and settings.deployment_environment != "test":
        raise RuntimeError("unauthenticated auth bypass is permitted only in the test environment")
    if production and settings.operator_auth_backend_workers != 1:
        raise RuntimeError("in-process login throttling requires exactly one production backend worker")
    if settings.operator_auth_secret and settings.operator_auth_secret_hash:
        raise RuntimeError("configure only one operator authentication credential")
    if auth_enabled() and (
        settings.operator_auth_idle_seconds <= 0
        or settings.operator_auth_absolute_seconds <= 0
        or settings.operator_auth_idle_seconds > settings.operator_auth_absolute_seconds
    ):
        raise RuntimeError("operator authentication expiry configuration is invalid")
    if settings.operator_auth_revocation_poll_seconds <= 0:
        raise RuntimeError("operator authentication revocation polling must be positive")


def _pbkdf2(value: str, salt: bytes, iterations: int = 600_000) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", value.encode(), salt, iterations)


def encode_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    return f"pbkdf2_sha256$600000${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(_pbkdf2(value, salt)).decode()}"


def _verify_secret_sync(value: str) -> bool:
    if settings.operator_auth_secret:
        return hmac.compare_digest(value, settings.operator_auth_secret)
    encoded = settings.operator_auth_secret_hash
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256" or int(rounds) != 600_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_pbkdf2(value, salt), expected)


_VERIFY_LIMIT = asyncio.Semaphore(2)

# Authentication is read-heavy: the cockpit can issue a burst of authenticated
# requests for one browser session while it refreshes several panels.  Keep
# validation on every request, but only persist the sliding idle-expiry touch
# once per process-local interval.  The database row remains authoritative;
# this lock only coalesces the stale-touch write path.
_AUTH_TOUCH_INTERVAL = timedelta(seconds=30)
_AUTH_TOUCH_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_AUTH_TOUCH_LOCKS_GUARD = threading.Lock()


def _auth_touch_lock() -> asyncio.Lock:
    """Return the bounded coalescing lock for the current event loop.

    ASGI normally uses one loop per worker, while test and embedding hosts can
    create and close loops sequentially.  A weak per-loop registry keeps the
    slow path serialized without retaining closed loops or binding one lock to
    a loop that no longer exists.
    """
    loop = asyncio.get_running_loop()
    with _AUTH_TOUCH_LOCKS_GUARD:
        lock = _AUTH_TOUCH_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _AUTH_TOUCH_LOCKS[loop] = lock
        return lock


async def verify_secret(value: str) -> bool:
    require_auth_configured()
    async with _VERIFY_LIMIT:
        return await asyncio.to_thread(_verify_secret_sync, value)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _principal(session_id: str) -> TrustPrincipal:
    return TrustPrincipal(
        principal_id="operator:single",
        principal_type=PrincipalType.OPERATOR,
        authenticated=True,
        grants=(
            AuthorityGrant.INGRESS,
            AuthorityGrant.MODEL_INFERENCE,
            AuthorityGrant.CAPABILITY_EXECUTE,
            AuthorityGrant.ARTIFACT_TRANSFER,
        ),
        session_id=session_id,
        operator_session_id=session_id,
    )


def bind_operator_principal(operator: AuthenticatedOperator, conversation_id: str) -> TrustPrincipal:
    """Bind authenticated operator authority to one conversation execution scope."""
    return TrustPrincipal(
        principal_id=operator.principal.principal_id,
        principal_type=operator.principal.principal_type,
        authenticated=True,
        grants=operator.principal.grants,
        session_id=conversation_id,
        operator_session_id=operator.session_id,
    )


def test_bypass_operator() -> AuthenticatedOperator:
    """Return a synthetic operator only for the explicit test bypass.

    This identity is intentionally unavailable to production configuration. It
    lets route tests exercise the same principal contract without weakening the
    production ingress boundary or minting authority from a client session id.
    """
    if not (
        settings.deployment_environment == "test"
        and settings.operator_auth_allow_unauthenticated_tests
    ):
        raise AuthFailure("authentication_required")
    now = datetime.now(timezone.utc)
    return AuthenticatedOperator(
        "test-auth-bypass",
        TrustPrincipal(
            principal_id="operator:test-bypass",
            principal_type=PrincipalType.OPERATOR,
            authenticated=True,
            grants=(
                AuthorityGrant.INGRESS,
                AuthorityGrant.MODEL_INFERENCE,
                AuthorityGrant.CAPABILITY_EXECUTE,
                AuthorityGrant.ARTIFACT_TRANSFER,
            ),
            session_id="test-auth-bypass",
            operator_session_id="test-auth-bypass",
        ),
        now + timedelta(hours=1),
        now + timedelta(hours=1),
    )


async def create_session(*, replace_session_id: str | None = None) -> tuple[str, AuthenticatedOperator]:
    require_auth_configured()
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    async with get_session() as db:
        absolute_expires_at = now + timedelta(seconds=settings.operator_auth_absolute_seconds)
        if replace_session_id:
            old = await db.get(OperatorSession, replace_session_id)
            if old is None or old.revoked_at is not None:
                raise AuthFailure("session_revoked")
            absolute_expires_at = _aware(old.absolute_expires_at)
            if now >= absolute_expires_at:
                raise AuthFailure("session_expired")
        record = OperatorSession(
            token_hash=_token_hash(token),
            idle_expires_at=min(
                now + timedelta(seconds=settings.operator_auth_idle_seconds), absolute_expires_at
            ),
            absolute_expires_at=absolute_expires_at,
        )
        if replace_session_id:
            claimed = await db.execute(
                update(OperatorSession)
                .where(OperatorSession.id == replace_session_id, OperatorSession.revoked_at.is_(None))
                .values(revoked_at=now, replaced_by_id=record.id)
            )
            if claimed.rowcount != 1:
                raise AuthFailure("session_revoked")
        db.add(record)
    return token, AuthenticatedOperator(
        record.id, _principal(record.id), record.idle_expires_at, record.absolute_expires_at
    )


def _touch_is_due(record: OperatorSession, now: datetime) -> bool:
    last_seen_at = _aware(record.last_seen_at)
    configured_idle_seconds = max(float(settings.operator_auth_idle_seconds), 0.001)
    interval = min(
        _AUTH_TOUCH_INTERVAL,
        timedelta(seconds=configured_idle_seconds / 2),
    )
    return now - last_seen_at >= interval


def _operator_for_record(record: OperatorSession) -> AuthenticatedOperator:
    return AuthenticatedOperator(
        record.id,
        _principal(record.id),
        _aware(record.idle_expires_at),
        _aware(record.absolute_expires_at),
    )


async def _find_token_record(
    db,
    token_hash: str,
    now: datetime,
) -> tuple[OperatorSession | None, str | None]:
    result = await db.execute(
        select(OperatorSession).where(OperatorSession.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()
    if record is None:
        return None, "authentication_required"
    if record.revoked_at is not None:
        return None, "session_revoked"
    idle_expires_at = _aware(record.idle_expires_at)
    absolute_expires_at = _aware(record.absolute_expires_at)
    if now >= idle_expires_at or now >= absolute_expires_at:
        # The caller deliberately lets this context exit normally so the
        # revocation is committed before the failure is returned.
        record.revoked_at = now
        db.add(record)
        return None, "session_expired"
    return record, None


async def _find_session_record(
    db,
    session_id: str,
    now: datetime,
) -> tuple[OperatorSession | None, str | None]:
    current_id = session_id
    visited: set[str] = set()
    for _ in range(8):
        if not current_id or current_id in visited:
            return None, "session_revoked"
        visited.add(current_id)
        record = await db.get(OperatorSession, current_id)
        if record is None:
            return None, "authentication_required"
        if record.revoked_at is not None:
            if record.replaced_by_id:
                current_id = record.replaced_by_id
                continue
            return None, "session_revoked"
        idle_expires_at = _aware(record.idle_expires_at)
        absolute_expires_at = _aware(record.absolute_expires_at)
        if now >= idle_expires_at or now >= absolute_expires_at:
            # See _find_token_record: expiry revocation must be committed by
            # the normal session-context exit before the error is raised.
            record.revoked_at = now
            db.add(record)
            return None, "session_expired"
        return record, None
    return None, "session_revoked"


async def _touch_token(token_hash: str) -> AuthenticatedOperator:
    async with _auth_touch_lock():
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            record, error = await _find_token_record(db, token_hash, now)
            if record is not None and _touch_is_due(record, now):
                absolute_expires_at = _aware(record.absolute_expires_at)
                record.last_seen_at = now
                record.idle_expires_at = min(
                    now + timedelta(seconds=settings.operator_auth_idle_seconds),
                    absolute_expires_at,
                )
                db.add(record)
            operator = _operator_for_record(record) if record is not None else None
        if error:
            raise AuthFailure(error)
        assert operator is not None
        return operator


async def authenticate_token(token: str | None, *, touch: bool = True) -> AuthenticatedOperator:
    if not token:
        raise AuthFailure("authentication_required")
    token_hash = _token_hash(token)
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        record, error = await _find_token_record(db, token_hash, now)
        operator = _operator_for_record(record) if record is not None else None
        touch_due = bool(record is not None and touch and _touch_is_due(record, now))
    if error:
        raise AuthFailure(error)
    assert operator is not None
    if touch_due:
        return await _touch_token(token_hash)
    return operator


async def authenticate_session(
    session_id: str | None,
    *,
    touch: bool = True,
) -> AuthenticatedOperator:
    """Validate a live session, following refresh replacements for WS continuity.

    A rotated bearer token remains revoked when presented to ``authenticate_token``.
    An already-authenticated WebSocket, however, is bound to the server-issued
    session identity rather than retaining the old token forever.  Following the
    bounded replacement chain lets that socket survive the normal refresh
    rotation while explicit logout/revocation still closes it.
    """
    if not session_id:
        raise AuthFailure("authentication_required")
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        record, error = await _find_session_record(db, session_id, now)
        operator = _operator_for_record(record) if record is not None else None
        touch_due = bool(record is not None and touch and _touch_is_due(record, now))
    if error:
        raise AuthFailure(error)
    assert operator is not None
    if touch_due:
        return await _touch_session(session_id)
    return operator


async def _touch_session(session_id: str) -> AuthenticatedOperator:
    async with _auth_touch_lock():
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            record, error = await _find_session_record(db, session_id, now)
            if record is not None and _touch_is_due(record, now):
                absolute_expires_at = _aware(record.absolute_expires_at)
                record.last_seen_at = now
                record.idle_expires_at = min(
                    now + timedelta(seconds=settings.operator_auth_idle_seconds),
                    absolute_expires_at,
                )
                db.add(record)
            operator = _operator_for_record(record) if record is not None else None
        if error:
            raise AuthFailure(error)
        assert operator is not None
        return operator


async def revoke_session(session_id: str) -> None:
    async with get_session() as db:
        record = await db.get(OperatorSession, session_id)
        if record and record.revoked_at is None:
            record.revoked_at = datetime.now(timezone.utc)
            db.add(record)
