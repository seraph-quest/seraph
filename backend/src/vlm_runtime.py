"""Effective VLM wrapper runtime configuration for Seraph."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from config.settings import settings


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
        "chat_api_base": effective_vlm_chat_api_base(),
        "queue_status_endpoint": f"{base_url}/queue/status" if base_url else "",
        "health_endpoint": f"{base_url}/health" if base_url else "",
        "backend_health_endpoint": f"{base_url}/health/backend" if base_url else "",
        "api_key_configured": bool(effective_vlm_api_key()),
        "feeder_window": effective_vlm_feeder_window(),
    }
    if live_probe is not None:
        status["live_probe"] = live_probe
    return status


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
        }

    timeout = max(min(timeout_seconds, 5.0), 0.1)
    async with httpx.AsyncClient(timeout=timeout) as client:
        health, backend_health, queue_status = await asyncio.gather(
            _probe_json_endpoint(client, base_url + "/health"),
            _probe_json_endpoint(client, base_url + "/health/backend"),
            _probe_json_endpoint(client, base_url + "/queue/status"),
        )
    reachable = bool(health.get("ok") and backend_health.get("ok") and queue_status.get("ok"))
    return {
        "checked": True,
        "reachable": reachable,
        "health": health,
        "backend_health": backend_health,
        "queue_status": queue_status,
    }


def _trim_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def _unprobed_endpoint() -> dict[str, object]:
    return {"checked": False, "ok": False, "status_code": None, "error": ""}


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
