"""Focused contract and fail-closed selector tests for model fabric v1."""

from dataclasses import replace
import hashlib
import time

import pytest

from src.llm_runtime import _attemptable_targets, _profile_from_payload
from src.model_fabric import (
    EndpointClass,
    GpuAdmissionCapacityError,
    GpuAdmissionReceipt,
    GpuPriority,
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    ModelRouteProof,
    NoCompliantModelRouteError,
    ProviderProfile,
    ReceiptPersistenceResult,
    RouteReceiptSession,
    bind_final_inference_payload,
    candidate_from_profile,
    classify_endpoint,
    execute_streaming,
    preflight_candidate,
    provider_family_exclusion_reason,
    profile_exclusion_reason,
    finalized_openai_compatible_body,
    transport_model_for_provider,
    select_route,
)
from src.model_fabric.proofs import build_model_route_proof
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    EgressClass,
    NO_TRANSFORMATION,
    PrincipalType,
    TrustPrincipal,
    TrustProvenance,
    canonical_digest,
)


def _profile(*, remote: bool = False, **changes) -> ProviderProfile:
    values = {
        "id": "remote" if remote else "local",
        "provider_kind": "openai_compatible" if remote else "local",
        "model": "model-v1",
        "api_base": "https://models.example/v1" if remote else "http://127.0.0.1:8000/v1",
        "capabilities": ("text",),
        "task_class": "chat",
        "task_classes": ("chat",),
        "transport_adapter": "openai_compatible_chat",
        "keyless": True,
        "context_window_tokens": 8192,
        "max_output_tokens": 1024,
        "cost_microusd": 500 if remote else None,
        "cost_source": "test-pricing" if remote else None,
        "cost_source_updated_at": 99.0 if remote else None,
        "local_resource_ms": None if remote else 5000,
        "max_latency_ms": 5000,
    }
    values.update(changes)
    return ProviderProfile(**values)


def _context(*, remote: bool = False, workload=InferenceWorkload.INTERACTIVE, **changes):
    values = {
        "principal": TrustPrincipal(
            principal_id="operator-1",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="session-1",
        ),
        "session_id": "session-1",
        "job_id": "",
        "provenance": (
            TrustProvenance(
                origin=ContentOrigin.OPERATOR_INPUT,
                source_id="message-1",
                data_digest=canonical_digest({"message": 1}),
                egress_class=(EgressClass.CLOUD_ALLOWED_REDACTED if remote else EgressClass.LOCAL_ONLY),
            ),
        ),
        "data_digest": canonical_digest({"message": 1}),
        "egress_class": EgressClass.CLOUD_ALLOWED_REDACTED if remote else EgressClass.LOCAL_ONLY,
        "transformation_digest": canonical_digest({"redaction": "v1"}) if remote else NO_TRANSFORMATION,
        "request_id": "request-1",
        "runtime_path": "chat_agent",
        "workload": workload,
        "requirements": InferenceRequirements(
            capabilities=("text",),
            context_tokens=4096,
            output_tokens=512,
            max_cost_microusd=1000 if remote else None,
            max_local_resource_ms=None if remote else 10000,
            max_latency_ms=10000,
            task_class="chat",
        ),
        "deadline_at": 200.0,
        "redaction_applied": remote,
    }
    values.update(changes)
    return InferenceRequestContext(**values)


def _proofs(profile: ProviderProfile, *, now: float = 100.0):
    candidate = candidate_from_profile(profile)
    values = {
        "text": "supported",
        "context_tokens": 8192,
        "output_tokens": 1024,
        "latency_ms": 5000,
        "task_class": "chat",
        "health": "healthy",
        "cost_microusd" if candidate.endpoint_class is EndpointClass.REMOTE else "local_resource_ms": (
            500 if candidate.endpoint_class is EndpointClass.REMOTE else 5000
        ),
    }
    return tuple(
        build_model_route_proof(
            profile=profile,
            endpoint_class=candidate.endpoint_class,
            adapter=profile.transport_adapter,
            capability=capability,
            canary_version="canary-v1",
            outcome="passed",
            checked_at=now - 10,
            expires_at=now + 10,
            probe_receipt_id=f"probe-{capability}",
            probe_receipt_hash="b" * 64,
            proven_value=value,
        )
        for capability, value in values.items()
    )


