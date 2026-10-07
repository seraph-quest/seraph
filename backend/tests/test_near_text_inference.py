"""Actual httpx boundary and real trust evaluator; no operational HTTP."""

from dataclasses import replace
import json
import time
from types import SimpleNamespace
from uuid import NAMESPACE_DNS, uuid5

import httpx
import pytest
from pydantic import ValidationError

from src.model_fabric import near_text
from src.model_fabric.contracts import InferenceRequestContext, InferenceRequirements, InferenceWorkload, ProviderProfile
from src.model_fabric.near_text_contracts import NearTextInput, NearTextError
from src.security.trust_contract import AuthorityGrant, ContentOrigin, EgressClass, PrincipalType, TrustPrincipal, TrustProvenance, canonical_digest


def context(**changes):
    digest = canonical_digest({"question": "private question"})
    values = dict(principal=TrustPrincipal("owner", PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,), session_id="root", job_id="job"),
        session_id="root", job_id="job", provenance=(TrustProvenance(ContentOrigin.OPERATOR_INPUT,
        "input", digest, EgressClass.CLOUD_ALLOWED_FULL),), data_digest=digest,
        egress_class=EgressClass.CLOUD_ALLOWED_FULL, transformation_digest=canonical_digest({"purpose": "text"}),
        request_id="request", runtime_path="near_text_native", workload=InferenceWorkload.INTERACTIVE,
        requirements=InferenceRequirements(("text",),8192,32,100,None,45000,"near_text"),
        deadline_at=time.time()+120, allowed_profile_ids=("near.text",), allowed_provider_kinds=("near",))
    values.update(changes)
    return InferenceRequestContext(**values)


def profile(**changes):
    values = dict(id="near.text",provider_kind="near",model=near_text.MODEL,api_base=near_text.BASE,
                  capabilities=("text",),secret_env="vault:near_text_api_key")
    values.update(changes)
    return ProviderProfile(**values)


@pytest.mark.parametrize("values", [dict(question=""),dict(question=" "),dict(question="é"*4097),
    dict(question=123),dict(max_output_tokens=True),dict(max_output_tokens=0),dict(max_output_tokens=1025),
    dict(extra="override")])
def test_closed_input_bounds(values):
    base=dict(schema_version="seraph.near.text.input.v1",question="question",max_output_tokens=32)
    base.update(values)
    with pytest.raises(ValidationError): NearTextInput(**base)


def test_actual_trust_allow_and_revoke():
    decision=near_text.preflight_near_text(context(),profile())
    assert decision.allowed and decision.trust_decision_id and not decision.proof_hashes
    original=context()
    revoked=replace(original.principal,revoked=True)
    decision=near_text.preflight_near_text(replace(original,principal=revoked),profile())
    assert not decision.allowed and decision.rejections


@pytest.mark.parametrize("changes",[dict(runtime_path="chat_agent"),dict(fallback_allowed=True),
    dict(allowed_profile_ids=("openrouter",)),dict(allowed_provider_kinds=("openrouter",))])
def test_fixed_route_rejects_alternate_context(changes):
    with pytest.raises(NearTextError): near_text.preflight_near_text(context(**changes),profile())


@pytest.mark.parametrize("changes",[dict(model="alias"),dict(api_base="https://evil.invalid/v1"),
    dict(options={"tools":[{}]}),dict(fallback_models=("other",)),dict(capabilities=("text","tool_use"))])
def test_fixed_route_rejects_profile_override(changes):
    with pytest.raises(NearTextError): near_text.preflight_near_text(context(),profile(**changes))


class Stream(httpx.AsyncByteStream):
    def __init__(self,raw): self.raw=raw
    async def __aiter__(self):
        yield self.raw


class Hooks:
    async def attempt_started(self,**kwargs): pass
    async def attempt_finished(self,**kwargs): pass


