from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import re
import hashlib
import hmac
import secrets
import asyncio
import time
import logging
import threading
import weakref

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from config.settings import settings
from src.db.engine import bound_lock_wait, get_session, is_database_locked
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


# Local single-operator mode without login (#1161).  The persisted root row is
# found by this marker in ``token_hash``.  Real bearer hashes are 64-character
# SHA-256 hex digests, so no presented cookie can ever hash to the marker and
# the root has no bearer credential at all.
LOCAL_ROOT_TOKEN_MARKER = "local-no-login-root"
# The local root has no idle or absolute expiry: identity and ownership stay
# stable across restarts while the mode stays enabled.
LOCAL_ROOT_EXPIRY = datetime(2999, 1, 1, tzinfo=timezone.utc)
AUTH_MODE_PASSWORD = "password"
AUTH_MODE_LOCAL_NO_LOGIN = "local_no_login"
AUTH_MODE_TEST_BYPASS = "test_bypass"
AUTH_MODE_UNCONFIGURED = "unconfigured"


def local_no_login_active() -> bool:
    """Whether the opt-in loopback-only no-login mode is in effect.

    Startup validation refuses an invalid combination; this predicate repeats
    the environment and credential conditions so a later settings change can
    never widen the mode beyond development without a password.
    """
    return (
        bool(settings.operator_auth_local_no_login)
        and not auth_enabled()
        and settings.deployment_environment.strip().lower() == "development"
    )


def auth_mode() -> str:
    if auth_enabled():
        return AUTH_MODE_PASSWORD
    if local_no_login_active():
        return AUTH_MODE_LOCAL_NO_LOGIN
    if (
        settings.deployment_environment == "test"
        and settings.operator_auth_allow_unauthenticated_tests
    ):
        return AUTH_MODE_TEST_BYPASS
    return AUTH_MODE_UNCONFIGURED


def auth_mode_status() -> dict:
    """Non-sensitive auth-mode projection for runtime status and audit."""
    mode = auth_mode()
    return {
        "mode": mode,
        "local_no_login": mode == AUTH_MODE_LOCAL_NO_LOGIN,
        "login_required": mode == AUTH_MODE_PASSWORD,
    }


_LOOPBACK_NAMES = {"localhost"}


def is_loopback_host(value: str | None) -> bool:
    """True for ``localhost`` or a literal loopback IPv4/IPv6 address."""
    import ipaddress

    raw = str(value or "").strip().lower().rstrip(".")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if not raw:
        return False
    if raw in _LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def _argv_options(argv: list[str]) -> dict[str, str | bool | None]:
    """Parse the uvicorn bind-relevant options from the process arguments."""
    options: dict[str, str | bool | None] = {
        "host": None,
        "inherited": False,
        "workers": None,
        "forwarded_allow_ips": None,
    }
    valued = {"--host": "host", "--workers": "workers", "--forwarded-allow-ips": "forwarded_allow_ips"}
    for index, item in enumerate(argv):
        if item in valued and index + 1 < len(argv):
            options[valued[item]] = argv[index + 1]
            continue
        name, separator, value = item.partition("=")
        if separator and name in valued:
            options[valued[name]] = value
        elif name in {"--fd", "--uds"}:
            options["inherited"] = True
    return options


def _is_uvicorn_cli(argv: list[str]) -> bool:
    """``uvicorn ...`` or ``python -m uvicorn ...`` (argv[0] is its __main__)."""
    import os

    if not argv:
        return False
    launcher = argv[0].replace("\\", "/")
    return os.path.basename(launcher) == "uvicorn" or launcher.endswith("/uvicorn/__main__.py")


def _is_gunicorn(argv: list[str], environ) -> bool:
    import os

    return bool(
        (argv and "gunicorn" in os.path.basename(argv[0]))
        or "GUNICORN_CMD_ARGS" in environ
        or str(environ.get("SERVER_SOFTWARE", "")).lower().startswith("gunicorn")
    )


def _loopback_only_list(value: str) -> bool:
    entries = [item.strip() for item in value.split(",") if item.strip()]
    return bool(entries) and all(is_loopback_host(item) for item in entries)


