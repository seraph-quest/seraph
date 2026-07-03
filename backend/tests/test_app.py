import pytest
from unittest.mock import patch

from config.settings import settings


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
    assert payload["model"] == "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"
    assert payload["model_label"] == "gemma-4-26B-A4B-it-qat-GGUF"
    assert payload["api_base"] == "http://127.0.0.1:8000/v1"
    assert payload["active_profile"] == "local-gemma-chat-thinking"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == "openrouter/x-ai/grok-4.1-fast"


@pytest.mark.asyncio
async def test_browser_provider_api_is_publicly_exposed(client):
    response = await client.get("/api/browser/providers")

    assert response.status_code == 200