@pytest.mark.parametrize(
    ("endpoint", "classification"),
    [
        ("http://localhost:8000/v1", EndpointClass.LOCAL),
        ("http://192.168.1.26:8000/v1", EndpointClass.TRUSTED_LAN),
        ("https://models.example/v1", EndpointClass.REMOTE),
        ("http://user:pass@localhost/v1", EndpointClass.INVALID),
        ("https://models.example/v1?token=secret", EndpointClass.INVALID),
        ("https://models.example/v1#secret", EndpointClass.INVALID),
        ("http://0.0.0.0:8000/v1", EndpointClass.INVALID),
    ],
)
def test_endpoint_classification_is_literal_and_rejects_credential_or_query_leakage(endpoint, classification):
    assert classify_endpoint(endpoint) is classification


def test_candidate_binds_exact_adapter_transport_endpoint():
    litellm = candidate_from_profile(_profile())
    vlm = candidate_from_profile(
        _profile(
            api_base="http://192.168.1.26:8001/v1",
            transport_adapter="vlm_analyze_file",
        )
    )

    assert litellm.endpoint == "http://127.0.0.1:8000/v1/chat/completions"
    assert vlm.endpoint == "http://192.168.1.26:8001/v1/analyze-file"


def test_selector_requires_fresh_exact_proof_and_profile_metadata():
    profile = _profile()
    candidate = candidate_from_profile(profile)
    context = _context()

    assert preflight_candidate(context, candidate, (), now=100.0)[2] == "proof_missing:health"
    stale = tuple(replace(proof, expires_at=99.0) for proof in _proofs(profile))
    assert preflight_candidate(context, candidate, stale, now=100.0)[2].startswith("proof_missing:")
    unknown = candidate_from_profile(replace(profile, context_window_tokens=None))
    assert preflight_candidate(context, unknown, _proofs(unknown.profile), now=100.0)[2] == (
        "profile_limit_unknown_or_insufficient:context_tokens"
    )


def test_no_compliant_route_fails_closed_and_attemptable_targets_do_not_degrade_open():
    profile = _profile(capabilities=())
    decision = select_route(_context(), (candidate_from_profile(profile),), (), now=100.0)

    assert decision.allowed is False
    assert decision.rejections[0].reason_code == "capability_not_declared"
    assert _attemptable_targets([{"policy_assessment": {"policy_compliant": False}}]) == []


@pytest.mark.parametrize("provider_kind", ["anthropic", "codex", "command"])
def test_transitional_and_command_provider_families_are_excluded(provider_kind):
    profile = _profile(provider_kind=provider_kind)

    assert provider_family_exclusion_reason(provider_kind) == "provider_family_excluded"
    assert preflight_candidate(
        _context(),
        candidate_from_profile(profile),
        _proofs(profile),
        now=100.0,
    )[2] == "provider_family_excluded"


def test_local_only_remote_route_is_denied_before_transport():
    profile = _profile(remote=True)
    context = replace(
        _context(remote=True),
        egress_class=EgressClass.LOCAL_ONLY,
        provenance=(
            replace(_context(remote=True).provenance[0], egress_class=EgressClass.LOCAL_ONLY),
        ),
        redaction_applied=False,
        transformation_digest=NO_TRANSFORMATION,
    )
    request, _decision_id, reason = preflight_candidate(
        context,
        candidate_from_profile(profile),
        _proofs(profile),
        now=100.0,
    )

    assert request is not None
    assert reason == "local_only_egress_blocked"


def test_redacted_remote_route_requires_exact_transformation_binding():
    profile = _profile(remote=True)
    context = _context(remote=True)
    candidate = candidate_from_profile(profile)

    assert preflight_candidate(context, candidate, _proofs(profile), now=100.0)[2] is None
    missing = replace(context, transformation_digest=NO_TRANSFORMATION)
    assert preflight_candidate(missing, candidate, _proofs(profile), now=100.0)[2] == (
        "transformation_binding_missing"
    )


def test_capability_probe_is_exact_primary_no_fallback_bootstrap_with_deadline():
    profile = _profile()
    context = _context(
        workload=InferenceWorkload.CAPABILITY_PROBE,
        requested_profile_id=profile.id,
        fallback_allowed=True,
    )

    assert preflight_candidate(context, candidate_from_profile(profile), (), now=100.0)[2] is None
    fallback = candidate_from_profile(profile, source="fallback")
    assert preflight_candidate(context, fallback, (), now=100.0)[2] == "probe_route_not_exact"
    assert preflight_candidate(
        replace(context, deadline_at=100.0),
        candidate_from_profile(profile),
        (),
        now=100.0,
    )[2] == "request_deadline_expired"


