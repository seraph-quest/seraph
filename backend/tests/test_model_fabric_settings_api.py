from pathlib import Path
import time
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.llm_runtime import provider_profiles
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.contracts import OPENROUTER_API_BASE, ProviderProfile
from src.model_fabric.configuration import (
    CONFIG_SCHEMA_VERSION,
    _configuration_from_payload,
    read_model_fabric_configuration,
)
from src.model_fabric.probe import CapabilityProbeObservation
from src.model_fabric.receipts import ReceiptPersistenceResult
from src.security.trust_contract import (
    AuthorityGrant,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
    canonical_digest,
)


PERSISTED_PROFILE_ID = "persisted-openrouter"
PERSISTED_MODEL = "anthropic/claude-sonnet-4"
_OPENROUTER_OPTIONS = {
    "provider": {
        "only": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }
}


def _capability_probe_policy(*, profile_id: str = PERSISTED_PROFILE_ID):
    return {
        "runtime_path": "capability_probe",
        "egress_class": "cloud_allowed_full",
        "cloud_egress_acknowledged": True,
        "allowed_profile_ids": [profile_id],
        "allowed_provider_kinds": ["openrouter"],
        "max_cost_microusd": 5000,
    }


def _profile_payload(*, profile_id=PERSISTED_PROFILE_ID):
    return {
        "id": profile_id,
        "provider_kind": "openrouter",
        "model": PERSISTED_MODEL,
        "api_base": OPENROUTER_API_BASE,
        "secret_env": "OPENROUTER_API_KEY",
        "options": _OPENROUTER_OPTIONS,
        "capabilities": ["text", "structured_output"],
        "enabled": True,
        "keyless": False,
        "transport_adapter": "openai_compatible_chat",
        "context_window_tokens": 8192,
        "max_output_tokens": 1024,
        "max_latency_ms": 2000,
        "cost_microusd": 500,
        "cost_source": "test-pricing",
        "cost_source_updated_at": time.time() - 10,
        "task_class": "interactive_chat",
        "task_classes": ["interactive_chat"],
    }


def _remote_profile_payload(*, cost_source: str = "provider_pricing_v1"):
    profile = _profile_payload(profile_id="persisted-remote")
    profile.update(
        {
            "api_base": "https://models.example/v1",
            "cost_microusd": 250,
            "cost_source": cost_source,
            "cost_source_updated_at": time.time() - 10,
        }
    )
    return profile


@pytest.fixture
def model_fabric_workspace(tmp_path, monkeypatch):
    from src.model_fabric.runtime_status import clear_receipt_persistence_observations

    clear_receipt_persistence_observations()
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(settings, "openrouter_allow_fallbacks", False)
    monkeypatch.setattr(settings, "openrouter_require_parameters", True)
    monkeypatch.setattr(settings, "openrouter_data_collection", "deny")
    monkeypatch.setattr(settings, "openrouter_zero_data_retention", True)
    monkeypatch.setattr(settings, "screen_analysis_model", "")
    principal = TrustPrincipal(
        principal_id="operator-test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-test",
    )
    tokens = set_runtime_context("session-test", "high_risk", trust_principal=principal)
    try:
        yield tmp_path
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
async def test_put_get_configuration_becomes_canonical_profile_and_policy(client, model_fabric_workspace):
    response = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [
                {
                    "runtime_path": "chat_agent",
                    "egress_class": "cloud_allowed_full",
                    "cloud_egress_acknowledged": True,
                    "allowed_profile_ids": [PERSISTED_PROFILE_ID],
                    "allowed_provider_kinds": ["openrouter"],
                    "fallback_allowed": False,
                    "max_cost_microusd": 5000,
                }
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["persisted_profile_ids"] == [PERSISTED_PROFILE_ID]
    assert payload["defaults"] == {"egress_class": "local_only", "fallback_allowed": False}
    assert PERSISTED_PROFILE_ID in provider_profiles()

    principal = TrustPrincipal(
        principal_id="operator-test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-test",
    )
    context = build_canonical_inference_context(
        "chat_agent",
        payload={"message": "hello"},
        output_tokens=64,
        timeout_seconds=5,
        principal=principal,
        session_id="session-test",
    )
    assert context.egress_class is EgressClass.CLOUD_ALLOWED_FULL
    assert context.allowed_profile_ids == (PERSISTED_PROFILE_ID,)
    assert context.allowed_provider_kinds == ("openrouter",)
    assert context.fallback_allowed is False
    assert context.requirements.max_cost_microusd == 5000


@pytest.mark.asyncio
async def test_cloud_widening_without_acknowledgement_is_rejected(client, model_fabric_workspace):
    response = await client.put(
        "/api/settings/model-fabric",
        json={
            "workload_policies": [
                {"runtime_path": "chat_agent", "egress_class": "cloud_allowed_full"}
            ]
        },
    )
    assert response.status_code == 422
    assert "explicit operator acknowledgement" in response.json()["detail"]
    assert read_model_fabric_configuration().status == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cost_source",
    ("provider pricing", "https://models.example/pricing", "x" * 129),
)
async def test_cost_source_rejects_values_receipts_cannot_safely_persist(
    client,
    model_fabric_workspace,
    cost_source,
):
    response = await client.put(
        "/api/settings/model-fabric",
        json={"profiles": [_remote_profile_payload(cost_source=cost_source)]},
    )
    assert response.status_code == 422
    assert read_model_fabric_configuration().status == "missing"


