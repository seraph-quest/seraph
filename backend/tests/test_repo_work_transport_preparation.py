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
from src.workflows.repo_repair import RepoRepairError, RepoRepairService
from src.workflows.repo_repair_source import repository_transport_route_digest
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


# ── The consented-egress binding checks body and target (#1184) ──
#
# The real ``bind_near_tee_egress`` binding and ``near_tee_egress_reason``
# comparator (the one prepare_near_tee_call and the precontact guard run).
# The call shape is a literal prepared-call projection; the sealed journeys
# in test_repo_repair_near_egress and the repository suites run the real
# adapter end to end.

CODE = "repository_iteration_final_transport_changed"


@pytest.fixture
def consented(configured_model):
    from src.model_fabric.near_tee_egress import bind_near_tee_egress
    prepared = configured_model.prepare_repository_iteration_transport([{"role": "user", "content": "safe"}],
        response_format=SCHEMA, max_tokens=4096)
    profile, target = _near_target()
    call = SimpleNamespace(body=json.loads(json.dumps(prepared["body"])), slot_name="text", stream=False,
        tool_names=(), headers={"Authorization": "Bearer never-sent", "x-no-aliasing": "true"},
        context=SimpleNamespace(runtime_path="strategist_agent"),
        decision=SimpleNamespace(selected=SimpleNamespace(profile=profile)))
    with bind_near_tee_egress(runtime_path="strategist_agent", body_digest=canonical_digest(prepared["body"]),
            route_digest=prepared["route_digest"], refusal_code=CODE):
        yield call, target


def test_consented_near_call_passes_the_binding(consented):
    from src.model_fabric.near_tee_egress import near_tee_egress_reason
    call, _target = consented
    assert near_tee_egress_reason(call) is None


def test_no_binding_constrains_nothing_and_bindings_never_nest(configured_model):
    from src.model_fabric.near_tee_egress import bind_near_tee_egress, near_tee_egress_reason
    assert near_tee_egress_reason(SimpleNamespace(body={})) is None
    with bind_near_tee_egress(runtime_path="strategist_agent", body_digest="a" * 64, route_digest="b" * 64,
            refusal_code=CODE):
        with pytest.raises(PermissionError, match=CODE):
            with bind_near_tee_egress(runtime_path="strategist_agent", body_digest="c" * 64,
                    route_digest="d" * 64, refusal_code=CODE):
                pytest.fail("a nested binding widened the consented request")
    with pytest.raises(PermissionError):
        with bind_near_tee_egress(runtime_path="strategist_agent", body_digest="not-a-digest",
                route_digest="b" * 64, refusal_code=CODE):
            pytest.fail("a malformed binding was accepted")


@pytest.mark.parametrize("change", ["extra_source", "model", "max_tokens", "no_aliasing", "stream", "tools",
    "runtime_path", "contract_hash", "openrouter_target"])
def test_any_other_body_or_target_is_refused_before_contact(consented, change):
    from src.model_fabric.near_tee_egress import near_tee_egress_reason
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
    elif change == "contract_hash":
        profile = call.decision.selected.profile
        call.decision.selected.profile = SimpleNamespace(id=profile.id, model=profile.model,
            contract_hash="0" * 64)
    if change == "openrouter_target":
        # The historical OpenRouter target is never a repository destination.
        from src.model_fabric.near_tee_egress import is_near_tee_target
        assert is_near_tee_target(target)
        assert not is_near_tee_target({**target, "api_base": "https://openrouter.ai/api/v1",
            "options": {"provider": {"only": ["anthropic"]}}})
        return
    assert near_tee_egress_reason(call) == CODE


def test_real_catalog_prices_never_gate_a_repository_request():
    """ADR-037 (#1284): at NEAR's real Qwen/Qwen3.8-27B prices a
    repository-sized slot (32,768 input, 1,024 output tokens) costs about
    2.7k micro-USD per request. That price is shown, never compared: a slot
    with a small (or no) stored legacy bound is still available."""
    from src.model_fabric.configuration import NearTeeSlot
    from src.model_fabric.near_tee_catalog import evaluate_catalog, parse_catalog_record
    from tests.test_near_tee_text_slot import catalog_record
    facts = parse_catalog_record(json.dumps(catalog_record()).encode(), model_id=NEAR_TEE_TEXT_MODEL)
    def slot(bound):
        return NearTeeSlot(model_id=NEAR_TEE_TEXT_MODEL, max_input_tokens=32768, max_output_tokens=1024,
            request_cost_bound_microusd=bound, enabled=False)
    reason, required = evaluate_catalog(facts, slot_name="text", slot=slot(100))
    assert reason is None and 2_600 <= required <= 2_800
    assert evaluate_catalog(facts, slot_name="text", slot=slot(None))[0] is None