def local_bind_error(argv: list[str] | None = None, environ=None) -> str | None:
    """Explain why the backend bind is not provably loopback-only.

    Uvicorn binds its listener only after application startup, so the real
    socket cannot be inspected here. The check therefore fails closed: the
    mode is accepted only for a recognised uvicorn command line with an
    explicit loopback ``--host``, a single worker and loopback-only proxy
    trust. ``uvicorn.run(...)`` from Python, gunicorn, inherited ``--fd`` or
    ``--uds`` listeners and a host taken only from ``UVICORN_HOST`` are all
    refused because their bind is invisible to this process.
    """
    import os
    import sys

    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ
    prefix = "OPERATOR_AUTH_LOCAL_NO_LOGIN"
    configured = settings.backend_bind_host.strip()
    if not configured:
        return (
            f"{prefix} requires BACKEND_BIND_HOST to name the loopback listener "
            "(the managed local launcher sets 127.0.0.1)"
        )
    if not is_loopback_host(configured):
        return f"{prefix} requires a loopback bind, not {configured!r}"
    if _is_gunicorn(argv, environ):
        return f"{prefix} cannot verify a gunicorn bind; use ./manage.sh -e dev local"
    if not _is_uvicorn_cli(argv):
        return (
            f"{prefix} requires the uvicorn command line with an explicit loopback "
            "--host (use ./manage.sh -e dev local)"
        )
    options = _argv_options(argv)
    if options["inherited"]:
        return f"{prefix} cannot verify an inherited --fd/--uds listener"
    argv_host = options["host"]
    if argv_host is None:
        return f"{prefix} requires an explicit loopback --host on the uvicorn command line"
    if not is_loopback_host(str(argv_host)):
        return f"{prefix} requires a loopback bind, not --host {argv_host!r}"
    env_host = str(environ.get("UVICORN_HOST", "") or "").strip()
    if env_host and not is_loopback_host(env_host):
        return f"{prefix} requires a loopback bind, not UVICORN_HOST={env_host!r}"
    workers = options["workers"] or environ.get("UVICORN_WORKERS") or environ.get("WEB_CONCURRENCY")
    if workers not in (None, ""):
        try:
            worker_count = int(str(workers))
        except ValueError:
            return f"{prefix} cannot verify worker count {workers!r}"
        if worker_count > 1:
            return f"{prefix} requires a single backend worker"
    forwarded = options["forwarded_allow_ips"]
    if forwarded is None:
        forwarded = environ.get("FORWARDED_ALLOW_IPS")
    if forwarded is not None and not _loopback_only_list(str(forwarded)):
        return f"{prefix} requires loopback-only FORWARDED_ALLOW_IPS/--forwarded-allow-ips"
    return None


def _validate_local_no_login() -> None:
    environment = settings.deployment_environment.strip().lower()
    if environment != "development":
        raise RuntimeError(
            "OPERATOR_AUTH_LOCAL_NO_LOGIN is permitted only when DEPLOYMENT_ENVIRONMENT=development"
        )
    if settings.operator_auth_secret or settings.operator_auth_secret_hash:
        raise RuntimeError(
            "OPERATOR_AUTH_LOCAL_NO_LOGIN cannot be combined with OPERATOR_AUTH_SECRET or OPERATOR_AUTH_SECRET_HASH"
        )
    if settings.operator_auth_allow_unauthenticated_tests:
        raise RuntimeError("OPERATOR_AUTH_LOCAL_NO_LOGIN cannot be combined with the test auth bypass")
    bind_error = local_bind_error()
    if bind_error:
        raise RuntimeError(bind_error)
    from urllib.parse import urlsplit

    hosts = [item.strip() for item in settings.operator_auth_allowed_hosts.split(",") if item.strip()]
    if not any(is_loopback_host(urlsplit(f"//{item}").hostname) for item in hosts):
        raise RuntimeError("OPERATOR_AUTH_LOCAL_NO_LOGIN requires a loopback host in OPERATOR_AUTH_ALLOWED_HOSTS")
    origins = [item.strip() for item in settings.operator_auth_allowed_origins.split(",") if item.strip()]
    if not any(is_loopback_host(urlsplit(item).hostname) for item in origins):
        raise RuntimeError("OPERATOR_AUTH_LOCAL_NO_LOGIN requires a loopback origin in OPERATOR_AUTH_ALLOWED_ORIGINS")


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
    if settings.operator_auth_local_no_login:
        _validate_local_no_login()


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
# The touch and the expiry revocation are bookkeeping writes on an already
# validated read. Under a foreign SQLite writer they wait this long and then
# defer instead of holding every authenticated request for the busy timeout.
_AUTH_BOOKKEEPING_WAIT_MS = 250
# After a deferral, skip touches briefly so sustained contention costs one
# bounded wait per interval rather than one per poll. Validation is unaffected.
_AUTH_TOUCH_BACKOFF_SECONDS = 2.0
_auth_touch_retry_after = 0.0
_AUTH_TOUCH_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_AUTH_TOUCH_LOCKS_GUARD = threading.Lock()


