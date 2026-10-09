"""Pure preparation checks; these do not assert a source-authorized dispatch."""
import json

import pytest

from src import llm_runtime
from src.llm_runtime import FallbackLiteLLMModel
from src.model_fabric.contracts import ProviderProfile
from src.workflows.repo_repair import RepoRepairError, RepoRepairService
from src.workflows.repo_repair_source import repository_transport_route_digest


@pytest.fixture
def configured_model(monkeypatch):
    profile = ProviderProfile(id="repository-fixture", provider_kind="openrouter",
        model="fixture/model", routing_model="openrouter/fixture/model",
        api_base="https://openrouter.ai/api/v1", keyless=True,
        options={"_seraph_openrouter": {"temperature": 0.2, "output_limit": 512,
            "timeout_seconds": 10}})
    monkeypatch.setattr(llm_runtime, "provider_profiles", lambda: {profile.id: profile})
    model = FallbackLiteLLMModel(model_id="openrouter/fixture/model", api_key="",
        api_base=profile.api_base, runtime_profile=profile.id,
        runtime_path="strategist_agent", max_tokens=4096,
        provider={"only": ["fixture-upstream"], "allow_fallbacks": False})
    return model


def test_preparation_includes_configured_model_options_controls_and_output_schema(configured_model):
    messages = [{"role": "user", "content": "safe"}]
    schema = {"type": "json_schema", "json_schema": {"name": "patch", "schema": {"type": "object"}}}
    prepared = configured_model.prepare_repository_iteration_transport(messages,
        response_format=schema, max_tokens=4096)
    body = prepared["body"]
    assert body["model"] == "fixture/model"
    assert body["messages"] == messages
    assert body["provider"] == {"only": ["fixture-upstream"], "allow_fallbacks": False}
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.2
    assert body["response_format"] == schema


@pytest.mark.parametrize("field,value", [("_runtime_path", "chat_agent"),
    ("api_base", "https://other.invalid/v1"), ("_runtime_profile", "missing")])
def test_nonfixed_or_missing_profile_cannot_prepare_iteration_body(configured_model, field, value):
    setattr(configured_model, field, value)
    with pytest.raises(PermissionError):
        configured_model.prepare_repository_iteration_transport([], response_format={}, max_tokens=4096)


def test_each_route_field_is_part_of_exact_primary_identity():
    target = {"source": "primary", "api_base": "https://openrouter.ai/api/v1",
        "profile": "profile", "model_id": "openrouter/fixture/model", "options": {"provider": "one"}}
    original = repository_transport_route_digest(target, "strategist_agent")
    for key, value in [("source", "fallback"), ("api_base", "https://other.invalid/v1"),
                       ("profile", "different"), ("model_id", "other/model"),
                       ("options", {"provider": "two"})]:
        assert repository_transport_route_digest({**target, key: value}, "strategist_agent") != original


def test_final_body_cap_includes_model_options_and_schema(configured_model):
    service = RepoRepairService()
    prepared = {"messages": [{"role": "user", "content": "safe"}]}
    result = service.finalize_iteration_egress(prepared, model=configured_model,
        response_format={"type": "json_object"})
    encoded = json.dumps(result["request_body"], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    assert result["combined_input_bytes"] == len(encoded)
    assert len(encoded) > len(json.dumps(prepared["messages"]).encode())
    with pytest.raises(RepoRepairError, match="model, options and schema"):
        service.finalize_iteration_egress(prepared, model=configured_model,
            response_format={"type": "json_object"}, original_input_byte_limit=len(encoded) - 1)
