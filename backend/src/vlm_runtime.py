"""Effective VLM wrapper runtime configuration for Seraph."""

from __future__ import annotations

from urllib.parse import urlparse

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


def effective_vlm_status() -> dict[str, object]:
    """Return operator-safe VLM runtime status metadata."""
    base_url = effective_vlm_base_url()
    backend_url = effective_vlm_backend_url()
    mode = effective_vlm_mode()
    return {
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


def _trim_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")