def test_configuration_rejects_unsafe_cost_source_without_api_validation():
    with pytest.raises(ValueError, match="cost source must be a bounded safe identifier"):
        _configuration_from_payload(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "profiles": [_remote_profile_payload(cost_source="provider pricing")],
                "workload_policies": [],
            }
        )


@pytest.mark.asyncio
async def test_cloud_widening_requires_cost_ceiling_and_binds_redaction_evidence(
    client,
    model_fabric_workspace,
):
    missing_budget = await client.put(
        "/api/settings/model-fabric",
        json={
            "workload_policies": [
                {
                    "runtime_path": "chat_agent",
                    "egress_class": "cloud_allowed_redacted",
                    "cloud_egress_acknowledged": True,
                }
            ]
        },
    )
    assert missing_budget.status_code == 422
    assert "cost ceiling" in missing_budget.json()["detail"]

    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "workload_policies": [
                {
                    "runtime_path": "chat_agent",
                    "egress_class": "cloud_allowed_redacted",
                    "cloud_egress_acknowledged": True,
                    "max_cost_microusd": 750,
                }
            ]
        },
    )
    assert configured.status_code == 200
    principal = TrustPrincipal(
        principal_id="operator-test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-test",
    )
    transformation = canonical_digest({"redaction": "fixture-v1"})
    context = build_canonical_inference_context(
        "chat_agent",
        payload={"message": "redacted"},
        output_tokens=64,
        timeout_seconds=5,
        principal=principal,
        session_id="session-test",
        transformation_digest=transformation,
        redaction_applied=True,
    )
    assert context.egress_class is EgressClass.CLOUD_ALLOWED_REDACTED
    assert context.requirements.max_cost_microusd == 750
    assert context.transformation_digest == transformation
    assert context.redaction_applied is True