@pytest.mark.asyncio
async def test_streaming_executor_disables_redirects_and_emits_attempt_and_final_hooks():
    profile = _profile()
    context = _context()
    redirect_values = []
    events = []

    async def transport(_candidate, _messages, follow_redirects):
        redirect_values.append(follow_redirects)
        yield "hello"

    class Hooks:
        async def attempt_started(self, **_kwargs):
            events.append("started")

        async def attempt_finished(self, **kwargs):
            events.append(kwargs["outcome"])

    chunks = [
        chunk
        async for chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=_proofs(profile),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=Hooks(),
            temperature=0.25,
            max_tokens=96,
            now=100.0,
        )
    ]

    assert chunks == ["hello"]
    assert redirect_values == [False]
    assert events == ["started", "succeeded"]


@pytest.mark.asyncio
async def test_streaming_admission_rejection_does_not_fallback_to_second_candidate(monkeypatch):
    from src.model_fabric import execution

    primary = _profile(id="primary")
    fallback = _profile(id="fallback", model="model-v2")
    context = _context(fallback_allowed=True)
    admission_requests = []
    transported = []

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)

    class SaturatedBroker:
        async def stream(self, request, _operation, *, now=None):
            admission_requests.append(request)
            receipt = GpuAdmissionReceipt(
                operation_id=request.operation_id,
                job_id=request.job_id,
                owner_id=request.owner_id,
                priority=request.priority,
                status="rejected",
                queue_position=None,
                active_operation_id="already-running",
                fencing_token=None,
                reason_code="capacity_exhausted",
                queued=32,
                max_queued=32,
            )
            raise GpuAdmissionCapacityError("GPU admission queue is full", receipt=receipt)
            if False:
                yield "unreachable"

    monkeypatch.setattr(execution, "gpu_admission_broker", SaturatedBroker())

    async def transport(candidate, _body, _follow_redirects):
        transported.append(candidate.profile.id)
        yield "unexpected"

    with pytest.raises(GpuAdmissionCapacityError) as error:
        async for _chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(primary), candidate_from_profile(fallback, source="fallback")),
            proofs=(*_proofs(primary), *_proofs(fallback)),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=session,
            temperature=0.0,
            max_tokens=512,
            now=100.0,
        ):
            pass

    assert error.value.receipt.reason_code == "capacity_exhausted"
    assert len(admission_requests) == 1
    assert admission_requests[0].priority is GpuPriority.INTERACTIVE_CHAT
    assert transported == []
    assert repository.receipt is not None
    assert repository.receipt.outcome == "denied"
    assert repository.receipt.attempts == ()
    assert repository.receipt.fallback_reason_code == "gpu_admission_rejected"
    assert repository.receipt.degradation_codes == (
        "gpu_admission_rejected",
        "gpu_admission_capacity_exhausted",
    )


@pytest.mark.asyncio
async def test_streaming_preflight_binds_exact_candidate_transport_body(monkeypatch):
    from src.model_fabric import execution

    profile = _profile(
        provider_kind="openai_compatible",
        model="exact/provider-model",
        routing_model="openrouter/attacker/override",
        options={
            "top_p": 0.8,
            "model": "forbidden-override",
            "temperature": 9.0,
            "stream": False,
        },
    )
    context = _context()
    messages = ({"role": "user", "content": "exact private input"},)
    captured = []
    real_select = select_route

    def capture_select(bound_context, candidates, proofs, *, now=None):
        captured.append(bound_context)
        return real_select(bound_context, candidates, proofs, now=now)

    monkeypatch.setattr(execution, "select_route", capture_select)

    async def transport(_candidate, authorized_body, follow_redirects):
        assert canonical_digest(authorized_body) == captured[0].data_digest
        assert authorized_body == {
            "top_p": 0.8,
            "model": "exact/provider-model",
            "messages": list(messages),
            "temperature": 0.35,
            "max_tokens": 77,
            "stream": True,
        }
        assert follow_redirects is False
        yield "ok"

    class Hooks:
        async def attempt_started(self, **_kwargs):
            return None

        async def attempt_finished(self, **_kwargs):
            return None

    chunks = [
        chunk
        async for chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=_proofs(profile),
            messages=messages,
            transport=transport,
            hooks=Hooks(),
            temperature=0.35,
            max_tokens=77,
            now=100.0,
        )
    ]

    assert chunks == ["ok"]
    assert captured[0].data_digest == canonical_digest(
        {
            "model": "exact/provider-model",
            "messages": list(messages),
            "temperature": 0.35,
            "max_tokens": 77,
            "stream": True,
            "top_p": 0.8,
        }
    )
