"""OpenRouter-only active model-fabric policy tests."""

from dataclasses import replace

import pytest

from unittest.mock import patch
from config.settings import settings
from src.model_fabric import (
    ACTIVE_PROVIDER_KINDS,
    EndpointClass,
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    OPENROUTER_API_BASE,
    ProviderProfile,
    active_provider_exclusion_reason,
    candidate_from_profile,
    profile_exclusion_reason,
    select_route,
)
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.configuration import (
    CONFIG_SCHEMA_VERSION,
    ModelFabricConfiguration,
    WorkloadPolicy,
    _configuration_from_payload,
    validate_active_model_fabric_configuration,
)
from src.model_fabric.proofs import build_model_route_proof
from src.llm_runtime import effective_runtime_model_id
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
    TrustProvenance,
    canonical_digest,
)


def _profile(**changes) -> ProviderProfile:
    values = {
        "id": "openrouter-test",
        "provider_kind": "openrouter",
        "model": "anthropic/claude-sonnet-4",
        "api_base": OPENROUTER_API_BASE,
        "secret_env": "OPENROUTER_API_KEY",
        "options": {
            "provider": {
                "only": ["anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
            }
        },
        "capabilities": ("text",),
        "task_class": "chat",
        "task_classes": ("chat",),
        "enabled": True,
        "keyless": False,
        "transport_adapter": "openai_compatible_chat",
        "context_window_tokens": 8192,
        "max_output_tokens": 1024,
        "cost_microusd": 500,
        "cost_source": "test-pricing",
        "cost_source_updated_at": 100.0,
        "max_latency_ms": 5000,
    }
    values.update(changes)
    return ProviderProfile(**values)


def _context(*, fallback_allowed: bool = False) -> InferenceRequestContext:
    principal = TrustPrincipal(
        principal_id="operator-policy-test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-policy-test",
    )
    digest = canonical_digest({"message": "hello"})
    return InferenceRequestContext(
        principal=principal,
        session_id=principal.session_id,
        job_id="",
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.OPERATOR_INPUT,
                source_id="policy-test",
                data_digest=digest,
                egress_class=EgressClass.CLOUD_ALLOWED_FULL,
                instruction_authority=True,
            ),
        ),
        data_digest=digest,
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        transformation_digest=canonical_digest({"redaction": "none"}),
        request_id="request-policy-test",
        runtime_path="chat_agent",
        workload=InferenceWorkload.INTERACTIVE,
        requirements=InferenceRequirements(
            capabilities=("text",),
            context_tokens=4096,
            output_tokens=512,
            max_cost_microusd=1000,
            max_local_resource_ms=None,
            max_latency_ms=10000,
            task_class="chat",
        ),
        deadline_at=200.0,
        fallback_allowed=fallback_allowed,
        redaction_applied=False,
    )


def _proofs(profile: ProviderProfile) -> tuple:
    candidate = candidate_from_profile(profile)
    now = 100.0
    values = {
        "text": "supported",
        "context_tokens": 8192,
        "output_tokens": 1024,
        "latency_ms": 5000,
        "task_class": "chat",
        "health": "healthy",
        "cost_microusd": 500,
    }
    return tuple(
        build_model_route_proof(
            profile=profile,
            endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter,
            capability=capability,
            canary_version="policy-test-v1",
            outcome="passed",
            checked_at=now - 1,
            expires_at=now + 100,
            probe_receipt_id=f"probe-{capability}",
            probe_receipt_hash="a" * 64,
            proven_value=value,
        )
        for capability, value in values.items()
    )


def test_active_provider_policy_is_openrouter_only_but_legacy_families_remain_known():
    assert ACTIVE_PROVIDER_KINDS == frozenset({"openrouter"})
    for provider_kind in ("local", "ollama", "openai", "openai_compatible"):
        profile = _profile(provider_kind=provider_kind)
        assert active_provider_exclusion_reason(profile) == "provider_kind_not_allowed"
        assert profile_exclusion_reason(profile) == "provider_kind_not_allowed"


@pytest.mark.parametrize(
    "api_base",
    [
        "http://openrouter.ai/api/v1",
        "https://evil.example/v1",
        "https://openrouter.ai:443/api/v1",
        "https://user:pass@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?token=secret",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai/api/v1/",
    ],
)
def test_active_openrouter_profile_requires_canonical_credential_free_endpoint(api_base):
    assert profile_exclusion_reason(_profile(api_base=api_base)) == (
        "openrouter_endpoint_not_canonical"
    )