@pytest.mark.asyncio
async def test_manual_canary_is_exact_bounded_sanitized_and_visible_in_runtime_status(
    client,
    model_fabric_workspace,
):
    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [
                _capability_probe_policy(),
            ],
        },
    )
    assert configured.status_code == 200

    from src.api.model_fabric_settings import _canary_fixture
    from src.model_fabric.probe import run_capability_probe as real_run_capability_probe

    captured_contexts = []

    async def capture_probe(**kwargs):
        captured_contexts.append(kwargs["context"])
        return await real_run_capability_probe(**kwargs)

    transport = AsyncMock(return_value=CapabilityProbeObservation(True, proven_value="verified"))
    with (
        patch("src.api.model_fabric_settings._execute_canary_transport", transport),
        patch("src.api.model_fabric_settings.run_capability_probe", side_effect=capture_probe),
    ):
        response = await client.post(
            "/api/settings/model-fabric/canary",
            json={
                "profile_id": PERSISTED_PROFILE_ID,
                "capability": "text",
                "timeout_seconds": 2,
                "proof_ttl_seconds": 120,
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["outcome"] == "passed"
    assert payload["receipt_persistence"] == "persisted"
    assert payload["proof_persistence"] == "persisted"
    assert payload["proof"]["profile_id"] == PERSISTED_PROFILE_ID
    assert isinstance(payload["proof"]["checked_at"], str)
    assert isinstance(payload["proof"]["expires_at"], str)
    assert "output" not in payload
    assert transport.await_count == 1
    candidate = transport.await_args.args[0]
    assert candidate.id == PERSISTED_PROFILE_ID
    assert transport.await_args.kwargs["timeout_seconds"] == 2
    fixture = _canary_fixture(provider_profiles()[PERSISTED_PROFILE_ID], "text")
    assert captured_contexts[0].data_digest == canonical_digest(fixture["digest_payload"])
    assert fixture["json"] == {
        "model": PERSISTED_MODEL,
        "messages": [{"role": "user", "content": "Reply with CANARY_OK only."}],
        "max_tokens": 64,
    }

    runtime = await client.get("/api/runtime/status")
    assert runtime.status_code == 200
    fabric = runtime.json()["model_fabric"]
    assert "chat_agent" in fabric["topology"]["text"]
    assert "orchestrator_agent" in fabric["topology"]["text"]
    assert fabric["topology"]["vlm"] == ["screenshot_image_analysis"]
    probe = fabric["workloads"]["capability_probe"]
    assert probe["selected"]["profile_id"] == PERSISTED_PROFILE_ID
    assert probe["attempted"]["outcome"] == "succeeded"
    assert probe["succeeded"]["profile_id"] == PERSISTED_PROFILE_ID
    assert probe["fallback_used"] is False
    assert probe["degradation_codes"] == []
    proof_status = next(
        item for item in fabric["proofs"]
        if item["profile_id"] == PERSISTED_PROFILE_ID and item["capability"] == "text"
    )
    assert proof_status["status"] == "fresh"


@pytest.mark.asyncio
async def test_failed_canary_is_visible_as_failed_but_never_routable(
    client,
    model_fabric_workspace,
):
    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [_capability_probe_policy()],
        },
    )
    assert configured.status_code == 200

    transport = AsyncMock(
        return_value=CapabilityProbeObservation(False, error_code="schema_mismatch")
    )
    with patch("src.api.model_fabric_settings._execute_canary_transport", transport):
        canary = await client.post(
            "/api/settings/model-fabric/canary",
            json={"profile_id": PERSISTED_PROFILE_ID, "capability": "text"},
        )
    assert canary.status_code == 200
    assert canary.json()["outcome"] == "failed"
    assert canary.json()["proof"] is None

    response = await client.get("/api/settings/model-fabric")
    profile = next(
        item for item in response.json()["profiles"] if item["id"] == PERSISTED_PROFILE_ID
    )
    assert profile["routable"] is False
    assert "proof_failed:text" in profile["non_routable_reasons"]

    runtime = await client.get("/api/runtime/status")
    proof_status = next(
        item for item in runtime.json()["model_fabric"]["proofs"]
        if item["profile_id"] == PERSISTED_PROFILE_ID and item["capability"] == "text"
    )
    assert proof_status["status"] == "failed"
    assert proof_status["outcome"] == "failed"


@pytest.mark.parametrize("capability", ["tool_use", "vision"])
def test_chat_canary_digest_payload_is_exact_transport_json_for_tool_and_vision(capability):
    from src.api.model_fabric_settings import _canary_fixture
    from src.model_fabric import ProviderProfile

    profile = ProviderProfile(**_profile_payload(profile_id="chat-test"))
    fixture = _canary_fixture(profile, capability)

    assert fixture["digest_payload"] is fixture["json"]
    assert canonical_digest(fixture["digest_payload"]) == canonical_digest(fixture["json"])
    assert "canary_version" not in fixture["digest_payload"]
    assert "transport" not in fixture["digest_payload"]
    assert fixture["json"]["model"] == PERSISTED_MODEL
    if capability == "tool_use":
        assert fixture["json"]["tools"]
        assert fixture["json"]["tool_choice"]
    else:
        content = fixture["json"]["messages"][0]["content"]
        assert content[1]["type"] == "image_url"


