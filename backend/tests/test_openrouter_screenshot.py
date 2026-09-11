"""Focused proof for the OpenRouter-only screenshot transport."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from src.model_fabric.configuration import WorkloadPolicy
from src.model_fabric.contracts import ProviderProfile, OPENROUTER_API_BASE
from src.security.trust_contract import EgressClass


def _profile() -> ProviderProfile:
    return ProviderProfile(
        id="openrouter-screenshot-vision",
        provider_kind="openrouter",
        model="anthropic/claude-sonnet-4",
        api_base=OPENROUTER_API_BASE,
        secret_env="OPENROUTER_API_KEY",
        options={
            "provider": {
                "only": ["anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            }
        },
        capabilities=("text", "vision", "structured_output"),
        task_class="vision_analysis",
        task_classes=("vision_analysis",),
        context_window_tokens=8192,
        max_output_tokens=1400,
        max_latency_ms=120_000,
        cost_microusd=100,
        cost_source="test",
        cost_source_updated_at=time.time(),
    )


def _analysis_payload() -> dict[str, object]:
    return {
        "schema_version": "seraph.screenshot_analysis.v1",
        "prompt_version": "seraph.screenshot_analysis.prompt.v1",
        "summary": "The operator is reviewing a Seraph test.",
        "detailed_observations": ["A test editor is visible."],
        "activity_type": "reviewing",
        "project": "seraph",
        "applications": ["editor"],
        "visible_artifacts": ["test_openrouter_screenshot.py"],
        "key_visible_text": [],
        "user_intent": "Verify remote screenshot analysis.",
        "goal_alignment": {
            "status": "aligned",
            "goal_refs": [],
            "evidence": [],
            "needle_movement": "pushed",
        },
        "confidence": 0.8,
        "sensitive_content_seen": False,
        "privacy_notes": [],
        "report_tags": ["screenshot"],
    }


def test_screenshot_model_identifier_requires_explicit_openrouter_qualification():
    from src.observer.screen_analysis_settings import normalize_openrouter_model_identifier

    assert normalize_openrouter_model_identifier("openrouter/anthropic/claude-sonnet-4") == (
        "openrouter/anthropic/claude-sonnet-4"
    )
    assert normalize_openrouter_model_identifier("anthropic/claude-sonnet-4") == ""
    assert normalize_openrouter_model_identifier("local/gemma-vision") == ""
    assert normalize_openrouter_model_identifier("openrouter/gemini") == ""


@pytest.mark.asyncio
async def test_openrouter_screenshot_uses_inline_bytes_and_no_local_endpoint(tmp_path, monkeypatch):
    from src.observer import screenshot_semantic_analysis as module

    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    profile = _profile()
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(_analysis_payload())}}]}

    class Client:
        def __init__(self, *, timeout, follow_redirects):
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, endpoint, *, json, headers):
            calls.append({"endpoint": endpoint, "json": json, "headers": headers})
            return Response()

    async def governed(*, context, profile, transport):
        return await transport(module.candidate_from_profile(profile), False)

    monkeypatch.setattr(module.settings, "screen_analysis_provider", "openrouter")
    monkeypatch.setattr(module.settings, "screen_analysis_model", "openrouter/anthropic/claude-sonnet-4")
    monkeypatch.setattr(module.settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(module.settings, "openrouter_provider_only", True)
    monkeypatch.setattr(module.settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(module.settings, "openrouter_allow_fallbacks", False)
    monkeypatch.setattr(module.settings, "openrouter_require_parameters", True)
    monkeypatch.setattr(module.settings, "openrouter_data_collection", "deny")
    monkeypatch.setattr(module.settings, "openrouter_zero_data_retention", True)
    monkeypatch.setattr(
        module,
        "effective_workload_policy",
        lambda _path: WorkloadPolicy(
            "screenshot_image_analysis",
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            cloud_egress_acknowledged=True,
            allowed_provider_kinds=("openrouter",),
            max_cost_microusd=1000,
        ),
    )
    monkeypatch.setattr(module, "provider_profiles", lambda: {profile.id: profile})
    monkeypatch.setattr(module, "_run_governed_vlm_adapter", governed)
    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        module,
        "build_canonical_inference_context",
        lambda *_args, **_kwargs: SimpleNamespace(deadline_at=10**12),
    )
    monkeypatch.setattr(module, "bind_final_inference_payload", lambda context, _body: context)

    result = await module.analyze_screenshot_image(image, {"created_at": "2026-09-08T00:00:00Z"})

    assert result is not None
    assert calls[0]["endpoint"] == f"{OPENROUTER_API_BASE}/chat/completions"
    body = calls[0]["json"]
    assert isinstance(body, dict)
    assert body["model"] == "anthropic/claude-sonnet-4"
    content = body["messages"][0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert str(image) not in json.dumps(body)
    assert "127.0.0.1" not in str(calls[0]["endpoint"])
    assert calls[0]["headers"] == {
        "Authorization": "Bearer test-openrouter-key",
        "Content-Type": "application/json",
    }


@pytest.mark.asyncio
async def test_openrouter_screenshot_rejects_oversize_before_governed_dispatch(tmp_path, monkeypatch):
    from src.observer import screenshot_semantic_analysis as module

    image = tmp_path / "large.png"
    image.write_bytes(b"x" * (module.MAX_IMAGE_BYTES + 1))
    profile = _profile()
    monkeypatch.setattr(module.settings, "screen_analysis_provider", "openrouter")
    monkeypatch.setattr(module.settings, "screen_analysis_model", f"openrouter/{profile.model}")
    monkeypatch.setattr(module.settings, "openrouter_api_key", "key")
    monkeypatch.setattr(module.settings, "openrouter_provider_only", True)
    monkeypatch.setattr(module.settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(module.settings, "openrouter_allow_fallbacks", False)
    monkeypatch.setattr(module.settings, "openrouter_require_parameters", True)
    monkeypatch.setattr(module.settings, "openrouter_data_collection", "deny")
    monkeypatch.setattr(module.settings, "openrouter_zero_data_retention", True)
    monkeypatch.setattr(
        module,
        "effective_workload_policy",
        lambda _path: WorkloadPolicy(
            "screenshot_image_analysis",
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            cloud_egress_acknowledged=True,
            allowed_provider_kinds=("openrouter",),
            max_cost_microusd=1000,
        ),
    )
    monkeypatch.setattr(module, "provider_profiles", lambda: {profile.id: profile})
    dispatched = False

    async def governed(**_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("oversize image must be rejected before governed dispatch")

    monkeypatch.setattr(module, "_run_governed_vlm_adapter", governed)

    with pytest.raises(module.ScreenshotSemanticAnalysisError, match="8 MiB"):
        await module.analyze_screenshot_image(image, {})
    assert dispatched is False


@pytest.mark.asyncio
async def test_openrouter_screenshot_requires_explicit_cloud_policy(tmp_path, monkeypatch):
    from src.observer import screenshot_semantic_analysis as module

    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    monkeypatch.setattr(module.settings, "screen_analysis_provider", "openrouter")
    monkeypatch.setattr(module.settings, "screen_analysis_model", "openrouter/anthropic/claude-sonnet-4")
    monkeypatch.setattr(module.settings, "openrouter_api_key", "key")
    monkeypatch.setattr(module.settings, "openrouter_provider_only", True)
    monkeypatch.setattr(module.settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(module.settings, "openrouter_allow_fallbacks", False)
    monkeypatch.setattr(module.settings, "openrouter_require_parameters", True)
    monkeypatch.setattr(module.settings, "openrouter_data_collection", "deny")
    monkeypatch.setattr(module.settings, "openrouter_zero_data_retention", True)
    monkeypatch.setattr(
        module,
        "effective_workload_policy",
        lambda _path: WorkloadPolicy("screenshot_image_analysis"),
    )

    assert module.screenshot_semantic_analysis_enabled() is False
    assert await module.analyze_screenshot_image(image, {}) is None
