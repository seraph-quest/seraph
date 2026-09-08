"""Guard the canonical inference callers that must adopt the model fabric.

This inventory is intentionally structural.  It prevents a new background or
privacy-sensitive completion path from bypassing the immutable request context
even when focused behavioral tests mock the transport.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"


CANONICAL_COMPLETION_CALLERS = {
    "agent/context_window.py": {"context_window_summary"},
    "agent/session.py": {"session_title_generation"},
    "agent/strategist.py": {"strategist_agent"},
    "memory/pipeline/extract.py": {"session_consolidation"},
    "scheduler/jobs/activity_digest.py": {"activity_digest"},
    "scheduler/jobs/daily_briefing.py": {"daily_briefing"},
    "scheduler/jobs/end_of_day_goal_report.py": {"end_of_day_goal_report"},
    "scheduler/jobs/evening_review.py": {"evening_review"},
    "scheduler/jobs/screenshot_observation_digest.py": {"screenshot_observation_digest"},
    "scheduler/jobs/weekly_activity_review.py": {"weekly_activity_review"},
}

EXPECTED_CANONICAL_ROUTES = {
    "chat_agent",
    "onboarding_agent",
    "orchestrator_agent",
    "strategist_agent",
    "session_title_generation",
    "session_consolidation",
    "context_window_summary",
    "daily_briefing",
    "activity_digest",
    "evening_review",
    "weekly_activity_review",
    "end_of_day_goal_report",
    "screenshot_observation_digest",
    "screenshot_image_analysis",
    "memory_embedding",
    "memory_keeper",
    "vault_keeper",
    "goal_planner",
    "web_researcher",
    "file_worker",
    "workflow_runner",
}


def _source(relative_path: str) -> str:
    return (BACKEND_SRC / relative_path).read_text(encoding="utf-8")


def _literal_runtime_paths(relative_path: str) -> set[str]:
    tree = ast.parse(_source(relative_path), filename=relative_path)
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "runtime_path"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                paths.add(keyword.value.value)
    return paths


def _completion_calls(relative_path: str) -> list[ast.Call]:
    tree = ast.parse(_source(relative_path), filename=relative_path)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = ""
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr
        if function_name in {
            "completion_fn",
            "completion_with_fallback",
            "completion_with_fallback_sync",
            "stream_completion_with_fallback",
        }:
            calls.append(node)
    return calls


def test_canonical_model_route_inventory_keeps_named_runtime_paths():
    from src.agent.specialists import SPECIALIST_CONFIGS
    from src.model_fabric.caller_context import (
        CANONICAL_ROUTE_SPECS,
        CANONICAL_SPECIALIST_ROUTES,
        canonical_route_spec,
    )

    assert set(CANONICAL_ROUTE_SPECS) == EXPECTED_CANONICAL_ROUTES
    assert set(CANONICAL_SPECIALIST_ROUTES) == {*SPECIALIST_CONFIGS, "workflow_runner"}
    assert canonical_route_spec("mcp_github").task_class == "interactive_chat"
    for relative_path, expected_paths in CANONICAL_COMPLETION_CALLERS.items():
        observed = _literal_runtime_paths(relative_path)
        assert expected_paths <= observed, (
            f"{relative_path} lost a canonical runtime path: "
            f"expected {sorted(expected_paths)}, observed {sorted(observed)}"
        )


def test_canonical_completion_callers_bind_immutable_request_context():
    missing: list[str] = []
    for relative_path in CANONICAL_COMPLETION_CALLERS:
        for call in _completion_calls(relative_path):
            if not any(keyword.arg == "request_context" for keyword in call.keywords):
                missing.append(f"{relative_path}:{call.lineno}")

    assert missing == [], (
        "canonical inference calls must pass request_context; missing at "
        + ", ".join(missing)
    )


def test_direct_chat_streaming_does_not_call_litellm_transport_directly():
    tree = ast.parse(_source("agent/direct_chat.py"), filename="agent/direct_chat.py")
    direct_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "litellm"
            and node.func.attr == "completion"
        ):
            direct_calls.append(node.lineno)

    assert direct_calls == [], (
        "direct chat streaming must enter the shared selector/receipt path; "
        f"direct litellm calls remain at lines {direct_calls}"
    )


@pytest.mark.parametrize(
    "runtime_path",
    [
        "orchestrator_agent",
        "memory_keeper",
        "vault_keeper",
        "goal_planner",
        "web_researcher",
        "file_worker",
        "workflow_runner",
        "mcp_github",
    ],
)
def test_registered_orchestrator_and_specialists_are_zero_transport_without_identity(
    runtime_path,
):
    from unittest.mock import patch

    from src.llm_runtime import FallbackLiteLLMModel

    model = FallbackLiteLLMModel(
        model_id="openai/test-model",
        api_base="http://127.0.0.1:8000/v1",
        api_key="test-key",
        runtime_profile="default",
        runtime_path=runtime_path,
        max_tokens=64,
    )
    with (
        patch("src.llm_runtime.get_current_trust_principal", return_value=None),
        patch("src.llm_runtime.BaseLiteLLMModel.generate") as transport,
    ):
        with pytest.raises(PermissionError, match="explicit runtime principal"):
            model.generate([{"role": "user", "content": "private task"}])
    transport.assert_not_called()


def test_registered_agent_binds_tools_response_format_and_options_to_exact_transport_digest():
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from src.llm_runtime import FallbackLiteLLMModel
    from src.security.trust_contract import (
        AuthorityGrant,
        PrincipalType,
        TrustPrincipal,
        canonical_digest,
    )

    principal = TrustPrincipal(
        principal_id="operator:test-agent",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-agent",
    )
    captured = {}
    decision = MagicMock(allowed=True, selected=MagicMock())

    def preflight(_target, context):
        captured["context"] = context
        return decision, ()

    def transport(*, body, **_kwargs):
        captured["body"] = body
        return (
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            ),
            {"choices": [{"message": {"content": "ok"}}]},
        )

    model = FallbackLiteLLMModel(
        model_id="openai/test-model",
        api_base="http://127.0.0.1:8000/v1",
        api_key="test-key",
        runtime_profile="default",
        runtime_path="orchestrator_agent",
        temperature=0.25,
        max_tokens=96,
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate one bounded task",
            "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
        },
    }]
    response_format = {"type": "json_object"}
    with (
        patch("src.llm_runtime.get_current_trust_principal", return_value=principal),
        patch("src.llm_runtime._governed_preflight_target", side_effect=preflight),
        patch("src.llm_runtime._new_route_receipt_session", return_value=None),
        patch("src.llm_runtime._governed_openai_chat_completion", side_effect=transport),
        patch("src.llm_runtime._can_log_request", return_value=False),
    ):
        model.generate(
            [{"role": "user", "content": "delegate this exact task"}],
            stop_sequences=["STOP"],
            tools_to_call_from=tools,
            response_format=response_format,
        )

    assert captured["body"]["tools"] == tools
    assert captured["body"]["response_format"] == response_format
    assert captured["body"]["temperature"] == 0.25
    assert captured["body"]["stop"] == ["STOP"]
    assert captured["context"].data_digest == canonical_digest(captured["body"])


def test_canonical_context_fails_closed_without_fabricating_principal_or_job_identity():
    from src.model_fabric.caller_context import build_canonical_inference_context

    with pytest.raises(PermissionError, match="authenticated runtime principal"):
        build_canonical_inference_context(
            "daily_briefing",
            payload="private briefing",
            output_tokens=100,
            timeout_seconds=10,
        )


def test_canonical_context_requires_exact_caller_supplied_identity_and_defaults_local_only():
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.security.trust_contract import (
        AuthorityGrant,
        EgressClass,
        PrincipalType,
        TrustPrincipal,
    )

    principal = TrustPrincipal(
        principal_id="service:test-scheduler",
        principal_type=PrincipalType.SERVICE,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        job_id="scheduler:test:attempt-1",
    )
    context = build_canonical_inference_context(
        "daily_briefing",
        payload="private briefing",
        output_tokens=100,
        timeout_seconds=10,
        principal=principal,
        job_id=principal.job_id,
    )

    assert context.principal is principal
    assert context.job_id == principal.job_id
    assert context.session_id == ""
    assert context.egress_class is EgressClass.LOCAL_ONLY
    assert context.provenance[0].egress_class is EgressClass.LOCAL_ONLY

    with pytest.raises(ValueError, match="exactly match"):
        build_canonical_inference_context(
            "daily_briefing",
            payload="private briefing",
            output_tokens=100,
            timeout_seconds=10,
            principal=principal,
            job_id="scheduler:test:fabricated",
        )


def test_canonical_context_preserves_bound_operator_and_scheduled_service_identity():
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal

    principals = (
        TrustPrincipal(
            principal_id="operator:test",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="session-test",
        ),
        TrustPrincipal(
            principal_id="service:scheduler:test",
            principal_type=PrincipalType.SERVICE,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            job_id="scheduler:test:attempt-1",
        ),
    )
    for principal in principals:
        tokens = set_runtime_context(
            principal.session_id or None,
            "high_risk",
            trust_principal=principal,
        )
        try:
            context = build_canonical_inference_context(
                "context_window_summary",
                payload="private session history",
                output_tokens=100,
                timeout_seconds=10,
            )
        finally:
            reset_runtime_context(tokens)
        assert context.principal is principal
        assert context.session_id == principal.session_id
        assert context.job_id == principal.job_id


def test_session_scoped_callers_do_not_clear_bound_principal():
    for relative_path in (
        "agent/context_window.py",
        "agent/session.py",
        "memory/pipeline/extract.py",
    ):
        source = _source(relative_path)
        assert "trust_principal=get_current_trust_principal()" in source


def test_screenshot_vlm_adapter_declares_model_fabric_preflight_and_receipt_hooks():
    source = _source("observer/screenshot_semantic_analysis.py")
    assert "build_canonical_inference_context" in source
    assert "select_route" in source
    assert "run_preflighted_adapter" in source
    assert "PersistedRouteReceiptHooks" in source
    assert "persistence_result" in source


@pytest.mark.asyncio
async def test_screenshot_vlm_adapter_preflights_and_requires_persisted_receipt(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from src.observer.screenshot_semantic_analysis import _run_governed_vlm_adapter

    context = SimpleNamespace(
        request_id="vlm-request",
        requirements=SimpleNamespace(capabilities=("vision", "structured_output")),
    )
    profile = SimpleNamespace(
        schema_version="seraph.model-fabric.v1",
        contract_hash="a" * 64,
        id="openrouter-screenshot",
        model="anthropic/claude-sonnet-4",
    )
    candidate = SimpleNamespace(
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        endpoint_class=SimpleNamespace(value="remote"),
        adapter="openai_compatible_chat",
    )
    proof = SimpleNamespace(proof_hash="b" * 64)
    latest = AsyncMock(return_value=proof)
    monkeypatch.setattr(
        "src.observer.screenshot_semantic_analysis.model_fabric_repository.latest_capability_proof",
        latest,
    )
    monkeypatch.setattr(
        "src.observer.screenshot_semantic_analysis.candidate_from_profile",
        lambda _profile: candidate,
    )
    decision = SimpleNamespace(allowed=True, selected=candidate)
    select = Mock(return_value=decision)
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.select_route", select)

    class Hooks:
        def __init__(self, *, capability_proof_hashes):
            self.proof_hashes = capability_proof_hashes

        async def persistence_result(self, request_id):
            assert request_id == "vlm-request"
            return SimpleNamespace(persisted=True)

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.PersistedRouteReceiptHooks", Hooks)
    execute = AsyncMock(return_value="analysis")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.run_preflighted_adapter", execute)

    transport = AsyncMock()
    result = await _run_governed_vlm_adapter(
        context=context,
        profile=profile,
        transport=transport,
    )

    assert result == "analysis"
    assert latest.await_count == 4
    assert {
        call.kwargs["capability"] for call in latest.await_args_list
    } == {"vision", "structured_output", "latency_ms", "health"}
    select.assert_called_once()
    assert execute.await_args.kwargs["context"] is context
    assert execute.await_args.kwargs["decision"] is decision


@pytest.mark.asyncio
async def test_screenshot_vlm_rejects_legacy_file_adapter_before_proof_or_transport(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.model_fabric import ProviderProfile
    from src.observer.screenshot_semantic_analysis import (
        ScreenshotSemanticAnalysisError,
        _run_governed_vlm_adapter,
    )

    profile = ProviderProfile(
        id="legacy-vlm-screenshot",
        provider_kind="openrouter",
        model="gemma-vlm-test",
        api_base="https://openrouter.ai/api/v1",
        capabilities=("vision", "structured_output"),
        task_classes=("vision_analysis",),
        transport_adapter="vlm_analyze_file",
        secret_env="OPENROUTER_API_KEY",
        context_window_tokens=8192,
        max_output_tokens=1024,
        max_latency_ms=5000,
    )
    context = SimpleNamespace(
        request_id="vlm-wrong-adapter",
        requirements=SimpleNamespace(capabilities=("vision", "structured_output")),
    )
    latest_proof = AsyncMock()
    monkeypatch.setattr(
        "src.observer.screenshot_semantic_analysis.model_fabric_repository.latest_capability_proof",
        latest_proof,
    )
    transport = AsyncMock()

    with pytest.raises(
        ScreenshotSemanticAnalysisError,
        match="requires the OpenRouter chat adapter",
    ):
        await _run_governed_vlm_adapter(
            context=context,
            profile=profile,
            transport=transport,
        )

    latest_proof.assert_not_awaited()
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_screenshot_vlm_no_compliant_route_persists_zero_attempt_denial(monkeypatch):
    import time
    from unittest.mock import AsyncMock

    from src.model_fabric import (
        InferenceRequestContext,
        InferenceRequirements,
        InferenceWorkload,
        NoCompliantModelRouteError,
        ProviderProfile,
        ReceiptPersistenceResult,
    )
    from src.observer.screenshot_semantic_analysis import _run_governed_vlm_adapter
    from src.model_fabric.runtime_status import (
        clear_receipt_persistence_observations,
        latest_receipt_persistence,
    )
    from src.security.trust_contract import (
        AuthorityGrant,
        ContentOrigin,
        EgressClass,
        PrincipalType,
        TrustPrincipal,
        TrustProvenance,
        canonical_digest,
    )

    clear_receipt_persistence_observations()
    digest = canonical_digest({"fields": {"prompt": "private"}, "file": {"sha256": "a" * 64}})
    context = InferenceRequestContext(
        principal=TrustPrincipal(
            principal_id="service:test-vlm",
            principal_type=PrincipalType.SERVICE,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            job_id="vlm-denied",
        ),
        session_id="",
        job_id="vlm-denied",
        provenance=(TrustProvenance(ContentOrigin.PAIRED_EDGE, "vlm", digest, EgressClass.LOCAL_ONLY),),
        data_digest=digest,
        egress_class=EgressClass.LOCAL_ONLY,
        transformation_digest="no_transformation",
        request_id="vlm-denied-request",
        runtime_path="screenshot_image_analysis",
        workload=InferenceWorkload.VISION,
        requirements=InferenceRequirements(
            capabilities=("vision", "structured_output"),
            context_tokens=64,
            output_tokens=64,
            max_cost_microusd=None,
            max_local_resource_ms=5000,
            max_latency_ms=5000,
            task_class="vision_analysis",
        ),
        deadline_at=time.time() + 5,
    )
    profile = ProviderProfile(
        id="openrouter-screenshot",
        provider_kind="openrouter",
        model="anthropic/claude-sonnet-4",
        api_base="https://openrouter.ai/api/v1",
        capabilities=("vision", "structured_output"),
        task_classes=("vision_analysis",),
        transport_adapter="openai_compatible_chat",
        secret_env="OPENROUTER_API_KEY",
        context_window_tokens=8192,
        max_output_tokens=1024,
        max_latency_ms=5000,
        cost_microusd=100,
        cost_source="test",
        cost_source_updated_at=time.time(),
        options={
            "provider": {
                "only": ["anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
        },
    )
    receipt = None

    async def persist(value):
        nonlocal receipt
        receipt = value
        return ReceiptPersistenceResult.degraded(value.receipt_id)

    monkeypatch.setattr(
        "src.observer.screenshot_semantic_analysis.model_fabric_repository.latest_capability_proof",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.model_fabric.hooks.model_fabric_repository.persist_route_receipt",
        persist,
    )
    transport = AsyncMock()

    with pytest.raises(NoCompliantModelRouteError, match="no_compliant_route"):
        await _run_governed_vlm_adapter(context=context, profile=profile, transport=transport)

    transport.assert_not_called()
    assert receipt is not None
    assert receipt.outcome == "denied"
    assert receipt.attempts == ()
    observation = latest_receipt_persistence("screenshot_image_analysis")
    assert observation is not None
    assert observation.status == "degraded"
    assert observation.error_code == "receipt_persistence_failed"


def test_canonical_screenshot_vlm_profile_passes_real_selector_with_exact_proofs():
    import time
    from unittest.mock import patch

    from config.settings import settings
    from src.model_fabric import ProviderProfile, candidate_from_profile, select_route
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.configuration import WorkloadPolicy
    from src.model_fabric.proofs import build_model_route_proof
    from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal

    principal = TrustPrincipal(
        principal_id="service:test-vlm",
        principal_type=PrincipalType.SERVICE,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        job_id="screenshot-analysis:test-1",
    )
    with (
        patch.object(settings, "openrouter_provider_only", True),
        patch.object(settings, "openrouter_api_key", "test-openrouter-key"),
        patch.object(settings, "openrouter_allowed_upstreams", "anthropic"),
        patch.object(settings, "openrouter_allow_fallbacks", False),
        patch.object(settings, "openrouter_require_parameters", True),
        patch.object(settings, "openrouter_data_collection", "deny"),
        patch("src.model_fabric.caller_context.effective_workload_policy", return_value=WorkloadPolicy(
            "screenshot_image_analysis",
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            cloud_egress_acknowledged=True,
            allowed_provider_kinds=("openrouter",),
            max_cost_microusd=1000,
        )),
    ):
        profile = ProviderProfile(
            id="openrouter-screenshot",
            provider_kind="openrouter",
            model="anthropic/claude-sonnet-4",
            api_base="https://openrouter.ai/api/v1",
            secret_env="OPENROUTER_API_KEY",
            options={"provider": {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True, "data_collection": "deny", "zdr": True}},
            capabilities=("vision", "structured_output"),
            task_classes=("vision_analysis",),
            context_window_tokens=8192,
            max_output_tokens=1400,
            max_latency_ms=5_000,
            cost_microusd=100,
            cost_source="test",
            cost_source_updated_at=time.time(),
        )
        candidate = candidate_from_profile(profile)
        context = build_canonical_inference_context(
            "screenshot_image_analysis",
            payload={
                "fields": {"prompt": "bounded prompt", "model": profile.model},
                "file": {"filename": "screen.png", "content_type": "image/png", "sha256": "a" * 64},
            },
            output_tokens=1400,
            timeout_seconds=5,
            principal=principal,
            job_id=principal.job_id,
        )
        now = time.time()
        proven_values = {
            "vision": "verified",
            "structured_output": "verified",
            "latency_ms": 100,
            "health": "healthy",
        }
        proofs = tuple(
            build_model_route_proof(
                profile=profile,
                endpoint_class=candidate.endpoint_class,
                adapter=candidate.adapter,
                capability=capability,
                canary_version="canary-v1",
                outcome="passed",
                checked_at=now - 1,
                expires_at=now + 60,
                probe_receipt_id=f"probe-{capability}",
                probe_receipt_hash="b" * 64,
                proven_value=value,
            )
            for capability, value in proven_values.items()
        )

        decision = select_route(context, (candidate,), proofs, now=now)
        assert decision.allowed is True
        assert decision.selected == candidate
        assert set(decision.proof_hashes) == {proof.proof_hash for proof in proofs}

        missing_health = select_route(
            context,
            (candidate,),
            tuple(proof for proof in proofs if proof.capability != "health"),
            now=now,
        )
        assert missing_health.allowed is False
        assert missing_health.rejections[0].reason_code == "proof_missing:health"


@pytest.mark.asyncio
async def test_screenshot_vlm_missing_principal_is_zero_transport(tmp_path):
    from src.observer.screenshot_semantic_analysis import (
        ScreenshotSemanticAnalysisError,
        _analyze_with_local_vlm,
    )

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"bounded-test-image")
    with pytest.raises(ScreenshotSemanticAnalysisError, match="local_vlm_disabled"):
        await _analyze_with_local_vlm(image_path, {})


@pytest.mark.asyncio
async def test_screenshot_vlm_binds_exact_multipart_fields_and_file_digest(tmp_path):
    from src.observer.screenshot_semantic_analysis import (
        ScreenshotSemanticAnalysisError,
        _analyze_with_local_vlm,
    )

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"exact-image-body")
    with pytest.raises(ScreenshotSemanticAnalysisError, match="local_vlm_disabled"):
        await _analyze_with_local_vlm(image_path, {})