def test_openrouter_vision_canary_digest_payload_is_exact_transport_json():
    from src.api.model_fabric_settings import _canary_fixture
    from src.model_fabric import ProviderProfile

    payload = _profile_payload(profile_id="openrouter-screenshot-vision")
    payload.update(
        {
            "capabilities": ["text", "vision", "structured_output"],
            "task_class": "vision_analysis",
            "task_classes": ["vision_analysis"],
        }
    )
    profile = ProviderProfile(**payload)
    fixture = _canary_fixture(profile, "vision")

    assert fixture["digest_payload"] is fixture["json"]
    assert canonical_digest(fixture["digest_payload"]) == canonical_digest(fixture["json"])
    assert fixture["json"]["model"] == PERSISTED_MODEL
    content = fixture["json"]["messages"][0]["content"]
    assert content[1]["type"] == "image_url"
    assert "canary_version" not in fixture["digest_payload"]
    assert "transport" not in fixture["digest_payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "expected_value"),
    [("health", "healthy"), ("latency_ms", int)],
)
async def test_health_and_latency_canary_api_use_real_transport_and_persist_proof(
    model_fabric_workspace,
    capability,
    expected_value,
    monkeypatch,
):
    from starlette.requests import Request

    from src.api.model_fabric_settings import (
        CapabilityCanaryRequest,
        run_model_fabric_canary,
    )
    from src.model_fabric import ProviderProfile
    from src.model_fabric.configuration import (
        ModelFabricConfiguration,
        WorkloadPolicy,
        write_model_fabric_configuration,
    )
    from src.model_fabric.repository import ProofPersistenceResult

    profile = ProviderProfile(**_profile_payload())
    write_model_fabric_configuration(
        ModelFabricConfiguration(
            profiles=(profile,),
            workload_policies=(
                WorkloadPolicy(
                    runtime_path="capability_probe",
                    egress_class=EgressClass.CLOUD_ALLOWED_FULL,
                    cloud_egress_acknowledged=True,
                    allowed_profile_ids=(profile.id,),
                    allowed_provider_kinds=("openrouter",),
                    max_cost_microusd=5000,
                ),
            ),
            status="ready",
        )
    )
    calls = []
    persisted = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "CANARY_OK"}}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects):
            assert timeout == 2
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, endpoint, *, headers, json):
            calls.append((endpoint, headers, json))
            return FakeResponse()

    monkeypatch.setattr(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        FakeAsyncClient,
    )

    async def persist_receipt(receipt):
        persisted["receipt"] = receipt
        return ReceiptPersistenceResult.success(receipt)

    async def persist_proof(proof):
        persisted["proof"] = proof
        return ProofPersistenceResult(
            proof_hash=proof.proof_hash,
            status="persisted",
            persisted=True,
        )

    monkeypatch.setattr(
        "src.model_fabric.probe.model_fabric_repository.persist_route_receipt",
        persist_receipt,
    )
    monkeypatch.setattr(
        "src.model_fabric.probe.model_fabric_repository.persist_capability_proof",
        persist_proof,
    )
    payload = await run_model_fabric_canary(
        CapabilityCanaryRequest(
            profile_id=PERSISTED_PROFILE_ID,
            capability=capability,
            timeout_seconds=2,
            proof_ttl_seconds=120,
        ),
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/settings/model-fabric/canary",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        ),
    )

    assert payload["outcome"] == "passed"
    assert payload["receipt_persistence"] == "persisted"
    assert payload["proof_persistence"] == "persisted"
    assert payload["proof"]["capability"] == capability
    assert persisted["receipt"].outcome == "succeeded"
    assert persisted["proof"].capability == capability
    if expected_value is int:
        assert isinstance(payload["proof"]["proven_value"], int)
    else:
        assert payload["proof"]["proven_value"] == expected_value
    assert calls == [
        (
            f"{OPENROUTER_API_BASE}/chat/completions",
            {
                "Authorization": "Bearer test-openrouter-key",
                "Content-Type": "application/json",
            },
            {
                "model": PERSISTED_MODEL,
                "messages": [{"role": "user", "content": "Reply with CANARY_OK only."}],
                "max_tokens": 64,
            },
        )
    ]


