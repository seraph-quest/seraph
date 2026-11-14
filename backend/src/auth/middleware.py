from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.middleware.base import BaseHTTPMiddleware
from urllib.parse import urlsplit

from config.settings import settings
from src.db.engine import is_database_locked
from src.auth.service import (
    AuthFailure,
    AuthenticatedOperator,
    auth_enabled,
    authenticate_local_operator,
    authenticate_token,
    is_loopback_host,
    local_no_login_active,
    test_bypass_operator,
)


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_PATHS = {"/health", "/api/auth/login"}
_PAIRED_EDGE_INGRESS_PATHS = {
    "/api/nodes/edge/heartbeat",
    "/api/nodes/edge/upload",
    "/api/nodes/edge/ingest",
    # Daemon local capture upload (ADR-035): the handler authenticates the
    # same scoped paired-edge credential before reading any bytes.
    "/api/observer/captures",
}
# Native-client ingress that may omit Origin. Mirrors the PAIRED_PATHS
# precedent: only when the request carries neither Origin nor a cookie, so no
# browser ambient authority can ride it; the host allow-list still applies and
# the handler's bearer credential is mandatory.
_NATIVE_NO_ORIGIN_PATHS = {"/api/observer/captures"}


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


def _origin_hostname(origin: str) -> str:
    try:
        return (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return ""


def validate_request_boundary(*, host: str, origin: str | None, method: str) -> str | None:
    allowed_hosts = {_hostname(item) for item in _csv(settings.operator_auth_allowed_hosts)}
    if not _hostname(host) or _hostname(host) not in allowed_hosts:
        return "origin_forbidden"
    local = local_no_login_active()
    if local and not is_loopback_host(_hostname(host)):
        # No-login authority is loopback-only: a configured LAN or test host
        # name never reaches it (DNS-rebinding defence).
        return "origin_forbidden"
    if method.upper() not in _SAFE_METHODS:
        if not origin:
            return "mutation_origin_required"
        if origin.rstrip("/") not in _csv(settings.operator_auth_allowed_origins):
            return "origin_forbidden"
    if local and origin:
        # Without a login there is no cookie to withhold from a cross-site
        # page, so any Origin a browser sends must be an allowed loopback one,
        # even on a safe method.
        if (
            origin.rstrip("/") not in _csv(settings.operator_auth_allowed_origins)
            or not is_loopback_host(_origin_hostname(origin))
        ):
            return "origin_forbidden"
    return None


def local_peer_error(peer: str | None) -> str | None:
    """No-login requests must come from a loopback peer."""
    if not is_loopback_host(peer):
        return "local_peer_forbidden"
    return None


def _header_values(headers, name: str) -> list[str]:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        return [str(value) for value in getlist(name)]
    value = headers.get(name) if hasattr(headers, "get") else None
    return [str(value)] if value is not None else []


def _forwarded_for_hosts(value: str) -> list[str]:
    hosts: list[str] = []
    for element in value.split(","):
        for pair in element.split(";"):
            key, separator, raw = pair.strip().partition("=")
            if not separator or key.strip().lower() != "for":
                continue
            host = raw.strip().strip('"')
            if host.startswith("["):
                host = host[1:].split("]", 1)[0]
            elif host.count(":") == 1:
                host = host.split(":", 1)[0]
            hosts.append(host)
    return hosts


def local_request_error(peer: str | None, headers, *, allow_cross_site: bool = False) -> str | None:
    """The no-login per-request origin checks beyond Host/Origin (#1161).

    - The peer uvicorn reports must be loopback.  A loopback reverse proxy
      such as the Vite dev server forwards the real client in
      ``X-Forwarded-For`` (``xfwd``); every forwarded hop must be loopback
      too, so a LAN client reaching an exposed proxy is refused even if the
      backend's proxy-header handling is disabled.
    - ``Sec-Fetch-Site: cross-site`` is refused, covering Origin-less
      browser requests such as no-cors GETs and image loads.
    """
    peer_error = local_peer_error(peer)
    if peer_error:
        return peer_error
    forwarded: list[str] = []
    for value in _header_values(headers, "x-forwarded-for"):
        forwarded.extend(item.strip() for item in value.split(","))
    forwarded.extend(item.strip() for item in _header_values(headers, "x-real-ip"))
    for value in _header_values(headers, "forwarded"):
        forwarded.extend(_forwarded_for_hosts(value))
    if any(not is_loopback_host(item) for item in forwarded):
        return "local_peer_forbidden"
    sites = {value.strip().lower() for value in _header_values(headers, "sec-fetch-site")}
    if "cross-site" in sites and not allow_cross_site:
        return "cross_site_forbidden"
    return None


def test_bypass_enabled() -> bool:
    return (
        settings.operator_auth_allow_unauthenticated_tests
        and settings.deployment_environment == "test"
    )


def _calendar_auth_failure(path: str, code: str) -> bool:
    """Use the Calendar contract's safe error envelope at the auth edge.

    Calendar setup/control callers need a recoverable, bounded response even
    when the request is rejected before FastAPI reaches the route.  Keep this
    narrow so existing API auth responses retain their established shape.
    """
    return path.startswith("/api/calendar") or path.startswith("/api/governed-schedules")


def is_auth_storage_busy(exc: BaseException) -> bool:
    """Auth-store contention: retryable, never an authentication verdict (#702)."""
    return isinstance(exc, SQLAlchemyTimeoutError) or (
        isinstance(exc, OperationalError) and is_database_locked(exc)
    )


def _auth_failure_response(path: str, code: str, *, status_code: int) -> JSONResponse:
    if _calendar_auth_failure(path, code):
        if code in {"auth_not_configured", "authentication_required", "invalid_token", "session_unavailable"}:
            message = "Authentication is required"
            recovery = "login"
        elif code == "auth_storage_busy":
            # Contention is not a session verdict; the cookie remains valid.
            message = "The operator session could not be checked; retry shortly"
            recovery = "retry_existing_request"
        else:
            message = "The operator session is unavailable"
            recovery = "login"
        return JSONResponse(
            {"detail": {"code": str(code)[:128], "message": message, "recovery_action": recovery}},
            status_code=status_code,
        )
    return JSONResponse({"detail": {"code": str(code)[:128]}}, status_code=status_code)


# ``ownership_proof_invalid`` is a rejected presented credential (continuity
# cookie or recovery code); ``/api/auth/login`` establishes it as 401.
# ``ownership_proof_required`` is an authenticated root without an enrolled
# identity; ``/api/auth/ownership/*`` establishes it as 403.
_AUTH_FAILURE_UNAUTHENTICATED = {
    "authentication_required", "invalid_token", "ownership_proof_invalid",
    "session_expired", "session_revoked", "session_unavailable",
}
_AUTH_FAILURE_FORBIDDEN = {
    "legacy_ownership_unproved", "mutation_origin_required", "ownership_proof_required",
}


def auth_failure_status(code: str) -> int:
    """Typed HTTP status for an ``AuthFailure`` a route did not map itself.

    Session verdicts are 401 (the middleware's own status), boundary and
    ownership-proof refusals 403, a missing recovery record 404, an
    unconfigured authority 503 (as at the middleware edge), and any other
    ownership/recovery state conflict 409 (as ``/api/auth/ownership/*``).
    """
    if code in _AUTH_FAILURE_UNAUTHENTICATED:
        return 401
    if code in _AUTH_FAILURE_FORBIDDEN or code.endswith("_forbidden"):
        return 403
    if code == "recovery_record_unavailable":
        return 404
    if code == "auth_not_configured":
        return 503
    return 409


async def auth_failure_handler(request: Request, exc: AuthFailure) -> JSONResponse:
    """App-wide guard: an ``AuthFailure`` is never surfaced as a 500 (#1175)."""
    code = str(getattr(exc, "code", "") or "authentication_required")
    return _auth_failure_response(request.url.path, code, status_code=auth_failure_status(code))


async def authenticate_websocket(websocket) -> AuthenticatedOperator:
    """Authenticate a WebSocket before accepting it.

    WebSockets do not pass through Starlette's HTTP middleware stack, so the
    handshake must enforce the same host/origin and session-cookie boundary.
    """
    local = False
    if not auth_enabled():
        if test_bypass_enabled():
            return test_bypass_operator()
        if not local_no_login_active():
            raise AuthFailure("auth_not_configured")
        local = True
        client = getattr(websocket, "client", None)
        peer_error = local_request_error(getattr(client, "host", None), websocket.headers)
        if peer_error:
            raise AuthFailure(peer_error)
    boundary_error = validate_request_boundary(
        host=websocket.headers.get("host", ""),
        origin=websocket.headers.get("origin"),
        method="POST",
    )
    if boundary_error:
        raise AuthFailure(boundary_error)
    if local:
        return await authenticate_local_operator()
    return await authenticate_token(
        websocket.cookies.get(settings.operator_auth_cookie_name)
    )


class OperatorAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        local = False
        if not auth_enabled():
            if test_bypass_enabled():
                request.state.operator = test_bypass_operator()
                return await call_next(request)
            if not local_no_login_active():
                if request.url.path.startswith("/api"):
                    return _auth_failure_response(request.url.path, "auth_not_configured", status_code=503)
                return await call_next(request)
            # Local single-operator mode (#1161): no login, but every request
            # must come from a loopback peer and pass the Host/Origin boundary.
            local = True
            from src.workflows.selected_context_contract import COMPANION_ORIGIN, PAIRED_PATHS
            # The paired MV3 companion is a cross-site extension by design; its
            # exact origin and pairing bearer are checked on that closed path.
            companion = (
                request.method == "POST"
                and request.url.path in PAIRED_PATHS
                and request.headers.get("origin") == COMPANION_ORIGIN
            )
            peer_error = local_request_error(
                request.client.host if request.client else None,
                request.headers,
                allow_cross_site=companion,
            )
            if peer_error:
                return JSONResponse({"detail": {"code": peer_error}}, status_code=403)
        from src.workflows.selected_context_contract import COMPANION_ORIGIN, PAIRED_PATHS
        if request.method == "POST" and request.url.path in PAIRED_PATHS:
            # Closed MV3 ingress only. Never grant browser cookie authority or
            # broaden global CORS, nodes, absent-Origin or OPTIONS behavior.
            boundary_error = validate_request_boundary(
                host=request.headers.get("host", ""), origin=None, method="GET")
            if boundary_error or request.headers.get("origin") != COMPANION_ORIGIN or "cookie" in request.headers:
                return JSONResponse({"detail": {"code": "selected_context_transport_denied"}}, status_code=403)
            return await call_next(request)
        native_no_origin = (
            request.method == "POST"
            and request.url.path in _NATIVE_NO_ORIGIN_PATHS
            and "origin" not in request.headers
            and "cookie" not in request.headers
        )
        boundary_error = validate_request_boundary(
            host=request.headers.get("host", ""),
            origin=request.headers.get("origin"),
            # A safe-method check validates the host without demanding an
            # Origin a native daemon does not send.
            method="GET" if native_no_origin else request.method,
        )
        if boundary_error:
            return JSONResponse({"detail": {"code": boundary_error}}, status_code=403)
        # The paired edge authenticates with its scoped node credential at the
        # ingress handler.  Keep the host/origin boundary enforced here, but do
        # not require a browser operator cookie that a native daemon cannot
        # safely hold. Artifact readback and pairing mutations remain operator
        # authenticated because only these exact ingress paths are exempted.
        if (
            request.url.path in _PUBLIC_PATHS
            or request.url.path in _PAIRED_EDGE_INGRESS_PATHS
            or not request.url.path.startswith("/api")
        ):
            return await call_next(request)
        try:
            if local:
                operator = await authenticate_local_operator()
            elif request.method == "GET" and request.url.path == "/api/operator/continuation":
                from src.auth.service import authenticate_home_token_readonly
                operator = await authenticate_home_token_readonly(request.cookies.get(settings.operator_auth_cookie_name))
            else:
                operator = await authenticate_token(request.cookies.get(settings.operator_auth_cookie_name))
        except AuthFailure as exc:
            return _auth_failure_response(request.url.path, exc.code, status_code=401)
        except (OperationalError, SQLAlchemyTimeoutError) as exc:
            # Storage contention is not an authentication verdict: answer an
            # explicit retryable 503 (the session cookie stays valid), never a
            # bare 500 and never a 401 that would send the operator to login.
            if not is_auth_storage_busy(exc):
                raise
            return _auth_failure_response(request.url.path, "auth_storage_busy", status_code=503)
        request.state.operator = operator
        return await call_next(request)
