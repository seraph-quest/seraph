"""ADR-035 PR3a: OpenRouter retired by policy, provider-neutral lane config.

Every provider transport is intercepted with spies that fail the test if they
are called. No provider, network or inference call is made.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from unittest.mock import patch

import httpx
import pytest

from config.settings import settings
from src.model_fabric import retirement
from src.model_fabric.caller_context import CANONICAL_ROUTE_SPECS, build_canonical_inference_context
from src.model_fabric.configuration import (
    InferenceLaneConfig,
    OpenRouterSetup,
    ModelFabricConfiguration,
    _configuration_from_payload,
    _configuration_payload,
    deployment_spend_ceiling,
    effective_inference_lane,
    effective_provider_profiles,
    effective_workload_policy,
    migrate_inference_lane,
    read_model_fabric_configuration,
    retire_openrouter_setup,
)
from src.model_fabric.contracts import NoCompliantModelRouteError
from src.model_fabric.retirement import OPENROUTER_RETIRED_REASON, ProviderRetiredError
from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal
from tests.test_model_fabric_openrouter_policy import _context, _profile, _proofs
from tests.test_openrouter_setup_api import _v2_setup_payload, keyless_openrouter, model_fabric_workspace  # noqa: F401


def _principal() -> TrustPrincipal:
    return TrustPrincipal(principal_id="operator-retirement", principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,), session_id="session-retirement")


@contextmanager
def _no_provider_contact():
    """Intercept every provider transport; any call fails the test."""
    calls: list[str] = []

    def forbidden(name, original=None):
        def spy(self=None, *args, **kwargs):
            # The in-process ASGI test client is not a provider transport.
            if original is not None and isinstance(getattr(self, "_transport", None), httpx.ASGITransport):
                return original(self, *args, **kwargs)
            calls.append(name)
            raise AssertionError(f"provider transport contacted: {name}")
        return spy

    with (
        patch.object(httpx.Client, "post", forbidden("httpx.Client.post")),
        patch.object(httpx.Client, "stream", forbidden("httpx.Client.stream")),
        patch.object(httpx.AsyncClient, "post", forbidden("httpx.AsyncClient.post", httpx.AsyncClient.post)),
        patch.object(httpx.AsyncClient, "stream", forbidden("httpx.AsyncClient.stream", httpx.AsyncClient.stream)),
        patch("litellm.completion", forbidden("litellm.completion")),
        patch("litellm.acompletion", forbidden("litellm.acompletion")),
    ):
        yield calls
    assert calls == []


def _legacy_context(runtime_path="chat_agent", **kwargs):
    """Build a context the way a pre-retirement caller (or a stale one) would."""
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        return build_canonical_inference_context(runtime_path, payload={"message": "hello"},
            output_tokens=64, timeout_seconds=5, principal=_principal(),
            session_id="session-retirement", **kwargs)


# ── Policy surface ───────────────────────────────────────────────


def test_retirement_is_policy_not_configuration(monkeypatch):
    monkeypatch.setenv("SERAPH_OPENROUTER_RETIRED", "false")
    monkeypatch.setattr(settings, "openrouter_provider_only", False)
    assert retirement.openrouter_retired() is True
    assert retirement.provider_retirement_reason("openrouter") == OPENROUTER_RETIRED_REASON
    assert retirement.provider_retirement_reason("near") is None


def test_selector_denies_openrouter_with_stable_reason_and_no_fallback():
    from src.model_fabric import active_provider_exclusion_reason, candidate_from_profile, select_route
    profile = _profile()
    assert active_provider_exclusion_reason(profile) == OPENROUTER_RETIRED_REASON
    decision = select_route(_context(fallback_allowed=True), (candidate_from_profile(profile),
        candidate_from_profile(_profile(id="openrouter-fallback"), source="fallback")), _proofs(profile), now=100.0)
    assert decision.allowed is False
    assert {item.reason_code for item in decision.rejections} == {OPENROUTER_RETIRED_REASON}


@pytest.mark.parametrize("runtime_path", sorted([*CANONICAL_ROUTE_SPECS, "mcp_fixture"]))
def test_every_canonical_route_fails_closed_before_context(runtime_path):
    principal = _principal()
    if runtime_path == "audio_transcription":
        principal = replace(principal, grants=(*principal.grants, AuthorityGrant.INGRESS))
    with pytest.raises(ProviderRetiredError) as raised:
        build_canonical_inference_context(runtime_path, payload={"message": "hello"}, output_tokens=64,
            timeout_seconds=5, principal=principal, session_id=principal.session_id)
    assert raised.value.code == OPENROUTER_RETIRED_REASON


def test_effective_policy_has_no_active_provider_even_with_stored_openrouter(model_fabric_workspace):
    from src.model_fabric.configuration import write_model_fabric_configuration
    setup = OpenRouterSetup(model_ids=("openai/gpt-4o-mini",), capabilities=("text",),
        allowed_upstreams=("openai",), egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True, spend_ceiling_microusd=1000, request_cost_bound_microusd=100,
        credential_ref="env:OPENROUTER_API_KEY")
    write_model_fabric_configuration(ModelFabricConfiguration(status="ready", openrouter_setup=setup))
    for runtime_path in ("chat_agent", "screenshot_image_analysis", "memory_embedding", "daily_briefing"):
        policy = effective_workload_policy(runtime_path)
        assert policy.allowed_provider_kinds == () and policy.max_cost_microusd is None
        assert policy.egress_class is EgressClass.LOCAL_ONLY


# ── Zero contact on representative routes ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_path", ["chat_agent", "daily_briefing", "end_of_day_goal_report",
    "cordis_interactive_inference", "cordis_background_inference"])
async def test_completion_with_fallback_never_contacts_openrouter(async_db, runtime_path):
    from src.llm_runtime import completion_with_fallback
    context = _legacy_context(runtime_path)
    with _no_provider_contact():
        with pytest.raises(NoCompliantModelRouteError) as raised:
            await completion_with_fallback(messages=[{"role": "user", "content": "hello"}],
                temperature=0.1, max_tokens=64, runtime_path=runtime_path, request_context=context)
    assert OPENROUTER_RETIRED_REASON in raised.value.reason_codes


@pytest.mark.asyncio
async def test_stream_completion_never_contacts_openrouter(async_db):
    from src.llm_runtime import stream_completion_with_fallback
    context = _legacy_context("chat_agent", streaming=True)
    with _no_provider_contact():
        with pytest.raises(NoCompliantModelRouteError) as raised:
            async for _delta in stream_completion_with_fallback([{"role": "user", "content": "hello"}],
                    0.1, 64, "chat_agent", context):
                pytest.fail("a retired route must not yield")
    assert OPENROUTER_RETIRED_REASON in raised.value.reason_codes


@pytest.mark.asyncio
async def test_preflighted_adapter_cannot_carry_a_retired_decision(async_db):
    """A caller-built allowed decision (screenshot VLM seam) is still denied."""
    from src.model_fabric import RouteDecision, candidate_from_profile, run_preflighted_adapter
    from src.model_fabric.hooks import PersistedRouteReceiptHooks
    candidate = candidate_from_profile(_profile())
    decision = RouteDecision(candidate, "d" * 64, "decision", (), "attempt:x", "replay:x", "route:x", ())
    called = []

    async def adapter(_candidate, _fallback):
        called.append(True)

    with _no_provider_contact():
        with pytest.raises(NoCompliantModelRouteError) as raised:
            await run_preflighted_adapter(context=_context(), decision=decision, adapter=adapter,
                hooks=PersistedRouteReceiptHooks())
    assert called == [] and raised.value.reason_codes == (OPENROUTER_RETIRED_REASON,)


@pytest.mark.asyncio
async def test_screenshot_vision_never_contacts_openrouter(tmp_path, monkeypatch):
    from src.observer import screenshot_semantic_analysis as analysis
    monkeypatch.setattr(settings, "screen_analysis_provider", "openrouter")
    monkeypatch.setattr(settings, "screen_analysis_model", "openrouter/fixture/vision")
    image = tmp_path / "capture.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    with _no_provider_contact():
        assert analysis.screenshot_semantic_analysis_enabled() is False
        assert await analysis.analyze_screenshot_image(image, {"image_sha256": "a" * 64}) is None
        with pytest.raises(analysis.ScreenshotSemanticAnalysisError, match=OPENROUTER_RETIRED_REASON):
            await analysis._analyze_with_openrouter(image, {"image_sha256": "a" * 64})


def test_embeddings_never_contact_openrouter():
    from src.memory.embedder import EmbeddingConfigurationError, embed, embed_batch
    with _no_provider_contact():
        for call in (lambda: embed("hello"), lambda: embed_batch(["a", "b"])):
            with pytest.raises(EmbeddingConfigurationError) as raised:
                call()
            assert getattr(raised.value, "reason_code", None) == OPENROUTER_RETIRED_REASON or OPENROUTER_RETIRED_REASON in str(raised.value)


def test_governed_transport_fence_refuses_openrouter_endpoint():
    from src.model_fabric.retirement import assert_openrouter_contact_allowed
    with pytest.raises(ProviderRetiredError):
        assert_openrouter_contact_allowed(api_base="https://openrouter.ai/api/v1")
    with pytest.raises(ProviderRetiredError):
        assert_openrouter_contact_allowed(model_id="openrouter/anthropic/claude")
    assert_openrouter_contact_allowed(provider_kind="near", api_base="https://cloud-api.near.ai/v1")


# ── Admission and liabilities ────────────────────────────────────


@pytest.mark.asyncio
async def test_admission_denies_new_openrouter_and_old_unknown_still_settles(accounting_db):
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_inference_accounting import request, setup_configuration
    repository = DurableJobRepository()
    # Historical state: an OpenRouter call contacted the provider before
    # retirement and returned no authoritative cost.
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        setup_configuration(ceiling=100, bound=100)
        await repository.configure_inference_accounting(100)

        async def no_cost():
            return {"id": "gen-fixture", "usage": {"prompt_tokens": 5}}
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("historical"), no_cost)
    before = await repository.inference_accounting_snapshot()
    row = before["operations"][0]
    assert row["state"] == "unknown" and before["unknown_microusd"] == 100
    calls = []

    async def transport():
        calls.append(True)
        return {"id": "must-not-run"}
    with pytest.raises(Exception) as raised:
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("after-retirement"), transport)
    assert calls == []
    assert OPENROUTER_RETIRED_REASON in repr(raised.value) or OPENROUTER_RETIRED_REASON in str(getattr(raised.value, "code", ""))
    # The pre-existing Unknown liability is untouched and still reconcilable.
    middle = await repository.inference_accounting_snapshot()
    assert [item["operation_id"] for item in middle["operations"]] == [row["operation_id"]]
    assert middle["unknown_microusd"] == 100
    settled = await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
        expected_revision=row["revision"], actual_cost_microusd=12, evidence_digest="b" * 64,
        operator_id="operator:settings-owner", idempotency_key="retired-settlement")
    assert settled["state"] == "settled"
    after = await repository.inference_accounting_snapshot()
    assert after["unknown_microusd"] == 0 and after["committed_microusd"] == 12


from tests.test_inference_accounting import accounting_db  # noqa: E402,F401


# ── Lane configuration migration ─────────────────────────────────


def test_lane_migration_is_pure_and_copies_values():
    setup = OpenRouterSetup(model_ids=("openai/gpt-4o-mini",), capabilities=("text",),
        allowed_upstreams=("openai",), egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True, spend_ceiling_microusd=25_000, request_cost_bound_microusd=100,
        max_queued=8, max_outstanding_per_owner=4, max_retries=1)
    frozen = repr(setup)
    lane = migrate_inference_lane(setup, None)
    assert lane == InferenceLaneConfig(spend_ceiling_microusd=25_000, max_queued=8, max_inflight=1,
        max_outstanding_per_owner=4, max_retries=1)
    assert repr(setup) == frozen
    assert migrate_inference_lane(None, None) is None
    configured = ModelFabricConfiguration(status="ready", openrouter_setup=setup)
    assert effective_inference_lane(configured) == lane
    payload = _configuration_payload(replace(configured, inference_lane=lane))
    assert payload["inference_lane"] == asdict(lane)
    assert _configuration_from_payload(payload).inference_lane == lane
    for malformed in ({**asdict(lane), "extra": 1}, {**asdict(lane), "max_inflight": 2},
            {**asdict(lane), "spend_ceiling_microusd": True}, {k: v for k, v in asdict(lane).items() if k != "max_retries"}):
        with pytest.raises(ValueError):
            _configuration_from_payload({**payload, "inference_lane": malformed})
    # The lane owns the ceiling once published; a retired OpenRouter ceiling is readback.
    published = replace(configured, inference_lane=replace(lane, spend_ceiling_microusd=30_000))
    assert deployment_spend_ceiling(published) == 30_000


def test_route_scoped_revoke_keeps_readable_state():
    from src.model_fabric.configuration import OpenRouterRoute
    route = OpenRouterRoute(model_id="openrouter/fixture/text", enabled=True, capabilities=("text",),
        allowed_upstreams=("fixture",), temperature=0.4, max_output_tokens=2048, timeout_seconds=45,
        zero_data_retention=False, request_cost_bound_microusd=100)
    setup = OpenRouterSetup(schema_version="seraph.openrouter.setup.v2", routes={"text": route, "vision": None,
        "embedding": None}, purpose_consents={"vision": 3}, egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True, spend_ceiling_microusd=25_000, credential_fingerprint="0123456789ab")
    retired = retire_openrouter_setup(setup)
    assert retired.cloud_egress_acknowledged is False and retired.purpose_consents == {}
    assert retired.routes["text"] == replace(route, enabled=False) and retired.routes["vision"] is None
    assert retired.credential_fingerprint == "0123456789ab"
    assert retire_openrouter_setup(retired) == retired
    # A retired object stays a readable, valid configuration beside the lane.
    configuration = ModelFabricConfiguration(status="ready", openrouter_setup=retired,
        inference_lane=InferenceLaneConfig(spend_ceiling_microusd=25_000))
    assert _configuration_from_payload(_configuration_payload(configuration)).openrouter_setup == retired


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_lane_publication_preserves_ledger_and_revokes_openrouter_routes(client, model_fabric_workspace, keyless_openrouter):
    from src.model_fabric.remote_inference_admission import remote_inference_admission_broker
    from src.workflows.job_runtime import durable_job_repository
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
            "openrouter_setup": _v2_setup_payload(api_key="sk-or-retained")})
        assert saved.status_code == 200, saved.text
    path = model_fabric_workspace / "model-fabric-settings.json"
    stored = path.read_bytes()
    before = await durable_job_repository.inference_accounting_snapshot()
    configured = read_model_fabric_configuration()
    assert configured.inference_lane is None
    assert effective_inference_lane(configured) == InferenceLaneConfig(spend_ceiling_microusd=25_000,
        max_queued=8, max_inflight=1, max_outstanding_per_owner=4, max_retries=1)
    # Reading (and admission configuration) never writes: the migration is pure.
    effective_provider_profiles({})
    assert remote_inference_admission_broker.max_queued == 8
    assert remote_inference_admission_broker.max_outstanding_per_owner == 4
    assert remote_inference_admission_broker.max_retries == 1
    assert path.read_bytes() == stored

    payload = (await client.get("/api/settings/model-fabric")).json()
    assert payload["inference_lane"]["published"] is False and payload["inference_lane"]["max_queued"] == 8
    assert payload["openrouter_setup"]["status"] == "retired"
    assert payload["openrouter_setup"]["error_code"] == OPENROUTER_RETIRED_REASON
    assert payload["provider_retirement"]["openrouter"]["settlement_available"] is True
    assert payload["inference_block_reason"] == OPENROUTER_RETIRED_REASON and payload["status"] == "blocked"

    # New OpenRouter activation is refused without mutating anything.
    refused = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "openrouter_setup": _v2_setup_payload()})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == OPENROUTER_RETIRED_REASON
    canary = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "openrouter.text", "capability": "text"})
    assert canary.status_code == 409 and canary.json()["detail"]["code"] == OPENROUTER_RETIRED_REASON
    assert path.read_bytes() == stored

    published = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "inference_lane": {"spend_ceiling_microusd": 25_000, "max_queued": 8, "max_inflight": 1,
            "max_outstanding_per_owner": 4, "max_retries": 1}})
    assert published.status_code == 200, published.text
    active = read_model_fabric_configuration()
    assert active.status == "ready" and active.egress_revision == 5 and active.egress_revoked is False
    assert active.inference_lane == effective_inference_lane(configured)
    assert active.openrouter_setup.cloud_egress_acknowledged is False
    assert active.openrouter_setup.purpose_consents == {}
    assert all(route.enabled is False for route in active.openrouter_setup.routes.values() if route is not None)
    assert all(profile.enabled is False for profile in active.profiles if profile.provider_kind == "openrouter")
    # Retained, readable: model ids and credential metadata survive.
    assert active.openrouter_setup.routes["text"].model_id == configured.openrouter_setup.routes["text"].model_id
    assert active.openrouter_setup.credential_fingerprint == configured.openrouter_setup.credential_fingerprint
    after = await durable_job_repository.inference_accounting_snapshot()
    for field in ("status", "ceiling_microusd", "deployment_id", "ledger_digest", "operations",
            "unknown_microusd", "committed_microusd"):
        assert after.get(field) == before.get(field), field
    assert published.json()["inference_lane"]["published"] is True

    # The lane owns the ceiling; changing it reconfigures the same ledger.
    raised = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "inference_lane": {"spend_ceiling_microusd": 30_000}})
    assert raised.status_code == 200, raised.text
    final = read_model_fabric_configuration()
    assert final.inference_lane.spend_ceiling_microusd == 30_000 and deployment_spend_ceiling(final) == 30_000
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    assert snapshot["ceiling_microusd"] == 30_000 and snapshot["deployment_id"] == before["deployment_id"]
    stale = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "inference_lane": {"spend_ceiling_microusd": 30_000}})
    assert stale.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_near_text_mirrors_lane_ceiling_and_keeps_routing(client, model_fabric_workspace, keyless_openrouter):
    from src.model_fabric.effective_policy import current_near_text_policy
    from tests.test_near_text_setup_api import save
    assert (await save(client, 1, api_key="sk-near-kept")).status_code == 200
    configured = read_model_fabric_configuration()
    assert configured.inference_lane == InferenceLaneConfig(spend_ceiling_microusd=25_000)
    lane = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "inference_lane": {"spend_ceiling_microusd": 40_000}})
    assert lane.status_code == 200, lane.text
    active, digest = current_near_text_policy()
    assert len(digest) == 64
    assert active.near_text.spend_ceiling_microusd == 40_000 and deployment_spend_ceiling(active) == 40_000
    assert active.near_text.plaintext_egress_consent_revision == active.egress_revision
    assert lane.json()["near_text"]["status"] == "configured"
    mismatch = await save(client, 5, spend_ceiling_microusd=25_000)
    assert mismatch.status_code == 409 and mismatch.json()["detail"] == "deployment_spend_ceiling_mismatch"
    assert (await save(client, 5, spend_ceiling_microusd=40_000)).status_code == 200


# ── Operator surfaces ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keyless_runtime_status_reports_blocked_not_crash(client):
    response = await client.get("/api/runtime/status")
    assert response.status_code == 200, response.text
    effective = response.json()["effective_runtime"]
    assert effective["inference_ready"] is False
    assert effective["inference_readiness"]["status"] == "blocked"
    assert effective["inference_readiness"]["reasons"][:2] == [OPENROUTER_RETIRED_REASON, "no_active_provider"]
    settings_payload = (await client.get("/api/settings/model-fabric")).json()
    assert settings_payload["inference_block_reason"] == "no_active_provider"
    assert settings_payload["active_inference_provider"] is None


@pytest.mark.asyncio
async def test_screen_analysis_refuses_openrouter_and_tells_daemon_no_cloud_provider(client, model_fabric_workspace):
    from src.observer.screen_analysis_settings import write_screen_analysis_settings
    write_screen_analysis_settings({"enabled": True, "provider": "openrouter", "model": "openrouter/fixture/vision"})
    current = (await client.get("/api/settings/screen-analysis")).json()
    assert current["provider"] == "" and current["model"] == ""
    assert current["provider_blocked_reason"] == OPENROUTER_RETIRED_REASON
    refused = await client.put("/api/settings/screen-analysis", json={"provider": "openrouter"})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == OPENROUTER_RETIRED_REASON
    cleared = await client.put("/api/settings/screen-analysis", json={"provider": "", "enabled": True})
    assert cleared.status_code == 200 and cleared.json()["provider"] == ""
    stored = json.loads((model_fabric_workspace / "screen-analysis-settings.json").read_text())
    assert "provider_blocked_reason" not in stored


def test_legacy_fixture_is_explicit_opt_in(openrouter_legacy_reenabled):
    assert retirement.openrouter_retired() is False


# ── Critic follow-ups ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audio_documentation_metadata_reads_never_contact_openrouter(client):
    from src.model_fabric.audio_documentation import DocumentationError, metadata_get, stage
    with _no_provider_contact():
        with pytest.raises(DocumentationError) as raised:
            await metadata_get("https://openrouter.ai/api/v1/key", "sk-management-fixture", 16384)
        assert raised.value.code == OPENROUTER_RETIRED_REASON and raised.value.status == 409
        with pytest.raises(DocumentationError, match=OPENROUTER_RETIRED_REASON):
            await stage(None, None, None)
        response = await client.post("/api/settings/model-fabric/audio-documentation", json={
            "action": "acquire_selected_profile_metadata", "expected_egress_revision": 1,
            "expected_audio_profile_hash": "a" * 64, "metadata_selection_ref": "selection-fixture",
            "expected_metadata_selection_digest": "b" * 64,
            "official_supplement_catalog_id": "openrouter-audio-guide.v1"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == OPENROUTER_RETIRED_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_boot_publishes_route_revoke_once_without_touching_ledger(client, model_fabric_workspace, keyless_openrouter):
    from src.model_fabric.configuration import openrouter_route_revoke_status, run_openrouter_route_revoke_at_boot
    from src.model_fabric.effective_policy import current_near_text_policy
    from src.workflows.job_runtime import durable_job_repository
    from tests.test_near_text_setup_api import save
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
            "openrouter_setup": _v2_setup_payload(api_key="sk-or-retained")})
        assert saved.status_code == 200, saved.text
        assert (await save(client, 3, api_key="sk-near-kept")).status_code == 200
    stored = read_model_fabric_configuration()
    assert stored.openrouter_setup.cloud_egress_acknowledged is True
    assert stored.openrouter_setup.purpose_consents and stored.openrouter_setup.routes["text"].enabled
    before = await durable_job_repository.inference_accounting_snapshot()

    with _no_provider_contact():
        first = run_openrouter_route_revoke_at_boot()
    assert first["status"] == "published", first
    revoked = read_model_fabric_configuration()
    assert revoked.status == "ready" and revoked.egress_revision == stored.egress_revision + 2
    assert revoked.egress_revoked is False
    assert revoked.openrouter_setup.cloud_egress_acknowledged is False
    assert revoked.openrouter_setup.purpose_consents == {}
    assert all(route.enabled is False for route in revoked.openrouter_setup.routes.values() if route is not None)
    assert all(not profile.enabled for profile in revoked.profiles if profile.provider_kind == "openrouter")
    assert revoked.inference_lane == effective_inference_lane(stored)
    # NEAR is not fenced: its consent moves to the new revision.
    near, _digest = current_near_text_policy()
    assert near.near_text.plaintext_egress_consent_revision == revoked.egress_revision
    after = await durable_job_repository.inference_accounting_snapshot()
    for field in ("ceiling_microusd", "deployment_id", "ledger_digest", "revision", "operations"):
        assert after.get(field) == before.get(field), field

    path = model_fabric_workspace / "model-fabric-settings.json"
    published = path.read_bytes()
    second = run_openrouter_route_revoke_at_boot()
    assert second["status"] == "noop" and path.read_bytes() == published
    status = (await client.get("/api/settings/model-fabric")).json()
    assert status["provider_retirement"]["openrouter"]["route_revoke"]["status"] == "noop"
    assert openrouter_route_revoke_status()["status"] == "noop"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_boot_revoke_failure_is_visible_and_never_crashes(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.model_fabric import configuration
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
            "openrouter_setup": _v2_setup_payload()})).status_code == 200
    path = model_fabric_workspace / "model-fabric-settings.json"
    stored = path.read_bytes()

    def broken(*_args, **_kwargs):
        raise RuntimeError("witness unavailable")
    monkeypatch.setattr(configuration, "write_model_fabric_configuration", broken)
    result = configuration.run_openrouter_route_revoke_at_boot()
    assert result == {"status": "degraded", "reason_code": "openrouter_route_revoke_unavailable"}
    assert path.read_bytes() == stored
    status = (await client.get("/api/settings/model-fabric")).json()
    assert status["provider_retirement"]["openrouter"]["route_revoke"]["status"] == "degraded"
    # Retirement still holds at every route without the stored revoke.
    assert effective_workload_policy("chat_agent").allowed_provider_kinds == ()


@pytest.mark.asyncio
async def test_lane_reserve_review_unblocks_openrouter_only_overrun(accounting_db):
    from fastapi import HTTPException
    from src.api.model_fabric_settings import ModelFabricConfigurationRequest, _put_inference_lane
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_inference_accounting import request, setup_configuration
    repository = DurableJobRepository()
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        setup_configuration(ceiling=1000, bound=1)
        await repository.configure_inference_accounting(1000)

        async def charged():
            return {"usage": {"cost": "0.0000051"}}
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("over-bound"), charged)
    assert (await repository.inference_accounting_snapshot())["overrun_max_cost_microusd"] == 6
    persisted = read_model_fabric_configuration()

    def body(**lane):
        return ModelFabricConfigurationRequest(expected_policy_revision=persisted.egress_revision,
            inference_lane={"spend_ceiling_microusd": 1000, **lane})
    with pytest.raises(HTTPException) as refused:
        await _put_inference_lane(body(), persisted)
    assert refused.value.status_code == 409 and refused.value.detail["code"] == "request_bound_below_overrun"
    with pytest.raises(HTTPException):
        await _put_inference_lane(body(request_reserve_review_microusd=5), persisted)
    assert read_model_fabric_configuration().inference_lane is None
    payload = await _put_inference_lane(body(request_reserve_review_microusd=6), persisted)
    active = read_model_fabric_configuration()
    assert active.inference_lane.request_reserve_review_microusd == 6
    assert payload["inference_lane"]["published"] is True
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["overrun_max_cost_microusd"] == 0 and snapshot["status"] == "ready"
    assert snapshot["committed_microusd"] == 6


@pytest.mark.asyncio
async def test_direct_chat_reports_openrouter_retired_reason(client):
    from src.agent.direct_chat import openrouter_route_block_recovery, stream_direct_local_chat
    with _no_provider_contact():
        with patch("src.api.chat.should_use_direct_local_chat", return_value=True), \
                patch("src.api.chat.direct_local_chat_route_error", return_value=None):
            response = await client.post("/api/chat", json={"message": "Hello"})
        with pytest.raises(NoCompliantModelRouteError) as raised:
            async for _delta in stream_direct_local_chat("Hello", runtime_path="chat_agent", is_onboarding=False,
                    session_id="session-retirement"):
                pytest.fail("a retired route must not stream")
    assert raised.value.reason_codes == (OPENROUTER_RETIRED_REASON,)
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["route_reason"] == OPENROUTER_RETIRED_REASON
    assert detail["recovery_action"] == OPENROUTER_RETIRED_REASON
    assert detail["remote_outcome"] == "not_contacted"
    message, _action = openrouter_route_block_recovery(OPENROUTER_RETIRED_REASON)
    assert "OpenRouter is retired (ADR-035)" in message and "No model call was made" in message


@pytest.mark.asyncio
async def test_settings_api_retired_counterparts_for_profiles_canary_and_status(client):
    profile = {"id": "persisted-openrouter", "provider_kind": "openrouter", "model": "anthropic/claude-sonnet-4",
        "api_base": "https://openrouter.ai/api/v1", "secret_env": "OPENROUTER_API_KEY",
        "capabilities": ["text"], "task_class": "interactive_chat"}
    refused = await client.put("/api/settings/model-fabric", json={"profiles": [profile]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == OPENROUTER_RETIRED_REASON
    for profile_id in ("openrouter", "openrouter-screenshot-vision", "openrouter.embedding"):
        canary = await client.post("/api/settings/model-fabric/canary", json={"profile_id": profile_id, "capability": "text"})
        assert canary.status_code == 409 and canary.json()["detail"]["code"] == OPENROUTER_RETIRED_REASON
    status = (await client.get("/api/settings/model-fabric")).json()
    assert status["status"] == "blocked" and status["status_scope"] == "canonical_inference_routes"
    assert status["near_text_ready"] is False
    runtime = (await client.get("/api/runtime/status")).json()
    assert runtime["effective_runtime"]["inference_readiness"]["status"] == "blocked"
    assert OPENROUTER_RETIRED_REASON in runtime["effective_runtime"]["inference_readiness"]["reasons"]