@pytest.mark.asyncio
async def test_streaming_executor_rejects_vlm_adapter_with_zero_transport():
    profile = _profile(
        capabilities=("text", "streaming"),
        transport_adapter="vlm_analyze_file",
    )
    context = _context(fallback_allowed=False)

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)
    transported = False

    async def transport(*_args):
        nonlocal transported
        transported = True
        yield "forbidden"

    with pytest.raises(NoCompliantModelRouteError, match="no_compliant_route"):
        async for _chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=_proofs(profile),
            messages=({"role": "user", "content": "private"},),
            transport=transport,
            hooks=session,
            temperature=0.0,
            max_tokens=64,
            now=100.0,
        ):
            pass

    assert transported is False
    assert repository.receipt is not None
    assert repository.receipt.outcome == "denied"
    assert repository.receipt.attempts == ()


@pytest.mark.asyncio
async def test_streaming_runtime_default_transport_sends_authorized_body_object_unchanged(
    monkeypatch,
):
    import httpx

    from src import llm_runtime

    profile = _profile(model="model-exact")
    context = _context(deadline_at=time.time() + 10)
    authorized_body = {
        "model": "model-exact",
        "messages": [{"role": "user", "content": "exact"}],
        "temperature": 0.2,
        "max_tokens": 42,
        "stream": True,
        "top_p": 0.7,
    }
    sent = {}

    class FakeRepository:
        async def latest_capability_proof(self, **_kwargs):
            return None

    class FakeResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield "data: [DONE]"

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return False

    class FakeAsyncClient:
        def __init__(self, *, follow_redirects, timeout):
            assert follow_redirects is False
            assert timeout > 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, endpoint, *, headers, json):
            sent.update(
                method=method,
                endpoint=endpoint,
                headers=headers,
                body=json,
            )
            return FakeStreamContext()

    async def injected_executor(**kwargs):
        assert kwargs["transport"] is not None
        async for delta in kwargs["transport"](
            kwargs["candidates"][0], authorized_body, False
        ):
            yield delta

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "src.model_fabric.repository.ModelFabricRepository", lambda: FakeRepository()
    )
    monkeypatch.setattr(llm_runtime, "runtime_profile_candidates", lambda **_kwargs: ["exact"])
    monkeypatch.setattr(llm_runtime, "_provider_profile", lambda _profile_id: profile)
    monkeypatch.setattr("src.model_fabric.execute_streaming", injected_executor)

    chunks = [
        chunk
        async for chunk in llm_runtime.stream_completion_with_fallback(
            messages=authorized_body["messages"],
            temperature=0.2,
            max_tokens=42,
            runtime_path="chat_agent",
            request_context=context,
            request_id=context.request_id,
        )
    ]

    assert chunks == ["ok"]
    assert sent["body"] is authorized_body
    assert sent["method"] == "POST"
    assert sent["endpoint"] == candidate_from_profile(profile).endpoint


@pytest.mark.asyncio
async def test_streaming_no_compliant_route_persists_zero_attempt_denial_and_never_transports():
    from src.model_fabric.runtime_status import (
        clear_receipt_persistence_observations,
        latest_receipt_persistence,
    )

    clear_receipt_persistence_observations()
    profile = _profile()
    context = _context()

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.degraded(receipt.receipt_id)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)
    transported = False

    async def transport(*_args):
        nonlocal transported
        transported = True
        yield "forbidden"

    with pytest.raises(NoCompliantModelRouteError, match="no_compliant_route"):
        async for _chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=(),
            messages=({"role": "user", "content": "private"},),
            transport=transport,
            hooks=session,
            temperature=0.1,
            max_tokens=32,
            now=100.0,
        ):
            pass

    assert transported is False
    assert repository.receipt.outcome == "denied"
    assert repository.receipt.attempts == ()
    assert repository.receipt.degradation_codes
    observation = latest_receipt_persistence("chat_agent")
    assert observation is not None
    assert observation.status == "degraded"
    assert observation.error_code == "receipt_persistence_failed"


