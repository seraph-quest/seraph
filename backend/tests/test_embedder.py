"""Tests for the OpenRouter embedding adapter and runtime receipts."""

import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from config.settings import settings
from src.memory import embedder
from src.model_fabric import ProviderProfile, ReceiptPersistenceResult, candidate_from_profile
from src.model_fabric.proofs import build_model_route_proof
from src.security.trust_contract import (
    AuthorityGrant,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
)


_EMBEDDING_COST_UPDATED_AT = time.time()
_AUDIT_EVENTS: list[dict[str, object]] = []


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(status_code: int, payload: object, *, headers: dict[str, str] | None = None):
    return httpx.Response(
        status_code,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", embedder.OPENROUTER_EMBEDDINGS_ENDPOINT),
    )


@pytest.fixture(autouse=True)
def _reset_embedder_state():
    embedder._reset_embedder_state()
    _AUDIT_EVENTS.clear()
    yield
    embedder._reset_embedder_state()
    _AUDIT_EVENTS.clear()


@pytest.fixture(autouse=True)
def _capture_embedding_audit_events():
    def record(*, integration_type, name, outcome, details=None):
        _AUDIT_EVENTS.append(
            {
                "event_type": f"integration_{outcome}",
                "tool_name": f"{integration_type}:{name}",
                "details": details or {},
            }
        )

    with patch.object(embedder, "log_integration_event_sync", side_effect=record):
        yield


def _configured_embedding_route():
    policy = SimpleNamespace(
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        max_cost_microusd=100,
        allowed_provider_kinds=("openrouter",),
    )
    return patch.multiple(
        settings,
        embedding_model="openrouter/openai/text-embedding-3-small",
        openrouter_api_key="test-openrouter-key",
        openrouter_provider_only=True,
        openrouter_allowed_upstreams="openai",
        openrouter_allow_fallbacks=False,
        openrouter_require_parameters=True,
        openrouter_data_collection="deny",
        llm_api_base=embedder.OPENROUTER_API_BASE,
    ), patch.object(embedder, "effective_workload_policy", return_value=policy)


def _embedding_profile() -> ProviderProfile:
    upstreams = [
        item.strip()
        for item in str(getattr(settings, "openrouter_allowed_upstreams", "") or "").split(",")
        if item.strip()
    ]
    return ProviderProfile(
        id="memory_embedding",
        provider_kind="openrouter",
        model="openai/text-embedding-3-small",
        routing_model="openrouter/openai/text-embedding-3-small",
        api_base=embedder.OPENROUTER_API_BASE,
        secret_env="OPENROUTER_API_KEY",
        options={
            "provider": {
                "only": upstreams,
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
        },
        capabilities=("embedding",),
        task_class="memory_embedding",
        task_classes=("memory_embedding",),
        transport_adapter="openai_compatible_embeddings",
        context_window_tokens=32768,
        max_output_tokens=1,
        cost_microusd=10,
        cost_source="test-pricing",
        cost_source_updated_at=_EMBEDDING_COST_UPDATED_AT,
        max_latency_ms=30000,
    )


class _EmbeddingRepository:
    def __init__(self):
        self.receipts = []

    async def latest_capability_proof(self, **kwargs):
        profile = _embedding_profile()
        candidate = candidate_from_profile(profile)
        if kwargs["profile_contract_hash"] != profile.contract_hash:
            return None
        now = time.time()
        values = {
            "embedding": "supported",
            "health": "healthy",
            "latency_ms": profile.max_latency_ms,
        }
        return build_model_route_proof(
            profile=profile,
            endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter,
            capability=kwargs["capability"],
            canary_version="embedding-test-v1",
            outcome="passed",
            checked_at=now - 1,
            expires_at=now + 60,
            probe_receipt_id=f"probe-{kwargs['capability']}",
            probe_receipt_hash="a" * 64,
            proven_value=values[kwargs["capability"]],
        )

    async def persist_route_receipt(self, receipt):
        self.receipts.append(receipt)
        return ReceiptPersistenceResult.success(receipt)


@pytest.fixture(autouse=True)
def _governed_embedding_context():
    principal = TrustPrincipal(
        principal_id="embedding-test-operator",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="embedding-test-session",
        job_id="embedding-test-job",
    )
    policy = SimpleNamespace(
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        max_cost_microusd=100,
        allowed_provider_kinds=("openrouter",),
        allowed_profile_ids=(),
    )
    repository = _EmbeddingRepository()

    def profiles():
        return {"memory_embedding": _embedding_profile()}

    with (
        patch.object(embedder, "get_current_trust_principal", return_value=principal),
        patch("src.llm_runtime.provider_profiles", side_effect=profiles),
        patch("src.model_fabric.caller_context.effective_workload_policy", return_value=policy),
        patch.object(embedder, "model_fabric_repository", repository),
    ):
        yield


def _embedding_events():
    return list(_AUDIT_EVENTS)


def test_embed_posts_validated_openrouter_request_and_records_metadata():
    response = _response(
        200,
        {
            "object": "list",
            "model": "openai/text-embedding-3-small",
            "data": [{"object": "embedding", "index": 0, "embedding": [3.0, 4.0]}],
        },
    )
    client = _FakeClient([response])
    settings_patch, policy_patch = _configured_embedding_route()

    with settings_patch, policy_patch, patch("src.memory.embedder.httpx.Client", return_value=client):
        vector = embedder.embed("hello")

    assert vector == [0.6, 0.8]
    assert len(client.calls) == 1
    endpoint, kwargs = client.calls[0]
    assert endpoint == (embedder.OPENROUTER_EMBEDDINGS_ENDPOINT,)
    assert kwargs["json"] == {
        "model": "openai/text-embedding-3-small",
        "input": "hello",
        "encoding_format": "float",
        "provider": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        },
    }
    assert kwargs["headers"]["Authorization"] == "Bearer test-openrouter-key"
    assert kwargs["headers"]["Content-Type"] == "application/json"

    metadata = embedder.embedding_metadata()
    assert metadata is not None
    assert metadata.provider == "openrouter"
    assert metadata.model == "openai/text-embedding-3-small"
    assert metadata.dimension == 2
    assert metadata.schema_version == embedder.EMBEDDING_SCHEMA_VERSION
    assert metadata.namespace.startswith("memory-")

    events = _embedding_events()
    loaded = [event for event in events if event["event_type"] == "integration_loaded"]
    assert loaded
    assert loaded[0]["details"]["dimension"] == 2
    assert loaded[0]["details"]["schema_version"] == embedder.EMBEDDING_SCHEMA_VERSION


