from fastapi import APIRouter, HTTPException, Request, Response
from collections import OrderedDict
from datetime import datetime, timezone
import ipaddress
import time
from pydantic import BaseModel

from config.settings import settings
from src.auth.service import AuthFailure, auth_enabled, create_session, revoke_session, verify_secret

router = APIRouter()

_LOGIN_ATTEMPTS: OrderedDict[str, list[float]] = OrderedDict()
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_LIMIT = 5
_LOGIN_BUCKET_LIMIT = 1024


def _reset_login_throttle_for_tests() -> None:
    _LOGIN_ATTEMPTS.clear()


def _trusted_proxy_ips() -> set[str]:
    return {value.strip() for value in settings.operator_auth_trusted_proxy_ips.split(",") if value.strip()}


def _login_source(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and peer in _trusted_proxy_ips():
        candidate = forwarded.split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return peer
    return peer


def _consume_login_attempt(source: str) -> None:
    now = time.monotonic()
    attempts = [
        value for value in _LOGIN_ATTEMPTS.pop(source, [])
        if now - value < _LOGIN_WINDOW_SECONDS
    ]
    if len(attempts) >= _LOGIN_LIMIT:
        _LOGIN_ATTEMPTS[source] = attempts
        raise HTTPException(status_code=429, detail={"code": "login_rate_limited"})
    attempts.append(now)
    _LOGIN_ATTEMPTS[source] = attempts
    while len(_LOGIN_ATTEMPTS) > _LOGIN_BUCKET_LIMIT:
        _LOGIN_ATTEMPTS.popitem(last=False)


def _require_configured() -> None:
    if not auth_enabled():
        raise HTTPException(status_code=503, detail={"code": "auth_not_configured"})


class LoginRequest(BaseModel):
    password: str


def _remaining_cookie_age(absolute_expires_at: datetime) -> int:
    """Keep the browser cookie no longer-lived than server authority."""

    expiry = absolute_expires_at
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    remaining = (expiry.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining))


def _set_cookie(response: Response, token: str, *, absolute_expires_at: datetime) -> None:
    response.set_cookie(
        settings.operator_auth_cookie_name,
        token,
        httponly=True,
        secure=settings.operator_auth_cookie_secure,
        samesite="strict",
        path="/",
        max_age=_remaining_cookie_age(absolute_expires_at),
    )


def _operator_payload(operator) -> dict:
    """Return the additive, non-sensitive operator session contract."""
    return {
        "authenticated": True,
        "principal_id": operator.principal.principal_id,
        "session_id": operator.session_id,
        "idle_expires_at": operator.idle_expires_at,
        "absolute_expires_at": operator.absolute_expires_at,
        "ownership_continuity": operator.ownership_continuity,
        "ownership_recovery_action": operator.ownership_recovery_action,
    }


@router.post("/login")
async def login(payload: LoginRequest, response: Response, request: Request):
    _require_configured()
    _consume_login_attempt(_login_source(request))
    if not await verify_secret(payload.password):
        raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})
    token, operator = await create_session()
    _set_cookie(response, token, absolute_expires_at=operator.absolute_expires_at)
    return _operator_payload(operator)


@router.get("/session")
async def session(request: Request):
    _require_configured()
    operator = request.state.operator
    return _operator_payload(operator)


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    _require_configured()
    operator = request.state.operator
    try:
        token, replacement = await create_session(
            replace_session_id=operator.session_id,
            expected_token_hash=operator._token_hash,
        )
    except AuthFailure as exc:
        # A second request can pass the middleware with the same old cookie
        # just before the first request wins the private hash CAS.  Keep that
        # bounded race an ordinary authenticated denial so the cockpit can
        # reconcile its current cookie instead of receiving a 500.
        if exc.code in {"authentication_required", "session_revoked", "session_expired"}:
            raise HTTPException(status_code=401, detail={"code": exc.code}) from exc
        raise HTTPException(status_code=503, detail={"code": "session_unavailable"}) from exc
    except Exception as exc:
        # A database or transaction failure must remain a bounded auth denial;
        # never turn it into a successful refresh or expose backend details.
        raise HTTPException(status_code=503, detail={"code": "session_unavailable"}) from exc
    _set_cookie(response, token, absolute_expires_at=replacement.absolute_expires_at)
    return _operator_payload(replacement)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    _require_configured()
    await revoke_session(request.state.operator.session_id)
    response.delete_cookie(settings.operator_auth_cookie_name, path="/", secure=settings.operator_auth_cookie_secure, httponly=True, samesite="strict")