def test_provider_profile_payload_migrates_into_canonical_contract():
    profile = _profile_from_payload(
        "team-local",
        {
            "provider_kind": "local",
            "model": "model-v1",
            "api_base": "http://127.0.0.1:8000/v1",
            "capabilities": ["text"],
            "transport_adapter": "openai_compatible_chat",
            "context_window_tokens": 8192,
            "max_output_tokens": 1024,
            "local_resource_ms": 5000,
            "max_latency_ms": 5000,
            "task_class": "chat",
        },
    )

    assert isinstance(profile, ProviderProfile)
    assert profile.schema_version == "seraph.model-fabric.v1"
    assert profile.context_window_tokens == 8192
    assert profile.follow_redirects is False


def test_inline_secret_options_are_ineligible_and_absent_from_contract_binding():
    secret = "low-entropy-secret"
    profile = _profile(options={"headers": {"Authorization": secret}})
    secret_hash = hashlib.sha256(secret.encode()).hexdigest()

    assert secret not in profile.contract_hash
    assert secret_hash not in profile.contract_hash
    assert preflight_candidate(
        _context(),
        candidate_from_profile(profile),
        (),
        now=100.0,
    )[2] == "inline_secret_option_forbidden"


@pytest.mark.asyncio
async def test_streaming_fallback_is_reauthorized_before_any_delta_and_uses_one_receipt():
    primary = _profile(id="primary")
    fallback = _profile(id="fallback", model="model-v2")
    context = _context(fallback_allowed=True)

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)

    async def transport(candidate, _messages, _follow_redirects):
        if candidate.profile.id == "primary":
            raise RuntimeError("failed before output")
        yield "fallback"

    chunks = [
        chunk
        async for chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(primary), candidate_from_profile(fallback, source="fallback")),
            proofs=(*_proofs(primary), *_proofs(fallback)),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=session,
            temperature=0.0,
            max_tokens=512,
            now=100.0,
        )
    ]

    assert chunks == ["fallback"]
    assert repository.receipt.outcome == "succeeded"
    assert [attempt.outcome for attempt in repository.receipt.attempts] == ["failed", "succeeded"]
    assert repository.receipt.fallback_reason_code == "transport_failed"
    assert repository.receipt.degradation_codes == (
        "fallback_used",
        "fallback_transport_failed",
    )
    assert repository.receipt.attempts[0].degradation_code == "transport_failed"
    assert repository.receipt.attempts[1].degradation_code == "fallback_transport_failed"


@pytest.mark.asyncio
async def test_streaming_preflight_rejection_fallback_records_exact_cause():
    primary = _profile(id="primary")
    fallback = _profile(id="fallback", model="model-v2")
    context = _context(fallback_allowed=True)

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)
    attempted = []

    async def transport(candidate, _body, _follow_redirects):
        attempted.append(candidate.profile.id)
        yield "fallback"

    chunks = [
        chunk
        async for chunk in execute_streaming(
            context=context,
            candidates=(
                candidate_from_profile(primary),
                candidate_from_profile(fallback, source="fallback"),
            ),
            proofs=_proofs(fallback),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=session,
            temperature=0.0,
            max_tokens=512,
            now=100.0,
        )
    ]

    assert chunks == ["fallback"]
    assert attempted == ["fallback"]
    assert repository.receipt.fallback_reason_code == "preflight_rejected"
    assert repository.receipt.degradation_codes == (
        "fallback_used",
        "fallback_preflight_rejected",
    )
    assert repository.receipt.attempts[0].outcome == "succeeded"
    assert repository.receipt.attempts[0].degradation_code == "fallback_preflight_rejected"