def test_active_openrouter_profile_rejects_redirect_following():
    assert profile_exclusion_reason(_profile(follow_redirects=True)) == "redirects_forbidden"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"secret_env": "OPENAI_API_KEY"}, "openrouter_credential_required"),
        ({"keyless": True}, "openrouter_credential_required"),
        ({"fallback_models": ("another-model",)}, "openrouter_fallbacks_forbidden"),
        ({"transport_adapter": "vlm_analyze_file"}, "openrouter_transport_adapter_not_allowed"),
        ({"options": {}}, "openrouter_provider_policy_missing"),
        (
            {"options": {"provider": {"allow_fallbacks": False}}},
            "openrouter_upstream_allowlist_missing",
        ),
        (
            {
                "options": {
                    "provider": {
                        "only": ["anthropic"],
                        "allow_fallbacks": True,
                        "require_parameters": True,
                        "data_collection": "deny",
                    }
                }
            },
            "openrouter_fallbacks_forbidden",
        ),
        (
            {
                "options": {
                    "provider": {
                        "only": ["anthropic"],
                        "allow_fallbacks": False,
                        "require_parameters": False,
                        "data_collection": "deny",
                    }
                }
            },
            "openrouter_parameters_required",
        ),
        (
            {
                "options": {
                    "provider": {
                        "only": ["anthropic"],
                        "allow_fallbacks": False,
                        "require_parameters": True,
                        "data_collection": "allow",
                    }
                }
            },
            "openrouter_data_policy_missing",
        ),
    ],
)
def test_active_openrouter_profile_requires_explicit_route_policy(changes, reason):
    assert profile_exclusion_reason(_profile(**changes)) == reason


def test_openrouter_profile_with_explicit_policy_is_accepted_and_selectable(monkeypatch):
    monkeypatch.setattr("config.settings.settings.openrouter_api_key", "openrouter-test-key")
    profile = _profile()
    assert profile_exclusion_reason(profile) is None
    candidate = candidate_from_profile(profile)
    assert candidate.endpoint == f"{OPENROUTER_API_BASE}/chat/completions"
    assert candidate.endpoint_class is EndpointClass.REMOTE
    decision = select_route(_context(), (candidate,), _proofs(profile), now=100.0)
    assert decision.allowed is True
    assert decision.selected.profile.provider_kind == "openrouter"


def test_selector_never_uses_legacy_local_fallback_even_when_it_is_offered(monkeypatch):
    monkeypatch.setattr("config.settings.settings.openrouter_api_key", "openrouter-test-key")
    primary = _profile()
    local_fallback = replace(
        primary,
        id="legacy-local",
        provider_kind="local",
        api_base="http://127.0.0.1:8000/v1",
        secret_env="",
        keyless=True,
        options=None,
        fallback_models=(),
    )
    decision = select_route(
        _context(fallback_allowed=True),
        (
            candidate_from_profile(local_fallback, source="fallback"),
            candidate_from_profile(primary),
        ),
        _proofs(primary),
        now=100.0,
    )
    assert decision.allowed is True
    assert decision.selected.profile.id == primary.id
    assert decision.rejections[0].reason_code == "provider_kind_not_allowed"


def test_openrouter_phase_rejects_unregistered_sync_route_before_litellm(monkeypatch):
    from src.llm_runtime import completion_with_fallback_sync

    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    with patch("litellm.completion") as transport:
        with pytest.raises(PermissionError, match="registered canonical runtime path"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "should not dispatch"}],
                temperature=0.0,
                max_tokens=32,
                runtime_path="unregistered_route",
            )
    transport.assert_not_called()


def test_historical_provider_agnostic_configuration_can_be_read_but_cannot_be_activated():
    legacy = {
        "id": "historical-local",
        "provider_kind": "local",
        "model": "gemma",
        "api_base": "http://127.0.0.1:8000/v1",
        "capabilities": ["text"],
        "keyless": True,
        "transport_adapter": "openai_compatible_chat",
    }
    configuration = _configuration_from_payload(
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "profiles": [legacy],
            "workload_policies": [],
        }
    )
    assert configuration.status == "ready"
    assert configuration.profiles[0].provider_kind == "local"
    with pytest.raises(ValueError, match="provider_kind_not_allowed"):
        validate_active_model_fabric_configuration(configuration)


