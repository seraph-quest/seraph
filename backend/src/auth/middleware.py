from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from urllib.parse import urlsplit

from config.settings import settings
from src.auth.service import (
    AuthFailure,
    AuthenticatedOperator,
    auth_enabled,
    authenticate_token,
    test_bypass_operator,
)


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_PATHS = {"/health", "/api/auth/login"}


def _csv(value: str) -> set[str]:
    return {item.strip().rstrip("/") for item in value.split(",") if item.strip()}


def _hostname(value: str) -> str:
    """Normalize a Host header or configured host to its hostname.

    Starlette exposes the port-bearing Host header verbatim (for example
    ``127.0.0.1:8004``).  Comparing it directly with a hostname allow-list
    rejects the managed dev/prod listeners.  Parsing the authority also keeps
    IPv6 literals and optional ports well-defined without accepting a path or
    userinfo component.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        return ""
    try:
        parsed = urlsplit(f"//{raw}")
        # A Host value is an authority, never a URL with userinfo, path, query,
        # or fragment.  Comparing netloc also rejects control characters that
        # urlsplit silently strips while parsing.
        if parsed.netloc != raw or parsed.path or parsed.query or parsed.fragment:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        hostname = parsed.hostname
        if not hostname:
            return ""
        # Accessing .port validates both the numeric form and the 0..65535
        # range.  An explicit trailing colon is an empty, invalid port.
        parsed.port
        if raw.endswith(":"):
            return ""
        # Unbracketed IPv6 authorities are ambiguous with host:port and must
        # be rejected; valid IPv6 Host values use brackets.
        if not raw.startswith("[") and raw.count(":") > 1:
            return ""
    except ValueError:
        return ""
    return hostname.strip().rstrip(".").lower()


def validate_request_boundary(*, host: str, origin: str | None, method: str) -> str | None:
    allowed_hosts = {_hostname(item) for item in _csv(settings.operator_auth_allowed_hosts)}
    if not _hostname(host) or _hostname(host) not in allowed_hosts:
        return "origin_forbidden"
    if method.upper() not in _SAFE_METHODS:
        if not origin:
            return "mutation_origin_required"
        if origin.rstrip("/") not in _csv(settings.operator_auth_allowed_origins):
            return "origin_forbidden"
    return None


def test_bypass_enabled() -> bool:
    return (
        settings.operator_auth_allow_unauthenticated_tests
        and settings.deployment_environment == "test"
    )


async def authenticate_websocket(websocket) -> AuthenticatedOperator:
    """Authenticate a WebSocket before accepting it.

    WebSockets do not pass through Starlette's HTTP middleware stack, so the
    handshake must enforce the same host/origin and session-cookie boundary.
    """
    if not auth_enabled():
        if test_bypass_enabled():
            return test_bypass_operator()
        raise AuthFailure("auth_not_configured")
    boundary_error = validate_request_boundary(
        host=websocket.headers.get("host", ""),
        origin=websocket.headers.get("origin"),
        method="POST",
    )
    if boundary_error:
        raise AuthFailure(boundary_error)
    return await authenticate_token(
        websocket.cookies.get(settings.operator_auth_cookie_name)
    )


class OperatorAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_enabled():
            if test_bypass_enabled():
                request.state.operator = test_bypass_operator()
                return await call_next(request)
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": {"code": "auth_not_configured"}}, status_code=503)
            return await call_next(request)
        boundary_error = validate_request_boundary(
            host=request.headers.get("host", ""),
            origin=request.headers.get("origin"),
            method=request.method,
        )
        if boundary_error:
            return JSONResponse({"detail": {"code": boundary_error}}, status_code=403)
        if request.url.path in _PUBLIC_PATHS or not request.url.path.startswith("/api"):
            return await call_next(request)
        try:
            operator = await authenticate_token(request.cookies.get(settings.operator_auth_cookie_name))
        except AuthFailure as exc:
            return JSONResponse({"detail": {"code": exc.code}}, status_code=401)
        request.state.operator = operator
        return await call_next(request)
