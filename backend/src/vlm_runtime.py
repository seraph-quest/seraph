"""Effective VLM wrapper runtime configuration for Seraph."""

from __future__ import annotations

from urllib.parse import urlparse

from config.settings import settings

# Canonical active profile identity. The old local-vlm name is intentionally
# not reused, so persisted local proofs/configuration cannot look current.
SCREENSHOT_VLM_PROFILE_ID = "openrouter-screenshot-vision"


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
    # The OpenRouter migration removes the local wrapper from the active
    # runtime.  Keep configured endpoint metadata for historical diagnostics,
    # but never advertise it as active or make readiness depend on it.
    local_runtime_active = False
    status = {
        "mode": mode,
        "active": local_runtime_active,
        "disabled_reason": None if local_runtime_active else "local_vlm_disabled_openrouter_only",
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
    """Return the retired local-runtime receipt without making a network call.

    The function remains as a compatibility seam for status callers and older
    integrations.  The OpenRouter-only phase must never probe or depend on a
    local wrapper, GPU host, or VLM queue.
    """
    return {
        "checked": False,
        "reachable": False,
        "reason": "local_vlm_disabled_openrouter_only",
        "health": _unprobed_endpoint(),
        "backend_health": _unprobed_endpoint(),
        "queue_status": _unprobed_endpoint(),
        "chat_proxy": _unprobed_endpoint(),
    }


async def direct_local_chat_route_error(
    *,
    timeout_seconds: float = 0.75,
    runtime_path: str | None = None,
) -> str | None:
    """Return a route error for legacy local chat callers.

    OpenRouter direct chat is governed by the model fabric and must not depend
    on the retired local VLM health endpoints.  Keep the no-argument behavior
    for historical diagnostics, while allowing active chat callers to bypass
    those probes explicitly.
    """
    # REST/WebSocket lightweight turns still use this compatibility seam, but
    # canonical chat/onboarding paths now dispatch through the governed
    # OpenRouter model fabric. Only callers without a canonical route remain
    # legacy local-runtime diagnostics.
    if runtime_path:
        try:
            from src.model_fabric.caller_context import is_canonical_inference_route

            if is_canonical_inference_route(runtime_path):
                return None
        except (ImportError, ValueError):
            pass
    return "local_vlm_disabled_openrouter_only"

def _trim_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def _unprobed_endpoint() -> dict[str, object]:
    return {"checked": False, "ok": False, "status_code": None, "error": ""}
