"""Pure preparation checks; these do not assert a source-authorized dispatch.

#1184: the repository iteration body is the exact NEAR TEE text-slot body,
its route digest names the NEAR target, and the NEAR sync path refuses any
other body or target before contact. Configuration is local; nothing here
contacts a provider.
"""
import json
from types import SimpleNamespace

import pytest

from src.llm_runtime import FallbackLiteLLMModel
from src.model_fabric.configuration import NEAR_TEE_API_BASE, near_tee_profile_for_slot, read_model_fabric_configuration
from src.model_fabric.near_tee_vertical import repository_repair_near_target
from src.security.trust_contract import canonical_digest
from src.workflows import repo_repair_source
from src.workflows.inference_accounting import InferenceAccountingError
from src.workflows.repo_repair import RepoRepairError, RepoRepairService
from src.workflows.repo_repair_source import (repository_transport_route_digest,
    verify_repository_iteration_near_call, verify_repository_iteration_transport_body)
from tests.near_tee_support import NEAR_TEE_TEXT_MODEL, write_near_tee_text_configuration
from tests.test_openrouter_setup_api import model_fabric_workspace  # noqa: F401

SCHEMA = {"type": "json_schema", "json_schema": {"name": "patch", "schema": {"type": "object"}}}


@pytest.fixture
def configured_model(model_fabric_workspace):
    write_near_tee_text_configuration()
    return FallbackLiteLLMModel(model_id="openrouter/fixture/model", api_key="",
        api_base="https://openrouter.ai/api/v1", runtime_path="strategist_agent", max_tokens=4096,
        temperature=0.2, provider={"only": ["fixture-upstream"], "allow_fallbacks": False})


def _near_target():
    configured = read_model_fabric_configuration()
    profile = near_tee_profile_for_slot("text", configured.near_tee.slots["text"])
    return profile, repository_repair_near_target(profile_id=profile.id,
        profile_contract_hash=profile.contract_hash, model_id=NEAR_TEE_TEXT_MODEL)


def test_preparation_is_the_exact_near_tee_body_and_target(configured_model):
    messages = [{"role": "user", "content": "safe"}]
    prepared = configured_model.prepare_repository_iteration_transport(messages,
        response_format=SCHEMA, max_tokens=4096)
    # The NEAR TEE text-slot body: canonical model id, the slot's output
    # bound, no OpenRouter provider routing and nothing beyond the messages.
    assert prepared["body"] == {"model": NEAR_TEE_TEXT_MODEL, "messages": messages, "max_tokens": 1024,
        "temperature": 0.2, "n": 1, "stream": False, "response_format": SCHEMA}
    _profile, target = _near_target()
    assert target["api_base"] == NEAR_TEE_API_BASE == "https://cloud-api.near.ai/v1"
    assert target["options"] == {"provider_kind": "near_tee", "profile_contract_hash": _profile.contract_hash,
        "x-no-aliasing": True}
    assert prepared["route_digest"] == repository_transport_route_digest(target, "strategist_agent")


@pytest.mark.parametrize("world", ["chat_agent", "no_near_slot", "egress_revoked", "disabled_slot"])
def test_no_fixed_near_tee_route_cannot_prepare_iteration_body(configured_model, world):
    from src.model_fabric.configuration import ModelFabricConfiguration, write_model_fabric_configuration
    if world == "chat_agent":
        configured_model._runtime_path = "chat_agent"
    elif world == "no_near_slot":
        write_model_fabric_configuration(ModelFabricConfiguration(status="ready", egress_revision=3))
    elif world == "egress_revoked":
        write_near_tee_text_configuration(egress_revision=3, egress_revoked=True)
    else:
        from dataclasses import replace
        configured = read_model_fabric_configuration()
        slot = replace(configured.near_tee.slots["text"], enabled=False, purpose_consent_revision=None)
        write_model_fabric_configuration(replace(configured, egress_revision=3,
            near_tee=replace(configured.near_tee, slots={"text": slot})))
    with pytest.raises(PermissionError):
        configured_model.prepare_repository_iteration_transport([{"role": "user", "content": "safe"}],
            response_format=SCHEMA, max_tokens=4096)


