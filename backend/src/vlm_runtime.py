"""Effective VLM wrapper runtime configuration for Seraph."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from config.settings import settings

SCREENSHOT_VLM_PROFILE_ID = "local-vlm-screenshot-fast"


def effective_vlm_base_url() -> str:
    """Return the configured Seraph VLM wrapper base URL without a trailing slash."""
    return _trim_url(settings.seraph_vlm_base_url) or _trim_url(settings.local_vlm_base_url)


def effective_vlm_chat_api_base() -> str:
    """Return the OpenAI-compatible chat API base for local Gemma profile routing."""
    explicit = _trim_url(settings.local_llm_api_base)
    if explicit:
        return explicit
    base_url = effective_vlm_base_url()
    if not base_url:
        return ""
    return base_url + "/v1"


def effective_vlm_wrapper_chat_api_base() -> str:
    """Return the wrapper's own optional chat-proxy API, independent of direct text routing."""
    base_url = effective_vlm_base_url()
    if not base_url:
        return ""
    return base_url if base_url.endswith("/v1") else base_url + "/v1"


def effective_vlm_backend_url() -> str:
    """Return the model backend URL advertised by the wrapper, when configured."""
    return _trim_url(settings.seraph_vlm_backend_url)


def effective_vlm_api_key() -> str:
    """Return the configured VLM wrapper API key without exposing it to status payloads."""
    return settings.seraph_vlm_api_key.strip() or settings.local_vlm_api_key.strip() or settings.local_llm_api_key.strip()


def effective_vlm_mode() -> str:
    """Return the operator-visible VLM runtime mode."""
    configured = settings.seraph_vlm_mode.strip().lower()
    if configured:
        return configured
    base_url = effective_vlm_base_url()
    if not base_url:
        return "not_configured"
    host = urlparse(base_url).hostname or ""
    if host in {"127.0.0.1", "localhost", "::1"}:
        return "mac-wrapper"
    return "gpu-server"


def effective_vlm_feeder_window() -> int:
    """Return the bounded Seraph feeder window for the serial GPU queue."""
    try:
        configured = int(settings.seraph_vlm_feeder_window)
    except (TypeError, ValueError):
        configured = 2
    return max(configured, 1)


def effective_vlm_status(*, live_probe: dict[str, object] | None = None) -> dict[str, object]:
    """Return operator-safe VLM runtime status metadata."""
    base_url = effective_vlm_base_url()
    backend_url = effective_vlm_backend_url()
    mode = effective_vlm_mode()
    status = {
        "mode": mode,
        "configured": bool(base_url),
        "base_url": base_url,
        "backend_url": backend_url,
        "chat_api_base": effective_vlm_wrapper_chat_api_base(),
        "chat_completion_endpoint": f"{effective_vlm_wrapper_chat_api_base()}/chat/completions"
        if effective_vlm_wrapper_chat_api_base()
        else "",
        "chat_health_endpoint": f"{base_url}/health/chat" if base_url else "",
        "queue_status_endpoint": f"{base_url}/queue/status" if base_url else "",
        "health_endpoint": f"{base_url}/health" if base_url else "",
        "backend_health_endpoint": f"{base_url}/health/backend" if base_url else "",
        "api_key_configured": bool(effective_vlm_api_key()),
        "feeder_window": effective_vlm_feeder_window(),
    }
    if live_probe is not None:
        status["live_probe"] = live_probe
    return status


def deferred_vlm_live_probe(reason: str = "deferred_fast_metadata") -> dict[str, object]:
    """Return the non-blocking live-probe placeholder for fast metadata endpoints."""
    return {
        "checked": False,
        "reachable": False,
        "reason": reason,
        "health": _unprobed_endpoint(),
        "backend_health": _unprobed_endpoint(),
        "queue_status": _unprobed_endpoint(),
        "chat_proxy": _unprobed_endpoint(),
    }


async def probe_effective_vlm_runtime(*, timeout_seconds: float = 0.75) -> dict[str, object]:
    """Probe the effective VLM wrapper route from this Seraph process."""
    base_url = effective_vlm_base_url()
    if not base_url:
        return {
            "checked": False,
            "reachable": False,
            "reason": "not_configured",
            "health": _unprobed_endpoint(),
            "backend_health": _unprobed_endpoint(),
            "queue_status": _unprobed_endpoint(),
            "chat_proxy": _unprobed_endpoint(),
        }

    timeout = max(min(timeout_seconds, 5.0), 0.1)
    async with httpx.AsyncClient(timeout=timeout) as client:
        health, backend_health, queue_status, chat_proxy = await asyncio.gather(
            _probe_json_endpoint(client, base_url + "/health"),
            _probe_json_endpoint(client, base_url + "/health/backend"),
            _probe_json_endpoint(client, base_url + "/queue/status"),
            _probe_chat_health(client, base_url + "/health/chat"),
        )
    reachable = bool(
        health.get("ok")
        and backend_health.get("ok")
        and queue_status.get("ok")
        and chat_proxy.get("ok")
    )
    return {
        "checked": True,
        "reachable": reachable,
        "health": health,
        "backend_health": backend_health,
        "queue_status": queue_status,
        "chat_proxy": chat_proxy,
    }


