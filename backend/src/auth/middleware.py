from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.settings import settings
from src.auth.service import AuthFailure, auth_enabled, authenticate_token


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_PATHS = {"/health", "/api/auth/login"}


def _csv(value: str) -> set[str]:
    return {item.strip().rstrip("/") for item in value.split(",") if item.strip()}


def validate_request_boundary(*, host: str, origin: str | None, method: str) -> str | None:
    allowed_hosts = {item.lower() for item in _csv(settings.operator_auth_allowed_hosts)}
    if host.strip().lower() not in allowed_hosts:
        return "origin_forbidden"
    if method.upper() not in _SAFE_METHODS:
        if not origin:
            return "mutation_origin_required"
        if origin.rstrip("/") not in _csv(settings.operator_auth_allowed_origins):
            return "origin_forbidden"
    return None


class OperatorAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_enabled():
            if (
                settings.operator_auth_allow_unauthenticated_tests
                and settings.deployment_environment == "test"
            ):
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