logger = logging.getLogger(__name__)


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
    if _is_local_root(record):
        # The local root has no idle window to slide.
        return False
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
        record.revoked_at = now
        db.add(record)
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
        record = await db.get(OperatorSession, current_id)
        if record is None:
            return None, "authentication_required"
        if _is_local_root(record) and not local_no_login_active():
            # The no-login root carries authority only while its mode is on.
            return None, "session_revoked"
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
            record.revoked_at = now
            db.add(record)
            return None, "session_expired"
        return record, None
    return None, "session_revoked"


async def _commit_auth_bookkeeping(db) -> bool:
    """Commit a touch or expiry revocation; lock contention defers it.

    Callers have already enforced revocation and both expiries on this read,
    so only the bookkeeping write is best-effort. A deferred touch leaves the
    stored idle expiry unchanged (the session is not extended this time) and a
    deferred expiry revocation still fails the request closed.
    """
    global _auth_touch_retry_after
    await bound_lock_wait(db, _AUTH_BOOKKEEPING_WAIT_MS)
    try:
        await db.commit()
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        await db.rollback()
        _auth_touch_retry_after = time.monotonic() + _AUTH_TOUCH_BACKOFF_SECONDS
        logger.info("operator session bookkeeping deferred: database busy")
        return False
    return True


def _touch_admitted() -> bool:
    """A touch is neither in flight nor backing off after a deferral."""
    return not _auth_touch_lock().locked() and time.monotonic() >= _auth_touch_retry_after


async def _touch_record(
    db,
    record: OperatorSession | None,
    now: datetime,
    *,
    token_hash: str | None = None,
) -> AuthenticatedOperator | None:
    if record is None:
        await _commit_auth_bookkeeping(db)
        return None
    # Classify ownership before the touch is pending so its read queries
    # cannot autoflush the write (and misreport contention as legacy state).
    continuity, recovery_action = await _ownership_metadata(db, record)

    def operator() -> AuthenticatedOperator:
        return _operator_for_record(
            record,
            token_hash=token_hash,
            ownership_continuity=continuity,
            ownership_recovery_action=recovery_action,
        )

    validated = operator()
    if not _touch_is_due(record, now):
        await _commit_auth_bookkeeping(db)
        return validated
    record.last_seen_at = now
    record.idle_expires_at = min(
        now + timedelta(seconds=settings.operator_auth_idle_seconds),
        _aware(record.absolute_expires_at),
    )
    db.add(record)
    return operator() if await _commit_auth_bookkeeping(db) else validated


async def _touch_token(token_hash: str) -> AuthenticatedOperator:
    async with _auth_touch_lock():
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            record, error = await _find_token_record(db, token_hash, now)
            operator = await _touch_record(db, record, now, token_hash=token_hash)
        if error:
            raise AuthFailure(error)
        assert operator is not None
        return operator


async def authenticate_home_token_readonly(token: str | None) -> AuthenticatedOperator:
    """Authenticate the exact passive Home GET without expiry or idle writes."""
    if not token:
        raise AuthFailure("authentication_required")
    token_hash = _token_hash(token)
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        from sqlalchemy import text
        await db.execute(text("BEGIN"))
        record = (await db.execute(select(OperatorSession).where(
            OperatorSession.token_hash == token_hash))).scalar_one_or_none()
        if record is None:
            raise AuthFailure("authentication_required")
        if (record.is_bearer_tombstone is not False or record.revoked_at is not None
                or record.replaced_by_id is not None):
            raise AuthFailure("session_revoked")
        if now >= _aware(record.idle_expires_at) or now >= _aware(record.absolute_expires_at):
            raise AuthFailure("session_expired")
        return await _operator_from_record(db, record, token_hash=token_hash)