def test_active_configuration_rejects_fallback_policy_and_non_openrouter_allowlist():
    profile = _profile()
    with pytest.raises(ValueError, match="disable fallback"):
        validate_active_model_fabric_configuration(
            ModelFabricConfiguration(
                profiles=(profile,),
                workload_policies=(WorkloadPolicy("chat_agent", fallback_allowed=True),),
                status="ready",
            )
        )
    with pytest.raises(ValueError, match="may allow only openrouter"):
        validate_active_model_fabric_configuration(
            ModelFabricConfiguration(
                profiles=(profile,),
                workload_policies=(
                    WorkloadPolicy("chat_agent", allowed_provider_kinds=("local",)),
                ),
                status="ready",
            )
        )


def test_canonical_caller_context_defaults_to_openrouter_and_disables_legacy_fallback(monkeypatch):
    principal = TrustPrincipal(
        principal_id="operator-policy-context",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-policy-context",
    )
    context = build_canonical_inference_context(
        "chat_agent",
        payload={"message": "hello"},
        output_tokens=64,
        timeout_seconds=5,
        principal=principal,
        session_id=principal.session_id,
    )
    assert context.allowed_provider_kinds == ("openrouter",)
    assert context.fallback_allowed is False


def test_canonical_runtime_ignores_persisted_model_override():
    with patch.object(
        settings,
        "runtime_model_overrides",
        "chat_agent=default:openai/gpt-4.1-mini",
    ):
        model_id = effective_runtime_model_id(runtime_path="chat_agent")

    assert model_id == "openrouter/anthropic/claude-sonnet-4"


def test_operator_settings_status_excludes_legacy_profiles_from_active_profiles():
    from src.api.model_fabric_settings import _operator_profile_statuses

    local = _profile(
        id="historical-local",
        provider_kind="local",
        api_base="http://127.0.0.1:8000/v1",
        secret_env="",
        keyless=True,
        options=None,
    )
    openrouter = _profile(id="active-openrouter")
    with patch(
        "src.api.model_fabric_settings.provider_profiles",
        return_value={local.id: local, openrouter.id: openrouter},
    ):
        statuses = _operator_profile_statuses(())

    local_status = next(item for item in statuses if item["id"] == local.id)
    openrouter_status = next(item for item in statuses if item["id"] == openrouter.id)
    assert local_status["model_fabric_eligible"] is False
    assert local_status["model_fabric_exclusion_reason"] == "provider_kind_not_allowed"
    assert openrouter_status["model_fabric_eligible"] is True


def test_openrouter_embeddings_adapter_requires_embedding_capability():
    profile = _profile(
        transport_adapter="openai_compatible_embeddings",
        capabilities=("text",),
    )
    assert profile_exclusion_reason(profile) == (
        "openrouter_embedding_adapter_requires_embedding_capability"
    )

    embedding_profile = _profile(
        transport_adapter="openai_compatible_embeddings",
        capabilities=("embedding",),
        task_class="memory_embedding",
        task_classes=("memory_embedding",),
    )
    assert profile_exclusion_reason(embedding_profile) is None
    assert candidate_from_profile(embedding_profile).endpoint.endswith("/embeddings")


def test_builtin_embedding_profile_is_derived_only_from_explicit_openrouter_model(monkeypatch):
    from src.llm_runtime import _builtin_provider_profiles

    monkeypatch.setattr(settings, "embedding_model", "")
    assert "memory_embedding" not in _builtin_provider_profiles()

    monkeypatch.setattr(
        settings,
        "embedding_model",
        "openrouter/openai/text-embedding-3-small",
    )
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "openai")
    profile = _builtin_provider_profiles()["memory_embedding"]
    assert profile.model == "openai/text-embedding-3-small"
    assert profile.routing_model == "openrouter/openai/text-embedding-3-small"
    assert profile.transport_adapter == "openai_compatible_embeddings"
    assert profile.capabilities == ("embedding",)
    assert profile.options["provider"]["only"] == ["openai"]