@pytest.fixture
def transport_setup(monkeypatch):
    from src.model_fabric import configuration,effective_policy,accounting
    setup=SimpleNamespace(max_output_tokens=64,timeout_seconds=45,credential_fingerprint="fingerprint",
                          request_cost_bound_microusd=100)
    config=SimpleNamespace(near_text=setup,egress_revision=3)
    state={"config":config,"policy":"a"*64,"calls":[],"evidence":[],"checks":0}
    monkeypatch.setattr(effective_policy,"current_near_text_policy",lambda:(state["config"],state["policy"]))
    monkeypatch.setattr(configuration,"near_text_profile_for_setup",lambda setup:profile())
    async def key(**kwargs):
        assert kwargs==dict(expected_revision=3,expected_fingerprint="fingerprint")
        return "private-fixture-key"
    monkeypatch.setattr(configuration,"capture_near_text_credential",key)
    monkeypatch.setattr(accounting,"current_near_accounting_operation_id",lambda:"operation")
    monkeypatch.setattr(accounting,"capture_near_billing_evidence",state["evidence"].append)
    # Isolate transport tests from accounting fixtures, preserving actual trust evaluator.
    # Real broker/contact/settlement proof belongs the separate native/accounting vertical.
    async def adapter_run(**kwargs):
        assert kwargs["decision"].allowed
        return await kwargs["adapter"](kwargs["decision"].selected,False)
    monkeypatch.setattr(near_text,"run_preflighted_adapter",adapter_run)
    actual_client=httpx.AsyncClient
    def configure(handler):
        async def observe(request):
            state["calls"].append(request)
            return await handler(request)
        monkeypatch.setattr(near_text.httpx,"AsyncClient",lambda **kwargs:
            actual_client(transport=httpx.MockTransport(observe),**kwargs))
    async def current(): state["checks"]+=1
    state.update(configure=configure,validate=current)
    return state


def completion(**changes):
    result=dict(id="completion-123",model=near_text.MODEL,choices=[dict(finish_reason="stop",
        message=dict(role="assistant",content="private answer"))])
    result.update(changes)
    return result


def response(request,payload,**kwargs):
    raw=payload if isinstance(payload,bytes) else json.dumps(payload).encode()
    return httpx.Response(kwargs.pop("status",200),stream=Stream(raw),request=request,**kwargs)


async def invoke(state,**kwargs):
    return await near_text.invoke_near_text(context=context(),question="private question",max_output_tokens=32,
        validate_current=state["validate"],hooks=Hooks(),**kwargs)


@pytest.mark.asyncio
async def test_one_http_inference_then_exact_billing(transport_setup):
    state=transport_setup
    provider_id=str(uuid5(NAMESPACE_DNS,"completion-123"))
    async def handler(request):
        assert request.headers["authorization"]=="Bearer private-fixture-key"
        assert request.headers["accept-encoding"]=="identity"
        body=json.loads(request.content)
        if request.url.path.endswith("chat/completions"):
            assert body==dict(model=near_text.MODEL,messages=[dict(role="user",content="private question")],
                              max_tokens=32,n=1,stream=False)
            return response(request,completion(),headers={"inference-id":provider_id})
        assert str(request.url)==near_text.BASE+"/billing/costs"
        assert body=={"requestIds":[provider_id]}
        return response(request,{"requests":[{"requestId":provider_id,"costNanoUsd":1001}]})
    state["configure"](handler)
    answer=await invoke(state)
    assert answer.text=="private answer" and "private answer" not in repr(answer)
    assert answer.receipt.cost_microusd==2 and answer.receipt.memory_status=="no_learning"
    assert len(state["calls"])==2 and len(state["evidence"])==1 and state["checks"]==3


@pytest.mark.asyncio
@pytest.mark.parametrize("payload",[completion(model="alias"),completion(choices=[]),
    completion(choices=[{},{}]),completion(choices=[dict(finish_reason="tool_calls",message=dict(role="assistant",content="answer"))]),
    completion(choices=[dict(finish_reason="stop",message=dict(role="assistant",content="answer",tool_calls=[{}]))]),
    completion(choices=[dict(finish_reason="stop",message=dict(role="assistant",content="answer",refusal="no"))])])
async def test_invalid_answer_still_captures_charge_without_answer(transport_setup,payload):
    state=transport_setup
    provider_id=str(uuid5(NAMESPACE_DNS,"completion-123"))
    async def handler(request):
        if request.url.path.endswith("chat/completions"): return response(request,payload)
        return response(request,{"requests":[{"requestId":provider_id,"costNanoUsd":0}]})
    state["configure"](handler)
    with pytest.raises(NearTextError,match="near_text_answer_invalid"): await invoke(state)
    assert len(state["calls"])==2 and len(state["evidence"])==1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["redirect","gzip","oversize","bad_json","timeout","bad_header"])
