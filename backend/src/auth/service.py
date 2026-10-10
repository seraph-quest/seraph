from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import re
import hashlib
import hmac
import secrets
import asyncio
import threading
import weakref

from sqlalchemy import select, update

from config.settings import settings
from src.db.engine import get_session, original_auth_header_budget
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
    # Captured from the authenticated request only.  It is deliberately
    # private, excluded from repr/equality, and never serialized into an
    # operator principal, response, audit event, or authority envelope.
    _token_hash: str | None = field(default=None, repr=False, compare=False)
    ownership_continuity: str = field(default="stable", compare=False)
    ownership_recovery_action: str | None = field(default=None, compare=False)
    operator_identity_id: str | None = field(default=None, compare=False)
    _continuity_token: str | None = field(default=None, repr=False, compare=False)


_OWNERSHIP_STABLE = "stable"
_OWNERSHIP_LEGACY = "legacy_rebind_required"
_OWNERSHIP_RECOVERY_ACTION = "review_and_recreate_work_in_current_scope"


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


def _principal(session_id: str, principal_id: str) -> TrustPrincipal:
    if not principal_id or not re.fullmatch(r"operator:root:[A-Za-z0-9_-]{7,114}", principal_id):
        raise AuthFailure("principal_migration_required")
    return TrustPrincipal(
        principal_id=principal_id,
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


async def create_session(
    *,
    replace_session_id: str | None = None,
    expected_token_hash: str | None = None,
    continuity_token: str | None = None,
    recovery_code: str | None = None,
) -> tuple[str, AuthenticatedOperator]:
    """Create a root session or atomically rotate its bearer in place.

    Refresh is intentionally a credential CAS, not an owner-id replacement.
    The retired hash is kept in a revoked tombstone row so a late old bearer
    remains distinguishable from an unknown credential without becoming a
    durable owner authority.
    """

    require_auth_configured()
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    if replace_session_id and not expected_token_hash:
        raise AuthFailure("authentication_required")

    async with get_session() as db:
        absolute_expires_at = now + timedelta(seconds=settings.operator_auth_absolute_seconds)
        if not replace_session_id:
            identity_id = None
            rotated_continuity = None
            if continuity_token or recovery_code:
                from src.auth.ownership import bind_login_identity
                identity_id, rotated_continuity = await bind_login_identity(
                    db, continuity_token=continuity_token, recovery_code=recovery_code,
                )
            record = OperatorSession(
                token_hash=token_hash,
                idle_expires_at=min(
                    now + timedelta(seconds=settings.operator_auth_idle_seconds),
                    absolute_expires_at,
                ),
                absolute_expires_at=absolute_expires_at,
                is_bearer_tombstone=False,
                operator_identity_id=identity_id,
            )
            db.add(record)
            await db.flush()
        else:
            current = await db.get(OperatorSession, replace_session_id)
            if current is None or current.is_bearer_tombstone is not False or current.revoked_at is not None:
                raise AuthFailure("session_revoked")
            absolute_expires_at = _aware(current.absolute_expires_at)
            if now >= absolute_expires_at or now >= _aware(current.idle_expires_at):
                raise AuthFailure("session_expired")
            claimed = await db.execute(
                update(OperatorSession)
                .where(
                    OperatorSession.id == replace_session_id,
                    OperatorSession.token_hash == expected_token_hash,
                    OperatorSession.revoked_at.is_(None),
                    OperatorSession.is_bearer_tombstone.is_(False),
                    OperatorSession.idle_expires_at > now,
                    OperatorSession.absolute_expires_at > now,
                )
                .values(
                    token_hash=token_hash,
                    last_seen_at=now,
                    idle_expires_at=min(
                        now + timedelta(seconds=settings.operator_auth_idle_seconds),
                        absolute_expires_at,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise AuthFailure("session_revoked")
            # Force the new unique hash into the active row before inserting
            # the retired hash.  This makes a failed tombstone insert roll
            # back both changes instead of committing a partial rotation.
            await db.flush()
            tombstone = OperatorSession(
                token_hash=expected_token_hash,
                idle_expires_at=_aware(current.idle_expires_at),
                absolute_expires_at=absolute_expires_at,
                revoked_at=now,
                replaced_by_id=replace_session_id,
                is_bearer_tombstone=True,
            )
            db.add(tombstone)
            await db.flush()
            current.token_hash = token_hash
            current.last_seen_at = now
            current.idle_expires_at = min(
                now + timedelta(seconds=settings.operator_auth_idle_seconds),
                absolute_expires_at,
            )
            record = current

        continuity, recovery_action = await _ownership_metadata(db, record)
        operator = _operator_for_record(
            record,
            token_hash=token_hash,
            ownership_continuity=continuity,
            ownership_recovery_action=recovery_action,
        )
        if not replace_session_id and rotated_continuity:
            from dataclasses import replace
            operator = replace(operator, _continuity_token=rotated_continuity)
    return token, operator


def _touch_is_due(record: OperatorSession, now: datetime) -> bool:
    last_seen_at = _aware(record.last_seen_at)
    configured_idle_seconds = max(float(settings.operator_auth_idle_seconds), 0.001)
    interval = min(
        _AUTH_TOUCH_INTERVAL,
        timedelta(seconds=configured_idle_seconds / 2),
    )
    return now - last_seen_at >= interval


def _operator_for_record(
    record: OperatorSession,
    *,
    token_hash: str | None = None,
    ownership_continuity: str = _OWNERSHIP_STABLE,
    ownership_recovery_action: str | None = None,
) -> AuthenticatedOperator:
    return AuthenticatedOperator(
        record.id,
        _principal(record.id, record.principal_id),
        _aware(record.idle_expires_at),
        _aware(record.absolute_expires_at),
        token_hash,
        ownership_continuity,
        ownership_recovery_action,
        record.operator_identity_id,
    )


async def _ownership_metadata(
    db,
    record: OperatorSession,
) -> tuple[str, str | None]:
    """Classify one active root with bounded, reverse-link lookups.

    New refreshes create revoked tombstone predecessors and keep the active id
    stable. A non-tombstone predecessor is legacy ownership metadata and an
    unrevoked tombstone is an impossible durable state; both require explicit
    recovery. The queries deliberately inspect only rows pointing at this
    root, so unrelated malformed or high-volume session history cannot affect
    a healthy owner request.
    """

    # ``False`` and ``None`` are intentionally distinct. A null marker is an
    # incomplete/legacy migration state, and even an empty replacement string
    # is malformed metadata rather than an active root.
    if record.is_bearer_tombstone is not False or record.replaced_by_id is not None:
        return _OWNERSHIP_LEGACY, _OWNERSHIP_RECOVERY_ACTION
    budget = db.info.get("auth_session_budget")
    if budget is not None:
        from src.memory.header_bounds import OPERATOR_SESSION
        await budget.certify_all(db, OPERATOR_SESSION)
        budget.debit(3 * (6 * 512 + 2) + 2, appearance=("auth-reverse-projection", record.id))
    try:
        non_tombstone_predecessors = (
            await db.execute(
                select(OperatorSession.id)
                .where(
                    OperatorSession.replaced_by_id == record.id,
                    OperatorSession.is_bearer_tombstone.is_(False),
                )
                .limit(2)
            )
        ).scalars().all()
        if non_tombstone_predecessors:
            return _OWNERSHIP_LEGACY, _OWNERSHIP_RECOVERY_ACTION

        # A tombstone must be revoked. Keep this second lookup bounded and
        # separate so ordinary, correctly-revoked refresh tombstones preserve
        # stable ownership while impossible rows fail closed.
        impossible_tombstone = (
            await db.execute(
                select(OperatorSession.id)
                .where(
                    OperatorSession.replaced_by_id == record.id,
                    OperatorSession.is_bearer_tombstone.is_(True),
                    OperatorSession.revoked_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if impossible_tombstone is not None:
            return _OWNERSHIP_LEGACY, _OWNERSHIP_RECOVERY_ACTION
    except Exception:
        return _OWNERSHIP_LEGACY, _OWNERSHIP_RECOVERY_ACTION

    return _OWNERSHIP_STABLE, None


async def _operator_from_record(
    db,
    record: OperatorSession,
    *,
    token_hash: str | None = None,
) -> AuthenticatedOperator:
    continuity, recovery_action = await _ownership_metadata(db, record)
    return _operator_for_record(
        record,
        token_hash=token_hash,
        ownership_continuity=continuity,
        ownership_recovery_action=recovery_action,
    )


@asynccontextmanager
async def _original_session_operation():
    """Original Auth lifetime, one frame before inventory or any Root body."""
    from src.memory.header_bounds import HeaderReadBudget
    from src.runtime_plugins.ownership import begin_native_writer
    budget = HeaderReadBudget()
    async with _auth_touch_lock():
        with original_auth_header_budget(budget):
            async with get_session() as db:
                if db.info.get("composition_read_guard") is not None:
                    guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
                    db.info["auth_session_budget"] = budget
                    if guard is None:
                        raise RuntimeError("composition_native_writer_required")
                yield db


async def _session_body_cover(db, *, session_id=None, token_hash=None):
    budget = db.info.get("auth_session_budget")
    if budget is None:
        return
    from src.memory.header_bounds import OPERATOR_SESSION
    from src.memory.composition_headers import locate_exact_rows, locate_operator_token
    identities = (await locate_operator_token(db, token_hash, budget) if token_hash is not None
        else await locate_exact_rows(db, OPERATOR_SESSION, session_id, budget))
    await budget.certify(db, OPERATOR_SESSION, identities)


async def _original_session_change(db, record, changes):
    if db.info.get("auth_session_budget") is not None:
        await db.info["composition_guard"].reserve_session_mutation(record, changes)
    for name, value in changes.items():
        setattr(record, name, value)
    db.add(record)


async def _original_due_touch(db, record, now):
    if record is not None and _touch_is_due(record, now):
        await _original_session_change(db, record, {"last_seen_at": now,
            "idle_expires_at": min(now + timedelta(seconds=settings.operator_auth_idle_seconds),
                _aware(record.absolute_expires_at))})


async def _find_token_record(
    db,
    token_hash: str,
    now: datetime,
) -> tuple[OperatorSession | None, str | None]:
    await _session_body_cover(db, token_hash=token_hash)
    result = await db.execute(
        select(OperatorSession).where(OperatorSession.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()
    if record is None:
        return None, "authentication_required"
    if (
        record.is_bearer_tombstone is not False
        or record.revoked_at is not None
        or record.replaced_by_id is not None
    ):
        return None, "session_revoked"
    idle_expires_at = _aware(record.idle_expires_at)
    absolute_expires_at = _aware(record.absolute_expires_at)
    if now >= idle_expires_at or now >= absolute_expires_at:
        # The caller deliberately lets this context exit normally so the
        # revocation is committed before the failure is returned.
        await _original_session_change(db, record, {"revoked_at": now})
        return None, "session_expired"
    return record, None


async def _find_session_record(
    db,
    session_id: str,
    now: datetime,
    *,
    follow_replacements: bool = False,
) -> tuple[OperatorSession | None, str | None]:
    current_id = session_id
    visited: set[str] = set()
    for _ in range(8):
        if not current_id or current_id in visited:
            return None, "session_revoked"
        visited.add(current_id)
        await _session_body_cover(db, session_id=current_id)
        record = await db.get(OperatorSession, current_id)
        if record is None:
            return None, "authentication_required"
        # Tombstones hold retired bearer hashes only.  They are never owner
        # authorities, including for the narrow WebSocket continuity path.
        if record.is_bearer_tombstone is not False:
            return None, "session_revoked"
        if record.replaced_by_id is not None and record.revoked_at is None:
            return None, "session_revoked"
        if record.revoked_at is not None:
            if follow_replacements and record.replaced_by_id:
                current_id = record.replaced_by_id
                continue
            return None, "session_revoked"
        idle_expires_at = _aware(record.idle_expires_at)
        absolute_expires_at = _aware(record.absolute_expires_at)
        if now >= idle_expires_at or now >= absolute_expires_at:
            # See _find_token_record: expiry revocation must be committed by
            # the normal session-context exit before the error is raised.
            await _original_session_change(db, record, {"revoked_at": now})
            return None, "session_expired"
        return record, None
    return None, "session_revoked"


async def _touch_token(token_hash: str) -> AuthenticatedOperator:
    async with _original_session_operation() as db:
        now = datetime.now(timezone.utc)
        record, error = await _find_token_record(db, token_hash, now)
        operator = await _operator_from_record(db, record, token_hash=token_hash) if record is not None else None
        await _original_due_touch(db, record, now)
        if record is not None:
            operator = _operator_for_record(record, token_hash=token_hash,
                ownership_continuity=operator.ownership_continuity,
                ownership_recovery_action=operator.ownership_recovery_action)
    if error:
        raise AuthFailure(error)
    assert operator is not None
    return operator


async def authenticate_token(token: str | None, *, touch: bool = True) -> AuthenticatedOperator:
    if not token:
        raise AuthFailure("authentication_required")
    token_hash = _token_hash(token)
    async with _original_session_operation() as db:
        now = datetime.now(timezone.utc)
        record, error = await _find_token_record(db, token_hash, now)
        operator = (
            await _operator_from_record(db, record, token_hash=token_hash)
            if record is not None
            else None
        )
        touch_due = bool(record is not None and touch and _touch_is_due(record, now))
        if touch_due and db.info.get("auth_session_budget") is not None:
            await _original_due_touch(db, record, now)
            operator = _operator_for_record(record, token_hash=token_hash,
                ownership_continuity=operator.ownership_continuity,
                ownership_recovery_action=operator.ownership_recovery_action)
            touch_due = False
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
    """Validate the exact active, non-tombstone owner session.

    Durable callers must never follow replacement aliases.  WebSocket
    continuity uses the separately named ``authenticate_websocket_session``
    helper below and is deliberately kept out of this owner-validation path.
    """
    return await _authenticate_session(session_id, touch=touch, follow_replacements=False)


async def authenticate_websocket_session(
    session_id: str | None,
    *,
    touch: bool = True,
) -> AuthenticatedOperator:
    """Validate an accepted WebSocket's bounded replacement continuity.

    This helper is only for the two existing WebSocket validation sites.  It
    may follow a legacy revoked row to its server-recorded replacement so an
    already accepted socket survives bearer rotation; it cannot authenticate
    tombstones and must not be used for durable owner checks.
    """
    return await _authenticate_session(session_id, touch=touch, follow_replacements=True)


async def _authenticate_session(
    session_id: str | None,
    *,
    touch: bool,
    follow_replacements: bool,
) -> AuthenticatedOperator:
    if not session_id:
        raise AuthFailure("authentication_required")
    async with _original_session_operation() as db:
        now = datetime.now(timezone.utc)
        record, error = await _find_session_record(
            db,
            session_id,
            now,
            follow_replacements=follow_replacements,
        )
        operator = await _operator_from_record(db, record) if record is not None else None
        touch_due = bool(record is not None and touch and _touch_is_due(record, now))
        if touch_due and db.info.get("auth_session_budget") is not None:
            await _original_due_touch(db, record, now)
            operator = _operator_for_record(record,
                ownership_continuity=operator.ownership_continuity,
                ownership_recovery_action=operator.ownership_recovery_action)
            touch_due = False
    if error:
        raise AuthFailure(error)
    assert operator is not None
    if touch_due:
        return await _touch_session(session_id, follow_replacements=follow_replacements)
    return operator


async def _touch_session(session_id: str, *, follow_replacements: bool = False) -> AuthenticatedOperator:
    async with _original_session_operation() as db:
        now = datetime.now(timezone.utc)
        record, error = await _find_session_record(db, session_id, now, follow_replacements=follow_replacements)
        operator = await _operator_from_record(db, record) if record is not None else None
        await _original_due_touch(db, record, now)
        if record is not None:
            operator = _operator_for_record(record,
                ownership_continuity=operator.ownership_continuity,
                ownership_recovery_action=operator.ownership_recovery_action)
    if error:
        raise AuthFailure(error)
    assert operator is not None
    return operator


async def revoke_session(session_id: str) -> None:
    async with _original_session_operation() as db:
        await _session_body_cover(db, session_id=session_id)
        record = await db.get(OperatorSession, session_id)
        if record and record.is_bearer_tombstone is False and record.revoked_at is None:
            await _original_session_change(db, record, {"revoked_at": datetime.now(timezone.utc)})


async def authenticate_principal(principal_id: str, *, db=None) -> AuthenticatedOperator:
    """Recheck server-bound device authority without adopting role metadata.

    Device credentials are separate ingress proofs. They may act only while
    their exact persisted operator root is live; this lookup is no login alias.
    """
    if principal_id == 'operator:test-bypass':
        return test_bypass_operator()
    if db is None:
        async with get_session() as session:
            return await authenticate_principal(principal_id, db=session)
    record = (await db.execute(select(OperatorSession).where(
        OperatorSession.principal_id == principal_id,
        OperatorSession.is_bearer_tombstone.is_(False),
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if record is None or record.revoked_at is not None or now >= _aware(record.idle_expires_at) or now >= _aware(record.absolute_expires_at):
        raise AuthFailure('session_revoked')
    return await _operator_from_record(db, record)
