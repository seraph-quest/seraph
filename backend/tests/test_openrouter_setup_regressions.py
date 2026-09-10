"""Focused, provider-free regressions for the persisted OpenRouter setup contract."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from config.settings import settings
from src.api import model_fabric_settings as settings_api
from src.app import _effective_runtime_route_status
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.configuration import (
    ModelFabricConfiguration,
    OpenRouterSetup,
    WorkloadPolicy,
)
from src.model_fabric.contracts import OPENROUTER_API_BASE
from src.model_fabric.gpu_admission import GpuAdmissionRequest
from src.security.trust_contract import (
    AuthorityGrant,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
)


def _setup(**changes) -> OpenRouterSetup:
    values = {
        "profile_id": "openrouter",
        "model_ids": ("openrouter/anthropic/claude-sonnet-4",),
        "capabilities": ("text",),
        "temperature": 0.4,
        "max_output_tokens": 2048,
        "timeout_seconds": 45.0,
        "allowed_upstreams": ("anthropic",),
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "data_retention_policy": "deny",
        "zero_data_retention": False,
        "egress_class": EgressClass.CLOUD_ALLOWED_FULL,
        "cloud_egress_acknowledged": True,
        "spend_ceiling_microusd": 25_000,
        "max_queued": 8,
        "max_inflight": 1,
        "max_outstanding_per_owner": 4,
        "max_retries": 1,
    }
    values.update(changes)
    return OpenRouterSetup(**values)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/settings/model-fabric",
            "headers": [],
            "client": ("127.0.0.1", 43100),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_canonical_context_derives_bounded_cost_and_owner_budget():
    principal = TrustPrincipal(
        principal_id="operator-cost-contract",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-cost-contract",
    )
    policy = WorkloadPolicy(
        "chat_agent",
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        allowed_profile_ids=("openrouter",),
        allowed_provider_kinds=("openrouter",),
        max_cost_microusd=25_000,
    )
    with patch("src.model_fabric.caller_context.effective_workload_policy", return_value=policy):
        context = build_canonical_inference_context(
            "chat_agent",
            payload={"message": "provider-free admission proof"},
            output_tokens=64,
            timeout_seconds=5,
            principal=principal,
            session_id=principal.session_id,
        )

    assert context.owner_budget_microusd == 25_000
    assert context.estimated_cost_microusd is not None
    assert 0 < context.estimated_cost_microusd <= context.owner_budget_microusd
    admission = GpuAdmissionRequest.from_inference_context(context)
    assert admission.estimated_cost_microusd == context.estimated_cost_microusd
    assert admission.owner_budget_microusd == context.owner_budget_microusd


def test_canonical_context_caps_large_estimate_to_persisted_ceiling():
    principal = TrustPrincipal(
        principal_id="operator-cost-cap",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-cost-cap",
    )
    policy = WorkloadPolicy(
        "chat_agent",
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        allowed_provider_kinds=("openrouter",),
        max_cost_microusd=3,
    )
    with patch("src.model_fabric.caller_context.effective_workload_policy", return_value=policy):
        context = build_canonical_inference_context(
            "chat_agent",
            payload={"message": "x" * 1000},
            output_tokens=64,
            timeout_seconds=5,
            principal=principal,
            session_id=principal.session_id,
        )

    assert context.estimated_cost_microusd == 3
    assert context.owner_budget_microusd == 3


def test_setup_input_requires_explicit_deny_retention_policy():
    with pytest.raises(ValidationError, match="data_retention_policy"):
        settings_api.OpenRouterSetupInput.model_validate(
            {
                "model_ids": ["anthropic/claude-sonnet-4"],
                "capabilities": ["text"],
                "allowed_upstreams": ["anthropic"],
                "egress_class": "cloud_allowed_full",
                "cloud_egress_acknowledged": True,
                "spend_ceiling_microusd": 1000,
            }
        )


@pytest.mark.parametrize("field", ["profiles", "policies"])
def test_setup_rejects_api_supplied_owned_configuration(field):
    kwargs = {
        "profiles": (),
        "policies": (),
    }
    if field == "profiles":
        kwargs["profiles"] = (
            settings_api.ModelFabricProfileInput(
                id="historical-local",
                provider_kind="local",
                model="gemma",
                api_base="http://127.0.0.1:8000/v1",
                capabilities=("text",),
                keyless=True,
            ),
        )
    else:
        kwargs["policies"] = (
            settings_api.WorkloadPolicyInput(
                runtime_path="chat_agent",
                egress_class=EgressClass.CLOUD_ALLOWED_FULL,
                cloud_egress_acknowledged=True,
                allowed_profile_ids=("historical-local",),
                allowed_provider_kinds=("local",),
                max_cost_microusd=1000,
            ),
        )

    with pytest.raises(ValueError, match="owns the canonical"):
        settings_api._setup_configuration(_setup(), **kwargs)


def test_blank_key_save_keeps_existing_vault_reference_and_fingerprint(monkeypatch):
    setup = _setup(
        credential_ref=settings_api.OPENROUTER_VAULT_CREDENTIAL_REF,
        credential_fingerprint="oldfinger12",
    )
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    async def get_secret(key):
        assert key == "openrouter_api_key"
        return "existing-vault-key"

    monkeypatch.setattr(settings_api.vault_repository, "get", get_secret)
    result = asyncio.run(settings_api._store_setup_credential(setup, None))

    assert result.credential_ref == settings_api.OPENROUTER_VAULT_CREDENTIAL_REF
    assert result.credential_fingerprint == settings_api._fingerprint_secret("existing-vault-key")


def test_vault_reference_does_not_fall_back_to_legacy_environment_key(monkeypatch):
    persisted = ModelFabricConfiguration(status="ready", openrouter_setup=_setup())
    monkeypatch.setattr("src.model_fabric.configuration.read_model_fabric_configuration", lambda: persisted)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "legacy-environment-key")

    async def missing_secret(key):
        assert key == "openrouter_api_key"
        return None

    monkeypatch.setattr(settings_api.vault_repository, "get", missing_secret)
    from src.model_fabric.configuration import hydrate_openrouter_credential

    assert asyncio.run(hydrate_openrouter_credential()) is False
    assert settings.openrouter_api_key == ""


def test_no_setup_put_refuses_to_replace_persisted_openrouter_setup(monkeypatch):
    persisted = ModelFabricConfiguration(
        profiles=(),
        workload_policies=(),
        status="ready",
        openrouter_setup=_setup(),
    )
    monkeypatch.setattr(settings_api, "read_model_fabric_configuration", lambda: persisted)
    body = settings_api.ModelFabricConfigurationRequest()

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_api.put_model_fabric_settings(body, _request()))

    assert error.value.status_code == 422
    assert "destructive replacement" in str(error.value.detail)


def test_setup_write_restores_vault_and_process_key_if_config_write_fails(monkeypatch):
    setup_input = settings_api.OpenRouterSetupInput(
        model_ids=("anthropic/claude-sonnet-4",),
        capabilities=("text",),
        allowed_upstreams=("anthropic",),
        data_collection="deny",
        data_retention_policy="deny",
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        spend_ceiling_microusd=25_000,
        api_key="replacement-key",
    )
    body = settings_api.ModelFabricConfigurationRequest(openrouter=setup_input)
    persisted = ModelFabricConfiguration(status="missing")
    operations: list[tuple[str, str | None]] = []
    monkeypatch.setattr(settings_api, "read_model_fabric_configuration", lambda: persisted)
    monkeypatch.setattr(settings, "openrouter_api_key", "previous-process-key")

    async def get_secret(key):
        assert key == "openrouter_api_key"
        return "previous-vault-key"

    async def store_secret(key, value, description=None):
        operations.append(("store", value))
        return SimpleNamespace(key=key)

    monkeypatch.setattr(settings_api.vault_repository, "get", get_secret)
    monkeypatch.setattr(settings_api.vault_repository, "store", store_secret)
    monkeypatch.setattr(settings_api, "write_model_fabric_configuration", lambda _config: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_api.put_model_fabric_settings(body, _request()))

    assert error.value.status_code == 503
    assert operations == [("store", "replacement-key"), ("store", "previous-vault-key")]
    assert settings.openrouter_api_key == "previous-process-key"


def test_runtime_readiness_uses_persisted_setup_controls_over_legacy_settings(monkeypatch):
    persisted = ModelFabricConfiguration(
        status="ready",
        profiles=(),
        workload_policies=(),
        openrouter_setup=_setup(),
    )
    monkeypatch.setattr("src.model_fabric.configuration.read_model_fabric_configuration", lambda: persisted)
    monkeypatch.setattr("src.app.read_model_fabric_configuration", lambda: persisted)
    monkeypatch.setattr(settings, "openrouter_provider_only", False)
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "")
    monkeypatch.setattr(settings, "openrouter_allow_fallbacks", True)
    monkeypatch.setattr(settings, "openrouter_require_parameters", False)
    monkeypatch.setattr(settings, "openrouter_data_collection", "allow")
    monkeypatch.setattr(settings, "openrouter_api_key", "configured-for-readiness-test")

    route = _effective_runtime_route_status(
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
            "model_label": "claude-sonnet-4",
            "active_profile": "openrouter",
            "api_base": OPENROUTER_API_BASE,
        },
        {"configured": False},
    )

    readiness = route["inference_readiness"]
    assert readiness["active_only"] is True
    assert readiness["cloud_egress"] == "cloud_allowed_full"
    assert readiness["cloud_consent"] is True
    assert readiness["cost_ceiling_microusd"] == 25_000
    assert "openrouter_only_mode_disabled" not in readiness["reasons"]
    assert "openrouter_upstream_allowlist_missing" not in readiness["reasons"]
    assert "openrouter_fallbacks_enabled" not in readiness["reasons"]
    assert "openrouter_parameter_requirement_disabled" not in readiness["reasons"]
    assert "openrouter_data_policy_not_deny" not in readiness["reasons"]
    assert "openrouter_retention_policy_not_deny" not in readiness["reasons"]