@pytest.mark.asyncio
async def test_canonical_openrouter_screenshot_profile_is_configurable_probeable_and_status_visible(
    client,
    model_fabric_workspace,
):
    from src.vlm_runtime import SCREENSHOT_VLM_PROFILE_ID

    vision_profile = _profile_payload(profile_id=SCREENSHOT_VLM_PROFILE_ID)
    vision_profile.update(
        {
            "capabilities": ["text", "vision", "structured_output"],
            "task_class": "vision_analysis",
            "task_classes": ["vision_analysis"],
            "max_output_tokens": 1400,
        }
    )
    configured_response = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [vision_profile],
            "workload_policies": [_capability_probe_policy(profile_id=SCREENSHOT_VLM_PROFILE_ID)],
        },
    )
    assert configured_response.status_code == 200, configured_response.text

    configured = provider_profiles()[SCREENSHOT_VLM_PROFILE_ID]
    assert configured.provider_kind == "openrouter"
    assert configured.transport_adapter == "openai_compatible_chat"
    assert configured.api_base == OPENROUTER_API_BASE
    assert configured.capabilities == ("text", "vision", "structured_output")
    assert configured.max_latency_ms == 2000
    assert configured.local_resource_ms is None

    settings_response = await client.get("/api/settings/model-fabric")
    assert settings_response.status_code == 200
    status_profile = next(
        item for item in settings_response.json()["profiles"]
        if item["id"] == SCREENSHOT_VLM_PROFILE_ID
    )
    assert status_profile["provider_kind"] == "openrouter"
    assert status_profile["transport_adapter"] == "openai_compatible_chat"
    assert status_profile["model_fabric_eligible"] is True
    assert status_profile["canary_timeout_seconds"] == 2

    transport = AsyncMock(
        return_value=CapabilityProbeObservation(True, proven_value="verified")
    )
    with patch("src.api.model_fabric_settings._execute_canary_transport", transport):
        canary = await client.post(
            "/api/settings/model-fabric/canary",
            json={
                "profile_id": SCREENSHOT_VLM_PROFILE_ID,
                "capability": "vision",
                "timeout_seconds": 2,
                "proof_ttl_seconds": 120,
            },
        )
    assert canary.status_code == 200, canary.text
    assert canary.json()["outcome"] == "passed"
    assert transport.await_args.args[0].id == SCREENSHOT_VLM_PROFILE_ID

    runtime = await client.get("/api/runtime/status")
    proof_status = next(
        item for item in runtime.json()["model_fabric"]["proofs"]
        if item["profile_id"] == SCREENSHOT_VLM_PROFILE_ID
        and item["capability"] == "vision"
    )
    assert proof_status["status"] == "fresh"


@pytest.mark.asyncio
async def test_canonical_openrouter_canary_timeout_matches_declared_default_bound(
    client,
    model_fabric_workspace,
):
    from src.vlm_runtime import SCREENSHOT_VLM_PROFILE_ID

    with (
        patch.object(settings, "screen_analysis_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "agent_chat_timeout", 120),
    ):
        response = await client.get("/api/settings/model-fabric")

    assert response.status_code == 200
    profile = next(
        item for item in response.json()["profiles"]
        if item["id"] == SCREENSHOT_VLM_PROFILE_ID
    )
    assert profile["canary_timeout_seconds"] == 120


@pytest.mark.asyncio
async def test_manual_canary_is_process_serialized(client, model_fabric_workspace):
    from src.api.model_fabric_settings import _MANUAL_CANARY_LOCK

    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [_capability_probe_policy()],
        },
    )
    assert configured.status_code == 200
    assert _MANUAL_CANARY_LOCK.acquire(blocking=False)
    try:
        response = await client.post(
            "/api/settings/model-fabric/canary",
            json={"profile_id": PERSISTED_PROFILE_ID, "capability": "text"},
        )
    finally:
        _MANUAL_CANARY_LOCK.release()
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


