import pytest
from unittest.mock import patch

from config.settings import settings
from src.app import _effective_runtime_route_status, _safe_runtime_endpoint


_DEFERRED_VLM_PROBE = {
    "checked": False,
    "reachable": False,
    "reason": "deferred_fast_metadata",
    "health": {"checked": False, "ok": False, "status_code": None, "error": ""},
    "backend_health": {"checked": False, "ok": False, "status_code": None, "error": ""},
    "queue_status": {"checked": False, "ok": False, "status_code": None, "error": ""},
    "chat_proxy": {"checked": False, "ok": False, "status_code": None, "error": ""},
}


@pytest.mark.parametrize(
    "unsafe_endpoint",
    [
        "https://user:password@models.example/v1",
        "https://models.example/v1?token=secret",
        "https://models.example/v1#secret-fragment",
        "http://models.example:not-a-port/v1",
        "http://[malformed/v1",
    ],
)
def test_runtime_endpoint_sanitizer_blanks_unsafe_values(unsafe_endpoint):
    assert _safe_runtime_endpoint(unsafe_endpoint) == ""


def test_runtime_endpoint_sanitizer_preserves_safe_absolute_value():
    assert _safe_runtime_endpoint("HTTP://[::1]:8000/v1") == "http://[::1]:8000/v1"


def test_effective_runtime_distinguishes_direct_gpu_text_from_wrapper_chat():
    base_runtime = {
        "provider": "local-gemma",
        "model": "gemma",
        "model_label": "gemma",
        "active_profile": "local-gemma-chat-thinking",
    }
    vlm = {
        "mode": "gpu-server",
        "configured": True,
        "chat_api_base": "http://192.168.1.26:8001/v1",
        "base_url": "http://192.168.1.26:8001",
        "backend_url": "http://192.168.1.26:8000/v1",
    }

    direct = _effective_runtime_route_status(
        {**base_runtime, "api_base": "http://192.168.1.26:8000/v1"},
        vlm,
    )
    wrapper = _effective_runtime_route_status(
        {**base_runtime, "api_base": "http://192.168.1.26:8001/v1"},
        vlm,
    )
    mac_local = _effective_runtime_route_status(
        {**base_runtime, "api_base": "http://127.0.0.1:8000/v1"},
        vlm,
    )

    assert direct["route_label"] == "GPU text"
    assert direct["provider_label"] == "local-gemma/gpu-text"
    assert wrapper["route_label"] == "GPU wrapper chat"
    assert wrapper["provider_label"] == "local-gemma/gpu-wrapper-chat"
    assert mac_local["route_label"] == "local Gemma"
    assert mac_local["provider_label"] == "local-gemma"


