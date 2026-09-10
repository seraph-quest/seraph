"""Deterministic operator setup tests for the fixed OpenRouter settings route."""

from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import settings
from src.llm_runtime import _profile_api_key, build_completion_kwargs, provider_profiles
from src.model_fabric.configuration import (
    OPENROUTER_VAULT_CREDENTIAL_REF,
    hydrate_openrouter_credential,
)
from src.model_fabric.remote_inference_admission import remote_inference_admission_broker
from src.vault.repository import vault_repository


@pytest.fixture
def model_fabric_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(settings, "openrouter_allow_fallbacks", False)
    monkeypatch.setattr(settings, "openrouter_require_parameters", True)
    monkeypatch.setattr(settings, "openrouter_data_collection", "deny")
    monkeypatch.setattr(settings, "openrouter_zero_data_retention", True)
    monkeypatch.setattr(settings, "screen_analysis_model", "")
    yield tmp_path


def _setup_payload(**overrides):
    payload = {
        "model_ids": ["anthropic/claude-sonnet-4"],
        "capabilities": ["text", "structured_output"],
        "temperature": 0.4,
        "max_output_tokens": 2048,
        "timeout_seconds": 45,
        "allowed_upstreams": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "data_retention_policy": "deny",
        "zero_data_retention": False,
        "egress_class": "cloud_allowed_full",
        "cloud_egress_acknowledged": True,
        "spend_ceiling_microusd": 25_000,
        "max_queued": 8,
        "max_inflight": 1,
        "max_outstanding_per_owner": 4,
        "max_retries": 1,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def keyless_openrouter(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_keyless_setup_is_persisted_and_status_is_configuration_required(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-keyless-sentinel-must-not-appear"
    with patch(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        side_effect=AssertionError("settings must not invoke provider transport"),
    ):
        response = await client.put(
            "/api/settings/model-fabric",
            json={"openrouter": _setup_payload()},
        )
        assert response.status_code == 200
        status = await client.get("/api/settings/model-fabric")

    assert status.status_code == 200
    body = status.json()
    setup = body["openrouter_setup"]
    assert body["status"] == "configuration_required"
    assert setup["status"] == "configuration_required"
    assert setup["credential_configured"] is False
    assert setup["credential_ref"] == OPENROUTER_VAULT_CREDENTIAL_REF
    assert setup["api_base"] == "https://openrouter.ai/api/v1"
    assert "api_key" not in body
    assert sentinel not in response.text
    assert sentinel not in status.text

    persisted = Path(settings.workspace_dir) / "model-fabric-settings.json"
    persisted_text = persisted.read_text(encoding="utf-8")
    assert sentinel not in persisted_text
    assert "openrouter" in body["persisted_profile_ids"]
    profile = next(item for item in body["profiles"] if item["id"] == "openrouter")
    assert profile["api_base"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_secret_input_is_vault_backed_and_only_fingerprint_is_returned(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-test-openrouter-write-only"
    with patch(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        side_effect=AssertionError("settings must not invoke provider transport"),
    ):
        response = await client.put(
            "/api/settings/model-fabric",
            json={"openrouter": _setup_payload(api_key=sentinel)},
        )

    assert response.status_code == 200
    body = response.json()
    setup = body["openrouter_setup"]
    assert setup["credential_configured"] is True
    assert setup["credential_ref"] == OPENROUTER_VAULT_CREDENTIAL_REF
    assert setup["credential_fingerprint"]
    assert sentinel not in response.text
    assert '"api_key"' not in response.text
    assert await vault_repository.get("openrouter_api_key") == sentinel

    config_text = (
        Path(settings.workspace_dir) / "model-fabric-settings.json"
    ).read_text(encoding="utf-8")
    assert sentinel not in config_text


@pytest.mark.asyncio
async def test_persisted_controls_drive_profile_and_remote_admission(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    response = await client.put(
        "/api/settings/model-fabric",
        json={"openrouter": _setup_payload()},
    )
    assert response.status_code == 200

    profile = provider_profiles()["openrouter"]
    controls = profile.options["_seraph_openrouter"]
    assert controls == {
        "model_ids": ["openrouter/anthropic/claude-sonnet-4"],
        "temperature": 0.4,
        "output_limit": 2048,
        "timeout_seconds": 45.0,
        "allowed_upstreams": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "data_retention_policy": "deny",
        "zero_data_retention": False,
        "egress_class": "cloud_allowed_full",
        "cloud_egress_acknowledged": True,
        "max_queued": 8,
        "max_inflight": 1,
        "max_outstanding_per_owner": 4,
        "max_retries": 1,
        "spend_ceiling_microusd": 25_000,
    }
    admission = await remote_inference_admission_broker.status()
    assert admission["max_inflight"] == 1
    assert admission["max_retries"] == 1
    assert admission["capacity"]["max_queued"] == 8
    assert admission["capacity"]["max_outstanding_per_owner"] == 4
    assert admission["capacity"]["max_owner_cost_microusd"] == 25_000
    settings.openrouter_api_key = "test-key"
    runtime_kwargs = build_completion_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        temperature=1.9,
        max_tokens=9999,
        profile="openrouter",
    )
    assert runtime_kwargs["model"] == "openrouter/anthropic/claude-sonnet-4"
    assert runtime_kwargs["temperature"] == 0.4
    assert runtime_kwargs["max_tokens"] == 2048
    assert runtime_kwargs["timeout"] == 45.0
    assert "_seraph_openrouter" not in runtime_kwargs


@pytest.mark.asyncio
async def test_invalid_setup_does_not_store_supplied_secret(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-invalid-policy-must-not-be-stored"
    response = await client.put(
        "/api/settings/model-fabric",
        json={
            "openrouter": _setup_payload(
                allow_fallbacks=True,
                api_key=sentinel,
            )
        },
    )
    assert response.status_code == 422
    assert await vault_repository.get("openrouter_api_key") is None


@pytest.mark.asyncio
async def test_vault_credential_hydrates_before_profile_key_resolution(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-restart-hydration-sentinel"
    response = await client.put(
        "/api/settings/model-fabric",
        json={"openrouter": _setup_payload(api_key=sentinel)},
    )
    assert response.status_code == 200
    settings.openrouter_api_key = ""

    assert await hydrate_openrouter_credential() is True
    assert _profile_api_key("openrouter") == sentinel
    status = await client.get("/api/settings/model-fabric")
    assert sentinel not in status.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("allow_fallbacks", True, "fallbacks"),
        ("capabilities", ["audio"], "capability"),
        ("model_ids", ["anthropic/claude-sonnet-4", "z-ai/glm-5.3-flash"], "multiple openrouter model"),
        ("max_queued", 65, "less than or equal to 64"),
        ("temperature", 2.1, "less than or equal to 2"),
    ),
)
async def test_setup_rejects_unsafe_policy_and_limits(
    client,
    model_fabric_workspace,
    keyless_openrouter,
    field,
    value,
    message,
):
    response = await client.put(
        "/api/settings/model-fabric",
        json={"openrouter": _setup_payload(**{field: value})},
    )
    assert response.status_code == 422
    assert message.lower() in str(response.json()["detail"]).lower()
    assert not (Path(settings.workspace_dir) / "model-fabric-settings.json").exists()


@pytest.mark.asyncio
async def test_setup_rejects_endpoint_injection_and_missing_cloud_ack(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    endpoint_injection = await client.put(
        "/api/settings/model-fabric",
        json={
            "openrouter": {
                **_setup_payload(),
                "api_base": "https://evil.example/v1",
            }
        },
    )
    assert endpoint_injection.status_code == 422

    missing_ack = await client.put(
        "/api/settings/model-fabric",
        json={
            "openrouter": _setup_payload(cloud_egress_acknowledged=False),
        },
    )
    assert missing_ack.status_code == 422
    assert "acknowledgement" in str(missing_ack.json()["detail"]).lower()