@pytest.mark.asyncio
async def test_static_guardrails_are_not_exposed_as_fake_canaries(
    client,
    model_fabric_workspace,
):
    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [_capability_probe_policy()],
        },
    )
    assert configured.status_code == 200
    response = await client.post(
        "/api/settings/model-fabric/canary",
        json={"profile_id": PERSISTED_PROFILE_ID, "capability": "context_tokens"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported model capability"


@pytest.mark.asyncio
async def test_unauthenticated_canary_is_zero_transport(client, model_fabric_workspace):
    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [_capability_probe_policy()],
        },
    )
    assert configured.status_code == 200
    transport = AsyncMock(return_value=CapabilityProbeObservation(True, proven_value="verified"))
    tokens = set_runtime_context("", "high_risk", trust_principal=None)
    try:
        with patch("src.api.model_fabric_settings._execute_canary_transport", transport):
            response = await client.post(
                "/api/settings/model-fabric/canary",
                json={"profile_id": PERSISTED_PROFILE_ID, "capability": "text"},
            )
    finally:
        reset_runtime_context(tokens)
    assert response.status_code == 401
    assert transport.await_count == 0


@pytest.mark.asyncio
async def test_arbitrary_environment_credential_reference_is_rejected_and_not_echoed(
    client,
    model_fabric_workspace,
    monkeypatch,
):
    monkeypatch.setenv("SERAPH_EXFIL_TEST", "top-secret-value")
    profile = _profile_payload()
    profile.update({"keyless": False, "secret_env": "SERAPH_EXFIL_TEST"})
    response = await client.put("/api/settings/model-fabric", json={"profiles": [profile]})
    assert response.status_code == 422
    serialized = response.text
    assert "SERAPH_EXFIL_TEST" not in serialized
    assert "top-secret-value" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_base", "adapter"),
    [
        ("http://user:pass@127.0.0.1:8000/v1", "openai_compatible_chat"),
        ("http://127.0.0.1:8000/v1?token=secret", "openai_compatible_chat"),
        ("http://127.0.0.1:8000/v1#fragment", "openai_compatible_chat"),
        ("not-a-url", "openai_compatible_chat"),
        ("http://127.0.0.1:8000/v1", "arbitrary_adapter"),
    ],
)
async def test_unsafe_endpoint_or_adapter_is_rejected(
    client,
    model_fabric_workspace,
    api_base,
    adapter,
):
    profile = _profile_payload()
    profile.update({"api_base": api_base, "transport_adapter": adapter})
    response = await client.put("/api/settings/model-fabric", json={"profiles": [profile]})
    assert response.status_code == 422
    assert "pass" not in response.text
    assert "token=secret" not in response.text


@pytest.mark.asyncio
async def test_receipt_persistence_degradation_remains_operator_visible(
    client,
    model_fabric_workspace,
):
    configured = await client.put(
        "/api/settings/model-fabric",
        json={
            "profiles": [_profile_payload()],
            "workload_policies": [_capability_probe_policy()],
        },
    )
    assert configured.status_code == 200
    transport = AsyncMock(return_value=CapabilityProbeObservation(True, proven_value="verified"))
    degraded = ReceiptPersistenceResult.degraded("probe-test")
    with (
        patch("src.api.model_fabric_settings._execute_canary_transport", transport),
        patch(
            "src.model_fabric.repository.model_fabric_repository.persist_route_receipt",
            AsyncMock(return_value=degraded),
        ),
    ):
        response = await client.post(
            "/api/settings/model-fabric/canary",
            json={"profile_id": PERSISTED_PROFILE_ID, "capability": "text"},
        )
    assert response.status_code == 200
    assert response.json()["receipt_persistence"] == "degraded"
    assert response.json()["proof"] is None

    runtime = await client.get("/api/runtime/status")
    assert runtime.json()["model_fabric"]["status"] == "degraded"
    workload = runtime.json()["model_fabric"]["workloads"]["capability_probe"]
    assert workload["persistence"] == "degraded"
    assert workload["persistence_error_code"] == "receipt_persistence_failed"
    assert workload["receipt_persistence_degraded"] is True