@pytest.mark.asyncio
async def test_streaming_empty_primary_fallback_records_exact_cause():
    primary = _profile(id="primary")
    fallback = _profile(id="fallback", model="model-v2")
    context = _context(fallback_allowed=True)

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)

    async def transport(candidate, _body, _follow_redirects):
        if candidate.profile.id == "fallback":
            yield "fallback"

    chunks = [
        chunk
        async for chunk in execute_streaming(
            context=context,
            candidates=(
                candidate_from_profile(primary),
                candidate_from_profile(fallback, source="fallback"),
            ),
            proofs=(*_proofs(primary), *_proofs(fallback)),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=session,
            temperature=0.0,
            max_tokens=512,
            now=100.0,
        )
    ]

    assert chunks == ["fallback"]
    assert repository.receipt.fallback_reason_code == "stream_empty"
    assert repository.receipt.degradation_codes == (
        "fallback_used",
        "fallback_stream_empty",
    )
    assert repository.receipt.attempts[0].degradation_code == "stream_empty"
    assert repository.receipt.attempts[1].degradation_code == "fallback_stream_empty"


@pytest.mark.asyncio
async def test_streaming_partial_output_failure_never_mixes_fallback_model():
    primary = _profile(id="primary")
    fallback = _profile(id="fallback", model="model-v2")
    context = _context(fallback_allowed=True)
    attempted = []

    class Repository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = Repository()
    session = RouteReceiptSession(context=context, repository=repository)

    async def transport(candidate, _messages, _follow_redirects):
        attempted.append(candidate.profile.id)
        yield "partial"
        raise RuntimeError("failed after output")

    received = []
    with pytest.raises(RuntimeError, match="failed after output"):
        async for chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(primary), candidate_from_profile(fallback, source="fallback")),
            proofs=(*_proofs(primary), *_proofs(fallback)),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=session,
            temperature=0.0,
            max_tokens=512,
            now=100.0,
        ):
            received.append(chunk)

    assert received == ["partial"]
    assert attempted == ["primary"]
    assert repository.receipt.outcome == "failed"


@pytest.mark.asyncio
async def test_zero_delta_stream_is_a_bounded_failure():
    profile = _profile()
    context = _context()
    events = []

    async def transport(_candidate, _messages, _follow_redirects):
        if False:
            yield "never"

    class Hooks:
        async def attempt_started(self, **_kwargs):
            events.append("started")

        async def attempt_finished(self, **kwargs):
            events.append((kwargs["outcome"], kwargs["error_code"]))

    with pytest.raises(RuntimeError, match="stream_empty"):
        async for _chunk in execute_streaming(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=_proofs(profile),
            messages=({"role": "user", "content": "hi"},),
            transport=transport,
            hooks=Hooks(),
            temperature=0.0,
            max_tokens=512,
            now=100.0,
        ):
            pass

    assert events == ["started", ("failed", "stream_empty")]


def test_synthetic_health_probe_bootstraps_without_declared_capability():
    profile = _profile(capabilities=("text",))
    candidate = candidate_from_profile(profile)
    context = _context(
        workload=InferenceWorkload.CAPABILITY_PROBE,
        requested_profile_id=profile.id,
        requirements=replace(_context().requirements, capabilities=("health",)),
    )

    request, decision_id, denial = preflight_candidate(context, candidate, (), now=100.0)

    assert request is not None
    assert decision_id is not None
    assert denial is None


def test_ordinary_route_still_requires_synthetic_health_proof():
    profile = _profile()
    candidate = candidate_from_profile(profile)
    proofs = tuple(proof for proof in _proofs(profile) if proof.capability != "health")

    _request, _decision_id, denial = preflight_candidate(
        _context(), candidate, proofs, now=100.0
    )

    assert denial == "proof_missing:health"


def test_legacy_litellm_adapter_is_excluded_from_governed_fabric():
    profile = _profile(transport_adapter="litellm_chat")

    assert profile_exclusion_reason(profile) == "legacy_transport_not_governed"


def test_final_payload_binding_replaces_digest_and_records_control_provenance():
    context = _context()
    payload = {"model": "model-v1", "messages": [{"role": "user", "content": "final"}]}

    bound = bind_final_inference_payload(context, payload)

    assert bound.data_digest == canonical_digest(payload)
    assert bound.data_digest != context.data_digest
    assert bound.transformation_digest != context.transformation_digest
    assert bound.provenance[-1].source_id == "model_fabric_transport_payload"
    assert bound.provenance[-1].data_digest == bound.data_digest


