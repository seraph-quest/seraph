import pytest
from unittest.mock import patch

from config.settings import settings
from src.app import (
    _active_chat_runtime_status,
    _augment_inference_readiness,
    _effective_runtime_route_status,
    _safe_runtime_endpoint,
)


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


def test_active_runtime_ignores_legacy_local_preference_and_uses_openrouter():
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
        patch.object(settings, "runtime_model_overrides", ""),
    ):
        runtime = _active_chat_runtime_status()

    assert runtime["model"] == "x-ai/grok-4.1-fast"
    assert runtime["active_profile"] == "openrouter"


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
    unrelated_remote = _effective_runtime_route_status(
        {**base_runtime, "api_base": "https://api.example.com/v1"},
        vlm,
    )

    assert direct["route_label"] == "GPU text"
    assert direct["provider_label"] == "local-gemma/gpu-text"
    assert wrapper["route_label"] == "GPU wrapper chat"
    assert wrapper["provider_label"] == "local-gemma/gpu-wrapper-chat"
    assert mac_local["route_label"] == "local Gemma"
    assert mac_local["provider_label"] == "local-gemma"
    assert unrelated_remote["route_label"] == "local Gemma"
    assert unrelated_remote["provider_label"] == "local-gemma"


def test_openrouter_runtime_receipt_stays_blocked_until_profile_and_proofs_are_routable():
    route = {
        "provider": "openrouter",
        "active_profile": "openrouter",
        "inference_ready": True,
        "inference_readiness": {"reasons": []},
    }
    fabric = {
        "profiles": [{
            "id": "openrouter",
            "model_fabric_eligible": True,
            "routable": False,
            "non_routable_reasons": ["cost_bound_missing"],
        }],
        "proofs": [{"profile_id": "openrouter", "capability": "health", "status": "missing"}],
    }

    receipt = _augment_inference_readiness(route, fabric)

    assert receipt["inference_ready"] is False
    assert receipt["inference_readiness"]["status"] == "configuration_required"
    assert "model_fabric_cost_bound_missing" in receipt["inference_readiness"]["reasons"]
    assert "model_fabric_proof_missing:health" in receipt["inference_readiness"]["reasons"]


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
async def test_cors_uses_exact_origins_without_loopback_port_wildcard(client):
    rejected = await client.options(
        "/api/capabilities/overview",
        headers={
            "Origin": "http://localhost:9999",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers

    allowed = await client.options(
        "/api/capabilities/overview",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3001"


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
    assert payload["model"] == settings.default_model.removeprefix("openrouter/")
    assert payload["model_label"] == settings.default_model.split("/")[-1]
    assert payload["active_profile"] == "openrouter"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == settings.default_model
    assert isinstance(payload["provider_profiles"], list)
    assert "local_operators" not in payload
    assert any(item["id"] == "openrouter" for item in payload["provider_profiles"])
    assert all("api_key" not in item for item in payload["provider_profiles"])


@pytest.mark.asyncio
async def test_runtime_status_rejects_removed_local_codex_when_selected(client):
    with patch.object(settings, "default_model", "codex-local"):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 410
    payload = response.json()
    assert payload["detail"]["code"] == "external_agent_runtime_removed"


@pytest.mark.asyncio
async def test_runtime_status_reports_openrouter_when_local_preference_is_stale(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "x-ai/grok-4.1-fast"
    assert payload["model_label"] == "grok-4.1-fast"
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["active_profile"] == "openrouter"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == "openrouter/x-ai/grok-4.1-fast"


@pytest.mark.asyncio
async def test_runtime_status_reports_openrouter_and_historical_vlm_metadata(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", ""),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["active_profile"] == "openrouter"
    assert payload["effective_runtime"]["provider"] == "openrouter"
    assert payload["effective_runtime"]["active_provider_policy"] == "openrouter_only"
    assert payload["vlm_runtime"]["active"] is False
    assert payload["vlm_runtime"]["disabled_reason"] == "local_vlm_disabled_openrouter_only"
    assert payload["vlm_runtime"]["live_probe"]["checked"] is False
    assert payload["vlm_runtime"]["live_probe"]["reason"] == "deferred_fast_metadata"
    assert "secret-token" not in str(payload)


@pytest.mark.asyncio
async def test_runtime_status_does_not_activate_direct_gpu_text(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
        patch.object(settings, "openrouter_provider_only", False),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["effective_runtime"]["route_label"] == "openrouter"
    assert payload["effective_runtime"]["provider_label"] == "openrouter"
    assert payload["vlm_runtime"]["active"] is False
    assert payload["vlm_runtime"]["disabled_reason"] == "local_vlm_disabled_openrouter_only"
    assert payload["vlm_runtime"]["live_probe"]["checked"] is False
    assert payload["vlm_runtime"]["live_probe"]["reason"] == "deferred_fast_metadata"


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
async def test_runtime_status_uses_openrouter_and_blanks_unsafe_legacy_endpoints(client, unsafe_endpoint):
    with (
        patch.object(settings, "default_model", "openai-compatible/model"),
        patch.object(settings, "llm_api_base", unsafe_endpoint),
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "local_runtime_paths", ""),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["default_api_base"] == ""
    assert payload["effective_runtime"]["api_base"] == "https://openrouter.ai/api/v1"
    assert all(profile.get("api_base") != unsafe_endpoint for profile in payload["provider_profiles"])
    serialized = str(payload)
    assert "password" not in serialized
    assert "token=secret" not in serialized
    assert "secret-fragment" not in serialized


@pytest.mark.asyncio
async def test_browser_provider_api_is_publicly_exposed(client):
    response = await client.get("/api/browser/providers?owner_session_id=test-auth-bypass")

    assert response.status_code == 200
