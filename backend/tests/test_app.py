import pytest

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
    response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "2026.4.10"
    assert payload["build_id"] == "SERAPH_PRIME_v2026.4.10"
    assert payload["provider"] == "openrouter"
    assert payload["model"] == settings.default_model
    assert payload["model_label"] == settings.default_model.split("/")[-1]


@pytest.mark.asyncio
async def test_browser_provider_api_is_publicly_exposed(client):
    response = await client.get("/api/browser/providers")

    assert response.status_code == 200