@pytest.mark.parametrize(
    ("provider_kind", "secret_env", "expected"),
    [
        ("openrouter", "OPENAI_API_KEY", "credential_ref_not_allowed"),
        ("openrouter", "OPENROUTER_API_KEY", None),
        ("openai", "OPENAI_API_KEY", None),
        ("openai", "OPENROUTER_API_KEY", "credential_ref_not_allowed"),
        ("openai_compatible", "OPENAI_API_KEY", "credential_ref_not_allowed"),
        ("ollama", "LOCAL_LLM_API_KEY", None),
    ],
)
def test_provider_scoped_credential_reference_contract(provider_kind, secret_env, expected):
    profile = _profile(provider_kind=provider_kind, secret_env=secret_env, keyless=False)

    assert profile_exclusion_reason(profile) == expected


@pytest.mark.parametrize(
    ("provider_kind", "api_base", "secret_env", "expected"),
    [
        ("openai", "https://evil.example/v1", "OPENAI_API_KEY", "credential_destination_mismatch"),
        ("openai_compatible", "https://evil.example/v1", "OPENAI_API_KEY", "credential_ref_not_allowed"),
        ("local", "https://evil.example/v1", "LOCAL_LLM_API_KEY", "credential_destination_mismatch"),
        ("openai_compatible", "https://evil.example/v1", "SERAPH_VLM_API_KEY", "credential_destination_mismatch"),
        ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", None),
        ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", None),
        ("openai_compatible", "https://models.example/v1", "LLM_API_KEY", None),
        ("local", "http://127.0.0.1:8000/v1", "LOCAL_LLM_API_KEY", None),
        ("openai_compatible", "http://192.168.1.26:8001/v1", "SERAPH_VLM_API_KEY", None),
    ],
)
def test_credentials_are_bound_to_provider_and_destination(
    monkeypatch,
    provider_kind,
    api_base,
    secret_env,
    expected,
):
    monkeypatch.setenv(secret_env, "bounded-test-secret")
    remote = api_base.startswith("https://")
    profile = _profile(
        remote=remote,
        provider_kind=provider_kind,
        api_base=api_base,
        secret_env=secret_env,
        keyless=False,
    )
    _request, _decision_id, denial = preflight_candidate(
        _context(remote=remote),
        candidate_from_profile(profile),
        _proofs(profile),
        now=100.0,
    )

    assert denial == expected


@pytest.mark.asyncio
async def test_legacy_openrouter_profile_with_openai_credential_is_zero_transport(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-secret")
    profile = _profile(
        remote=True,
        provider_kind="openrouter",
        secret_env="OPENAI_API_KEY",
        keyless=False,
    )
    transported = False

    async def adapter(_candidate, _trust_request):
        nonlocal transported
        transported = True
        return "must-not-run"

    decision = select_route(
        _context(remote=True),
        (candidate_from_profile(profile),),
        (),
        now=100.0,
    )
    if decision.allowed:
        await adapter(decision.selected, False)

    assert transported is False
    assert decision.rejections[0].reason_code == "credential_ref_not_allowed"


@pytest.mark.parametrize(
    ("provider_kind", "routing_model", "exact_model"),
    [
        ("openrouter", "openrouter/anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4"),
        ("openai", "openai/gpt-4.1-mini", "gpt-4.1-mini"),
        ("ollama", "ollama/qwen2.5:7b", "qwen2.5:7b"),
        ("openai_compatible", "openai/foo", "openai/foo"),
    ],
)
def test_provider_aware_exact_model_is_shared_by_profile_proof_and_body(
    provider_kind, routing_model, exact_model
):
    profile = _profile(
        provider_kind=provider_kind,
        model=transport_model_for_provider(provider_kind, routing_model),
        routing_model=routing_model,
    )
    proof = _proofs(profile)[0]
    body = finalized_openai_compatible_body(
        model_id=profile.model,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert profile.model == exact_model
    assert proof.model == exact_model
    assert body["model"] == exact_model


def test_inconsistent_routing_label_cannot_change_exact_proof_or_body_model():
    profile = _profile(
        provider_kind="openrouter",
        model="anthropic/claude-sonnet-4",
        routing_model="openrouter/attacker/override",
    )
    proof = _proofs(profile)[0]
    body = finalized_openai_compatible_body(
        model_id=profile.model,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert proof.model == "anthropic/claude-sonnet-4"
    assert body["model"] == proof.model
    assert profile.routing_model not in body.values()