@pytest.mark.asyncio
async def test_cors_allows_loopback_dev_origin(client):
    response = await client.options(
        "/api/capabilities/overview",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"


@pytest.mark.asyncio
async def test_runtime_status_exposes_release_and_model(client):
    with (
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "local_runtime_paths", ""),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "2026.4.11"
    assert payload["build_id"] == "SERAPH_PRIME_v2026.4.11"
    assert payload["provider"] == "openrouter"
    assert payload["model"] == settings.default_model
    assert payload["model_label"] == settings.default_model.split("/")[-1]
    assert payload["active_profile"] == "default"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == settings.default_model
    assert isinstance(payload["provider_profiles"], list)
    assert isinstance(payload["local_operators"], list)
    assert any(item["id"] == "openrouter" for item in payload["provider_profiles"])
    assert any(item["id"] == "codex-local" for item in payload["local_operators"])
    assert all("api_key" not in item for item in payload["provider_profiles"])


@pytest.mark.asyncio
async def test_runtime_status_reports_local_codex_when_selected(client):
    with patch.object(settings, "default_model", "codex-local"), patch.object(settings, "codex_local_model", "gpt-5.5"):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "codex-local"
    assert payload["model"] == "codex-local"
    assert payload["model_label"] == "gpt-5.5"
    assert payload["active_profile"] == "codex-local"


@pytest.mark.asyncio
async def test_runtime_status_reports_effective_local_gemma_chat_profile(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "local-gemma"
    assert payload["model"] == "unsloth/gemma-4-26B-A4B-it-qat-GGUF"
    assert payload["model_label"] == "gemma-4-26B-A4B-it-qat-GGUF"
    assert payload["api_base"] == "http://127.0.0.1:8000/v1"
    assert payload["active_profile"] == "local-gemma-chat-thinking"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == "openrouter/x-ai/grok-4.1-fast"


@pytest.mark.asyncio
async def test_runtime_status_distinguishes_gpu_wrapper_chat_from_screenshot_vlm(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", ""),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "local-gemma"
    assert payload["api_base"] == "http://192.168.1.26:8001/v1"
    assert payload["active_profile"] == "local-gemma-chat-thinking"
    assert payload["effective_runtime"] == {
        "runtime_path": "chat_agent",
        "active_profile": "local-gemma-chat-thinking",
        "provider": "local-gemma",
        "provider_label": "local-gemma/gpu-wrapper-chat",
            "model": "unsloth/gemma-4-26B-A4B-it-qat-GGUF",
        "model_label": "gemma-4-26B-A4B-it-qat-GGUF",
        "mode": "gpu-server",
        "route_label": "GPU wrapper chat",
        "summary_label": "GPU wrapper chat · gemma-4-26B-A4B-it-qat-GGUF",
        "api_base": "http://192.168.1.26:8001/v1",
        "vlm_base_url": "http://192.168.1.26:8001",
        "vlm_backend_url": "http://192.168.1.26:8000/v1",
        "vlm_configured": True,
        "queue_status_endpoint": "http://192.168.1.26:8001/queue/status",
        "health_endpoint": "http://192.168.1.26:8001/health",
        "backend_health_endpoint": "http://192.168.1.26:8001/health/backend",
    }
    assert payload["vlm_runtime"] == {
        "mode": "gpu-server",
        "configured": True,
        "base_url": "http://192.168.1.26:8001",
        "backend_url": "http://192.168.1.26:8000/v1",
        "chat_api_base": "http://192.168.1.26:8001/v1",
        "chat_completion_endpoint": "http://192.168.1.26:8001/v1/chat/completions",
        "chat_health_endpoint": "http://192.168.1.26:8001/health/chat",
        "queue_status_endpoint": "http://192.168.1.26:8001/queue/status",
        "health_endpoint": "http://192.168.1.26:8001/health",
        "backend_health_endpoint": "http://192.168.1.26:8001/health/backend",
        "api_key_configured": True,
        "feeder_window": 2,
        "live_probe": _DEFERRED_VLM_PROBE,
    }
    assert "secret-token" not in str(payload)


@pytest.mark.asyncio
async def test_runtime_status_labels_direct_gpu_text_separately_from_screenshot_vlm(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_base"] == "http://192.168.1.26:8000/v1"
    assert payload["effective_runtime"]["route_label"] == "GPU text"
    assert payload["effective_runtime"]["provider_label"] == "local-gemma/gpu-text"
    assert payload["effective_runtime"]["api_base"] == "http://192.168.1.26:8000/v1"
    assert payload["effective_runtime"]["vlm_base_url"] == "http://192.168.1.26:8001"
    assert payload["vlm_runtime"]["chat_api_base"] == "http://192.168.1.26:8001/v1"
    assert payload["vlm_runtime"]["chat_completion_endpoint"].endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_runtime_status_does_not_wait_for_live_vlm_probe(client):
    with (
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch("src.vlm_runtime.probe_effective_vlm_runtime", side_effect=AssertionError("live probe should not run")),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["vlm_runtime"]["live_probe"]["checked"] is False
    assert payload["vlm_runtime"]["live_probe"]["reason"] == "deferred_fast_metadata"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_endpoint",
    [
        "https://user:password@models.example/v1",
        "https://models.example/v1?token=secret",
        "https://models.example/v1#secret-fragment",
        "http://models.example:not-a-port/v1",
        "http://[malformed/v1",
    ],
)
async def test_runtime_status_blanks_unsafe_legacy_endpoints(client, unsafe_endpoint):
    with (
        patch.object(settings, "default_model", "openai-compatible/model"),
        patch.object(settings, "llm_api_base", unsafe_endpoint),
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "local_runtime_paths", ""),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_base"] == ""
    assert payload["default_api_base"] == ""
    assert payload["effective_runtime"]["api_base"] == ""
    assert all(profile.get("api_base") != unsafe_endpoint for profile in payload["provider_profiles"])
    serialized = str(payload)
    assert "password" not in serialized
    assert "token=secret" not in serialized
    assert "secret-fragment" not in serialized


@pytest.mark.asyncio
async def test_browser_provider_api_is_publicly_exposed(client):
    response = await client.get("/api/browser/providers")

    assert response.status_code == 200