async def authenticate_token(token: str | None, *, touch: bool = True) -> AuthenticatedOperator:
    if not token:
        raise AuthFailure("authentication_required")
    token_hash = _token_hash(token)
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        record, error = await _find_token_record(db, token_hash, now)
        if record is None:
            await _commit_auth_bookkeeping(db)
        operator = (
            await _operator_from_record(db, record, token_hash=token_hash)
            if record is not None
            else None
        )
        touch_due = bool(record is not None and touch and _touch_is_due(record, now))
    if error:
        raise AuthFailure(error)
    assert operator is not None
    if touch_due and _touch_admitted():
        return await _touch_token(token_hash)
    # A touch in flight (or just deferred) covers this validated request;
    # queueing every request behind one stalled bookkeeping write is #702.
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
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        record, error = await _find_session_record(
            db,
            session_id,
            now,
            follow_replacements=follow_replacements,
        )
        if record is None:
            await _commit_auth_bookkeeping(db)
        operator = await _operator_from_record(db, record) if record is not None else None
        touch_due = bool(record is not None and touch and _touch_is_due(record, now))
    if error:
        raise AuthFailure(error)
    assert operator is not None
    if touch_due and _touch_admitted():
        return await _touch_session(session_id, follow_replacements=follow_replacements)
    return operator


async def _touch_session(
    session_id: str,
    *,
    follow_replacements: bool = False,
) -> AuthenticatedOperator:
    async with _auth_touch_lock():
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            record, error = await _find_session_record(
                db,
                session_id,
                now,
                follow_replacements=follow_replacements,
            )
            operator = await _touch_record(db, record, now)
        if error:
            raise AuthFailure(error)
        assert operator is not None
        return operator


async def revoke_session(session_id: str) -> None:
    async with get_session() as db:
        record = await db.get(OperatorSession, session_id)
        if record and record.is_bearer_tombstone is False and record.revoked_at is None:
            record.revoked_at = datetime.now(timezone.utc)
            db.add(record)

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
    if _is_local_root(record) and not local_no_login_active():
        raise AuthFailure('session_revoked')
    return await _operator_from_record(db, record)


def _is_local_root(record: OperatorSession) -> bool:
    token_hash = str(getattr(record, "token_hash", "") or "")
    return token_hash == LOCAL_ROOT_TOKEN_MARKER or token_hash.startswith(LOCAL_ROOT_TOKEN_MARKER + ":")


def local_root_marker_valid(token_hash: str | None) -> bool:
    """Shared current-root fence rule for the local no-login root (#1175).

    A root row (or operator proof) whose ``token_hash`` is the local-root
    marker is a current-root proof only while local no-login mode is active.
    Every other value, including real bearer hashes, is left to the caller's
    own comparison.  Every current-root/owner fence applies this rule, so a
    stale local operator context is refused once the mode is switched off,
    even before startup reconciliation retires the row.
    """
    return token_hash != LOCAL_ROOT_TOKEN_MARKER or local_no_login_active()


def _local_root_usable(record: OperatorSession, now: datetime) -> bool:
    return (
        record.is_bearer_tombstone is False
        and record.revoked_at is None
        and record.replaced_by_id is None
        and now < _aware(record.idle_expires_at)
        and now < _aware(record.absolute_expires_at)
    )


_LOCAL_ROOT_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _local_root_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _AUTH_TOUCH_LOCKS_GUARD:
        lock = _LOCAL_ROOT_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _LOCAL_ROOT_LOCKS[loop] = lock
        return lock