@pytest.mark.asyncio
async def test_invalid_persisted_metadata_is_operator_visible(client, model_fabric_workspace):
    path = Path(settings.workspace_dir) / "model-fabric-settings.json"
    path.write_text("{invalid", encoding="utf-8")

    response = await client.get("/api/settings/model-fabric")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["error_code"] == "configuration_unreadable"

    runtime = await client.get("/api/runtime/status")
    assert runtime.json()["model_fabric"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_transitional_provider_is_visible_only_as_excluded(client, model_fabric_workspace):
    response = await client.get("/api/settings/model-fabric")
    assert response.status_code == 200
    payload = response.json()
    assert all(profile["provider_kind"] != "anthropic" for profile in payload["profiles"])
    claude = next(
        profile for profile in payload["excluded_profiles"]
        if profile["provider_kind"] == "anthropic"
    )
    assert claude["model_fabric_eligible"] is False
    assert claude["model_fabric_exclusion_reason"] == "provider_family_excluded"
    assert "secret_env" not in claude


def test_streaming_canary_rejects_keepalives_and_requires_content_delta():
    from src.api.model_fabric_settings import _streaming_canary_delta

    assert _streaming_canary_delta(": keepalive") is False
    assert _streaming_canary_delta("data: [DONE]") is False
    assert _streaming_canary_delta('data: {"choices":[{"delta":{"role":"assistant"}}]}') is False
    assert _streaming_canary_delta('data: {"choices":[{"delta":{"content":"OK"}}]}') is True


def test_vlm_canary_rejects_health_only_shape_and_requires_analysis_content():
    from src.api.model_fabric_settings import _validate_vlm_canary_response

    assert _validate_vlm_canary_response({"status": "healthy"}) is False
    assert _validate_vlm_canary_response({"error": "backend unavailable"}) is False
    assert _validate_vlm_canary_response(
        {"result": {"text": "visible pixel"}}, "structured_output"
    ) is False
    assert _validate_vlm_canary_response(
        {"analysis": "generic nonempty text"}, "structured_output"
    ) is False
    assert _validate_vlm_canary_response(
        {
            "analysis": {
                "schema_version": "seraph.screenshot_analysis.v1",
                "summary": "Visible pixel",
            }
        },
        "structured_output",
    ) is True
    assert _validate_vlm_canary_response(
        {
            "analysis": {
                "schema_version": "seraph.screenshot_analysis.v1",
                "summary": "   ",
            }
        },
        "structured_output",
    ) is False


def test_legacy_advisory_profile_tags_remain_excluded_without_fake_proof_requirements():
    from src.api.model_fabric_settings import _operator_profile_statuses

    profile = ProviderProfile(
        id="local-advisory",
        provider_kind="openai_compatible",
        model="gemma",
        api_base="http://127.0.0.1:8000/v1",
        capabilities=("text", "local", "private", "reasoning_profile", "coding"),
        keyless=True,
        transport_adapter="openai_compatible_chat",
        context_window_tokens=8192,
        max_output_tokens=1024,
        local_resource_ms=2000,
        max_latency_ms=2000,
        task_classes=("interactive_chat",),
    )
    proofs = [
        {"profile_id": profile.id, "capability": capability, "status": "fresh"}
        for capability in ("text", "health", "latency_ms")
    ]
    with patch("src.api.model_fabric_settings.provider_profiles", return_value={profile.id: profile}):
        status = _operator_profile_statuses(proofs)[0]

    assert status["routable"] is False
    assert status["model_fabric_eligible"] is False
    assert status["model_fabric_exclusion_reason"] == "provider_kind_not_allowed"
    assert status["capabilities"] == ["text", "local", "private", "reasoning_profile", "coding"]
    assert status["non_routable_reasons"] == ["provider_kind_not_allowed"]
    assert not any("local" in reason or "coding" in reason for reason in status["non_routable_reasons"])