def test_each_route_field_is_part_of_exact_primary_identity():
    target = repository_repair_near_target(profile_id="near_tee.text", profile_contract_hash="a" * 64,
        model_id=NEAR_TEE_TEXT_MODEL)
    original = repository_transport_route_digest(target, "strategist_agent")
    for key, value in [("source", "fallback"), ("api_base", "https://openrouter.ai/api/v1"),
                       ("profile", "different"), ("model_id", "other/model"),
                       ("options", {**target["options"], "profile_contract_hash": "b" * 64})]:
        assert repository_transport_route_digest({**target, key: value}, "strategist_agent") != original


def test_final_body_cap_includes_model_options_and_schema(configured_model):
    service = RepoRepairService()
    prepared = {"messages": [{"role": "user", "content": "safe"}]}
    result = service.finalize_iteration_egress(prepared, model=configured_model,
        response_format={"type": "json_object"})
    assert result["request_body"]["model"] == NEAR_TEE_TEXT_MODEL
    encoded = json.dumps(result["request_body"], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    assert result["combined_input_bytes"] == len(encoded)
    assert len(encoded) > len(json.dumps(prepared["messages"]).encode())
    with pytest.raises(RepoRepairError, match="model, options and schema"):
        service.finalize_iteration_egress(prepared, model=configured_model,
            response_format={"type": "json_object"}, original_input_byte_limit=len(encoded) - 1)


# ── The NEAR sync path checks the consented body and target (#1184) ──
#
# The comparison only: the witness seal itself is never counterfeited here
# (its own negatives live in test_repository_iteration_accounting); the
# journeys in the repository suites run the sealed positive path.


@pytest.fixture
def consented(configured_model, monkeypatch):
    prepared = configured_model.prepare_repository_iteration_transport([{"role": "user", "content": "safe"}],
        response_format=SCHEMA, max_tokens=4096)
    witness = SimpleNamespace(_request_body_digest=canonical_digest(prepared["body"]),
        _request_route_digest=prepared["route_digest"])
    from src.model_fabric import accounting
    monkeypatch.setattr(accounting, "current_repository_iteration_accounting_witness", lambda: witness)
    monkeypatch.setattr(repo_repair_source, "assert_repository_iteration_witness", lambda _witness: None)
    profile, target = _near_target()
    call = SimpleNamespace(body=dict(prepared["body"]), slot_name="text", stream=False, tool_names=(),
        headers={"Authorization": "Bearer never-sent", "x-no-aliasing": "true"},
        context=SimpleNamespace(runtime_path="strategist_agent"),
        decision=SimpleNamespace(selected=SimpleNamespace(profile=profile)))
    return call, target


def test_consented_near_call_passes_the_sync_path_check(consented):
    call, target = consented
    verify_repository_iteration_near_call(call, runtime_path="strategist_agent")
    assert repo_repair_source.repository_iteration_near_call_reason(call) is None


@pytest.mark.parametrize("change", ["extra_source", "model", "max_tokens", "no_aliasing", "stream", "tools",
    "runtime_path", "openrouter_target"])
def test_any_other_body_or_target_is_refused_before_contact(consented, change):
    call, target = consented
    if change == "extra_source":
        call.body["messages"] = [*call.body["messages"], {"role": "user", "content": "unconsented source"}]
    elif change == "model":
        call.body["model"] = "z-ai/glm-5.3-flash"
    elif change == "max_tokens":
        call.body["max_tokens"] = 1023
    elif change == "no_aliasing":
        call.headers.pop("x-no-aliasing")
    elif change == "stream":
        call.stream = True
    elif change == "tools":
        call.tool_names = ("final_answer",)
    elif change == "runtime_path":
        call.context.runtime_path = "chat_agent"
    if change == "openrouter_target":
        # The historical OpenRouter target is never a repository destination.
        with pytest.raises(InferenceAccountingError, match="repository_iteration_final_transport_changed"):
            verify_repository_iteration_transport_body(call.body, target={**target,
                "api_base": "https://openrouter.ai/api/v1", "options": {"provider": {"only": ["anthropic"]}}},
                runtime_path="strategist_agent")
        return
    with pytest.raises(InferenceAccountingError, match="repository_iteration_final_transport_changed"):
        verify_repository_iteration_near_call(call, runtime_path="strategist_agent")
    assert (repo_repair_source.repository_iteration_near_call_reason(call)
        == "repository_iteration_final_transport_changed")