async def test_transport_negative_never_retries_inference(transport_setup,kind):
    state=transport_setup
    async def handler(request):
        if kind=="timeout": raise httpx.ReadTimeout("private error",request=request)
        if kind=="redirect": return response(request,b"",status=307,headers={"location":"https://evil.invalid"})
        if kind=="gzip": return response(request,b"",headers={"content-encoding":"gzip"})
        if kind=="oversize": return response(request,b"x"*262145)
        if kind=="bad_json": return response(request,b'{"id":"one","id":"two"}')
        return response(request,completion(),headers={"inference-id":"malformed"})
    state["configure"](handler)
    with pytest.raises(ValueError): await invoke(state)
    assert len(state["calls"])==1 and not state["evidence"]


@pytest.mark.asyncio
async def test_two_billing_attempts_unknown_releases_no_answer(transport_setup):
    state=transport_setup
    async def handler(request):
        if request.url.path.endswith("chat/completions"): return response(request,completion())
        return response(request,{"requests":[]})
    state["configure"](handler)
    with pytest.raises(NearTextError,match="near_text_cost_liability"): await invoke(state)
    assert len(state["calls"])==3 and not state["evidence"]


@pytest.mark.asyncio
async def test_revocation_after_contact_settles_but_never_returns_answer(transport_setup):
    state=transport_setup
    provider_id=str(uuid5(NAMESPACE_DNS,"completion-123"))
    async def handler(request):
        if request.url.path.endswith("chat/completions"):
            state["policy"]="b"*64
            return response(request,completion())
        return response(request,{"requests":[{"requestId":provider_id,"costNanoUsd":1}]})
    state["configure"](handler)
    with pytest.raises(NearTextError,match="near_text_adoption_stale"): await invoke(state)
    assert len(state["evidence"])==1 and len(state["calls"])==2


@pytest.mark.asyncio
async def test_current_config_cap_denies_before_http(transport_setup):
    state=transport_setup
    state["config"].near_text.max_output_tokens=16
    with pytest.raises(NearTextError,match="near_text_output_cap_exceeded"): await invoke(state)
    assert not state["calls"] and not state["evidence"]


@pytest.mark.asyncio
async def test_genuine_overrun_charge_is_captured_without_answer(transport_setup):
    state=transport_setup
    provider_id=str(uuid5(NAMESPACE_DNS,"completion-123"))
    async def handler(request):
        if request.url.path.endswith("chat/completions"): return response(request,completion())
        return response(request,{"requests":[{"requestId":provider_id,"costNanoUsd":100001}]})
    state["configure"](handler)
    with pytest.raises(NearTextError,match="near_text_cost_overrun"): await invoke(state)
    assert len(state["calls"])==2 and state["evidence"][0].cost_microusd==101


@pytest.mark.asyncio
async def test_contact_cap_race_denies_before_inference(transport_setup):
    state=transport_setup
    async def current():
        state["checks"]+=1
        if state["checks"]==2: state["config"].near_text.max_output_tokens=1
    state["validate"]=current
    with pytest.raises(NearTextError,match="near_text_policy_changed"): await invoke(state)
    assert not state["calls"] and not state["evidence"]


@pytest.mark.asyncio
async def test_authority_denial_before_capture_or_inference(transport_setup):
    state=transport_setup
    async def current(): raise NearTextError("root_revoked")
    state["validate"]=current
    with pytest.raises(NearTextError,match="root_revoked"): await invoke(state)
    assert not state["calls"] and not state["evidence"]


@pytest.mark.asyncio
async def test_input_output_limit_cannot_differ_from_admitted_context(transport_setup):
    state=transport_setup
    with pytest.raises(NearTextError,match="near_text_input_binding_invalid"):
        await near_text.invoke_near_text(context=context(),question="question",max_output_tokens=64,
            validate_current=state["validate"],hooks=Hooks())
    assert not state["calls"]


@pytest.mark.asyncio
async def test_answer_byte_limit_still_reads_real_charge(transport_setup):
    state=transport_setup
    provider_id=str(uuid5(NAMESPACE_DNS,"completion-123"))
    payload=completion(choices=[dict(finish_reason="stop",message=dict(role="assistant",content="x"*65537))])
    async def handler(request):
        if request.url.path.endswith("chat/completions"): return response(request,payload)
        return response(request,{"requests":[{"requestId":provider_id,"costNanoUsd":1}]})
    state["configure"](handler)
    with pytest.raises(NearTextError,match="near_text_answer_invalid"): await invoke(state)
    assert len(state["evidence"])==1 and len(state["calls"])==2


def test_actual_trust_original_expired_deadline_denied():
    decision=near_text.preflight_near_text(context(deadline_at=time.time()-1),profile())
    assert not decision.allowed and decision.rejections[0].reason_code=="decision_expired"