async def _read_local_root(db) -> OperatorSession | None:
    return (
        await db.execute(
            select(OperatorSession)
            .where(OperatorSession.token_hash == LOCAL_ROOT_TOKEN_MARKER)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _local_operator_from_record(db, record: OperatorSession) -> AuthenticatedOperator:
    """Operator context for the local root with its current-root proof.

    A password request carries the hash of the presented bearer, and every
    owner recheck (``ownership._current_root``, ``assert_current_root`` and
    the durable owner fences) compares it with the root row's ``token_hash``.
    The local root has no bearer; its row's ``token_hash`` is the marker,
    which retirement rewrites.  Carrying the marker as the proof gives the
    local root the same "this exact root is still current" recheck as a
    password session (#1175) without inventing a bearer credential.
    """
    return await _operator_from_record(db, record, token_hash=LOCAL_ROOT_TOKEN_MARKER)


async def authenticate_local_operator() -> AuthenticatedOperator:
    """Resolve the persisted local single-operator root (#1161).

    The root is an ordinary ``OperatorSession`` owner row with a normal
    ``operator:root:*`` principal, the normal operator grants and a bound
    ``OperatorIdentity``, so ownership, WebSocket watches and durable owner
    checks use the same contracts as a password session.  It is created once
    and reused across restarts.  It has no bearer token and no expiry; it is
    reachable only through the loopback/Host/Origin-checked local mode.
    """
    if not local_no_login_active():
        raise AuthFailure("auth_not_configured")
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        record = await _read_local_root(db)
        if record is not None and _local_root_usable(record, now):
            return await _local_operator_from_record(db, record)
    operator, _created = await _ensure_local_root()
    return operator


async def _ensure_local_root() -> tuple[AuthenticatedOperator, bool]:
    from sqlalchemy.exc import IntegrityError

    try:
        return await _create_local_root()
    except IntegrityError:
        # Another process won the unique marker; adopt its root.
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            record = await _read_local_root(db)
            if record is not None and _local_root_usable(record, now):
                return await _local_operator_from_record(db, record), False
        raise


async def _create_local_root() -> tuple[AuthenticatedOperator, bool]:
    from src.db.models import OperatorIdentity

    async with _local_root_lock():
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            record = await _read_local_root(db)
            if record is not None and _local_root_usable(record, now):
                return await _local_operator_from_record(db, record), False
            identity_id: str | None = None
            predecessor = record
            if predecessor is None:
                # A root retired while the mode was off keeps its identity.
                predecessor = (
                    await db.execute(
                        select(OperatorSession)
                        .where(OperatorSession.token_hash.like(LOCAL_ROOT_TOKEN_MARKER + ":retired:%"))
                        .order_by(OperatorSession.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if predecessor is not None and predecessor.operator_identity_id:
                # A live identity carries over so earlier work stays
                # discoverable through the normal ownership-recovery contract;
                # the retired root itself never regains authority.
                identity = await db.get(OperatorIdentity, predecessor.operator_identity_id)
                if identity is not None and identity.revoked_at is None:
                    identity_id = identity.id
            if record is not None:
                # Keep the retired root as revoked history and free the marker.
                record.revoked_at = record.revoked_at or now
                record.token_hash = f"{LOCAL_ROOT_TOKEN_MARKER}:retired:{record.id}"
                db.add(record)
                await db.flush()
            if identity_id is None:
                identity = OperatorIdentity()
                db.add(identity)
                await db.flush()
                identity_id = identity.id
            record = OperatorSession(
                token_hash=LOCAL_ROOT_TOKEN_MARKER,
                idle_expires_at=LOCAL_ROOT_EXPIRY,
                absolute_expires_at=LOCAL_ROOT_EXPIRY,
                is_bearer_tombstone=False,
                operator_identity_id=identity_id,
            )
            db.add(record)
            await db.flush()
            operator = await _local_operator_from_record(db, record)
    logger.warning(
        "Local no-login operator root created (principal %s); any loopback "
        "process or page passing the Host/Origin checks has operator authority",
        operator.principal.principal_id,
    )
    return operator, True


async def reconcile_local_operator_root() -> dict:
    """Startup reconciliation of the local root with the configured mode.

    With the mode on, the root is ensured (created once, then reused).  With
    the mode off, an active local root is revoked so no background owner check
    keeps treating it as live under password authentication.  Its operator
    identity is kept, so re-enabling the mode binds a new root to the same
    identity rather than silently reviving revoked authority.
    """
    status = auth_mode_status()
    if local_no_login_active():
        operator, created = await _ensure_local_root()
        status.update({
            "principal_id": operator.principal.principal_id,
            "operator_session_id": operator.session_id,
            "operator_identity_id": operator.operator_identity_id,
            "root_created": created,
            "bind_host": settings.backend_bind_host.strip(),
        })
        return status
    if settings.deployment_environment.strip().lower() == "test":
        # The mode can never be active in the test environment, so no local
        # root can exist there; skip the store read for test-process startup.
        return status
    async with get_session() as db:
        record = await _read_local_root(db)
        if record is not None:
            now = datetime.now(timezone.utc)
            if record.revoked_at is None:
                record.revoked_at = now
            record.token_hash = f"{LOCAL_ROOT_TOKEN_MARKER}:retired:{record.id}"
            db.add(record)
            status["local_root_retired"] = record.id
    return status