def test_embed_batch_reorders_indexed_vectors_and_normalizes_each_vector():
    response = _response(
        200,
        {
            "model": "openai/text-embedding-3-small",
            "data": [
                {"index": 1, "embedding": [0.0, 2.0]},
                {"index": 0, "embedding": [2.0, 0.0]},
            ],
        },
    )
    client = _FakeClient([response])
    settings_patch, policy_patch = _configured_embedding_route()

    with settings_patch, policy_patch, patch("src.memory.embedder.httpx.Client", return_value=client):
        vectors = embedder.embed_batch(["first", "second"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert client.calls[0][1]["json"]["input"] == ["first", "second"]


def test_missing_openrouter_configuration_is_typed_and_logged():
    with patch.object(settings, "embedding_model", "all-MiniLM-L6-v2"), \
        patch("src.memory.embedder.httpx.Client") as mock_client:
        with pytest.raises(
            embedder.EmbeddingConfigurationError,
            match="embedding_model_must_be_openrouter_qualified",
        ) as error:
            embedder.embed("private source text")

    mock_client.assert_not_called()
    assert "private source text" not in str(error.value)
    events = _embedding_events()
    failed = [event for event in events if event["event_type"] == "integration_failed"]
    assert failed
    assert failed[0]["details"]["reason_code"] == "embedding_model_must_be_openrouter_qualified"
    assert "private source text" not in str(failed[0]["details"])


def test_provider_response_is_validated_without_logging_response_body():
    response = _response(
        200,
        {
            "model": "openai/text-embedding-3-small",
            "data": [{"index": 0, "embedding": [1.0, "not-a-number"]}],
            "error_context": "private source text must not escape",
        },
    )
    client = _FakeClient([response])
    settings_patch, policy_patch = _configured_embedding_route()

    with settings_patch, policy_patch, patch("src.memory.embedder.httpx.Client", return_value=client):
        with pytest.raises(embedder.EmbeddingResponseError, match="response_vector_value_invalid") as error:
            embedder.embed("private source text")

    assert "private source text" not in str(error.value)
    events = _embedding_events()
    failed = [event for event in events if event["event_type"] == "integration_failed"]
    assert failed
    assert failed[0]["details"]["stage"] == "response"
    assert failed[0]["details"]["reason_code"] == "response_vector_value_invalid"
    assert "private source text" not in str(failed[0]["details"])


def test_rate_limit_does_not_auto_retry_uncertain_remote_request():
    retry_response = _response(429, {"error": "rate limited"}, headers={"retry-after": "0"})
    client = _FakeClient([retry_response])
    settings_patch, policy_patch = _configured_embedding_route()

    with (
        settings_patch,
        policy_patch,
        patch("src.memory.embedder.httpx.Client", return_value=client),
        patch("src.memory.embedder.time.sleep") as sleep_mock,
    ):
        with pytest.raises(embedder.EmbeddingUnavailableError, match="rate_limited"):
            embedder.embed("retry safely")

    assert len(client.calls) == 1
    sleep_mock.assert_not_called()


def test_credit_failure_does_not_retry_and_is_visible():
    client = _FakeClient([_response(402, {"error": "credits unavailable"})])
    settings_patch, policy_patch = _configured_embedding_route()

    with settings_patch, policy_patch, patch("src.memory.embedder.httpx.Client", return_value=client):
        with pytest.raises(embedder.EmbeddingUnavailableError, match="credits_unavailable"):
            embedder.embed("paid request")

    assert len(client.calls) == 1
    events = _embedding_events()
    failed = [event for event in events if event["event_type"] == "integration_failed"]
    assert failed[0]["details"]["reason_code"] == "credits_unavailable"
    assert failed[0]["details"]["status_code"] == 402


def test_embedding_bounds_fail_before_network():
    settings_patch, policy_patch = _configured_embedding_route()
    with settings_patch, policy_patch, patch("src.memory.embedder.httpx.Client") as mock_client:
        with pytest.raises(embedder.EmbeddingUnavailableError, match="input_length_exceeded"):
            embedder.embed("x" * (embedder.MAX_TEXT_CHARS + 1))
    mock_client.assert_not_called()