async def direct_local_chat_route_error(*, timeout_seconds: float = 0.75) -> str | None:
    """Return an operator-readable route error when local chat cannot run."""
    status = effective_vlm_status()
    base_url = str(status.get("base_url") or "")
    chat_api_base = effective_vlm_chat_api_base()
    chat_health_endpoint = str(status.get("chat_health_endpoint") or "")
    if not chat_api_base:
        return (
            "Local chat runtime is not configured for the Seraph backend. "
            "Set SERAPH_VLM_BASE_URL or LOCAL_VLM_BASE_URL before using direct local chat."
        )
    if not base_url:
        return None

    wrapper_chat_api_base = effective_vlm_wrapper_chat_api_base()
    if _trim_url(chat_api_base) != _trim_url(wrapper_chat_api_base):
        # An explicitly configured text endpoint is independent of the VLM
        # wrapper. The completion transport reports its own reachability error.
        return None

    probe = await probe_effective_vlm_runtime(timeout_seconds=timeout_seconds)
    if probe.get("reachable") is True:
        return None

    detail = _probe_failure_detail(probe)
    endpoint = chat_health_endpoint or str(status.get("backend_health_endpoint") or status.get("health_endpoint") or base_url)
    return (
        "Local chat runtime is unreachable from the Seraph backend at "
        f"{base_url}. Health endpoint {endpoint} reported {detail}. "
        "Check the VLM wrapper route before retrying."
    )


def _trim_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def _unprobed_endpoint() -> dict[str, object]:
    return {"checked": False, "ok": False, "status_code": None, "error": ""}


def _probe_failure_detail(probe: dict[str, object]) -> str:
    for key, label in (
        ("chat_proxy", "chat proxy"),
        ("backend_health", "backend health"),
        ("health", "wrapper health"),
        ("queue_status", "queue status"),
    ):
        endpoint = probe.get(key)
        if not isinstance(endpoint, dict) or endpoint.get("ok") is True:
            continue
        error = str(endpoint.get("error") or "unreachable")
        status_code = endpoint.get("status_code")
        if status_code is not None:
            return f"{label} {error} ({status_code})"
        return f"{label} {error}"
    reason = str(probe.get("reason") or "unreachable")
    return _safe_probe_detail(reason)


async def _probe_json_endpoint(client: httpx.AsyncClient, endpoint: str) -> dict[str, object]:
    try:
        response = await client.get(endpoint)
    except httpx.TimeoutException:
        return {"checked": True, "ok": False, "status_code": None, "error": "timeout"}
    except httpx.ConnectError:
        return {"checked": True, "ok": False, "status_code": None, "error": "connect_error"}
    except httpx.HTTPError:
        return {"checked": True, "ok": False, "status_code": None, "error": "http_error"}

    payload: object
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    ok = 200 <= response.status_code < 400
    result: dict[str, object] = {
        "checked": True,
        "ok": ok,
        "status_code": response.status_code,
        "error": "" if ok else "bad_status",
    }
    if isinstance(payload, dict):
        queue = payload.get("queue")
        if isinstance(queue, dict):
            result["queue"] = {
                "queued": queue.get("queued"),
                "active": queue.get("active"),
                "workers": queue.get("workers"),
                "background_workers": queue.get("background_workers"),
            }
        elif any(key in payload for key in ("queued", "active", "workers", "background_workers")):
            result["queue"] = {
                "queued": payload.get("queued"),
                "active": payload.get("active"),
                "workers": payload.get("workers"),
                "background_workers": payload.get("background_workers"),
            }
        for key in ("status", "backend_status", "model"):
            if key in payload:
                result[key] = payload[key]
    return result


async def _probe_chat_health(client: httpx.AsyncClient, endpoint: str) -> dict[str, object]:
    headers = {}
    api_key = effective_vlm_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = await client.get(endpoint, headers=headers)
    except httpx.TimeoutException:
        return {"checked": True, "ok": False, "status_code": None, "error": "timeout"}
    except httpx.ConnectError:
        return {"checked": True, "ok": False, "status_code": None, "error": "connect_error"}
    except httpx.HTTPError:
        return {"checked": True, "ok": False, "status_code": None, "error": "http_error"}

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    status = ""
    enabled = False
    auth_configured = False
    auth_ok = False
    model = ""
    if isinstance(payload, dict):
        status = _safe_probe_detail(str(payload.get("status") or ""))
        enabled = payload.get("enabled") is True
        auth_configured = payload.get("auth_configured") is True
        auth_ok = payload.get("auth_ok") is True
        model_value = payload.get("model")
        if isinstance(model_value, str):
            model = _safe_probe_detail(model_value)
    ok = 200 <= response.status_code < 400 and enabled and auth_configured and auth_ok
    result: dict[str, object] = {
        "checked": True,
        "ok": ok,
        "status_code": response.status_code,
        "error": "" if ok else _chat_health_error(response, status, enabled, auth_configured, auth_ok),
        "enabled": enabled,
        "auth_configured": auth_configured,
        "auth_ok": auth_ok,
    }
    if status:
        result["status"] = status
    if model:
        result["model"] = model
    return result


def _chat_health_error(
    response: httpx.Response,
    status: str,
    enabled: bool,
    auth_configured: bool,
    auth_ok: bool,
) -> str:
    if not 200 <= response.status_code < 400:
        return "bad_status"
    if status:
        return _safe_probe_detail(status.strip().lower().replace(" ", "_"))
    if not enabled:
        return "disabled"
    if not auth_configured:
        return "auth_not_configured"
    if not auth_ok:
        return "auth_failed"
    return "bad_status"


def _safe_probe_detail(value: str) -> str:
    safe = value.strip()
    for secret in (settings.seraph_vlm_api_key, settings.local_vlm_api_key, settings.local_llm_api_key):
        if secret:
            safe = safe.replace(secret, "[redacted]")
    return safe[:160]
